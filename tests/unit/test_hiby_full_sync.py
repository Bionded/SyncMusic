"""HiBy R1 full sync: the player's databases are deleted and written anew from the libraries."""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3

import pytest

from syncmusic.core.models import (
    DeviceConfig,
    DevicePlaylist,
    Interruption,
    ItemRef,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    MediaKind,
    Track,
)
from syncmusic.devices.HibyR1.sonixos import Hiby, clear_index
from tests.contracts.harness import hiby_tree, work_item


def _dev(tmp_path) -> Hiby:
    mount = hiby_tree(tmp_path / "hiby")
    (mount / ".syncmusic").mkdir()
    clear_index(mount)  # the full SonixOS schema
    return Hiby(DeviceConfig(id="h1", type="hiby", name="H"), mount, None)


def hiby_sql(mount, db: str, *statements: str) -> None:
    con = sqlite3.connect(mount / ".local" / db)
    try:
        for sql in statements:
            con.execute(sql)
        con.commit()
    finally:
        con.close()


def _q(dev: Hiby, db: str, sql: str) -> list[tuple]:
    con = sqlite3.connect(dev.mount / ".local" / db)
    try:
        return list(con.execute(sql))
    finally:
        con.close()


async def _commit(
    dev, manifest, *, reverse_ok=True, tracks=None, starred=None, playlists=None, moved=()
):
    s = await dev.begin()
    s.manifest = manifest
    s.data.update(
        reverse_ok=reverse_ok,
        library_tracks=tracks or {},
        library_starred=starred or {},
        device_positions=set(moved),
    )
    if playlists is not None:
        await dev.set_playlists(s, playlists)
    await dev.commit(s, manifest)
    return s


async def _music(dev, tmp_path, titles):
    s = await dev.begin()
    m = Manifest(header=ManifestHeader(device_id="h1"))
    tracks: dict[str, Track] = {}
    for t in titles:
        item = work_item(tmp_path, t, artist="Скрябін")
        ref = await dev.put_track(s, item)
        m.items[item.track.ref.key()] = ManifestEntry(
            ref=item.track.ref,
            device_ref=ref.device_ref,
            path=ref.path,
            size=ref.size,
            meta={"title": t, "artist": "Скрябін"},
        )
        tracks[t] = item.track
    await dev.commit(s, m)
    return m, tracks


