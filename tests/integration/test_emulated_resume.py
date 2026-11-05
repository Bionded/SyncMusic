"""Resume an interrupted sync on emulated devices against the compose Navidrome: the mount is pulled away
in the middle of the sync (like an unplug, tests/hardware/test_mount_lost.py); the checkpoints before it stay on the
device, the next plan says it resumes, keeps them and writes only the rest, and the result is the same as an
uninterrupted sync of the same rules."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from syncmusic.core import applier
from syncmusic.core.models import ActionKind
from syncmusic.core.sync import RegisterRequest
from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.devices.iPod.ipod import IPod
from syncmusic.infra.blockdev import mount as mnt
from tests.emulated import (
    CLASSIC_LAYOUT,
    attach,
    flash,
    ipod,
    navidrome_config,
    needs_sudo,
    running_app,
)
from tests.factories import own_rules

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

SIX = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": 6}]}
PULL_AFTER = 4  # files written before the mount is pulled away


def _files(root) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and ".syncmusic" not in p.parts
    )


async def _resume_scenario(app, blk, device_class, monkeypatch) -> None:
    puts = 0
    real_put = device_class.put_track

    async def put(self, s, item):
        nonlocal puts
        ref = await real_put(self, s, item)
        puts += 1
        if puts == PULL_AFTER:  # pulled away behind our back right after this file
            await mnt.run_cmd(["umount", "-l", str(self.mount)])
        return ref

    monkeypatch.setattr(device_class, "put_track", put)
    # every file takes 100 s and a commit no time: a checkpoint after every file
    monkeypatch.setattr(applier, "time", SimpleNamespace(monotonic=lambda: 100.0 * puts))
    req = RegisterRequest(
        name="Resume",
        disk=blk.path,
        rule_set=await own_rules(app, "Resume", SIX, ["nd"]),
        on_connect="manual",
    )
    cfg = await app.sync.register(req)
    pc = await app.sync.plan(cfg.id)
    total = pc.plan.count(ActionKind.ADD)
    assert total == 6 and pc.plan.resuming is None
    job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
    assert job.status == "failed" and puts == PULL_AFTER, job.error
    monkeypatch.undo()

    pc = await app.sync.plan(cfg.id)
    kept = pc.plan.count(ActionKind.KEEP)
    # the checkpoint after the last file came after the mount was gone: the ones before it are on the device
    assert kept == PULL_AFTER - 1, [(a.kind, a.title) for a in pc.plan.actions]
    assert pc.plan.count(ActionKind.ADD) == total - kept
    assert pc.plan.resuming is not None
    assert (pc.plan.resuming.done, pc.plan.resuming.total) == (kept, total)
    job = await app.jobs.wait((await app.sync.apply(pc.plan.id)).id)
    assert job.status == "succeeded", job.error
    assert job.result["counts"]["add"] == total - kept

    dev = await app.sync.open_device(cfg.id)
    manifest = await dev.read_manifest()
    assert len(manifest.items) == total and manifest.header.interrupted is None
    # nothing left over from the interrupted run: exactly the manifest's files (+ playlists / the iPod's DB)
    paths = {e.path for e in manifest.items.values()}
    sidecars = {p for e in manifest.items.values() for p in e.sidecars}
    assert paths <= set(_files(dev.mount))
    if device_class is IPod:
        from pygpod import Database

        db = Database(str(dev.mount), apply_play_counts=False)
        assert sorted(str(t.dbid) for t in db.tracks) == sorted(
            e.device_ref for e in manifest.items.values()
        )
        music = [f for f in _files(dev.mount) if f.startswith("iPod_Control/Music/")]
        assert sorted(music) == sorted(paths)
    else:
        extra = [f for f in _files(dev.mount) if f not in paths | sidecars]
        assert all(f.startswith("Playlists/") for f in extra), extra
    pc = await app.sync.plan(cfg.id)
    assert {a.kind for a in pc.plan.actions} == {ActionKind.KEEP} and pc.plan.resuming is None


async def test_resume_on_a_flash_drive(services, tmp_path, monkeypatch):
    with attach("emulated_flash", 256) as disk:
        await flash(disk, "vfat", "OLD")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            await _resume_scenario(app, blk, FlashDrive, monkeypatch)


async def test_resume_on_an_ipod(services, tmp_path, monkeypatch):
    with attach("emulated_ipod_classic", 1024, layout=CLASSIC_LAYOUT) as disk:
        await ipod(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            await _resume_scenario(app, blk, IPod, monkeypatch)
