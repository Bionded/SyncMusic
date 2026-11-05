"""Audiobookshelf unreachable during a sync, against the compose ABS: the plan is made while it
answers, then its address points at a closed port (a real httpx ConnectError through the library code, without
stopping the shared container): the items are postponed, not failed; with ABS back the next sync writes them."""

# ruff: noqa: F811 - fixtures imported from other test modules

from __future__ import annotations

import socket

import pytest

from syncmusic.core import applier as applier_mod
from syncmusic.core.applier import Applier
from syncmusic.core.models import ActionKind, MediaKind
from syncmusic.core.planner import plan_sync
from syncmusic.core.prepare import Chain
from tests.integration.services import ENV
from tests.integration.test_abs_local import abs_lib  # noqa: F401 - fixture
from tests.unit.test_applier import _flash, preparer  # noqa: F401 - fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif("ABS_URL" not in ENV, reason="no Audiobookshelf credentials in .env.test"),
]


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])  # closed again when the block ends


async def test_unreachable_abs_postpones_and_the_next_sync_writes(
    abs_lib,
    preparer,
    tmp_path,
    monkeypatch,  # noqa: F811
):
    monkeypatch.setattr(applier_mod, "RETRY_DELAYS_S", (0.05, 0.1))
    snap = await abs_lib.snapshot()
    episodes = sorted(
        (t for t in snap.tracks.values() if t.kind is MediaKind.PODCAST),
        key=lambda t: t.size_bytes or 0,
    )[:2]
    assert len(episodes) == 2
    snap = snap.model_copy(update={"tracks": {t.ref.item_id: t for t in episodes}, "playlists": {}})
    dev = _flash(tmp_path)
    cfg = dev.config.model_copy(update={"library_ids": ["abs"]})

    async def plan():
        return plan_sync(
            cfg, {"abs": snap}, await dev.read_manifest(), dev.effective_capabilities(), Chain([])
        )

    real = abs_lib._client
    abs_lib._client = None
    monkeypatch.setattr(abs_lib.s, "url", f"http://127.0.0.1:{_closed_port()}")
    rep = await Applier(dev, {"abs": abs_lib}, Chain([]), preparer).apply(await plan())
    assert rep.status == "partial" and rep.failures == [] and rep.postponed == 2, rep.message
    assert rep.warnings == ["ABS unreachable - 2 items postponed to the next sync"]

    await abs_lib.client().aclose()
    abs_lib._client = real
    monkeypatch.undo()
    again = await plan()
    assert again.count(ActionKind.ADD) == 2
    rep = await Applier(dev, {"abs": abs_lib}, Chain([]), preparer).apply(again)
    assert rep.status == "succeeded" and rep.counts["add"] == 2, rep.message
