"""Failures name the track: every failed item's log line and report entry say artist – album – title, the
step that failed and the error, not only a cache or download path."""

from __future__ import annotations

import errno
import logging

import pytest

from syncmusic.core.applier import Applier, DeviceIOError
from syncmusic.core.prepare import Chain
from syncmusic.devices.flashdrive import FlashDrive
from tests.fakes import FakeLibrary, FakeTrailer
from tests.unit.test_applier import _cfg, _plan, library, preparer  # noqa: F401 - fixtures

NAME = "Скрябін – Група крові"


class Named(FakeLibrary):
    async def snapshot(self):
        snap = await super().snapshot()
        for t in snap.tracks.values():
            t.artist, t.album = "Скрябін", "Група крові"
        return snap


class Broken(FakeTrailer):
    type_name = "broken"

    async def run(self, item, caps):
        raise RuntimeError(f"ffmpeg exited 1 for {item.path}")


@pytest.fixture
def named(library):  # noqa: F811
    return Named(library.config, None)


@pytest.fixture
def dev(tmp_path):
    mount = tmp_path / "flash"
    (mount / ".syncmusic").mkdir(parents=True)
    return FlashDrive(_cfg(), mount, None)


async def _apply(dev, lib, preparer, chain=None):  # noqa: F811
    chain = chain or Chain([])
    plan = await _plan(dev, lib, chain)
    return await Applier(dev, {"lib": lib}, chain, preparer).apply(plan)


def _check(rep, caplog, step, error):
    assert rep.failures, rep
    for f in rep.failures:
        assert f.item.startswith(NAME + " – ") and f.step == step and error in f.error, f
    lines = [r for r in caplog.records if " failed for " in r.getMessage()]
    assert len(lines) == len(rep.failures)
    for r in lines:
        msg = r.getMessage()
        assert NAME in msg and f"{step} failed" in msg and error in msg, msg
        assert r.kv["item_step"] == step and "step" not in r.kv and r.kv["item"].startswith(NAME)


async def test_a_failed_download_names_the_track(dev, named, preparer, caplog, monkeypatch):  # noqa: F811
    async def gone(
        item_id,
    ):  # the item, not the library: a connection error postpones instead
        raise RuntimeError("server closed the connection")

    monkeypatch.setattr(named, "open_media", gone)
    with caplog.at_level(logging.WARNING):
        rep = await _apply(dev, named, preparer)
    assert len(rep.failures) == 4
    _check(rep, caplog, "download", "server closed the connection")


async def test_a_failed_processor_names_the_track_and_the_processor(dev, named, preparer, caplog):  # noqa: F811
    with caplog.at_level(logging.WARNING):
        rep = await _apply(dev, named, preparer, Chain([Broken({}, None)]))
    _check(rep, caplog, "broken", "ffmpeg exited 1")
    assert {f.title for f in rep.failures} == {"Один", "Два", "Три", "Чотири"}


async def test_a_failed_put_names_the_track(dev, named, preparer, caplog, monkeypatch):  # noqa: F811
    real = dev.put_track

    async def put(s, item):
        if item.track.title == "Два":
            raise ValueError("name too long for the file system")
        return await real(s, item)

    monkeypatch.setattr(dev, "put_track", put)
    with caplog.at_level(logging.WARNING):
        rep = await _apply(dev, named, preparer)
    _check(rep, caplog, "put", "name too long")
    assert [f.item for f in rep.failures] == [NAME + " – Два"]


async def test_an_io_error_names_the_track_being_written(dev, named, preparer, caplog, monkeypatch):  # noqa: F811
    async def put(s, item):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(dev, "put_track", put)
    with caplog.at_level(logging.WARNING), pytest.raises(DeviceIOError) as e:
        await _apply(dev, named, preparer)
    assert f"while writing {NAME} – " in str(e.value)
    assert any(f"put failed for {NAME}" in r.getMessage() for r in caplog.records)