async def test_full_sync_writes_the_databases_anew(tmp_path):
    dev = _dev(tmp_path)
    m, tracks = await _music(dev, tmp_path, ["Зірка", "Зозуля"])
    a, b = (next(e for e in m.items.values() if e.meta["title"] == t) for t in ("Зірка", "Зозуля"))
    # what the player made since: plays, history, playback state, a heart, its own playlist
    hiby_sql(
        dev.mount,
        "library.db",
        f"INSERT INTO PLAY_COUNTS VALUES ('/mnt/sd_0/{a.path}', 3, 100)",
        f"INSERT INTO PLAY_COUNTS VALUES ('/mnt/sd_0/{b.path}', 4, 200)",
        f"INSERT INTO PLAY_HISTORY(path, played_at) VALUES ('/mnt/sd_0/{a.path}', 100)",
        f"INSERT INTO PLAYBACK_STATE VALUES (0, '/mnt/sd_0/{a.path}', 12.0, 100)",
        f"INSERT INTO FAVOURITES VALUES ('/mnt/sd_0/{b.path}', 'Зозуля', 'Скрябін', 5)",
        'CREATE TABLE "M3U_Own"(idx INTEGER PRIMARY KEY, path TEXT, title TEXT, artist TEXT, seconds INT,'
        " present INT, mount INT)",
        f"INSERT INTO \"M3U_Own\" VALUES (1, '/mnt/sd_0/{b.path}', 'Зозуля', 'Скрябін', 1, 1, NULL)",
        "INSERT INTO \"M3U_Own\" VALUES (2, '/mnt/sd_0/Music/gone.mp3', 'Gone', '', 1, 1, NULL)",
    )
    played = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    lib = {
        a.ref.key(): tracks["Зірка"].model_copy(update={"play_count": 5, "last_played": played}),
        b.ref.key(): tracks["Зозуля"].model_copy(update={"play_count": 1}),
    }
    s = await _commit(
        dev,
        m,
        tracks=lib,
        starred={a.ref.key(): True, b.ref.key(): False},
        playlists=[DevicePlaylist(name="Mix", refs=[b.ref, a.ref])],
    )
    assert not s.data.get("commit_warnings")
    rows = dict((p, (n, t)) for p, n, t in _q(dev, "library.db", "SELECT * FROM PLAY_COUNTS"))
    # the library's count; the player's own where it is higher (the plan may predate the reverse push)
    assert rows == {
        f"/mnt/sd_0/{a.path}": (5, int(played.timestamp())),
        f"/mnt/sd_0/{b.path}": (4, 200),
    }
    assert (a.last_playcount, b.last_playcount) == (5, 4)
    assert _q(dev, "library.db", "SELECT * FROM PLAY_HISTORY") == []
    assert _q(dev, "library.db", "SELECT * FROM PLAYBACK_STATE") == []
    # the library's new star and the player's heart (unchanged in the library) - nothing else
    assert {p for (p,) in _q(dev, "library.db", "SELECT path FROM FAVOURITES")} == {
        f"/mnt/sd_0/{a.path}",
        f"/mnt/sd_0/{b.path}",
    }
    tables = {
        n for (n,) in _q(dev, "library.db", "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "M3U_Mix" in tables
    # the player's own playlist is written back, entries that are not ours on the card dropped
    assert _q(dev, "library.db", 'SELECT idx, path, present, mount FROM "M3U_Own"') == [
        (1, f"/mnt/sd_0/{b.path}", 1, None)
    ]
    assert s.data["commit_counts"] == {
        "device_tracks": 2,
        "device_hearts": 2,
        "device_playlists": 2,
    }
    assert len(_q(dev, "library.db", "SELECT path FROM MEDIA_TABLE")) == 2
    backups = sorted((dev.mount / ".syncmusic" / "hiby-db-backups").glob("full-*/library.db"))
    assert len(backups) == 2  # _music's commit and this one: every commit rewrites
    assert len(sqlite3.connect(backups[-1]).execute("SELECT * FROM PLAY_HISTORY").fetchall()) == 1

    # the reverse sync of the next job sees nothing new
    fb = await dev.read_feedback(m)
    assert not fb.plays and not fb.stars


async def test_full_sync_rewrites_once_at_the_final_commit(tmp_path):
    """Checkpoints of a full sync leave the databases alone; the final commit deletes and writes them once, and
    nothing after it deletes the hearts or playlists again."""
    dev = _dev(tmp_path)
    m, _ = await _music(dev, tmp_path, ["Зірка", "Зозуля"])
    a, b = sorted(m.items.values(), key=lambda e: e.meta["title"])
    hiby_sql(
        dev.mount,
        "library.db",
        f"INSERT INTO FAVOURITES VALUES ('/mnt/sd_0/{a.path}', 'Зірка', 'Скрябін', 5)",
    )
    s = await dev.begin()
    s.manifest = m
    s.data.update(library_tracks={}, library_starred={}, device_positions=set())
    for done in (1, 2):
        m.header.interrupted = Interruption(plan_id="p", done=done, total=2)
        await dev.commit(s, m)
        assert len(list((dev.mount / ".syncmusic" / "hiby-db-backups").glob("full-*"))) == 1
        assert len(_q(dev, "library.db", "SELECT * FROM FAVOURITES")) == 1
    m.header.interrupted = None
    await dev.set_playlists(s, [DevicePlaylist(name="Mix", refs=[a.ref, b.ref])])
    await dev.commit(s, m)
    assert len(list((dev.mount / ".syncmusic" / "hiby-db-backups").glob("full-*"))) == 2
    assert {p for (p,) in _q(dev, "library.db", "SELECT path FROM FAVOURITES")} == {
        f"/mnt/sd_0/{a.path}"
    }
    assert len(_q(dev, "library.db", 'SELECT * FROM "M3U_Mix"')) == 2
    assert len(_q(dev, "library.db", "SELECT * FROM MEDIA_TABLE")) == 2


async def test_update_sync_rewrites_the_databases_too(tmp_path):
    """every commit writes the databases anew, updates included."""
    dev = _dev(tmp_path)
    m, _ = await _music(dev, tmp_path, ["Зірка"])
    (e,) = m.items.values()
    hiby_sql(
        dev.mount,
        "library.db",
        f"INSERT INTO PLAY_HISTORY(path, played_at) VALUES ('/mnt/sd_0/{e.path}', 1)",
    )
    s = await _commit(dev, m)
    assert not s.data.get("commit_warnings")
    assert _q(dev, "library.db", "SELECT * FROM PLAY_HISTORY") == []
    assert len(_q(dev, "library.db", "SELECT * FROM MEDIA_TABLE")) == 1


@pytest.mark.parametrize("damage", ["hot_journal", "wal", "malformed", "no_local", "no_library_db"])
async def test_any_leftover_ends_with_fresh_valid_databases(tmp_path, damage):
    """a leftover journal, a malformed or missing database, no .local at all - the commit
    backs up what is there, deletes it and writes fresh databases, without a warning."""
    dev = _dev(tmp_path)
    m, _ = await _music(dev, tmp_path, ["Зірка"])
    local = dev.mount / ".local"
    if damage == "hot_journal":
        (local / "library.db-journal").write_bytes(b"x" * 512)
        (local / "podcast.db-journal").write_bytes(b"x" * 512)
    elif damage == "wal":
        (local / "library.db-wal").write_bytes(b"x" * 512)
    elif damage == "malformed":
        (local / "library.db").write_bytes(b"SQLite format 3\0" + b"\xff" * 4000)
        (local / "audiobooks.db").write_bytes(b"garbage")
    elif damage == "no_local":
        shutil.rmtree(local)
    else:
        (local / "library.db").unlink()
    s = await _commit(dev, m)
    assert not s.data.get("commit_warnings")
    # no journal with an unfinished write left (journal_mode=TRUNCATE leaves empty ones, like the player)
    assert [f.name for f in local.iterdir() if "-" in f.name and f.stat().st_size] == []
    for db in ("library.db", "audiobooks.db", "podcast.db"):
        con = sqlite3.connect(local / db)
        assert con.execute("PRAGMA quick_check").fetchone() == ("ok",)
        con.close()
    assert len(_q(dev, "library.db", "SELECT * FROM MEDIA_TABLE")) == 1


async def test_failed_reverse_sync_keeps_the_players_plays_pending(tmp_path):
    """the databases are rewritten even when the reverse sync failed; the player's plays are written
    back and the manifest baseline stays, so the next reverse sync still sends them."""
    dev = _dev(tmp_path)
    m, tracks = await _music(dev, tmp_path, ["Зірка"])
    (e,) = m.items.values()
    e.last_playcount = 1
    hiby_sql(
        dev.mount, "library.db", f"INSERT INTO PLAY_COUNTS VALUES ('/mnt/sd_0/{e.path}', 3, 100)"
    )
    lib = {e.ref.key(): tracks["Зірка"].model_copy(update={"play_count": 1})}
    s = await _commit(dev, m, reverse_ok=False, tracks=lib)
    assert not s.data.get("commit_warnings")
    assert _q(dev, "library.db", "SELECT plays FROM PLAY_COUNTS") == [(3,)]
    assert e.last_playcount == 1
    fb = await dev.read_feedback(m)
    assert sum(1 for _ in fb.plays) == 2


async def test_full_sync_writes_positions_from_the_library(tmp_path):
    dev = _dev(tmp_path)
    m = Manifest(header=ManifestHeader(device_id="h1"))
    lib: dict[str, Track] = {}

    def add(path: str, kind: MediaKind, item_id: str, **track) -> ManifestEntry:
        (dev.mount / path).parent.mkdir(parents=True, exist_ok=True)
        (dev.mount / path).write_bytes(b"x" * 100)
        ref = ItemRef(library_id="abs", item_id=item_id)
        e = ManifestEntry(ref=ref, device_ref="", path=path, size=100, kind=kind, duration_s=600.0)
        m.items[ref.key()] = e
        lib[ref.key()] = Track(ref=ref, title=item_id, kind=kind, **track)
        return e

    book = "Audiobooks/Book"
    add(f"{book}/001.mp3", MediaKind.AUDIOBOOK, "b:0", position_s=600.0)
    p2 = add(f"{book}/002.mp3", MediaKind.AUDIOBOOK, "b:1", position_s=42.0)
    single = add("Audiobooks/Mort/Mort.mp3", MediaKind.AUDIOBOOK, "m", finished=True)
    ep = add("Podcast/Show/2026 - Ep.mp3", MediaKind.PODCAST, "p", position_s=30.0)
    moved = add("Podcast/Show/2026 - Ep2.mp3", MediaKind.PODCAST, "p2", position_s=10.0)
    hiby_sql(
        dev.mount,
        "podcast.db",
        f"INSERT INTO PODCAST_EPISODES(key, path, position) VALUES ('f:{moved.path}', '{moved.path}', 99.0)",
    )
    s = await _commit(dev, m, tracks=lib, moved=[moved.ref.key()])
    assert not s.data.get("commit_warnings")
    books = dict(
        (p, (f, pos))
        for p, f, pos in _q(
            dev, "audiobooks.db", "SELECT path, resume_file, resume_pos FROM AUDIOBOOK_TABLE"
        )
    )
    assert books[f"/mnt/sd_0/{book}"] == (f"/mnt/sd_0/{p2.path}", 42.0)
    assert _q(dev, "audiobooks.db", "SELECT path FROM AUDIOBOOK_FINISHED") == [
        (f"/mnt/sd_0/{single.path}",)
    ]
    eps = dict(
        (p, pos) for p, pos in _q(dev, "podcast.db", "SELECT path, position FROM PODCAST_EPISODES")
    )
    # the player's moved position (pushed in step 0) wins over the plan's library value
    assert eps == {ep.path: 30.0, moved.path: 99.0}
    assert (ep.last_position_s, moved.last_position_s) == (30.0, 99.0)
    fb = await dev.read_feedback(m)
    assert set(fb.positions) == {
        single.ref.key()
    }  # a finished book is always reported (idempotent)


async def test_reverse_sync_commit_leaves_the_databases_alone(tmp_path):
    """the reverse sync's baseline commit only saves the manifest; the player's databases are deleted and
    written anew once, by the forward sync's final commit, with the plays read before."""
    from syncmusic.core.reverse import ReverseSync

    dev = _dev(tmp_path)
    m, _ = await _music(dev, tmp_path, ["Зірка"])
    (e,) = m.items.values()
    hiby_sql(
        dev.mount, "library.db", f"INSERT INTO PLAY_COUNTS VALUES ('/mnt/sd_0/{e.path}', 5, 100)"
    )
    fb = await dev.read_feedback(m)
    assert fb.play_counts
    backups = dev.mount / ".syncmusic" / "hiby-db-backups"
    before = len(list(backups.glob("full-*")))
    await ReverseSync._commit(None, dev, m, fb)  # type: ignore[arg-type]  # uses no store
    assert len(list(backups.glob("full-*"))) == before
    assert _q(dev, "library.db", "SELECT plays FROM PLAY_COUNTS") == [(5,)]
    assert m.items[e.ref.key()].last_playcount == 5
    await _commit(dev, m, reverse_ok=False)
    assert len(list(backups.glob("full-*"))) == before + 1
    assert _q(dev, "library.db", "SELECT plays FROM PLAY_COUNTS") == [(5,)]
