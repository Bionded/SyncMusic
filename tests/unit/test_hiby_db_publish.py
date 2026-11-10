# SPDX-License-Identifier: GPL-3.0-or-later
"""HiBy R1 databases are built on the host and put onto the card atomically, and only when their content changed:
the card never holds a half-written SQLite file, and an update with nothing new writes nothing."""

from __future__ import annotations

import os
import sqlite3

import pytest

from syncmusic.devices.HibyR1 import sonixos
from syncmusic.devices.HibyR1.sonixos import SCHEMAS, publish_databases, stage_databases
from tests.unit.test_hiby_databases import _dev
from tests.unit.test_hiby_playlists import _rows, _sync


def _stamp(dev) -> dict[str, tuple[int, int]]:
    d = dev.mount / ".local"
    return {n: (os.stat(d / n).st_mtime_ns, os.stat(d / n).st_ino) for n in SCHEMAS}


async def test_second_commit_with_nothing_new_writes_nothing(tmp_path):
    dev = _dev(tmp_path)
    manifest = await _sync(dev, tmp_path, ["Прелюдія", "Етюд"], {"Mix": ["Етюд"]})
    before = _stamp(dev)
    for _ in range(3):
        manifest = await _sync(dev, tmp_path, ["Прелюдія", "Етюд"], {"Mix": ["Етюд"]}, old=manifest)
    assert _stamp(dev) == before
    assert not list((dev.mount / ".local").glob("*.new"))
    assert [r[2] for r in _rows(dev, "Mix")] == ["Етюд"]


async def test_a_change_replaces_the_db_by_rename(tmp_path):
    dev = _dev(tmp_path)
    manifest = await _sync(dev, tmp_path, ["Прелюдія"], {"Mix": ["Прелюдія"]})
    ino = os.stat(dev.mount / ".local" / "library.db").st_ino
    await _sync(dev, tmp_path, ["Прелюдія", "Етюд"], {"Mix": ["Етюд", "Прелюдія"]}, old=manifest)
    assert (
        os.stat(dev.mount / ".local" / "library.db").st_ino != ino
    )  # a new file, not an in-place write
    assert [r[2] for r in _rows(dev, "Mix")] == ["Етюд", "Прелюдія"]


async def test_no_sqlite_connection_is_opened_on_the_card(tmp_path, monkeypatch):
    dev = _dev(tmp_path)
    card = str(dev.mount / ".local")
    real = sqlite3.connect
    opened: list[str] = []

    def spy(db, *a, **k):
        opened.append(str(db))
        return real(db, *a, **k)

    monkeypatch.setattr(sonixos.sqlite3, "connect", spy)
    await _sync(dev, tmp_path, ["Прелюдія"], {"Mix": ["Прелюдія"]})
    assert opened
    assert not [p for p in opened if card in p]


def test_crash_before_rename_keeps_the_old_db(tmp_path, monkeypatch):
    dev = _dev(tmp_path)
    d = dev.mount / ".local"
    old = (d / "library.db").read_bytes()
    stage = stage_databases(dev.mount, tmp_path / "stage")
    con = sqlite3.connect(stage / "library.db")
    con.execute("INSERT INTO PLAY_COUNTS(path, plays, last_played) VALUES ('a', 1, 1)")
    con.commit()
    con.close()

    def boom(*a, **k):
        raise OSError("unplugged")

    monkeypatch.setattr(sonixos.os, "replace", boom)
    with pytest.raises(OSError):
        publish_databases(dev.mount, stage)
    assert (d / "library.db").read_bytes() == old
    monkeypatch.undo()
    assert publish_databases(dev.mount, stage) == [
        "library.db"
    ]  # the stray .new is simply overwritten
    assert not (d / "library.db.new").exists()


def test_corrupt_staged_db_keeps_the_old_one(tmp_path):
    dev = _dev(tmp_path)
    d = dev.mount / ".local"
    old = (d / "library.db").read_bytes()
    stage = stage_databases(dev.mount, tmp_path / "stage")
    data = bytearray((stage / "library.db").read_bytes())
    data[200:4000] = b"\xff" * 3800
    (stage / "library.db").write_bytes(bytes(data))
    with pytest.raises(sqlite3.DatabaseError):
        publish_databases(dev.mount, stage)
    assert (d / "library.db").read_bytes() == old


def test_a_journal_on_the_card_forces_a_write_and_goes(tmp_path):
    dev = _dev(tmp_path)
    d = dev.mount / ".local"
    stage = stage_databases(dev.mount, tmp_path / "stage")
    (stage / "library.db-journal").unlink(missing_ok=True)
    (d / "library.db-journal").write_bytes(b"x" * 512)
    assert publish_databases(dev.mount, stage) == ["library.db"]
    assert not (d / "library.db-journal").exists()
