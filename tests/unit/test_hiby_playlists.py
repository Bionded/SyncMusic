"""HiBy R1 playlists: SonixOS keeps them only as `M3U_<name>` tables in library.db."""

from __future__ import annotations

import sqlite3

from syncmusic.core.models import (
    DeviceConfig,
    DevicePlaylist,
    Manifest,
    ManifestEntry,
    ManifestHeader,
)
from syncmusic.devices.HibyR1.sonixos import Hiby, playlist_table_name, read_playlist_tables
from tests.contracts.harness import hiby_sql, hiby_tree, work_item


def _dev(tmp_path) -> Hiby:
    mount = hiby_tree(tmp_path / "hiby")
    (mount / ".syncmusic").mkdir()
    return Hiby(DeviceConfig(id="h1", type="hiby", name="H"), mount, None)


async def _sync(dev: Hiby, tmp_path, titles: list[str], playlists: dict[str, list[str]], old=None):
    """Write `titles`, then the playlists (name -> titles) and commit, like the applier does."""
    s = await dev.begin()
    manifest = old or Manifest(header=ManifestHeader(device_id="h1"))
    for t in titles:
        item = work_item(tmp_path, t, artist="Скрябін")
        if item.track.ref.key() in manifest.items:
            continue
        ref = await dev.put_track(s, item)
        manifest.items[item.track.ref.key()] = ManifestEntry(
            ref=item.track.ref,
            device_ref=ref.device_ref,
            path=ref.path,
            size=ref.size,
            duration_s=181.0,
            meta={"title": t, "artist": "Скрябін"},
        )
    by_title = {e.meta["title"]: e.ref for e in manifest.items.values()}
    pls = [DevicePlaylist(name=n, refs=[by_title[t] for t in ts]) for n, ts in playlists.items()]
    await dev.set_playlists(s, pls)
    manifest.playlists = list(playlists)
    await dev.commit(s, manifest)
    return manifest


def _rows(dev: Hiby, table: str) -> list[tuple]:
    con = sqlite3.connect(dev.mount / ".local" / "library.db")
    try:
        return list(
            con.execute(
                f'SELECT idx, path, title, artist, seconds, present, mount FROM "M3U_{table}" ORDER BY idx'
            )
        )
    finally:
        con.close()


def test_table_names_follow_sonixos_rules():
    assert playlist_table_name("Rock: 80s/90s?") == "Rock_ 80s_90s_"
    assert playlist_table_name(".hidden") == "hidden"
    assert len(playlist_table_name("Я" * 150).encode()) <= 200


async def test_playlists_written_as_tables_and_owner_table_kept(tmp_path):
    dev = _dev(tmp_path)
    # a own playlist made on the player, and a .m3u8 of ours from before
    hiby_sql(
        dev.mount,
        "library.db",
        'CREATE TABLE "M3U_SuperTest"(idx INTEGER PRIMARY KEY, path TEXT, title TEXT, artist TEXT,'
        " seconds INT, present INT, mount INT)",
    )
    old_file = dev.mount / "Playlists" / "Mix.m3u8"
    old_file.parent.mkdir()
    old_file.write_text("#EXTM3U\n")
    old = Manifest(header=ManifestHeader(device_id="h1"), playlists=["Mix"])
    old.playlist_paths = {"Mix": "Playlists/Mix.m3u8"}
    from syncmusic.core import manifest as mf

    mf.write(dev.mount, old)
    m = await _sync(
        dev,
        tmp_path,
        ["Зірка", "Зозуля"],
        {"Mix": ["Зозуля", "Зірка"], 'A "q"': ["Зірка"]},
        old,
    )
    assert not old_file.exists() and not old_file.parent.exists()
    rows = _rows(dev, "Mix")
    assert [(r[0], r[2], r[3], r[4], r[5], r[6]) for r in rows] == [
        (1, "Зозуля", "Скрябін", 181, 1, None),
        (2, "Зірка", "Скрябін", 181, 1, None),
    ]
    assert all(r[1].startswith("/mnt/sd_0/Music/") for r in rows)
    assert m.playlist_paths == {"Mix": "M3U_Mix", 'A "q"': "M3U_A _q_"}
    tables = read_playlist_tables(dev.mount)
    assert set(tables) == {"Mix", "A _q_", "SuperTest"}
    assert tables["Mix"] == [r[1].removeprefix("/mnt/sd_0/") for r in rows]
    assert list((dev.mount / ".syncmusic" / "hiby-db-backups").glob("library-*.db"))
    # reverse: ours by their manifest names, the player's own by its table
    assert set(await dev.read_playlists(m)) == {"Mix", 'A "q"', "SuperTest"}

    # removed from the rules: our table goes, a stays
    m = await _sync(dev, tmp_path, [], {"Mix": ["Зірка"]}, m)
    assert set(read_playlist_tables(dev.mount)) == {"Mix", "SuperTest"}
    assert [r[2] for r in _rows(dev, "Mix")] == ["Зірка"]


