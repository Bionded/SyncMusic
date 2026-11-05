"""A library that stops answering mid-sync: its downloads pause and are retried; if it stays down its
items are postponed (no failures, one warning) while the other libraries' items are still written."""

from __future__ import annotations

import os

import httpx
import pytest

from syncmusic.base.library import LibraryUnavailable, library_unavailable
from syncmusic.core import applier as applier_mod
from syncmusic.core.applier import Applier
from syncmusic.core.models import ActionKind, LibraryConfig
from syncmusic.core.planner import plan_sync
from syncmusic.core.prepare import Chain
from tests.fakes import FakeLibrary
from tests.unit.test_applier import KB, Stop, _cfg, _flash, preparer  # noqa: F401


class Down(FakeLibrary):
    """Refuses the first `fails` downloads (None: all of them) like an unreachable server."""

    fails: int | None = None
    calls = 0

    async def open_media(self, item_id):
        self.calls += 1
        if self.fails is None or self.calls <= self.fails:
            raise httpx.ConnectError("All connection attempts failed")
        return await super().open_media(item_id)


def _lib(tmp_path, lib_id, names, cls=FakeLibrary):
    root = tmp_path / lib_id
    root.mkdir()
    for i, name in enumerate(names):
        (root / f"{name}.mp3").write_bytes(os.urandom(20 * KB + i))
    return cls(
        LibraryConfig(id=lib_id, type="fake", name=lib_id.upper(), settings={"root": str(root)}),
        None,
    )


async def _plan(dev, libs):
    snaps = {k: await v.snapshot() for k, v in libs.items()}
    cfg = dev.config.model_copy(update={"library_ids": list(libs)})
    return plan_sync(cfg, snaps, await dev.read_manifest(), dev.effective_capabilities(), Chain([]))


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    monkeypatch.setattr(applier_mod, "RETRY_DELAYS_S", (0.01, 0.02, 0.03))


def test_connection_errors_and_5xx_mean_the_library_is_down():
    assert library_unavailable(httpx.ConnectError("All connection attempts failed"))
    assert library_unavailable(httpx.ReadTimeout("timed out"))
    assert library_unavailable(LibraryUnavailable("HTTP 503 (server error)"))
    try:
        try:
            raise ConnectionRefusedError(111, "Connection refused")
        except OSError as e:
            raise RuntimeError("download failed") from e
    except RuntimeError as wrapped:
        assert library_unavailable(wrapped)
    assert not library_unavailable(KeyError("download failed: HTTP 404"))
    assert not library_unavailable(OSError("download ended early"))


async def test_a_short_outage_pauses_and_then_everything_is_written(tmp_path, preparer):  # noqa: F811
    dev = _flash(tmp_path)
    lib = _lib(tmp_path, "lib", ["Один", "Два", "Три", "Чотири"], Down)
    lib.fails = 2  # down for the first two attempts, then back
    msgs: list[str] = []
    plan = await _plan(dev, {"lib": lib})
    rep = await Applier(
        dev,
        {"lib": lib},
        Chain([]),
        preparer,
        workers=1,
        progress=lambda f, m, **kw: msgs.append(m or ""),
    ).apply(plan)
    assert rep.status == "succeeded", rep.message
    assert rep.counts["add"] == 4 and rep.failures == [] and rep.postponed == 0
    assert any("LIB unreachable: retrying in" in m for m in msgs)


async def test_a_library_that_stays_down_postpones_its_items_others_go_on(tmp_path, preparer):  # noqa: F811
    dev = _flash(tmp_path)
    down = _lib(tmp_path, "abs", ["Подкаст 1", "Подкаст 2", "Подкаст 3"], Down)
    up = _lib(tmp_path, "lib", ["Один", "Два"])
    libs = {"abs": down, "lib": up}
    plan = await _plan(dev, libs)
    rep = await Applier(dev, libs, Chain([]), preparer, workers=2).apply(plan)
    assert rep.status == "partial"
    assert rep.failures == []  # not 3 failures
    assert rep.counts["add"] == 2 and rep.postponed == 3
    assert rep.warnings == ["ABS unreachable - 3 items postponed to the next sync"]
    assert "3 postponed" in rep.message
    # tried once plus once per retry, not once per item and retry
    assert down.calls == 2 + len(
        applier_mod.RETRY_DELAYS_S
    )  # the first window, then one probe per retry
    # still planned: the next sync adds them
    again = await _plan(dev, libs)
    assert again.count(ActionKind.ADD) == 3


async def test_cancel_while_waiting_for_the_library(tmp_path, preparer, monkeypatch):  # noqa: F811
    monkeypatch.setattr(applier_mod, "RETRY_DELAYS_S", (30.0,))
    dev = _flash(tmp_path)
    lib = _lib(tmp_path, "lib", ["Один"], Down)
    waits = []

    def check():
        if waits:
            raise Stop()

    def progress(f, m, **kw):
        if m and "retrying" in m:
            waits.append(m)

    plan = await _plan(dev, {"lib": lib})
    rep = await Applier(
        dev, {"lib": lib}, Chain([]), preparer, check_cancelled=check, progress=progress
    ).apply(plan)
    assert rep.status == "cancelled" and waits
