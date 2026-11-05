"""Positions round trip against the local Audiobookshelf: a bookmark the forward sync wrote from the library
is not a position the user made on the device, so the next reverse sync must not push it back over progress made
elsewhere (another ABS client) in the meantime."""

from __future__ import annotations

import pytest
from pygpod import Database as IPodDB

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import MediaKind
from tests.contracts.harness import ipod_tree
from tests.integration.test_reverse_sync_services import _register, _sync, env  # noqa: F401 - fixture

pytestmark = pytest.mark.integration


async def test_synced_bookmark_does_not_rewind_progress_made_elsewhere(env, tmp_path):  # noqa: F811
    app, c = env
    pods = {
        "id": "p",
        "libraries": ["abs"],
        "kind": ["podcast"],
        "criteria": {"all": []},
        "sort": "size",
        "limit": 1,
    }
    abs_lib = await app.sync.library("abs")
    snap = await abs_lib.snapshot()
    episode = min(
        (t for t in snap.tracks.values() if t.kind is MediaKind.PODCAST), key=lambda t: t.size_bytes
    )
    await abs_lib.set_position(episode.ref, 50.0, False)  # listened to 50 s before the first sync
    app.sync.forget_library("abs")

    mount = ipod_tree(tmp_path / "ipod")
    ipod = await _register(c, mount, "iPod", "ipod", [pods], ["abs"])
    await _sync(app, c, ipod, first_sync="keep")
    entry = next(iter(manifest_io.read(mount).items.values()))
    assert entry.ref == episode.ref
    track = next(t for t in IPodDB(str(mount)).tracks if str(t.dbid) == entry.device_ref)
    assert track.bookmark_time == 50_000  # the library's position went to the device

    abs_lib = await app.sync.library("abs")
    await abs_lib.set_position(episode.ref, 70.0, False)  # then 20 s more on the phone
    app.sync.forget_library("abs")

    report = await _sync(app, c, ipod)  # nothing was played on the iPod
    pos = (await (await app.sync.library("abs")).snapshot()).tracks[episode.ref.item_id].position_s
    assert pos == pytest.approx(70.0, abs=0.5), report["reverse"]