async def test_hot_journal_is_dropped_and_the_database_rewritten(tmp_path):
    """An unfinished write's journal is backed up and dropped."""
    dev = _dev(tmp_path)
    (dev.mount / ".local" / "library.db-journal").write_bytes(b"x" * 512)
    m = await _sync(dev, tmp_path, ["Зірка"], {"Mix": ["Зірка"]})
    assert list(read_playlist_tables(dev.mount)) == ["Mix"]
    assert m.playlist_paths == {"Mix": "M3U_Mix"}


async def test_audiobooks_indexed_keeping_player_data(tmp_path):
    """SonixOS never indexes new books by itself - our books get AUDIOBOOK_TABLE/PARTS rows like its scan
    makes; the player's resume data stays, rows of books gone from the card go."""
    from syncmusic.core.models import ItemRef, MediaKind

    dev = _dev(tmp_path)
    folder = dev.mount / "Audiobooks" / "Рей Бредбері - 451"
    folder.mkdir(parents=True)
    single = dev.mount / "Audiobooks" / "Mort" / "001 - Mort.mp3"
    single.parent.mkdir()
    single.write_bytes(b"m" * 10)
    m = Manifest(header=ManifestHeader(device_id="h1"))
    for i, name in enumerate(["001 - Частина перша.mp3", "002 - Частина друга.mp3"]):
        (folder / name).write_bytes(b"x" * 100)
        ref = ItemRef(library_id="abs", item_id=f"b1:{i}")
        m.items[ref.key()] = ManifestEntry(
            ref=ref,
            device_ref="",
            path=f"Audiobooks/Рей Бредбері - 451/{name}",
            size=100,
            kind=MediaKind.AUDIOBOOK,
            meta={"title": name[6:-4], "album": "451° за Фаренгейтом", "author": "Рей Бредбері"},
        )
    ref = ItemRef(library_id="abs", item_id="b2:0")
    m.items[ref.key()] = ManifestEntry(
        ref=ref,
        device_ref="",
        path="Audiobooks/Mort/001 - Mort.mp3",
        size=10,
        kind=MediaKind.AUDIOBOOK,
        meta={"title": "Mort", "album": "Mort", "author": "Terry Pratchett"},
    )
    book = "/mnt/sd_0/Audiobooks/Рей Бредбері - 451"
    hiby_sql(
        dev.mount,
        "audiobooks.db",
        "INSERT INTO AUDIOBOOK_TABLE(path, name, last_played, resume_file, resume_pos)"
        " VALUES (?, 'old', 5, ?, 12.5)",
        book,
        f"{book}/002 - Частина друга.mp3",
    )
    hiby_sql(
        dev.mount,
        "audiobooks.db",
        "INSERT INTO AUDIOBOOK_TABLE(path, name) VALUES ('/mnt/sd_0/Audiobooks/Gone', 'gone')",
    )
    s = await dev.begin()
    await dev.commit(s, m)
    con = sqlite3.connect(dev.mount / ".local" / "audiobooks.db")
    rows = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT path, name, size, author, folder, last_played, resume_pos FROM AUDIOBOOK_TABLE"
        )
    }
    parts = list(con.execute("SELECT path, book, idx, title FROM AUDIOBOOK_PARTS ORDER BY idx"))
    con.close()
    # the player's resume position is written back into the fresh database
    assert rows[book][:4] == ("451° за Фаренгейтом", 200, "Рей Бредбері", 1)
    assert rows[book][5] == 12.5
    assert rows["/mnt/sd_0/Audiobooks/Mort/001 - Mort.mp3"][:4] == (
        "Mort",
        10,
        "Terry Pratchett",
        0,
    )
    assert "/mnt/sd_0/Audiobooks/Gone" not in rows
    assert [(p[1], p[2], p[3]) for p in parts] == [
        (book, 0, "Частина перша"),
        (book, 1, "Частина друга"),
    ]
