"""The SonixOS music index rebuilt from the card: our formulas against a synthetic index
(tests/fixtures/hiby/synthetic-index.json, written by make_synthetic_index.py next to it), and a rebuild of an
emulated card."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from syncmusic.devices.HibyR1 import hiby_index as h
from syncmusic.devices.HibyR1.sonixos import clear_index, ensure_databases, write_library_index
from tests.media import make_track

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/hiby/synthetic-index.json").read_text())


def test_media_columns_match_the_player():
    for r in FIXTURE["media"]:
        assert h.index_char(r["name"])[0] == r["character"], r
        assert h.sort_key(r["name"]) == r["sortkey"], r
        assert h.album_key(r["album_artist"], r["path"]) == r["album_key"], r


def test_lookup_tables_match_the_player():
    for table, rows in FIXTURE["lookups"].items():
        for value, character, cn, sortkey in rows:
            assert (*h.index_char(value), h.sort_key(value)) == (character, cn, sortkey), (
                table,
                value,
            )


def test_genre_links_match_the_player():
    rules = h.Rules.parse(FIXTURE["rules"])
    mine = sorted(
        [p, g] for p, value in FIXTURE["media_genre"] for g in h.split_names(value, rules.genre)
    )
    assert mine == sorted(FIXTURE["genre_link"])


def test_folder_signatures_match_the_player():
    for f in FIXTURE["folders"]:
        assert h.folder_signature([(n, d) for n, d in f["entries"]]) == (f["files"], f["names"]), f[
            "path"
        ]


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("A (feat. B & C)", ["A", "B", "C"]),
        ("A (B & C)", []),
        ("Rock (Hard/Soft)", []),
        ("AC/DC", []),
        ("Rock; Pop; rock", ["Rock", "Pop"]),
        ("A feat. B", ["A", "B"]),
        # separator words and symbols, brackets, trimming, exceptions
        ("A feat B", ["A", "B"]),
        ("A Feat. B", ["A", "B"]),
        ("A ft. B", ["A", "B"]),
        ("A featuring B", ["A", "B"]),
        ("A&B", ["A", "B"]),
        ("A / B", ["A", "B"]),
        ("Rock/Pop", ["Rock", "Pop"]),
        ("A [feat. B]", ["A", "B"]),
        ("A (feat. B & C) & D", ["A", "B", "C", "D"]),
        ("  A ; B  ", ["A", "B"]),
        ("A; ;B", ["A", "B"]),
        ("A -; B", ["A", "B"]),
        ("A; B (", ["A", "B"]),
        ("A (; B)", []),
        ("feat. B", []),
        ("ac/dc; Pop", ["ac/dc", "Pop"]),
        ("", []),
    ],
)
def test_split_names(text, names):
    seps = h.SEMICOLON | h.SLASH | h.AMPERSAND | h.FEAT
    assert h.split_names(text, seps, ("AC/DC",)) == names


def test_split_names_only_enabled_separators_and_limits():
    assert h.split_names("A & B", h.AMPERSAND) == ["A", "B"]
    assert h.split_names("A & B", h.SLASH) == []
    assert h.split_names("A feat. B", h.SEMICOLON) == []
    assert h.split_names("A;B", 0) == []
    many = h.split_names(";".join(str(i) for i in range(20)), h.SEMICOLON)
    assert len(many) == h.SPLIT_MAX and many[-1] == "1516171819"  # the last part keeps the rest
    long = h.split_names("A;" + "x" * 300, h.SEMICOLON)
    assert [len(n) for n in long] == [1, h.SPLIT_NAME_MAX - 1]


def test_sort_key_and_letter_edge_cases():
    assert h.sort_key("The Rebel Path") == "3rebel path"
    assert h.sort_key("Ёлка") == "5елка"
    assert h.index_char("Ёлка") == ("Ё", h.CYRILLIC)
    assert h.index_char("10 Years") == ("0-9", h.DIGIT)
    assert h.index_char("") == ("#", h.EMPTY)
    assert h.sort_key("Przybyłowicz") == "3przybylowicz"


def _card(tmp_path: Path) -> Path:
    card = tmp_path / "card"
    make_track(card / "Music/Artist/2001 - Album/01 - One.flac", title="One", artist="Artist", album="Album",
               genre=["Rock", "Pop"], tracknumber="1/9", discnumber="2", date="2001-05-01")  # fmt: skip
    make_track(
        card / "Music/Artist/2001 - Album/02 - Two.mp3",
        title="The Two",
        artist="Artist",
        album="Album",
    )
    make_track(card / "Music/Loose/untagged.flac")
    make_track(card / "Audiobooks/Book/01.mp3", title="Chapter")  # not in MEDIA_TABLE
    make_track(card / "Podcast/Show/ep.mp3", title="Episode")
    (card / "Music/Artist/2001 - Album/cover.jpg").write_bytes(b"x")
    ensure_databases(card, tmp_path / "bak")
    return card


def _q(card: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(card / ".local/library.db")
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_rebuild_indexes_the_card_like_a_scan(tmp_path):
    card = _card(tmp_path)
    con = sqlite3.connect(card / ".local/library.db")
    con.execute("INSERT INTO ORGANIZE_STATE VALUES (0, 'a0/g11/j1/x00000000')")
    con.execute(
        "INSERT INTO MEDIA_TABLE(id, path, name) VALUES (1, '/mnt/sd_0/Music/gone.flac', 'gone')"
    )
    con.execute("INSERT INTO FAVOURITES VALUES ('/mnt/sd_0/Music/Loose/untagged.flac', 'u', '', 1)")
    con.commit()
    con.close()
    st = write_library_index(card, tmp_path / "bak")
    assert (st.tracks, st.read) == (3, 3)
    rows = {r[0]: r[1:] for r in _q(card, "SELECT path, name, genre, year, dis_id, disc, sample_rate, bit, format,"
                                          " sortkey, character, mtime FROM MEDIA_TABLE")}  # fmt: skip
    one = "/mnt/sd_0/Music/Artist/2001 - Album/01 - One.flac"
    assert set(rows) == {
        one,
        "/mnt/sd_0/Music/Artist/2001 - Album/02 - Two.mp3",
        "/mnt/sd_0/Music/Loose/untagged.flac",
    }
    assert rows[one][:9] == ("One", "Rock; Pop", 2001, 1, 2, 44100, 16, 2, "3one")
    assert rows[one][10] == int(os.stat(card / one.removeprefix("/mnt/sd_0/")).st_mtime)
    assert rows["/mnt/sd_0/Music/Artist/2001 - Album/02 - Two.mp3"][8:10] == ("3two", "T")
    assert rows["/mnt/sd_0/Music/Loose/untagged.flac"][0] == "untagged"
    assert sorted(_q(card, "SELECT genre FROM GENRE_TABLE")) == [("Pop",), ("Rock",)]
    assert len(_q(card, "SELECT * FROM GENRE_LINK")) == 2
    folders = {p: (f, n) for p, _, f, n in _q(card, "SELECT * FROM FOLDER_TABLE")}
    assert set(folders) == {"/mnt/sd_0", "/mnt/sd_0/Music", "/mnt/sd_0/Music/Artist",
                            "/mnt/sd_0/Music/Artist/2001 - Album", "/mnt/sd_0/Music/Loose"}  # fmt: skip
    assert folders["/mnt/sd_0"][0] == 3  # Music/, Audiobooks/, Podcast/ (counted, not entered)
    assert folders["/mnt/sd_0/Music/Artist/2001 - Album"][0] == 2  # cover.jpg not counted
    assert _q(card, "SELECT cn FROM COUNT_TABLE") == [(3,)]
    assert _q(card, "SELECT count(*) FROM FAVOURITES") == [(1,)]  # user tables kept
    names = {n for (n,) in _q(card, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"media_sort_idx", "genre_sort_idx", "media_album_key_idx"} <= names

    # second run: unchanged files keep their rows without a tag read; a removed file leaves no row
    (card / "Music/Loose/untagged.flac").unlink()
    st = write_library_index(card, tmp_path / "bak")
    assert (st.tracks, st.read) == (2, 0)
    assert rows[one][:9] == _q(card, "SELECT name, genre, year, dis_id, disc, sample_rate, bit, format, sortkey"
                                     f" FROM MEDIA_TABLE WHERE path = '{one}'")[0]  # fmt: skip
    assert _q(
        card, "SELECT files, names FROM FOLDER_TABLE WHERE path = '/mnt/sd_0/Music/Loose'"
    ) == [(0, 0)]


def test_clear_index_keeps_the_players_rules(tmp_path):
    card = _card(tmp_path)
    con = sqlite3.connect(card / ".local/library.db")
    con.execute("INSERT INTO ORGANIZE_STATE VALUES (0, 'a0/g11/j1/x00000000')")
    con.commit()
    con.close()
    clear_index(card)
    assert _q(card, "SELECT rules FROM ORGANIZE_STATE") == [("a0/g11/j1/x00000000",)]


def test_album_join_puts_a_compilation_on_one_key(tmp_path):
    card = tmp_path / "card"
    for i, artist in enumerate(["X", "Y", "Y"], 1):
        make_track(card / f"Music/Comp/{i}.flac", title=str(i), artist=artist, album="Comp")
    make_track(
        card / "Music/Other/1.flac", title="o", artist="Z", album="Comp"
    )  # own key, own folder: stays apart
    ensure_databases(card, tmp_path / "bak")
    con = sqlite3.connect(card / ".local/library.db")
    con.execute("INSERT INTO ORGANIZE_STATE VALUES (0, 'a0/g0/j1/x00000000')")
    con.commit()
    con.close()
    write_library_index(card, tmp_path / "bak")
    keys = dict(_q(card, "SELECT path, album_key FROM MEDIA_TABLE"))
    comp = {k for p, k in keys.items() if "/Comp/" in p}
    assert comp == {h.album_key("Y", "")}  # the key with the most tracks
    assert keys["/mnt/sd_0/Music/Other/1.flac"] == h.album_key("Z", "")
    assert len(_q(card, "SELECT * FROM ALBUM_GROUP_TABLE")) == 2


def test_rebuild_takes_known_tags_and_reads_the_card_once(tmp_path, monkeypatch):
    """tags known from writing the file (or from an earlier read) are taken without a card read, even
    when the old database's rows don't match (one the player wrote itself); what was read lands in `known`."""
    card = _card(tmp_path)
    con = sqlite3.connect(card / ".local/library.db")
    con.execute("DELETE FROM MEDIA_TABLE")  # no old rows to reuse
    con.commit()
    con.close()
    one = "/mnt/sd_0/Music/Artist/2001 - Album/01 - One.flac"
    st1 = os.stat(card / one.removeprefix("/mnt/sd_0/"))
    known: h.KnownTags = {
        one: (st1.st_size, int(st1.st_mtime), h.Tags(title="Known", artist="Cached")),
        "/mnt/sd_0/Music/gone.flac": (1, 1, h.Tags(title="gone")),
    }
    reads: list[Path] = []
    real = h.read_tags
    monkeypatch.setattr(h, "read_tags", lambda p: reads.append(p) or real(p))
    seen: list[tuple[int, int | None]] = []
    from syncmusic.base.progress import on_progress

    with on_progress(lambda d, t: seen.append((d, t))):
        st = write_library_index(card, tmp_path / "bak", known=known)
    assert (st.tracks, st.read, len(reads)) == (3, 2, 2)
    assert _q(card, f"SELECT name, artist FROM MEDIA_TABLE WHERE path = '{one}'") == [
        ("Known", "Cached")
    ]
    assert set(known) == {one, "/mnt/sd_0/Music/Artist/2001 - Album/02 - Two.mp3",
                          "/mnt/sd_0/Music/Loose/untagged.flac"}  # fmt: skip
    assert seen[0] == (0, 3) and seen[-1] == (3, 3)

    con = sqlite3.connect(card / ".local/library.db")
    con.execute(
        "DELETE FROM MEDIA_TABLE"
    )  # the player rewrote its index: rows gone, the cache still knows
    con.commit()
    con.close()
    reads.clear()
    st = write_library_index(card, tmp_path / "bak", known=known)
    assert (st.tracks, st.read, reads) == (3, 0, [])


def test_rebuild_keeps_a_folder_time_while_its_entries_are_the_same(tmp_path):
    card = _card(tmp_path)
    write_library_index(card, tmp_path / "bak")
    first = dict(_q(card, "SELECT path, mtime FROM FOLDER_TABLE"))
    loose = card / "Music/Loose"
    os.utime(
        card, (2_000_000_000, 2_000_000_000)
    )  # e.g. our own hidden files written into the root
    make_track(loose / "new.flac")  # a new entry: that folder takes its current time
    os.utime(loose, (2_000_000_000, 2_000_000_000))
    write_library_index(card, tmp_path / "bak")
    second = dict(_q(card, "SELECT path, mtime FROM FOLDER_TABLE"))
    assert second["/mnt/sd_0"] == first["/mnt/sd_0"]
    assert (
        second["/mnt/sd_0/Music/Loose"]
        == int(loose.stat().st_mtime)
        != first["/mnt/sd_0/Music/Loose"]
    )
