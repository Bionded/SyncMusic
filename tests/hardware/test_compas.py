"""HiBy player with the Compás firmware on the real device (USB storage): register by disk, a small first sync
next to what is on the card (kept), no-op re-run, the real tagcache and plugin files parse, the synced items removed
again. The player's own files (`.compas/`, plugin assets and state) are never written (checked by hash)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from syncmusic.app import Application
from syncmusic.core.models import ActionKind, LibraryConfig
from syncmusic.core.sync import RegisterRequest
from syncmusic.devices.HibyR1 import compas_feedback as cf
from tests.factories import own_rules
from tests.hardware.test_flash_acceptance import _apply, _data_listing
from tests.hardware.test_hiby import FEW, NONE
from tests.integration.test_navidrome_local import ENV

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]

PLAYER_TREES = (".compas", ".plugin-assets", ".plugins")


def _player_hashes(mount: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for top in PLAYER_TREES:
        for p in sorted((mount / top).rglob("*")):
            if p.is_file():
                out[str(p.relative_to(mount))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture
async def app(config, compas_disk):
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
    await a._disk_added(compas_disk)
    yield a, compas_disk
    await a.stop()


def _ours(m):
    return {k: e for k, e in m.items.items() if k.startswith("nd/") and not e.orphan}


async def test_compas_sync_feedback_and_cleanup(app):
    a, disk = app
    before = _data_listing()
    cfg = await a.sync.register(
        RegisterRequest(
            name="HiBy Compás",
            disk=disk.path,
            rule_set=await own_rules(a, "HiBy Compás", FEW, ["nd"]),
            on_connect="manual",
        )
    )
    assert cfg.type == "hiby_compas"
    dev = await a.sync.open_device(cfg.id)
    player = _player_hashes(dev.mount)
    assert any(k.startswith(".compas/") for k in player), "no .compas/ on the card"
    other = {p for p in dev.mount.rglob("*") if p.is_file() and not p.name.startswith(".")}
    # the real tagcache (if the player indexed the card) parses
    cf.read_tagcache(dev.mount)

    pc = await a.sync.plan(cfg.id)
    assert pc.plan.count(ActionKind.ADD) == 5, pc.plan.warnings
    rep = await _apply(a, pc.plan.id, first_sync="keep")
    assert rep["status"] == "succeeded" and not rep["failures"], rep
    m = await dev.read_manifest()
    assert m is not None and len(_ours(m)) == 5
    for e in _ours(m).values():
        assert e.path.startswith("Music/") and (dev.mount / e.path).stat().st_size == e.size
    assert (dev.mount / "Podcasts" / "database.ignore").is_file()

    pc = await a.sync.plan(cfg.id)  # re-run: no-op
    assert {x.kind for x in pc.plan.actions} <= {ActionKind.KEEP}
    fb = await dev.read_feedback(m)  # the player has not indexed our new files: nothing for them
    assert not fb.plays and not fb.stars

    cfg = await a.sync.update_device(
        cfg.model_copy(update={"rule_set": await own_rules(a, cfg.name, NONE, ["nd"])})
    )
    pc = await a.sync.plan(cfg.id)
    assert pc.plan.count(ActionKind.REMOVE) == 5
    await _apply(a, pc.plan.id, confirm=True)
    assert not _ours(await dev.read_manifest())  # type: ignore[arg-type]
    left = {p for p in dev.mount.rglob("*") if p.is_file() and not p.name.startswith(".")}
    assert other <= left, "a file that was on the card before is gone"
    assert _player_hashes(dev.mount) == player, "the player's own files changed"
    assert _data_listing() == before, "the test media changed"
