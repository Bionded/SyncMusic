"""Resume after a real crash: the daemon process is SIGKILLed in the middle of a sync to an emulated
device (no unmount, no final commit, the mount stays behind like after a crash of the unit). A fresh process on the
same data dir must find the device again, keep what the checkpoints committed, write only the rest, and end with the
same device state as an uninterrupted sync. test_emulated_resume.py covers the pulled-mount path in-process."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from syncmusic.core.models import ActionKind
from tests.emulated import (
    CLASSIC_LAYOUT,
    IMAGE_ROOT,
    attach,
    flash,
    ipod,
    navidrome_config,
    needs_sudo,
    running_app,
)

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

SIX = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": 6}]}
KILL_AFTER = 4  # files written before the process is killed

# The child: one sync with a checkpoint after every file and a pause after each put, printing "PUT n".
CHILD = textwrap.dedent(
    """
    import asyncio, json, sys
    from pathlib import Path
    from types import SimpleNamespace

    from syncmusic.core import applier
    from syncmusic.core.models import LibraryConfig
    from syncmusic.core.sync import RegisterRequest
    from syncmusic.devices.flashdrive import FlashDrive
    from syncmusic.devices.iPod.ipod import IPod
    from tests.emulated import running_app
    from tests.factories import own_rules

    args = json.loads(sys.argv[1])
    cls = IPod if args["device"] == "ipod" else FlashDrive
    puts = 0
    real_put = cls.put_track

    async def put(self, s, item):
        global puts
        ref = await real_put(self, s, item)
        puts += 1
        print("PUT", puts, flush=True)
        await asyncio.sleep(1.5)  # time for the checkpoint and for the parent's kill
        return ref

    cls.put_track = put
    applier.time = SimpleNamespace(monotonic=lambda: 100.0 * puts)  # a checkpoint after every file

    class Disk:
        dev = args["dev"]

    async def main():
        lib = LibraryConfig.model_validate(args["lib"])
        async with running_app(Path(args["tmp"]), [Disk], [lib]) as (app, [blk]):
            print("CACHE", app.config.cache_dir, flush=True)  # the parent removes it: the kill leaves it
            rules = await own_rules(app, "Kill", args["rules"], ["nd"])
            req = RegisterRequest(name="Kill", disk=blk.path, rule_set=rules, on_connect="manual")
            cfg = await app.sync.register(req)
            print("DEVICE", cfg.id, flush=True)
            pc = await app.sync.plan(cfg.id)
            print("PLAN", {k.value: pc.plan.count(k) for k in {a.kind for a in pc.plan.actions}}, flush=True)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            print("FINISHED", job.status, job.error, flush=True)

    asyncio.run(main())
    """
)


def _run_and_kill(tmp: Path, dev: str, device: str, services: dict[str, str]) -> str:
    args = {
        "dev": dev,
        "device": device,
        "tmp": str(tmp),
        "rules": SIX,
        "lib": navidrome_config(services).model_dump(mode="json"),
    }
    root = Path(__file__).parents[2]
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD, json.dumps(args)],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    device_id = ""
    cache: Path | None = None  # the child's (other workers' caches next to it are in use)
    log: list[str] = []
    deadline = time.monotonic() + 240
    assert proc.stdout is not None
    for line in proc.stdout:
        log.append(line)
        if line.startswith("DEVICE "):
            device_id = line.split()[1]
        if line.startswith("CACHE "):
            cache = Path(line.split(" ", 1)[1].strip())
        if line.strip() == f"PUT {KILL_AFTER}":
            time.sleep(0.5)  # inside the pause after the put: its checkpoint is (being) committed
            proc.send_signal(signal.SIGKILL)
            break
        assert time.monotonic() < deadline, "".join(log[-30:])
    proc.wait(timeout=30)
    if cache is not None and cache.parent == IMAGE_ROOT:  # the killed child never removed its cache
        shutil.rmtree(cache, ignore_errors=True)
    assert proc.returncode == -signal.SIGKILL, "".join(log[-30:])
    assert device_id, "".join(log[-30:])
    return device_id


def _files(root: Path) -> set[str]:
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and ".syncmusic" not in p.parts
    }


async def _resume_after_kill(tmp_path: Path, disk, device: str, services) -> None:
    device_id = _run_and_kill(tmp_path, disk.dev, device, services)
    async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, _):
        pc = await app.sync.plan(device_id)
        kept = pc.plan.count(ActionKind.KEEP)
        added = pc.plan.count(ActionKind.ADD)
        assert kept + added == 6, [(a.kind, a.title) for a in pc.plan.actions]
        # the checkpoint after file N-1 at the latest was committed before the kill
        assert kept >= KILL_AFTER - 1, [(a.kind, a.title) for a in pc.plan.actions]
        assert pc.plan.resuming is not None
        assert pc.plan.resuming.done == kept and pc.plan.resuming.total == 6
        job = await app.jobs.wait((await app.sync.apply(pc.plan.id)).id)
        assert job.status == "succeeded", job.error
        assert job.result["counts"]["add"] == added

        dev = await app.sync.open_device(device_id)
        manifest = await dev.read_manifest()
        assert len(manifest.items) == 6 and manifest.header.interrupted is None
        paths = {e.path for e in manifest.items.values()}
        files = _files(Path(dev.mount))
        assert paths <= files
        if device == "ipod":
            from pygpod import Database

            db = Database(str(dev.mount), apply_play_counts=False)
            assert sorted(str(t.dbid) for t in db.tracks) == sorted(
                e.device_ref for e in manifest.items.values()
            )
            music = {f for f in files if f.startswith("iPod_Control/Music/")}
            assert music == paths, sorted(music ^ paths)  # no orphan of the killed write
        else:
            sidecars = {p for e in manifest.items.values() for p in e.sidecars}
            extra = sorted(f for f in files - paths - sidecars if not f.startswith("Playlists/"))
            assert extra == [], extra
        pc = await app.sync.plan(device_id)
        assert {a.kind for a in pc.plan.actions} == {ActionKind.KEEP} and pc.plan.resuming is None


async def test_resume_on_a_flash_drive_after_a_kill(services, tmp_path):
    with attach("emulated_flash", 256) as disk:
        await flash(disk, "vfat", "OLD")
        await _resume_after_kill(tmp_path, disk, "flash", services)


async def test_resume_on_an_ipod_after_a_kill(services, tmp_path):
    with attach("emulated_ipod_classic", 1024, layout=CLASSIC_LAYOUT) as disk:
        await ipod(disk, tmp_path / "build")
        await _resume_after_kill(tmp_path, disk, "ipod", services)
