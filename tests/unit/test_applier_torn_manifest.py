# SPDX-License-Identifier: GPL-3.0-or-later
"""A long sync to a flash drive fails with an I/O error; a file system check then cuts the manifest short. The next
sync keeps what was committed, takes over the files already on the device and writes every track exactly once."""

from __future__ import annotations

import errno
import os
from types import SimpleNamespace

import pytest

from syncmusic.core import applier
from syncmusic.core import manifest as manifest_io
from syncmusic.core.applier import DeviceIOError
from syncmusic.core.models import ActionKind, LibraryConfig, Rule
from syncmusic.core.prepare import Preparer
from syncmusic.infra.cache import ProcessingCache
from syncmusic.infra.db import Database
from tests.fakes import FakeLibrary
from tests.unit.test_applier import KB, Recording, _cfg, _sync

TRACKS = ["Один", "Два", "Три", "Чотири", "Пʼять", "Шість"]
FAIL_AT = 5  # the fifth write fails


@pytest.fixture
async def preparer(tmp_path):
    db = Database(tmp_path / "sd.db")
    await db.open()
    yield Preparer(ProcessingCache(db, tmp_path / "cache"), tmp_path / "work")
    await db.close()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    for i, name in enumerate(TRACKS):
        (root / f"{name}.mp3").write_bytes(os.urandom(20 * KB + i))
    return FakeLibrary(
        LibraryConfig(id="lib", type="fake", name="L", settings={"root": str(root)}), None
    )


class Failing(Recording):
    """Writes fail with EIO from the `fail_at`-th file on (a dying card)."""

    fail_at: int | None = None

    def _place(self, src, dest):
        if self.fail_at is not None and self.calls.count("put") >= self.fail_at:
            raise OSError(errno.EIO, "Input/output error")
        super()._place(src, dest)


def _music(dev) -> list[str]:
    return sorted(
        p.relative_to(dev.mount).as_posix()
        for p in dev.mount.rglob("*.mp3")
        if ".syncmusic" not in p.parts
    )


async def _failed_long_sync(tmp_path, library, preparer, monkeypatch) -> Failing:
    """A checkpoint after every file; the fifth file fails with EIO: four items are committed."""
    dev = Failing(_cfg(), tmp_path / "flash", None)
    (dev.mount / ".syncmusic").mkdir(parents=True)
    dev.calls, dev.fail_at = [], FAIL_AT
    clock = SimpleNamespace(monotonic=lambda: 100.0 * dev.calls.count("put"))
    monkeypatch.setattr(applier, "time", clock)
    with pytest.raises(DeviceIOError):
        await _sync(dev, library, preparer, checkpoint_s=1)
    monkeypatch.undo()
    dev.fail_at = None
    manifest_io.drop_cache()  # the next sync runs after a reconnect
    return dev


async def _next_sync_is_clean(dev, library, preparer):
    dev.calls = []
    plan, rep = await _sync(dev, library, preparer)
    assert rep.status == "succeeded", rep.message
    music = _music(dev)
    assert len(music) == len(TRACKS), music  # every track once, no "(1)" copies
    assert not any("(" in m for m in music), music
    m = await dev.read_manifest()
    assert sorted(e.path for e in m.items.values()) == music
    manifest_io.drop_cache()
    plan2, _ = await _sync(dev, library, preparer)
    assert {a.kind for a in plan2.actions} == {ActionKind.KEEP}
    return plan, rep


async def test_items_committed_before_an_io_error_are_kept(
    tmp_path, library, preparer, monkeypatch
):
    dev = await _failed_long_sync(tmp_path, library, preparer, monkeypatch)
    assert len((await dev.read_manifest()).items) == FAIL_AT - 1
    plan, rep = await _next_sync_is_clean(dev, library, preparer)
    assert plan.count(ActionKind.KEEP) == FAIL_AT - 1
    assert rep.counts["add"] == len(TRACKS) - FAIL_AT + 1
    assert dev.calls.count("put") == len(TRACKS) - FAIL_AT + 1


def _lines(dev) -> list[bytes]:
    return (dev.mount / manifest_io.MANIFEST_PATH).read_bytes().splitlines(keepends=True)


def _cut(dev, keep: bytes) -> None:
    (dev.mount / manifest_io.MANIFEST_PATH).write_bytes(keep)


async def test_a_body_cut_mid_line_falls_back_to_the_backup(
    tmp_path, library, preparer, monkeypatch
):
    dev = await _failed_long_sync(tmp_path, library, preparer, monkeypatch)
    lines = _lines(dev)
    assert not any(b'"type":"append"' in ln for ln in lines)  # checkpoints write in full
    assert (dev.mount / manifest_io.BACKUP_PATH).read_bytes() == b"".join(lines)
    _cut(dev, lines[0] + lines[1][: len(lines[1]) // 2])
    assert len(manifest_io.read(dev.mount).items) == FAIL_AT - 1  # the backup copy
    plan, _ = await _next_sync_is_clean(dev, library, preparer)
    assert plan.count(ActionKind.KEEP) == FAIL_AT - 1
    assert dev.calls.count("put") == len(TRACKS) - FAIL_AT + 1


async def test_a_body_cut_at_a_line_end_takes_over_the_files(
    tmp_path, library, preparer, monkeypatch
):
    """cut at a line end the manifest still parses, with fewer items: the files of the lost items are on the device
    byte for byte and are taken over, not copied again next to them."""
    dev = await _failed_long_sync(tmp_path, library, preparer, monkeypatch)
    lines = _lines(dev)
    _cut(dev, lines[0] + lines[1])
    assert len(manifest_io.read(dev.mount).items) == 1
    plan, _ = await _next_sync_is_clean(dev, library, preparer)
    assert plan.count(ActionKind.KEEP) == 1


@pytest.mark.parametrize("where", ["tail_mid_line", "tail_dropped"])
async def test_an_appended_tail_cut_short(tmp_path, library, preparer, where):
    """an update sync appends its items to the manifest; cut inside that tail, the next sync takes the lost
    items' files over and ends with every track once."""
    # no playlist: a changed playlist would rewrite the manifest in full
    dev = Failing(_cfg(rules=[Rule(id="all", criteria={"all": []})]), tmp_path / "flash", None)
    (dev.mount / ".syncmusic").mkdir(parents=True)
    dev.calls = []
    later = [library.root / f"{n}.mp3" for n in TRACKS[3:]]
    held = {p: p.read_bytes() for p in later}
    for p in later:
        p.unlink()
    await _sync(dev, library, preparer)
    for p, data in held.items():
        p.write_bytes(data)
    await _sync(dev, library, preparer)
    lines = _lines(dev)
    marker = next(i for i, ln in enumerate(lines) if b'"type":"append"' in ln)
    assert len(lines) - marker - 1 == len(later)
    if where == "tail_mid_line":
        _cut(dev, b"".join(lines[:-1]) + lines[-1][: len(lines[-1]) // 2])
        expect = len(TRACKS) - len(later)  # the unfinished commit is dropped as a whole
    else:
        _cut(dev, b"".join(lines[:marker]))
        expect = len(TRACKS) - len(later)
    manifest_io.drop_cache()
    assert len(manifest_io.read(dev.mount).items) == expect
    before = {m: os.stat(dev.mount / m).st_ino for m in _music(dev)}
    plan, _ = await _next_sync_is_clean(dev, library, preparer)
    assert plan.count(ActionKind.KEEP) == expect
    assert {
        m: os.stat(dev.mount / m).st_ino for m in _music(dev)
    } == before  # taken over, not copied
