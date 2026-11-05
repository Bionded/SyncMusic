"""HiBy R1 / SonixOS on the real player: register by disk, a small first sync next to the music
already on it (kept), no-op re-run, reverse sync reads the player's databases, the synced items removed again.

The player's own databases in /.local are never written (checked by hash). Only the allowlisted R1 (fs UUID from
tests/hardware.toml; its USB serial "HiBy_R1" is the same on every R1) is touched.
"""

from __future__ import annotations

import hashlib

import pytest

from syncmusic.app import Application
from syncmusic.core.models import ActionKind, LibraryConfig
from syncmusic.core.sync import RegisterRequest
from syncmusic.infra.blockdev.identify import scan_block_devices
from tests.factories import own_rules
from tests.hardware.conftest import hw_value
from tests.hardware.test_flash_acceptance import _apply, _data_listing
from tests.integration.test_navidrome_local import ENV

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]

HIBY_UUID = hw_value("hiby_r1", "fs_uuid")
FEW = {
    "rules": [
        {"id": "few", "name": "Five tracks", "criteria": {"all": []}, "limit": 5, "sort": "title"}
    ]
}
NONE = {"rules": [{"id": "none", "name": "Nothing", "criteria": {"contains": {"title": "⁣"}}}]}


@pytest.fixture
async def app(config):
    r1 = next((d for d in scan_block_devices() if d.uuid == HIBY_UUID), None)
    if r1 is None:
        pytest.skip("allowlisted HiBy R1 not connected")
    a = Application(config)
    await a.start()
    await a.store.put_library(
        LibraryConfig(
            id="nd",
            type="navidrome",
            name="Navidrome",
            settings={
                "url": ENV["NAVIDROME_URL"],
                "username": ENV["NAVIDROME_USER"],
                "password": ENV["NAVIDROME_PASSWORD"],
            },
        )
    )
    await a._disk_added(r1)
    yield a, r1
    await a.stop()


def _ours(m):
    return {k: e for k, e in m.items.items() if k.startswith("nd/") and not e.orphan}


def _media_paths(mount) -> set[str]:
    import sqlite3

    con = sqlite3.connect(f"file:{mount / '.local' / 'library.db'}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("SELECT path FROM MEDIA_TABLE")}
    finally:
        con.close()


def _db_hashes(mount) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((mount / ".local").glob("*.db"))
    }


async def test_hiby_sync_feedback_and_cleanup(app):
    a, r1 = app
    before = _data_listing()
    cfg = await a.sync.register(
        RegisterRequest(
            name="HiBy R1",
            disk=r1.path,
            rule_set=await own_rules(a, "HiBy R1", FEW, ["nd"]),
            on_connect="manual",
        )
    )
    assert cfg.type == "hiby" and cfg.identity.fs_uuid == HIBY_UUID
    dev = await a.sync.open_device(cfg.id)
    owner_music = {p for p in (dev.mount / "Music").rglob("*") if p.is_file()}

    pc = await a.sync.plan(cfg.id)
    assert pc.plan.count(ActionKind.ADD) == 5, pc.plan.warnings
    rep = await _apply(a, pc.plan.id, first_sync="keep")
    assert rep["status"] == "succeeded" and not rep["failures"], rep
    m = await dev.read_manifest()
    # the card may carry another instance's manifest too (other libraries' items are orphans for this app)
    assert m is not None and len(_ours(m)) == 5
    for e in _ours(m).values():
        assert (dev.mount / e.path).stat().st_size == e.size
    # a card without .local (fresh Format) gets the SonixOS databases at the first sync
    dbs = _db_hashes(dev.mount)
    assert {"library.db", "audiobooks.db", "podcast.db"} <= set(dbs), dbs
    # library.db's index is rebuilt from the card - every one of our files has its MEDIA_TABLE row
    assert {f"/mnt/sd_0/{e.path}" for e in _ours(m).values()} <= _media_paths(dev.mount)

    pc = await a.sync.plan(cfg.id)  # re-run: no-op
    assert {x.kind for x in pc.plan.actions} <= {ActionKind.KEEP}
    fb = await dev.read_feedback(m)  # the real databases parse; our new files have no plays yet
    assert not fb.plays and not fb.ratings

    cfg = await a.sync.update_device(
        cfg.model_copy(update={"rule_set": await own_rules(a, cfg.name, NONE, ["nd"])})
    )
    pc = await a.sync.plan(cfg.id)
    assert pc.plan.count(ActionKind.REMOVE) == 5
    await _apply(a, pc.plan.id, confirm=True)
    assert not _ours(await dev.read_manifest())  # type: ignore[arg-type]
    assert {p for p in (dev.mount / "Music").rglob("*") if p.is_file()} == owner_music
    # the index follows the card (no stale rows); the other databases are not touched by an update
    assert not {f"/mnt/sd_0/{e.path}" for e in _ours(m).values()} & _media_paths(dev.mount)
    after = _db_hashes(dev.mount)
    assert {k: v for k, v in after.items() if k != "library.db"} == {
        k: v for k, v in dbs.items() if k != "library.db"
    }, "the player's other databases changed"
    assert _data_listing() == before, "the test media changed"


