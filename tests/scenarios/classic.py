"""Classic scenario: run on emulated players (tests/integration) and on real ones."""

from __future__ import annotations

import hashlib

from syncmusic.core.models import ActionKind, MediaKind
from syncmusic.core.sync import RegisterRequest
from tests.factories import own_rules
from tests.integration.services import media_listing

NAME = "Домашній Classic"


MUSIC_AND_PODCASTS = {
    "rules": [
        {"id": "music", "name": "All music", "criteria": {"is": {"kind": "music"}}},
        {"id": "pods", "name": "Podcasts", "criteria": {"is": {"kind": "podcast"}}},
    ]
}


LESS_MUSIC = {
    "rules": [
        {
            "id": "music",
            "name": "Some music",
            "criteria": {"is": {"kind": "music"}},
            "limit": 5,
            "sort": "title",
        },
        {"id": "pods", "name": "Podcasts", "criteria": {"is": {"kind": "podcast"}}},
    ]
}


def _data_listing() -> list[str]:
    return media_listing()


def _partition_table(disk_path: str) -> str:
    import subprocess

    raw = subprocess.run(
        ["sudo", "-n", "dd", f"if={disk_path}", "bs=4096", "count=1", "status=none"],
        capture_output=True,
        check=True,
    ).stdout
    return hashlib.sha256(raw[:446] + raw[446:512]).hexdigest()  # boot code + MBR partition entries


async def _apply(a, plan_id, **kw):
    job = await a.sync.apply(plan_id, **kw)
    job = await a.jobs.wait(job.id)
    assert job.status == "succeeded", job.error
    rep = job.result
    assert rep["status"] == "succeeded" and not rep["failures"], [
        f["error"] for f in rep["failures"]
    ] or rep
    return rep


async def _check_db(a, device_id):
    """Re-read the iPod database with pygpod: every manifest entry is a track with its file."""
    from pygpod import Database

    dev = await a.sync.open_device(device_id)
    m = await dev.read_manifest()
    db = Database(str(dev.mount))
    by_dbid = {str(t.dbid): t for t in db.tracks}
    assert set(by_dbid) == {e.device_ref for e in m.items.values()}
    for e in m.items.values():
        assert (dev.mount / e.path).stat().st_size == e.size
        assert by_dbid[e.device_ref].title
    raw = (dev.mount / "iPod_Control" / "iTunes" / "iTunesDB").read_bytes()
    assert raw[0x58:0x72] != b"\x00" * 26, "hash58 missing: the Classic would reject the database"
    assert not list((dev.mount / "iPod_Control").rglob("*.pygpod-new"))
    assert not (dev.mount / "iPod_Control" / "iTunes" / "pygpod.journal").exists()
    return dev, m, db


async def classic_scenario(a, classic):
    """The acceptance scenario on one stock iPod (emulated in tests/integration/test_emulated_ipod.py, or a real one)."""
    before = _data_listing()
    table = _partition_table(classic.path)
    cfg = await a.sync.register(
        RegisterRequest(
            name=NAME,
            disk=classic.path,
            rule_set=await own_rules(a, NAME, MUSIC_AND_PODCASTS, ["nd", "abs"]),
            on_connect="manual",
        )
    )
    assert cfg.type == "ipod" and cfg.identity.usb_serial == classic.serial
    cfg = await a.sync.format(cfg.id, confirm=NAME)
    assert _partition_table(classic.path) == table
    dev = await a.sync.open_device(cfg.id)
    sysinfo = (dev.mount / "iPod_Control" / "Device" / "SysInfo").read_text()
    assert classic.serial in sysinfo.upper()

    pc = await a.sync.plan(cfg.id)
    adds = pc.plan.count(ActionKind.ADD)
    assert adds >= 25, pc.plan.warnings
    rep = await _apply(a, pc.plan.id, first_sync="keep")
    dev, m, db = await _check_db(a, cfg.id)
    assert len(m.items) == rep["counts"]["add"] == adds
    assert db.master_playlist.name == NAME
    kinds = {e.kind for e in m.items.values()}
    assert kinds == {MediaKind.MUSIC, MediaKind.PODCAST}
    podcasts = [p for p in db.playlists if p.is_podcast]
    n_pods = sum(1 for e in m.items.values() if e.kind is MediaKind.PODCAST)
    assert len(podcasts) == 1 and len(podcasts[0].tracks) == n_pods == 5
    assert any(t.has_artwork for t in db.tracks), "no cover art in the ArtworkDB"
    assert all(t.filetype_str for t in db.tracks)

    pc = await a.sync.plan(cfg.id)  # re-run: no-op
    assert {x.kind for x in pc.plan.actions} == {ActionKind.KEEP}, [
        (x.kind, x.reason) for x in pc.plan.actions
    ]
    rep = await _apply(a, pc.plan.id)
    assert rep["bytes_written"] == 0

    cfg = await a.sync.update_device(
        cfg.model_copy(update={"rule_set": await own_rules(a, cfg.name, LESS_MUSIC, ["nd", "abs"])})
    )
    pc = await a.sync.plan(cfg.id)
    removes = pc.plan.count(ActionKind.REMOVE)
    assert removes == adds - 5 - 5 and pc.plan.count(ActionKind.ADD) == 0
    removed_paths = [x.entry.path for x in pc.plan.actions if x.kind is ActionKind.REMOVE]
    await _apply(a, pc.plan.id, confirm=True)
    dev, m, db = await _check_db(a, cfg.id)
    assert len(m.items) == 10 and len(db.tracks) == 10
    assert not any((dev.mount / p).exists() for p in removed_paths)

    cfg = await a.sync.update_device(
        cfg.model_copy(
            update={"rule_set": await own_rules(a, cfg.name, MUSIC_AND_PODCASTS, ["nd", "abs"])}
        )
    )
    pc = await a.sync.plan(cfg.id)
    assert pc.plan.count(ActionKind.ADD) == removes and pc.plan.count(ActionKind.REMOVE) == 0
    await _apply(a, pc.plan.id)
    dev, m, db = await _check_db(a, cfg.id)
    assert len(m.items) == adds and db.master_playlist.name == NAME
    assert _data_listing() == before, "the test media changed"
