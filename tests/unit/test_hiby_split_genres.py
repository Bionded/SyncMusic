# SPDX-License-Identifier: GPL-3.0-or-later
"""HiBy R1 Split genres device setting: follow the player's ORGANIZE_STATE rules, or write an explicit choice
(genre bits only) that the index is then built with, with a warning when the player's own setting differs."""

from __future__ import annotations

import sqlite3

import pytest

from syncmusic.core.models import DeviceConfig, Manifest, ManifestHeader
from syncmusic.devices.HibyR1 import hiby_index as h
from syncmusic.devices.HibyR1.sonixos import Hiby, clear_index
from tests.contracts.harness import hiby_tree
from tests.media import make_track


@pytest.mark.parametrize(
    ("rules", "bits", "out"),
    [
        ("a0/g11/j1/x00000000", 0, "a0/g0/j1/x00000000"),
        ("a5/g0/j0/x1a2b3c4d/b3", 1, "a5/g1/j0/x1a2b3c4d/b3"),
        (None, 3, "a0/g3/j0/x00000000"),
        ("a0/j1/x00000000", 2, "a0/g2/j1/x00000000"),
    ],
)
def test_genre_bits_replace_only_the_genre_part(rules, bits, out):
    assert h.with_genre_separators(rules, bits) == out
    assert h.Rules.parse(out).genre == bits


def _dev(tmp_path, rules: str, **settings) -> Hiby:
    mount = hiby_tree(tmp_path / "hiby")
    (mount / ".syncmusic").mkdir()
    clear_index(mount, rules)
    make_track(mount / "Music/A/01.flac", title="One", artist="A", genre="Rock; Pop/Indie")
    return Hiby(DeviceConfig(id="h1", type="hiby", name="H", settings=settings), mount, None)


def _q(dev: Hiby, sql: str) -> list[tuple]:
    con = sqlite3.connect(dev.mount / ".local/library.db")
    try:
        return list(con.execute(sql))
    finally:
        con.close()


async def _commit(dev: Hiby):
    s = await dev.begin()
    m = Manifest(header=ManifestHeader(device_id="h1"))
    s.manifest = m
    await dev.commit(s, m)
    return s


def test_genre_bits_of_the_setting():
    def bits(**kw):
        return Hiby(
            DeviceConfig(id="h", type="hiby", name="H", settings=kw), None, None
        ).genre_bits()

    assert bits() is None
    assert bits(split_genres="off") == 0
    assert bits(split_genres="on") == h.SEMICOLON | h.SLASH | h.COMMA
    assert bits(split_genres="on", genre_separators=["semicolon"]) == h.SEMICOLON


async def test_follow_the_player_keeps_its_rules(tmp_path):
    dev = _dev(tmp_path, "a0/g1/j1/x00000000")
    s = await _commit(dev)
    assert not s.data.get("commit_warnings")
    assert _q(dev, "SELECT rules FROM ORGANIZE_STATE") == [("a0/g1/j1/x00000000",)]
    assert sorted(_q(dev, "SELECT genre FROM GENRE_TABLE")) == [("Pop/Indie",), ("Rock",)]


async def test_explicit_split_writes_the_bits_and_files_genres_by_them(tmp_path):
    dev = _dev(
        tmp_path, "a0/g0/j1/x00000000", split_genres="on", genre_separators=["semicolon", "slash"]
    )
    s = await _commit(dev)
    assert any("Split genres differs" in w for w in s.data["commit_warnings"])
    assert _q(dev, "SELECT rules FROM ORGANIZE_STATE") == [("a0/g3/j1/x00000000",)]
    assert sorted(_q(dev, "SELECT genre FROM GENRE_TABLE")) == [("Indie",), ("Pop",), ("Rock",)]
    # the player now has the same setting: no warning, nothing to write
    before = (dev.mount / ".local/library.db").read_bytes()
    s = await _commit(dev)
    assert not s.data.get("commit_warnings")
    assert (dev.mount / ".local/library.db").read_bytes() == before


async def test_explicit_off_keeps_one_genre(tmp_path):
    dev = _dev(tmp_path, "a0/g11/j0/x00000000", split_genres="off")
    await _commit(dev)
    assert _q(dev, "SELECT rules FROM ORGANIZE_STATE") == [("a0/g0/j0/x00000000",)]
    assert _q(dev, "SELECT genre FROM GENRE_TABLE") == [("Rock; Pop/Indie",)]