def _subsonic(action: str, **params) -> None:
    import httpx

    r = httpx.get(
        f"{ENV['NAVIDROME_URL']}/rest/{action}",
        params={
            "u": ENV["NAVIDROME_USER"],
            "p": ENV["NAVIDROME_PASSWORD"],
            "v": "1.16.1",
            "c": "syncmusic-test",
            "f": "json",
            **params,
        },
        timeout=30,
    )
    r.raise_for_status()
    assert r.json()["subsonic-response"]["status"] == "ok", r.text


def _favourites(mount) -> set[str]:
    import sqlite3

    con = sqlite3.connect(f"file:{mount / '.local' / 'library.db'}?mode=ro", uri=True)
    try:
        return {p for (p,) in con.execute("SELECT path FROM FAVOURITES")}
    finally:
        con.close()


async def test_hiby_library_star_becomes_heart(app):
    """On the real R1: a Navidrome star becomes a FAVOURITES row for our file, unstar removes it, the
    user's own hearts stay. (That the heart shows on the player after a reboot is checked by the user.)"""
    a, r1 = app
    cfg = await a.sync.register(
        RegisterRequest(
            name="HiBy R1",
            disk=r1.path,
            rule_set=await own_rules(a, "HiBy R1", FEW, ["nd"]),
            on_connect="manual",
        )
    )
    dev = await a.sync.open_device(cfg.id)
    if not (dev.mount / ".local" / "library.db").exists():
        pytest.skip(
            "no .local/library.db on the card (the player has not rebuilt it after a format)"
        )
    owner_hearts = _favourites(dev.mount)
    lib = await a.sync.library("nd")
    pc = await a.sync.plan(cfg.id)
    await _apply(a, pc.plan.id, first_sync="keep")
    m = await dev.read_manifest()
    key = sorted(_ours(m))[0]  # type: ignore[arg-type]
    song_id = key.split("/", 1)[1]
    ours = "/mnt/sd_0/" + m.items[key].path  # type: ignore[union-attr]
    try:
        _subsonic("star", id=song_id)
        await a.sync.snapshots.get(lib, force=True)
        pc = await a.sync.plan(cfg.id)
        rep = await _apply(a, pc.plan.id)
        assert ours in _favourites(dev.mount), rep
        assert owner_hearts <= _favourites(dev.mount)

        _subsonic("unstar", id=song_id)
        await a.sync.snapshots.get(lib, force=True)
        pc = await a.sync.plan(cfg.id)
        rep = await _apply(a, pc.plan.id)
        assert ours not in _favourites(dev.mount), rep
        assert rep["reverse"]["stars_pushed"] == 0, rep
        assert _favourites(dev.mount) == owner_hearts
    finally:
        _subsonic("unstar", id=song_id)
        cfg = await a.sync.update_device(
            cfg.model_copy(update={"rule_set": await own_rules(a, cfg.name, NONE, ["nd"])})
        )
        pc = await a.sync.plan(cfg.id)
        await _apply(a, pc.plan.id, confirm=True)
