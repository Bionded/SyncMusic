"""On the real Classic: GET /space reports the iTunesDB's real size (= stat of the file) and the 63 MiB
limit, and a plan's estimate for the same content stays close to it. Read-only: registers the Classic as it is,
plans, applies nothing."""

from __future__ import annotations

import pytest

from syncmusic.app import Application
from syncmusic.core.models import LibraryConfig
from syncmusic.core.sync import RegisterRequest
from syncmusic.devices.iPod.ipod import ITUNESDB, ITUNESDB_LIMIT
from tests.factories import own_rules
from tests.integration.test_navidrome_local import ENV

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]


async def test_classic_itunesdb_size_matches_the_file(config, classic_disk):
    classic = classic_disk
    a = Application(config)
    await a.start()
    try:
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
        await a._disk_added(classic)
        cfg = await a.sync.register(
            RegisterRequest(
                name="Size Classic",
                disk=classic.path,
                rule_set=await own_rules(
                    a,
                    "Size Classic",
                    {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}}]},
                    ["nd"],
                ),
                on_connect="manual",
            )
        )
        space = await a.sync.space(cfg.id)
        dev = await a.sync.open_device(cfg.id)
        size = (dev.mount / ITUNESDB).stat().st_size
        assert space["db"] == {"size": size, "limit": ITUNESDB_LIMIT, "level": "ok"}
        pc = await a.sync.plan(cfg.id)
        db = pc.plan.db
        assert db is not None and db.current == size and db.level == "ok"
        assert 0.5 * size < db.estimated < 2 * size + 64 * 1024, (size, db.estimated)
    finally:
        await a.stop()
