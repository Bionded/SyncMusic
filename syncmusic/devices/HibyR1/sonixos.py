# SPDX-License-Identifier: GPL-3.0-or-later
"""HiBy R1 running SonixOS (recon in the developer notes).

The player exposes its storage over USB mass storage as one whole-disk exFAT volume (USB 32bb:0101, vendor "HiBy");
music lives under `Music/`. SonixOS keeps its own SQLite databases in `/.local/` (paths stored as
`/mnt/sd_0/<path on the volume>`):

- `library.db` `PLAY_COUNTS(path, plays, last_played)` - absolute counts (the player's own play rule; skips are not
  counted), `FAVOURITES(path, …, added_at)`, `PLAY_HISTORY` (every start, skips included: not used for counts);
- `audiobooks.db` `AUDIOBOOK_TABLE(path, resume_file, resume_pos, last_played)` + `AUDIOBOOK_FINISHED(path)`;
- `podcast.db` `PODCAST_EPISODES(path, position, duration, played, touched)` - paths relative (`Podcast/…`), `touched`
  a write counter, not a time.

We read them from a temp copy (the player may hold a hot journal); we write the playlists, one
`M3U_<name>` table each in `library.db` (SonixOS has no playlist files), see `write_playlist_tables`. Plays are deltas of
`PLAY_COUNTS` against the manifest like Rockbox's tagcache; SonixOS has no ratings, only favourites (hearts) - the
library's favourite/starred: a heart added since the last reverse sync stars the song in the
library, a removed one unstars it; the library's favourites go back as hearts on our files at commit (`write_favourites`, a heart the reverse sync has not pushed yet is never removed). The
library rating travels in the file tags (retag `write_rating`). Resume positions come from the audiobook and
podcast tables. Clock: the player keeps wall-clock time (unix seconds, UTC).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from syncmusic.base.capabilities import CoverSpec, DeviceCapabilities
from syncmusic.base.device import Device, DeviceSession, ProbeResult
from syncmusic.core.manifest import STATE_DIR
from syncmusic.core.models import (
    DeviceContents,
    DeviceFeedback,
    DeviceIdentity,
    DevicePlaylist,
    DeviceStats,
    DeviceTrackRef,
    Manifest,
    ManifestEntry,
    MediaKind,
    PlayEvent,
    PositionInfo,
    PreparedItem,
    Track,
)
from syncmusic.core.mount_path import mount_source
from syncmusic.devices._filedevice import FileDeviceMixin
from syncmusic.devices.HibyR1 import hiby_index
from syncmusic.devices.paths import PathLayout, PathRules, clean_folder, path_key
from syncmusic.infra.blockdev.format import make_filesystem
from syncmusic.infra.blockdev.identify import BlockDeviceInfo, find_disk_of, scan_block_devices
from syncmusic.infra.blockdev.mount import mount as mount_fs
from syncmusic.infra.blockdev.mount import probe_read

log = logging.getLogger(__name__)

HIBY_USB_VENDOR = "32bb"
COMPAS_USB_PRODUCT = "0004"  # the Compás firmware's storage gadget (SonixOS presents 0101)
DB_DIR = ".local"
VOLUME_PREFIX = "/mnt/sd_0/"  # how SonixOS names the volume in its databases
FAVOURITES_STATE = (
    "hiby-favourites.json"  # in our state dir: the favourites seen at the last reverse sync
)
FORMATS = frozenset(
    {"mp3", "flac", "aac", "alac", "ogg", "opus", "wav", "aiff", "ape", "wv", "dsd"}
)


def device_rel(path: str) -> str:
    """SonixOS path (`/mnt/sd_0/Music/x.flac`) → path relative to the volume."""
    if path.startswith(VOLUME_PREFIX):
        return path[len(VOLUME_PREFIX) :]
    return path.lstrip("/")


@dataclass
class HibyData:
    """What the player's databases hold (paths relative to the volume)."""

    play_counts: dict[str, tuple[int, int | None]] = field(
        default_factory=dict
    )  # path -> (plays, last_played)
    favourites: set[str] = field(default_factory=set)
    # path of the file -> (position s, finished, updated unix s)
    positions: dict[str, tuple[float, bool, int | None]] = field(default_factory=dict)


def _query(db: Path, sql: str) -> list[tuple[Any, ...]]:
    """Rows of `sql` from a copy of `db` ([] when the file or table is missing or unreadable)."""
    if not db.is_file():
        return []
    with tempfile.TemporaryDirectory(prefix="syncmusic-hiby-") as tmp:
        copy = Path(tmp) / db.name
        shutil.copyfile(db, copy)
        try:
            con = sqlite3.connect(f"file:{copy}?immutable=1", uri=True)
            try:
                return list(con.execute(sql))
            finally:
                con.close()
        except sqlite3.DatabaseError:
            return []


def read_databases(mount: Path, db_dir: Path | None = None) -> HibyData:
    d = db_dir or mount / DB_DIR
    out = HibyData()
    for path, plays, last in _query(
        d / "library.db", "SELECT path, plays, last_played FROM PLAY_COUNTS"
    ):
        if path and plays:
            out.play_counts[device_rel(path)] = (int(plays), int(last) if last else None)
    out.favourites = {
        device_rel(p) for (p,) in _query(d / "library.db", "SELECT path FROM FAVOURITES") if p
    }
    finished_books = {
        device_rel(p)
        for (p,) in _query(d / "audiobooks.db", "SELECT path FROM AUDIOBOOK_FINISHED")
        if p
    }
    for book, file, pos, last in _query(
        d / "audiobooks.db",
        "SELECT path, resume_file, resume_pos, last_played FROM AUDIOBOOK_TABLE",
    ):
        target = device_rel(file or book or "")
        if target and (pos or device_rel(book or "") in finished_books):
            out.positions[target] = (
                float(pos or 0),
                device_rel(book or "") in finished_books,
                int(last) if last else None,
            )
    # `touched` is a write counter (MAX+1 per write, orders "most recently left"), not a time: the
    # episode's update time is unknown, so the device's position wins a conflict ('s rule for unknown times)
    for path, pos, played in _query(
        d / "podcast.db", "SELECT path, position, played FROM PODCAST_EPISODES"
    ):
        if path and (pos or played):
            out.positions[device_rel(path)] = (float(pos or 0), bool(played), None)
    return out


def _utc(ts: int | None) -> dt.datetime | None:
    return dt.datetime.fromtimestamp(ts, dt.UTC) if ts else None


def hiby_feedback(
    data: HibyData,
    manifest: Manifest,
    rules: PathRules,
    seen_favourites: set[str],
    *,
    now: dt.datetime,
    device_id: str,
) -> DeviceFeedback:
    """Deltas against the manifest (plays, positions) and favourites added/removed since `seen_favourites`."""
    by_path: dict[str, ManifestEntry] = {
        path_key(e.path, rules): e for e in manifest.items.values()
    }
    fb = DeviceFeedback()
    for rel, (plays, last_played) in data.play_counts.items():
        entry = by_path.get(path_key(rel, rules))
        if entry is None:
            fb.unmapped += 1
            continue
        fb.play_counts[entry.ref.key()] = plays
        last = (
            entry.last_playcount if plays >= entry.last_playcount else 0
        )  # counter reset: all of it is new
        latest = _utc(last_played)
        for i in range(plays - last):
            # the newest play has the player's timestamp; earlier ones walk back by the track length
            step = dt.timedelta(seconds=(entry.duration_s or 180) * i)
            fb.plays.append(
                PlayEvent(
                    ref=entry.ref,
                    played_at=(latest or now) - step,
                    played_at_estimated=latest is None or i > 0,
                    source_device=device_id,
                )
            )
    now_keys = {path_key(p, rules) for p in data.favourites}
    seen = {path_key(p, rules) for p in seen_favourites}
    for key in now_keys ^ seen:
        entry = by_path.get(key)
        if entry is None:
            fb.unmapped += (
                key in now_keys
            )  # a removed heart of a file no longer here is not an error
            continue
        fb.stars[entry.ref.key()] = key in now_keys
    for rel, (pos, finished, updated) in data.positions.items():
        entry = by_path.get(path_key(rel, rules))
        if entry is None:
            fb.unmapped += 1
            continue
        if finished or entry.last_position_s is None or abs(pos - entry.last_position_s) >= 1:
            fb.positions[entry.ref.key()] = PositionInfo(
                position_s=pos, finished=finished, updated_at=_utc(updated)
            )
    return fb


PLAYLIST_PREFIX = "M3U_"  # one table per playlist in library.db (SonixOS library.c)
PLAYLIST_NAME_MAX = 200  # bytes; SonixOS refuses longer names and these characters (playlists.c)
PLAYLIST_BAD_CHARS = '/\\:*?"<>|'
DB_BACKUPS = "hiby-db-backups"  # under the cache dir (or our state dir without one)
TAG_CACHE = "hiby-tags.json"  # next to DB_BACKUPS: the tags of the card's files, so the index needs no reads (610)


def load_known_tags(f: Path) -> hiby_index.KnownTags:
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        return {p: (int(v[0]), int(v[1]), hiby_index.Tags(**v[2])) for p, v in raw.items()}
    except (OSError, ValueError, TypeError, IndexError, KeyError, AttributeError):
        return {}


def save_known_tags(f: Path, known: hiby_index.KnownTags) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    raw = {p: [size, mtime, dataclasses.asdict(t)] for p, (size, mtime, t) in known.items()}
    tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)


def playlist_table_name(name: str) -> str:
    """The playlist name SonixOS accepts for `name`: refused characters become '_', no leading dot, at most 200
    bytes (cut at a character boundary)."""
    out = "".join("_" if c in PLAYLIST_BAD_CHARS or ord(c) < 0x20 else c for c in name)
    out = out.lstrip(".") or "Playlist"
    while len(out.encode("utf-8")) > PLAYLIST_NAME_MAX:
        out = out[:-1]
    return out


def _quoted(table_name: str) -> str:
    return '"' + (PLAYLIST_PREFIX + table_name).replace('"', '""') + '"'


def read_playlist_tables(mount: Path, db_dir: Path | None = None) -> dict[str, list[str]]:
    """Every `M3U_<name>` table of library.db as name -> entries in order (paths relative to the volume)."""
    db = (db_dir or mount / DB_DIR) / "library.db"
    if not db.is_file():
        return {}
    with tempfile.TemporaryDirectory(prefix="syncmusic-hiby-") as tmp:
        copy = Path(tmp) / db.name
        shutil.copyfile(db, copy)
        try:
            con = sqlite3.connect(f"file:{copy}?immutable=1", uri=True)
            try:
                names = [
                    n
                    for (n,) in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                        " AND name LIKE 'M3U\\_%' ESCAPE '\\'"
                    )
                ]
                return {
                    n[len(PLAYLIST_PREFIX) :]: [
                        device_rel(p)
                        for (p,) in con.execute(
                            f"SELECT path FROM {_quoted(n[len(PLAYLIST_PREFIX) :])} ORDER BY idx"
                        )
                        if p
                    ]
                    for n in names
                }
            finally:
                con.close()
        except sqlite3.DatabaseError:
            return {}


class HotDatabase(RuntimeError):
    """library.db has an unfinished transaction (a non-empty journal): the player was not shut down cleanly."""


def write_playlist_tables(
    mount: Path,
    playlists: dict[str, list[tuple[str, str, str, int]]],
    drop: set[str],
    backup_dir: Path,
    db_dir: Path | None = None,
) -> Path:
    """Replace the `M3U_<name>` tables of `playlists` (table name -> rows (path relative to the volume, title,
    artist, seconds)) and drop the tables in `drop`, in one transaction, after a copy of library.db went to
    `backup_dir`. Returns the backup's path. Raises HotDatabase when the journal holds an unfinished transaction."""
    db = (db_dir or mount / DB_DIR) / "library.db"
    journal = db.with_name(db.name + "-journal")
    if journal.is_file() and journal.stat().st_size > 0:
        raise HotDatabase(
            f"{db} has an unfinished transaction ({journal.name}); playlists not written"
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"library-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S%fZ}.db"
    shutil.copy2(db, backup)
    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        for name in sorted(drop - set(playlists)):
            con.execute(f"DROP TABLE IF EXISTS {_quoted(name)}")
        for name, rows in playlists.items():
            q = _quoted(name)
            con.execute(f"DROP TABLE IF EXISTS {q}")
            con.execute(
                f"CREATE TABLE {q}(idx INTEGER PRIMARY KEY, path TEXT, title TEXT, artist TEXT,"
                " seconds INT, present INT, mount INT)"
            )
            con.executemany(
                f"INSERT INTO {q}(idx, path, title, artist, seconds, present) VALUES (?, ?, ?, ?, ?, 1)",
                [(i, VOLUME_PREFIX + r[0], r[1], r[2], r[3]) for i, r in enumerate(rows, 1)],
            )
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return backup


def write_library_index(
    mount: Path,
    backup_dir: Path,
    old_db: Path | None = None,
    known: hiby_index.KnownTags | None = None,
    db_dir: Path | None = None,
) -> hiby_index.IndexStats:
    """Rebuild the music index of library.db from the files on the card (see hiby_index), in one
    transaction after a copy went to `backup_dir`; `old_db`: the deleted database whose tag values unchanged
    files reuse (full sync). Raises HotDatabase when the journal holds an unfinished transaction."""
    db = (db_dir or mount / DB_DIR) / "library.db"
    journal = db.with_name(db.name + "-journal")
    if journal.is_file() and journal.stat().st_size > 0:
        raise HotDatabase(f"{db} has an unfinished transaction ({journal.name}); index not rebuilt")
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db, backup_dir / f"library-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S%fZ}.db")
    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=TRUNCATE")  # like the player: no WAL on FAT/exFAT
        con.execute("BEGIN IMMEDIATE")
        stats = hiby_index.rebuild(con, mount, old_db, known)
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return stats


def write_favourites(
    mount: Path,
    add: dict[str, tuple[str, str]],
    remove: set[str],
    backup_dir: Path,
    db_dir: Path | None = None,
    added_at: dict[str, int] | None = None,
) -> tuple[int, int]:
    """Hearts for the library's favourites: `add` = path relative to the volume -> (name, artist)
    gets a FAVOURITES row unless it has one (the player's own added_at stays), `remove` paths lose theirs. One
    transaction after a backup; SonixOS reads FAVOURITES on every list, so the hearts show at its next start.
    `added_at`: path -> the time a heart keeps (a full sync writes the player's hearts back as they were).
    Returns (added, removed); (0, 0) without a database or table. Raises HotDatabase like the playlists."""
    db = (db_dir or mount / DB_DIR) / "library.db"
    if not db.is_file() or not (add or remove):
        return 0, 0
    journal = db.with_name(db.name + "-journal")
    if journal.is_file() and journal.stat().st_size > 0:
        raise HotDatabase(
            f"{db} has an unfinished transaction ({journal.name}); hearts not written"
        )
    con = sqlite3.connect(db, isolation_level=None)
    try:
        if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='FAVOURITES'"
        ).fetchone():
            return 0, 0
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db, backup_dir / f"library-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S%fZ}.db")
        now = int(dt.datetime.now(dt.UTC).timestamp())
        con.execute("BEGIN IMMEDIATE")
        added = removed = 0
        for rel, (name, artist) in sorted(add.items()):
            added += con.execute(
                "INSERT OR IGNORE INTO FAVOURITES(path, name, artist, added_at) VALUES (?, ?, ?, ?)",
                (VOLUME_PREFIX + rel, name, artist, (added_at or {}).get(rel, now)),
            ).rowcount
        for rel in sorted(remove):
            removed += con.execute(
                "DELETE FROM FAVOURITES WHERE path=?", (VOLUME_PREFIX + rel,)
            ).rowcount
        con.execute("COMMIT")
        return added, removed
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


@dataclass
class ListeningState:
    """What a full sync writes into the freshly created databases, paths relative to the volume:
    `plays` path -> (plays, last played unix s); `books` book path (AUDIOBOOK_TABLE) -> (resume file, position s,
    finished, last played unix s); `episodes` podcast path -> (title, show, position s, duration s, played)."""

    plays: dict[str, tuple[int, int | None]] = field(default_factory=dict)
    books: dict[str, tuple[str, float, bool, int | None]] = field(default_factory=dict)
    episodes: dict[str, tuple[str, str, float, float, bool]] = field(default_factory=dict)


def write_listening_state(
    mount: Path, state: ListeningState, db_dir: Path | None = None, skip: Collection[str] = ()
) -> None:
    """PLAY_COUNTS, audiobook resume points / finished flags and PODCAST_EPISODES rows (key `f:` + path, like the
    player's), each database in one transaction; the databases in `skip` (kept as the player had them) are not
    written."""
    d = db_dir or mount / DB_DIR

    def run(db: str, work: Any) -> None:
        con = sqlite3.connect(d / db, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            work(con)
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    run(
        "library.db",
        lambda con: con.executemany(
            "INSERT OR REPLACE INTO PLAY_COUNTS(path, plays, last_played) VALUES (?, ?, ?)",
            [(VOLUME_PREFIX + p, n, last) for p, (n, last) in sorted(state.plays.items()) if n],
        ),
    )

    def books(con: sqlite3.Connection) -> None:
        for book, (file, pos, finished, last) in sorted(state.books.items()):
            con.execute(
                "UPDATE AUDIOBOOK_TABLE SET resume_file=?, resume_pos=?, last_played=? WHERE path=?",
                (VOLUME_PREFIX + file, pos, last or 0, VOLUME_PREFIX + book),
            )
            if finished:
                con.execute(
                    "INSERT OR REPLACE INTO AUDIOBOOK_FINISHED(path, finished_at) VALUES (?, ?)",
                    (VOLUME_PREFIX + book, last or 0),
                )

    if "audiobooks.db" not in skip:
        run("audiobooks.db", books)
    if "podcast.db" in skip:
        return
    run(
        "podcast.db",
        lambda con: con.executemany(
            "INSERT OR REPLACE INTO PODCAST_EPISODES(key, path, title, show, position, duration, played,"
            " touched) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (f"f:{p}", p, title, show, pos, dur, int(played), i)
                for i, (p, (title, show, pos, dur, played)) in enumerate(
                    sorted(state.episodes.items()), 1
                )
            ],
        ),
    )


def audiobook_rows(
    mount: Path, books: dict[str, list[ManifestEntry]]
) -> tuple[list[tuple[Any, ...]], list[tuple[str, str, int, str]]]:
    """AUDIOBOOK_TABLE rows (path, name, size, mtime, author, added, folder) and AUDIOBOOK_PARTS rows for our books
    (folder on the card -> its files), the way SonixOS's audiobook scan files them: a folder of 2+
    files is one folder book with its parts in name order, a single file is a book of its own."""
    table: list[tuple[Any, ...]] = []
    parts: list[tuple[str, str, int, str]] = []
    for folder, entries in sorted(books.items()):
        entries = sorted(entries, key=lambda e: e.path)
        stats = [(mount / e.path).stat() for e in entries]
        first = entries[0]
        author = (
            first.meta.get("author")
            or first.meta.get("album_artist")
            or first.meta.get("artist")
            or ""
        )
        if len(entries) == 1:
            st = stats[0]
            name = (
                first.meta.get("album") or first.meta.get("title") or PurePosixPath(first.path).stem
            )
            table.append(
                (
                    VOLUME_PREFIX + first.path,
                    name,
                    st.st_size,
                    int(st.st_mtime),
                    author,
                    int(st.st_mtime),
                    0,
                )
            )
            continue
        book = VOLUME_PREFIX + folder
        name = first.meta.get("album") or PurePosixPath(folder).name
        table.append(
            (
                book,
                name,
                sum(st.st_size for st in stats),
                max(int(st.st_mtime) for st in stats),
                author,
                int(stats[0].st_mtime),
                1,
            )
        )
        for i, e in enumerate(entries):
            parts.append(
                (VOLUME_PREFIX + e.path, book, i, e.meta.get("title") or PurePosixPath(e.path).stem)
            )
    return table, parts


def write_audiobook_index(
    mount: Path,
    table: list[tuple[Any, ...]],
    parts: list[tuple[str, str, int, str]],
    backup_dir: Path,
    db_dir: Path | None = None,
) -> int:
    """Add or refresh our books in audiobooks.db (SonixOS never updates it by itself), keeping the
    player's own data (last_played, resume_*, finished, bookmarks); rows of books no longer on the card go. Returns
    the number of books written; 0 without a database (the player makes one at its first scan)."""
    db = (db_dir or mount / DB_DIR) / "audiobooks.db"
    if not db.is_file():
        return 0
    journal = db.with_name(db.name + "-journal")
    if journal.is_file() and journal.stat().st_size > 0:
        raise HotDatabase(
            f"{db} has an unfinished transaction ({journal.name}); audiobooks not indexed"
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db, backup_dir / f"audiobooks-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S%fZ}.db")
    con = sqlite3.connect(db, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        for (path,) in list(con.execute("SELECT path FROM AUDIOBOOK_TABLE")):
            rel = device_rel(path).split("/\x1f", 1)[0]
            if not (mount / rel).exists():
                con.execute("DELETE FROM AUDIOBOOK_TABLE WHERE path=?", (path,))
                con.execute("DELETE FROM AUDIOBOOK_PARTS WHERE book=?", (path,))
        for path, name, size, mtime, author, added, folder in table:
            con.execute(
                "INSERT INTO AUDIOBOOK_TABLE(path, name, size, mtime, author, added, folder)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET name=excluded.name,"
                " size=excluded.size, mtime=excluded.mtime, author=excluded.author, folder=excluded.folder",
                (path, name, size, mtime, author, added, folder),
            )
            con.execute("DELETE FROM AUDIOBOOK_PARTS WHERE book=?", (path,))
        con.executemany(
            "INSERT OR REPLACE INTO AUDIOBOOK_PARTS(path, book, idx, title) VALUES (?, ?, ?, ?)",
            parts,
        )
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return len(table)


# The SonixOS schema of the card's databases, reimplemented from its CREATE TABLE IF NOT EXISTS lists
# (library.c SCHEMA, audiobookdb.c, podcaststate.c, podcastsubs.c): SQLite creates a missing database file but
# SonixOS never creates `.local/` itself, so after a Format there is no index, no Detect changes and nowhere to
# write playlists, hearts or positions. The player adds its own later columns/indexes to these on open.
_NC = "TEXT COLLATE NOCASE"
SCHEMAS: dict[str, tuple[str, ...]] = {
    "library.db": (
        f"CREATE TABLE IF NOT EXISTS MEDIA_TABLE(id INT, path {_NC}, name {_NC}, album {_NC}, artist {_NC},"
        f" genre {_NC}, year INT, dis_id INT, ck_id INT, has_child_file INT, begin_time INT, end_time INT,"
        f" cue_id INT, character {_NC}, size INT, sample_rate INT, bit_rate INT, bit INT, channel INT,"
        f" format INT, quality {_NC}, album_pic_path {_NC}, lrc_path {_NC}, track_gain REAL, track_peak REAL,"
        f" album_artist {_NC}, ctime INT, mtime INT, sortkey TEXT, disc INT, album_key TEXT,"
        " PRIMARY KEY(id, path, cue_id))",
        f"CREATE TABLE IF NOT EXISTS ALBUM_TABLE(id INT, album {_NC}, character {_NC}, cn INT, sortkey TEXT,"
        " PRIMARY KEY(album))",
        f"CREATE TABLE IF NOT EXISTS ALBUM_GROUP_TABLE(album {_NC}, album_key TEXT, sortkey TEXT,"
        " PRIMARY KEY(album, album_key))",
        f"CREATE TABLE IF NOT EXISTS ARTIST_TABLE(id INT, artist {_NC}, character {_NC}, cn INT, sortkey TEXT,"
        " PRIMARY KEY(artist))",
        f"CREATE TABLE IF NOT EXISTS ALBUM_ARTIST_TABLE(id INT, album_artist {_NC}, character {_NC}, cn INT,"
        " ctime INT, mtime INT, mqa INT, pinyin_charater TEXT, sortkey TEXT, PRIMARY KEY(album_artist))",
        f"CREATE TABLE IF NOT EXISTS GENRE_TABLE(id INT, genre {_NC}, character {_NC}, cn INT, sortkey TEXT,"
        " PRIMARY KEY(genre))",
        "CREATE TABLE IF NOT EXISTS FAVOURITES(path TEXT PRIMARY KEY, name TEXT, artist TEXT, added_at INT)",
        "CREATE TABLE IF NOT EXISTS PLAY_COUNTS(path TEXT PRIMARY KEY, plays INT NOT NULL, last_played INT)",
        "CREATE TABLE IF NOT EXISTS PLAY_HISTORY(id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL,"
        " name TEXT, artist TEXT, played_at INT)",
        "CREATE TABLE IF NOT EXISTS PLAYBACK_STATE(id INT PRIMARY KEY, path TEXT, position REAL, updated_at INT)",
        "CREATE TABLE IF NOT EXISTS PLAYBACK_QUEUE(idx INT PRIMARY KEY, path TEXT, slot INT)",
        "CREATE TABLE IF NOT EXISTS PLAYBACK_QUEUE_STATE(id INT PRIMARY KEY, pos INT, custom INT, updated_at INT)",
        "CREATE TABLE IF NOT EXISTS PLAYBACK_QUEUE_QUERY(id INT PRIMARY KEY, kind INT, filter INT, ord INT,"
        " desc INT, value TEXT, pos INT, updated_at INT)",
        "CREATE TABLE IF NOT EXISTS PLAYBACK_QUEUE_ALBUM(id INT PRIMARY KEY, album TEXT)",
        "CREATE TABLE IF NOT EXISTS PLAYLIST_COUNTS(path TEXT PRIMARY KEY, mtime INT, size INT, tracks INT,"
        " mount INT)",
        "CREATE TABLE IF NOT EXISTS MOUNT_SERIAL(id INT PRIMARY KEY, serial INT)",
        "CREATE TABLE IF NOT EXISTS PLAYLIST_MIGRATION(id INT PRIMARY KEY, done INT)",
        f"CREATE TABLE IF NOT EXISTS FOLDER_TABLE(path {_NC} PRIMARY KEY, mtime INT, files INT, names INT)",
        f"CREATE TABLE IF NOT EXISTS CUE_STATE(path {_NC} PRIMARY KEY, mtime INT, size INT, audio TEXT,"
        " version INT)",
        f"CREATE TABLE IF NOT EXISTS ARTIST_LINK(path {_NC}, artist {_NC})",
        f"CREATE TABLE IF NOT EXISTS ALBUM_ARTIST_LINK(path {_NC}, album_artist {_NC})",
        f"CREATE TABLE IF NOT EXISTS GENRE_LINK(path {_NC}, genre {_NC})",
        "CREATE TABLE IF NOT EXISTS ORGANIZE_STATE(id INT PRIMARY KEY, rules TEXT)",
        "CREATE TABLE IF NOT EXISTS VERSION_TABLE(version_id INT, PRIMARY KEY(version_id))",
        "CREATE TABLE IF NOT EXISTS COUNT_TABLE(cn INT)",
        *hiby_index.INDEXES,  # a database without them was not opened by the player after a Format
    ),
    "audiobooks.db": (
        "CREATE TABLE IF NOT EXISTS AUDIOBOOK_TABLE(path TEXT PRIMARY KEY, name TEXT COLLATE NOCASE, size INT,"
        " mtime INT, last_played INT DEFAULT 0, resume_file TEXT, resume_pos REAL, author TEXT DEFAULT '',"
        " series TEXT DEFAULT '', series_part REAL, added INT DEFAULT 0, folder INT DEFAULT 0, summary TEXT)",
        "CREATE TABLE IF NOT EXISTS AUDIOBOOK_PARTS(path TEXT PRIMARY KEY, book TEXT, idx INT, title TEXT)",
        "CREATE INDEX IF NOT EXISTS AUDIOBOOK_PARTS_BOOK ON AUDIOBOOK_PARTS(book, idx)",
        "CREATE TABLE IF NOT EXISTS AUDIOBOOK_FINISHED(path TEXT PRIMARY KEY, finished_at INT DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS AUDIOBOOK_BOOKMARKS(id INTEGER PRIMARY KEY, book TEXT, file TEXT, pos REAL,"
        " label TEXT DEFAULT '', created INT DEFAULT 0)",
        "CREATE INDEX IF NOT EXISTS AUDIOBOOK_BOOKMARKS_BOOK ON AUDIOBOOK_BOOKMARKS(book)",
        "PRAGMA user_version=4",  # audiobookdb.c SCHEMA_VERSION: rows carry author/series/folder
    ),
    "podcast.db": (
        "CREATE TABLE IF NOT EXISTS PODCAST_SUBS(id INTEGER PRIMARY KEY, title TEXT, author TEXT, image TEXT,"
        " followed_at INTEGER DEFAULT 0, url TEXT DEFAULT '', episodes INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS PODCAST_RSS(id INTEGER PRIMARY KEY, url TEXT)",
        "CREATE TABLE IF NOT EXISTS PODCAST_EPISODES(key TEXT PRIMARY KEY, path TEXT, title TEXT, show TEXT,"
        " image TEXT, feed_id INTEGER DEFAULT 0, episode_id INTEGER DEFAULT 0, position REAL DEFAULT 0,"
        " duration REAL DEFAULT 0, played INTEGER DEFAULT 0, touched INTEGER DEFAULT 0)",
        "CREATE INDEX IF NOT EXISTS PODCAST_EPISODES_PATH ON PODCAST_EPISODES(path)",
        "CREATE INDEX IF NOT EXISTS PODCAST_EPISODES_FEED ON PODCAST_EPISODES(feed_id)",
        "CREATE INDEX IF NOT EXISTS PODCAST_EPISODES_TOUCHED ON PODCAST_EPISODES(touched)",
    ),
}
# the index of the card's media that Wipe and Format clear; everything else in `.local` is the
# player's own (EQ/PEQ/MSEB presets, radio.db, streaming logins and caches, fonts, cacert.pem …)
INDEX_FILES = (*SCHEMAS, "thumbnails")
# what a malformed database's rescue keeps (like SonixOS's rescue_user_tables): the user's data, not the index
USER_TABLES = {
    "library.db": ("FAVOURITES", "PLAY_COUNTS", "PLAY_HISTORY", "PLAYBACK_STATE"),
    "audiobooks.db": ("AUDIOBOOK_FINISHED", "AUDIOBOOK_BOOKMARKS"),
    "podcast.db": ("PODCAST_SUBS", "PODCAST_RSS", "PODCAST_EPISODES"),
}


def _create(db: Path) -> None:
    con = sqlite3.connect(db, isolation_level=None)
    try:
        for sql in SCHEMAS[db.name]:
            con.execute(sql)
    finally:
        con.close()


def _healthy(db: Path) -> bool:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return bool(con.execute("PRAGMA quick_check").fetchone() == ("ok",))
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _rescue(db: Path, backup_dir: Path) -> None:
    """Move a malformed database to `backup_dir`, create a fresh one and carry over the readable rows of its
    USER_TABLES."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    old = backup_dir / f"{db.stem}-malformed-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S%fZ}.db"
    shutil.move(db, old)
    _create(db)
    con = sqlite3.connect(db, isolation_level=None)
    try:
        try:
            con.execute("ATTACH DATABASE ? AS old", (str(old),))
        except sqlite3.Error as e:
            log.warning("%s: nothing recovered from %s: %s", db, old, e)
            return
        for table in USER_TABLES[db.name]:
            try:
                con.execute(f"INSERT OR IGNORE INTO main.{table} SELECT * FROM old.{table}")
            except sqlite3.Error as e:
                log.warning("%s: %s not recovered: %s", db, table, e)
    finally:
        con.close()


def ensure_databases(mount: Path, backup_dir: Path, db_dir: Path | None = None) -> list[str]:
    """Create `.local/` and every SonixOS database that is missing (empty, with the SonixOS schema); a malformed
    one is backed up and recreated with its user rows. A database with a hot journal is left to the player.
    Returns what was done, one line each (for the log)."""
    done: list[str] = []
    d = db_dir or mount / DB_DIR
    d.mkdir(exist_ok=True)
    for name in SCHEMAS:
        db = d / name
        journal = db.with_name(name + "-journal")
        if not db.is_file() or db.stat().st_size == 0:
            journal.unlink(
                missing_ok=True
            )  # a journal without its database is nothing to roll back
            _create(db)
            done.append(f"created {DB_DIR}/{name}")
        elif not (journal.is_file() and journal.stat().st_size > 0) and not _healthy(db):
            _rescue(db, backup_dir)
            done.append(f"recreated malformed {DB_DIR}/{name}")
    return done


def stage_databases(mount: Path, stage: Path) -> Path:
    """Copy the card's databases (with their journals) into `stage`, a fresh directory on the host's disk: every
    write of a commit goes there, never into an open SQLite file on the card (a half-written database on the card
    is what an unplug or a crash leaves behind). Returns `stage`."""
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    d = mount / DB_DIR
    for name in SCHEMAS:
        for f in (name, f"{name}-journal"):
            if (d / f).is_file():
                shutil.copyfile(d / f, stage / f)
    return stage


def _dump(db: Path) -> list[str]:
    """The database's content as SQL (from a read-only copy: page layout and free pages do not count)."""
    with tempfile.TemporaryDirectory(prefix="syncmusic-hiby-") as tmp:
        copy = Path(tmp) / db.name
        shutil.copyfile(db, copy)
        con = sqlite3.connect(copy)
        try:
            return list(con.iterdump())
        finally:
            con.close()


def _same_content(a: Path, b: Path) -> bool:
    if a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes():
        return True
    try:
        return _dump(a) == _dump(b)
    except sqlite3.Error:
        return False


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # not every file system syncs a directory handle; syncfs before eject covers it
    finally:
        os.close(fd)


def _integrity_ok(db: Path) -> bool:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return bool(con.execute("PRAGMA integrity_check").fetchone() == ("ok",))
        finally:
            con.close()
    except sqlite3.Error:
        return False


def publish_databases(mount: Path, stage: Path) -> list[str]:
    """Put the staged databases onto the card: one whose content equals the card's (and has no journal there) is
    not written at all; a changed one is checked (integrity_check), copied next to the old one as `<name>.new`,
    fsynced and renamed over it, then its journal goes. A crash at any point leaves either the old or the new
    database complete. Returns the names written; raises sqlite3.DatabaseError when a staged database is corrupt
    (the card keeps the old ones)."""
    d = mount / DB_DIR
    d.mkdir(exist_ok=True)
    todo: list[str] = []
    for name in SCHEMAS:
        staged, card = stage / name, d / name
        if not staged.is_file():
            continue
        journals = [d / f"{name}-{x}" for x in ("journal", "wal", "shm")]
        if card.is_file() and not any(j.exists() for j in journals) and _same_content(staged, card):
            continue
        if not _integrity_ok(staged):
            raise sqlite3.DatabaseError(
                f"the new {name} failed its integrity check; the card keeps the old one"
            )
        todo.append(name)
    for name in todo:
        new = d / f"{name}.new"
        with open(stage / name, "rb") as src, open(new, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
            dst.flush()
            os.fsync(dst.fileno())
        _fsync_dir(d)
        os.replace(new, d / name)
        for x in ("journal", "wal", "shm"):
            (d / f"{name}-{x}").unlink(missing_ok=True)
        _fsync_dir(d)
    if "library.db" in todo:
        (d / "thumbnails").unlink(missing_ok=True)  # the player's thumbnails of the old index
    return todo


def organize_rules(mount: Path, db_dir: Path | None = None) -> str | None:
    """library.db's ORGANIZE_STATE rules (the player's split/join settings, which are not on the card otherwise)."""
    db = (db_dir or mount / DB_DIR) / "library.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT rules FROM ORGANIZE_STATE WHERE id = 0").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def clear_index(
    mount: Path,
    rules: str | None = None,
    db_dir: Path | None = None,
    keep: Collection[str] = (),
) -> None:
    """Delete the player's index of the card (INDEX_FILES with their journals) and recreate empty databases; the
    player's ORGANIZE_STATE rules (`rules`, else the old database's) are carried over so our rebuilt index is filed
    the player's way. The databases named in `keep` (of a kind SyncMusic does not sync) stay as they are."""
    d = db_dir or mount / DB_DIR
    rules = rules or organize_rules(mount, db_dir)
    for name in INDEX_FILES:
        if name in keep:
            continue
        for f in (name, f"{name}-journal", f"{name}-wal", f"{name}-shm"):
            (d / f).unlink(missing_ok=True)
    d.mkdir(exist_ok=True)
    for name in SCHEMAS:
        _create(d / name)  # CREATE TABLE IF NOT EXISTS: a kept database keeps its rows
    if rules:
        con = sqlite3.connect(d / "library.db", isolation_level=None)
        try:
            con.execute("INSERT OR REPLACE INTO ORGANIZE_STATE(id, rules) VALUES (0, ?)", (rules,))
        finally:
            con.close()


@dataclass
class _Fresh:
    """A full sync's deleted databases: what they held (after the library's star changes) and a copy of the old
    library.db whose tag values the index rebuild reuses."""

    data: HibyData
    old_db: Path | None  # None: there was no library.db
    # the old M3U_* tables (name -> paths); the player's own ones are written back
    playlists: dict[str, list[str]]


class HibySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formats: set[str] | None = None  # None = everything SonixOS plays (FORMATS)
    # SonixOS keeps podcasts in `Podcast/<show>` (podcastdl.h PODCASTDL_FOLDER, show folder = the feed title) and
    # audiobooks in `Audiobooks/` (audiobookdb.h): our layout uses the same folders
    layout: PathLayout = PathLayout(podcast="Podcast/{series}/{date} - {title}")
    filesystem: str | None = None  # override the detected filesystem (path rules)
    ascii_only: bool = False
    lyrics: Literal["none", "embedded", "lrc_sidecar"] = "lrc_sidecar"
    playlists: bool = True
    # the player's Split genres: "player" files genres the way the card's library.db says (the player's own
    # setting); "off"/"on" write that choice, which only lasts if the player is set the same way
    split_genres: Literal["player", "off", "on"] = "player"
    genre_separators: list[Literal["semicolon", "slash", "ampersand", "comma", "plus"]] = [
        "semicolon",
        "slash",
        "comma",
    ]
    # unused (playlists are tables in library.db); kept so stored device configs still load
    playlist_dir: str = "Playlists"

    @field_validator("playlist_dir")
    @classmethod
    def _folder(cls, v: str) -> str:
        return clean_folder(v)


class Hiby(FileDeviceMixin, Device):
    type_name = "hiby"
    display_name = "HiBy R1 - SonixOS"
    owns_device_tree = True
    firmware_siblings = ("hiby_compas",)
    # the player's own settings and caches stay; Wipe clears only its index of the card (`wipe`)
    WIPE_KEEP = (*FileDeviceMixin.WIPE_KEEP, DB_DIR, ".temp")
    Settings: ClassVar[type[BaseModel]] = HibySettings
    # exFAT (the player's own), FAT32, NTFS - one whole-disk volume, no partition table
    format_filesystems = ("exfat", "vfat", "ntfs")
    # a half-written database here makes the kernel remount the card read-only on its first read
    probe_files = tuple(f"{DB_DIR}/{name}" for name in SCHEMAS)
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats=set(FORMATS),
        supports_podcasts=True,
        supports_audiobooks=True,
        supports_playlists=True,
        supports_playcounts_read=True,
        supports_positions_read=True,
        has_rtc=True,
        cover=CoverSpec(max_size=600, embed=True, sidecar_name="cover.jpg"),
        lyrics="lrc_sidecar",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._favourites: set[str] | None = (
            None  # from the last read_feedback, saved by reset_feedback
        )
        self._known: hiby_index.KnownTags | None = None  # loaded from TAG_CACHE on first use
        # where a commit writes the databases (a copy on the host, see stage_databases); None = on the card
        self._db_dir: Path | None = None

    @property
    def s(self) -> HibySettings:
        settings: HibySettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        hiby_usb = (
            blk is not None
            and blk.tran == "usb"
            and blk.is_disk
            and (blk.usb_vendor_id or "").casefold() == HIBY_USB_VENDOR
        )
        if not hiby_usb and not (mount is not None and (mount / DB_DIR / "library.db").is_file()):
            return None
        data = None
        if blk is not None:
            data = next((p for p in blk.partitions() if p.fstype), None) or (
                blk if blk.fstype else None
            )
        model = blk.model.replace("U-DISK", "").strip() if blk and blk.model else None
        return ProbeResult(
            type_name=cls.type_name,
            model=model,
            suggested_name=(data.label if data and data.label else None)
            or " ".join(x for x in ("HiBy", model) if x),
            identity=DeviceIdentity(
                usb_serial=blk.serial if blk else None, fs_uuid=data.uuid if data else None
            ),
            # above the flash drive (10), which takes any USB disk; an unmounted R1 that presents the
            # Compás gadget id is most likely running Compás (that class claims it at 85)
            confidence=60
            if mount is None and blk is not None and blk.usb_product_id == COMPAS_USB_PRODUCT
            else 80,
            data_partition=data.path if data else None,
            details={"fstype": data.fstype if data else None},
        )

    def setting_profile(self) -> dict[str, Any]:
        out: dict[str, Any] = {"formats": set(self.s.formats)} if self.s.formats else {}
        if not self.s.playlists:
            out["supports_playlists"] = False
        return out

    def model_capabilities(self) -> DeviceCapabilities:
        return (
            super()
            .model_capabilities()
            .model_copy(update={"path_rules": self.path_rules(), "lyrics": self.s.lyrics})
        )

    def _stats(self) -> dict[str, DeviceStats]:
        """What the player's databases already hold per path: the manifest baseline starts from these, not from
        the library's counts (a library play count of 1 hid the player's first play)."""
        data = read_databases(self.mount)
        rules = self._rules()
        out: dict[str, DeviceStats] = {}
        for rel, (plays, _last) in data.play_counts.items():
            out[path_key(rel, rules)] = DeviceStats(playcount=plays)
        for rel, (pos, _finished, _updated) in data.positions.items():
            key = path_key(rel, rules)
            out[key] = out.get(key, DeviceStats()).model_copy(update={"position_s": pos})
        return out

    async def read_contents(self) -> DeviceContents:
        contents = await super().read_contents()
        stats = await asyncio.to_thread(self._stats)
        rules = self._rules()
        for item in contents.items:
            item.stats = stats.get(path_key(item.path, rules), DeviceStats())
        return contents

    async def begin(self) -> DeviceSession:
        s = await super().begin()
        s.data["stats"] = await asyncio.to_thread(self._stats)
        return s

    async def put_track(self, s: DeviceSession, item: PreparedItem) -> DeviceTrackRef:
        """Writes the file; `stats` = what SonixOS already holds for that path (usually 0 plays): its counters
        cannot be set, so the manifest baseline must start from them."""
        ref = await super().put_track(s, item)
        stats: dict[str, DeviceStats] = s.data["stats"]
        ref.stats = stats.get(path_key(ref.path, self._rules()), DeviceStats())

        def remember() -> None:
            # the written copy has the local file's tags: read them here (NVMe), not from the card at commit
            st = (self.mount / ref.path).stat()
            tags = hiby_index.read_tags(item.path)
            self._known_tags()[f"{hiby_index.VOLUME}/{ref.path}"] = (
                st.st_size,
                int(st.st_mtime),
                tags,
            )

        await asyncio.to_thread(remember)
        return ref

    def _known_tags(self) -> hiby_index.KnownTags:
        if self._known is None:
            self._known = load_known_tags(self._backup_dir().parent / TAG_CACHE)
        return self._known

    def _state_file(self) -> Path:
        return self.mount / STATE_DIR / FAVOURITES_STATE

    def _seen_favourites(self) -> set[str] | None:
        try:
            return set(json.loads(self._state_file().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    async def read_playlists(self, manifest: Manifest) -> dict[str, list[str]] | None:
        """The player's playlists (its `M3U_*` tables) go back to the library; ours are named as in
        the manifest, the player's own by their table."""
        if not self.s.playlists:
            return None
        ours = {
            t[len(PLAYLIST_PREFIX) :]: n
            for n, t in manifest.playlist_paths.items()
            if n in manifest.playlists and t.startswith(PLAYLIST_PREFIX)
        }
        tables = await asyncio.to_thread(read_playlist_tables, self.mount)
        return {ours.get(t, t): entries for t, entries in tables.items()}

    async def remove_playlists(self, s: DeviceSession, names: list[str]) -> None:
        """The player's own `M3U_<name>` tables (`read_playlists` names them by their table) are dropped at commit,
        and a full sync does not write them back."""
        s.data["hiby_remove_playlists"] = set(names)

    async def set_playlists(self, s: DeviceSession, playlists: list[DevicePlaylist]) -> None:
        """SonixOS keeps playlists only in library.db (`M3U_<name>`, the `Playlist/` folder is an import source it reads once): the tables are written at commit, when the manifest knows every file. Our .m3u8 files
        from before go."""
        s.data["hiby_playlists"] = playlists
        old = s.manifest.playlist_paths if s.manifest else {}
        stale = [p for p in old.values() if not p.startswith(PLAYLIST_PREFIX)]

        def work() -> None:
            for rel in stale:
                (self.mount / rel).unlink(missing_ok=True)
                d = (self.mount / rel).parent
                while d != self.mount and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent

        await asyncio.to_thread(work)

    def _backup_dir(self) -> Path:
        if self.ctx is not None:
            return Path(self.ctx.cache_dir) / "device-backups" / self.id / DB_BACKUPS
        return self.mount / STATE_DIR / DB_BACKUPS

    def _warn(self, s: DeviceSession, msg: str) -> None:
        """A database write that failed: in the log and in the sync report (never skip silently)."""
        log.warning("%s: %s", self.id, msg)
        s.data.setdefault("commit_warnings", []).append(msg)

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        if manifest.header.interrupted is not None or s.data.get("manifest_only"):
            # a checkpoint or the reverse sync's baselines: the databases are deleted and
            # written anew once, by the final commit after the last file; these only save the manifest
            await super().commit(s, manifest)
            return
        # every sync and update: the databases are built anew on the host (a journal, a malformed or
        # missing database is backed up and dropped), and only those whose content changed go onto the card
        try:
            self._db_dir = await asyncio.to_thread(
                stage_databases, self.mount, self._backup_dir().parent / "stage"
            )
        except OSError as e:
            self._warn(s, f"the player's databases could not be read from /{DB_DIR}: {e}")
            await super().commit(s, manifest)
            return
        try:
            await self._commit_databases(s, manifest)
            written = await asyncio.to_thread(publish_databases, self.mount, self._db_dir)
            if written:
                log.info("%s: %s written to the card", self.id, ", ".join(written))
            else:
                log.info("%s: the player's databases are unchanged, nothing written", self.id)
        except (OSError, sqlite3.Error) as e:
            self._warn(
                s, f"the player's databases were not written (the card keeps the old ones): {e}"
            )
        finally:
            self._db_dir = None
        await super().commit(s, manifest)

    def genre_bits(self) -> int | None:
        """The genre separators the device setting asks for, None = follow the player's (the card's) rules."""
        if self.s.split_genres == "player":
            return None
        if self.s.split_genres == "off":
            return 0
        bits = 0
        for name in self.s.genre_separators:
            bits |= hiby_index.GENRE_SEPARATORS[name]
        return bits

    def _apply_genre_setting(self, s: DeviceSession) -> None:
        """Put the Split genres device setting into the staged library.db's ORGANIZE_STATE (before the index is
        rebuilt from it); warn when the player's last rules differ, as the player re-files to its own setting."""
        bits = self.genre_bits()
        if bits is None or self._db_dir is None or not (self._db_dir / "library.db").is_file():
            return
        current = organize_rules(self.mount, self._db_dir)
        player = hiby_index.Rules.parse(current).genre
        if player != bits:
            self._warn(
                s,
                "Split genres differs from the player's own setting (Settings → Library on the player): "
                "the player files its genres again its own way after unplugging; set it the same on both",
            )
        con = sqlite3.connect(self._db_dir / "library.db", isolation_level=None)
        try:
            con.execute(
                "INSERT OR REPLACE INTO ORGANIZE_STATE(id, rules) VALUES (0, ?)",
                (hiby_index.with_genre_separators(current, bits),),
            )
        finally:
            con.close()

    async def _commit_databases(self, s: DeviceSession, manifest: Manifest) -> None:
        """Every database write of a commit, into the staged copies (`self._db_dir`)."""
        fresh = await self._delete_databases(s, manifest)
        if fresh is None:
            try:
                for line in await asyncio.to_thread(
                    ensure_databases, self.mount, self._backup_dir(), self._db_dir
                ):
                    log.info("%s: %s", self.id, line)
            except (OSError, sqlite3.Error) as e:
                self._warn(s, f"the player's databases in /{DB_DIR} could not be created: {e}")
        try:
            await asyncio.to_thread(self._apply_genre_setting, s)
        except (OSError, sqlite3.Error) as e:
            self._warn(s, f"the Split genres setting was not applied: {e}")
        tracks = await self._rebuild_index(s, fresh.old_db if fresh else None)
        if tracks is not None:
            s.data.setdefault("commit_counts", {})["device_tracks"] = tracks
        if fresh is not None:
            await self._write_fresh_hearts(s, fresh, manifest)
        elif note := await self._write_hearts(s.data.get("library_starred") or {}, manifest):
            self._warn(s, note)
        playlists: list[DevicePlaylist] | None = s.data.pop("hiby_playlists", None)
        old = s.manifest.playlist_paths if s.manifest else {}
        if playlists is None and fresh is not None:
            playlists = []  # the deleted library.db held the player's own playlists: they are written back
        if playlists is not None:
            tables = {p.name: playlist_table_name(p.name) for p in playlists}
            removed: set[str] = s.data.pop("hiby_remove_playlists", set()) - set(tables.values())
            drop = {
                t[len(PLAYLIST_PREFIX) :]
                for n, t in old.items()
                if t.startswith(PLAYLIST_PREFIX) and n not in tables
            } | removed
            rows: dict[str, list[tuple[str, str, str, int]]] = {}
            if fresh is not None:
                player = self._player_playlists(fresh, old, set(tables.values()), manifest)
                rows.update({t: r for t, r in player.items() if t not in removed})
            for p in playlists:
                entries = [manifest.items[r.key()] for r in p.refs if r.key() in manifest.items]
                rows[tables[p.name]] = [
                    (
                        e.path,
                        e.meta.get("title") or PurePosixPath(e.path).stem,
                        e.meta.get("artist") or "",
                        int(e.duration_s or 0),
                    )
                    for e in entries
                ]
            try:
                backup = await asyncio.to_thread(
                    write_playlist_tables, self.mount, rows, drop, self._backup_dir(), self._db_dir
                )
                log.info(
                    "%s: %d playlists written to library.db (backup %s)", self.id, len(rows), backup
                )
                s.data.setdefault("commit_counts", {})["device_playlists"] = len(rows)
                s.data["playlist_paths"] = {n: PLAYLIST_PREFIX + t for n, t in tables.items()}
            except (HotDatabase, OSError, sqlite3.Error) as e:
                self._warn(s, f"playlists not written to the player: {e}")
                s.data["playlist_paths"] = {
                    n: t for n, t in old.items() if t.startswith(PLAYLIST_PREFIX)
                }
        books: dict[str, list[ManifestEntry]] = {}
        for e in manifest.items.values():
            if e.kind is MediaKind.AUDIOBOOK and not e.orphan and (self.mount / e.path).is_file():
                books.setdefault(PurePosixPath(e.path).parent.as_posix(), []).append(e)
        try:
            table, parts = await asyncio.to_thread(audiobook_rows, self.mount, books)
            n = await asyncio.to_thread(
                write_audiobook_index, self.mount, table, parts, self._backup_dir(), self._db_dir
            )
            if n:
                log.info("%s: %d audiobooks indexed in audiobooks.db", self.id, n)
        except (HotDatabase, OSError, sqlite3.Error) as e:
            self._warn(s, f"audiobooks not indexed on the player: {e}")
        if fresh is not None:
            await self._write_fresh_state(s, fresh, manifest)

    def _player_playlists(
        self, fresh: _Fresh, old: dict[str, str], ours: set[str], manifest: Manifest
    ) -> dict[str, list[tuple[str, str, str, int]]]:
        """The player's own playlists of a full sync's deleted library.db (made on the player, e.g. 'Pacific Drive Hiby'): written back with the entries whose file is still ours on the card, in their order. Our
        tables of before are not among them (the plan's playlists replace those)."""
        rules = self._rules()
        by_path = {path_key(e.path, rules): e for e in manifest.items.values() if not e.orphan}
        before = {t[len(PLAYLIST_PREFIX) :] for t in old.values() if t.startswith(PLAYLIST_PREFIX)}
        out: dict[str, list[tuple[str, str, str, int]]] = {}
        for table, paths in fresh.playlists.items():
            if table in before or table in ours:
                continue
            entries = [by_path.get(path_key(p, rules)) for p in paths]
            out[table] = [
                (
                    e.path,
                    e.meta.get("title") or PurePosixPath(e.path).stem,
                    e.meta.get("artist") or "",
                    int(e.duration_s or 0),
                )
                for e in entries
                if e is not None and (self.mount / e.path).is_file()
            ]
        return out

    @staticmethod
    def _kept_databases(s: DeviceSession) -> set[str]:
        """The player's databases of the kinds this device's rules do not sync: a full sync leaves them as they are
        (the player's own audiobooks, podcast subscriptions and positions)."""
        kinds: set[MediaKind] | None = s.data.get("synced_kinds")
        if kinds is None:
            return set()
        return {
            db
            for db, kind in (
                ("audiobooks.db", MediaKind.AUDIOBOOK),
                ("podcast.db", MediaKind.PODCAST),
            )
            if kind not in kinds
        }

    async def _delete_databases(self, s: DeviceSession, manifest: Manifest) -> _Fresh | None:
        """Every commit: an unfinished transaction's journal is backed up and dropped, the
        library's star changes go onto the player's hearts (so the merged hearts are known), then library.db,
        audiobooks.db, podcast.db (with journals) and the thumbnails are backed up, deleted and created empty
        (ORGANIZE_STATE rules kept); the caller writes everything anew. The reverse sync ran before, so the player's
        plays, hearts, positions and playlists are in the libraries (when it failed, `_write_fresh_state` keeps the
        player's). None (in-place update) only when the delete itself fails."""
        backup = self._backup_dir() / f"full-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S%fZ}"
        d = self._db_dir or self.mount / DB_DIR
        kept = self._kept_databases(s)

        def drop_journals() -> None:
            backup.mkdir(parents=True, exist_ok=True)
            for name in SCHEMAS:
                for f in (d / f"{name}-journal", d / f"{name}-wal", d / f"{name}-shm"):
                    if f.is_file():
                        if f.stat().st_size > 0:
                            log.warning(
                                "%s: %s dropped (unfinished write, backed up)", self.id, f.name
                            )
                        shutil.copy2(f, backup / f.name)
                        f.unlink()

        def work() -> _Fresh:
            data = read_databases(self.mount, self._db_dir)
            tables = read_playlist_tables(self.mount, self._db_dir)
            for name in SCHEMAS:
                if (d / name).is_file():
                    shutil.copy2(d / name, backup / name)
            clear_index(self.mount, db_dir=self._db_dir, keep=kept)
            old_db = backup / "library.db"
            return _Fresh(data=data, old_db=old_db if old_db.is_file() else None, playlists=tables)

        try:
            await asyncio.to_thread(drop_journals)
            if await asyncio.to_thread(_healthy, d / "library.db") and (
                note := await self._write_hearts(s.data.get("library_starred") or {}, manifest)
            ):
                self._warn(s, note)
            fresh = await asyncio.to_thread(work)
        except (OSError, sqlite3.Error) as e:
            self._warn(s, f"the player's databases were not rewritten (updated in place): {e}")
            return None
        log.info(
            "%s: full sync: the player's databases are built anew (written to the card only if they changed)",
            self.id,
        )
        return fresh

    async def _write_fresh_hearts(
        self, s: DeviceSession, fresh: _Fresh, manifest: Manifest
    ) -> None:
        """Hearts on every file of ours that had one after the library's changes were mirrored (`_write_hearts`),
        and on every file the library has starred: a reset or emptied player database never leaves the new one
        without the library's favourites."""
        rules = self._rules()
        by_path = {path_key(e.path, rules): e for e in manifest.items.values() if not e.orphan}
        keys = {path_key(p, rules) for p in fresh.data.favourites}
        keys |= {k for k, e in by_path.items() if e.last_starred}
        add: dict[str, tuple[str, str]] = {}
        for k in sorted(keys):
            e = by_path.get(k)
            if e is not None and (self.mount / e.path).is_file():
                add[e.path] = (
                    e.meta.get("title") or PurePosixPath(e.path).stem,
                    e.meta.get("artist") or "",
                )

        def work() -> None:
            added_at = {
                device_rel(p): int(t)
                for p, t in (
                    _query(fresh.old_db, "SELECT path, added_at FROM FAVOURITES")
                    if fresh.old_db
                    else []
                )
                if p and t
            }
            write_favourites(self.mount, add, set(), self._backup_dir(), self._db_dir, added_at)
            self._save_seen(set(add))

        try:
            await asyncio.to_thread(work)
        except (HotDatabase, OSError, sqlite3.Error) as e:
            self._warn(s, f"favourites not written to the player: {e}")
            return
        s.data.setdefault("commit_counts", {})["device_hearts"] = len(add)
        if self._favourites is not None:
            self._favourites = set(add)

    async def _write_fresh_state(self, s: DeviceSession, fresh: _Fresh, manifest: Manifest) -> None:
        """Play counts and audiobook / podcast positions into the new databases, and the manifest baselines to
        match them. Plays: the library's count, or the player's own when higher (the plan's tracks may predate
        this job's reverse push); positions: the player's where it moved one (pushed to the library in step 0),
        else the library's. After a failed reverse sync the player's values were not pushed: they are
        written back as they were and the manifest baselines stay, so the next reverse sync still sends them."""
        rules = self._rules()
        tracks: dict[str, Track] = s.data.get("library_tracks") or {}
        moved: set[str] = s.data.get("device_positions") or set()
        pushed = s.data.get("reverse_ok", True)
        dev_plays = {path_key(p, rules): v for p, v in fresh.data.play_counts.items()}
        dev_pos = {path_key(p, rules): v for p, v in fresh.data.positions.items()}
        state = ListeningState()
        book_files: dict[
            str, int
        ] = {}  # folder -> our audiobook files in it (audiobook_rows' book rule)
        resume: dict[str, tuple[str, float, bool]] = {}  # folder -> its last file with a position
        for key, e in sorted(manifest.items.items(), key=lambda kv: kv[1].path):
            if pushed:
                e.last_playcount, e.last_position_s = 0, None
            if e.orphan or not (self.mount / e.path).is_file():
                continue
            folder = PurePosixPath(e.path).parent.as_posix()
            if e.kind is MediaKind.AUDIOBOOK:
                book_files[folder] = book_files.get(folder, 0) + 1
            t, pk = tracks.get(key), path_key(e.path, rules)
            plays, last = dev_plays.get(pk, (0, None))
            if t is not None and t.play_count >= plays:
                plays = t.play_count
                last = int(t.last_played.timestamp()) if t.last_played else last
            if plays:
                state.plays[e.path] = (plays, last)
                if pushed:
                    e.last_playcount = plays
            if e.kind not in (MediaKind.AUDIOBOOK, MediaKind.PODCAST):
                continue
            pos, finished = (t.position_s or 0.0, t.finished) if t is not None else (0.0, False)
            if (key in moved or t is None or not pushed) and pk in dev_pos:
                pos, finished, _ = dev_pos[pk]
            if not (pos or finished):
                continue
            if pushed:
                e.last_position_s = pos
            if e.kind is MediaKind.PODCAST:
                state.episodes[e.path] = (
                    e.meta.get("title") or PurePosixPath(e.path).stem,
                    PurePosixPath(folder).name,
                    pos,
                    float(e.duration_s or 0),
                    finished,
                )
            else:
                resume[folder] = (e.path, pos, finished)
        for folder, (file, pos, finished) in resume.items():
            state.books[file if book_files[folder] == 1 else folder] = (file, pos, finished, None)
        try:
            await asyncio.to_thread(
                write_listening_state, self.mount, state, self._db_dir, self._kept_databases(s)
            )
        except (OSError, sqlite3.Error) as e:
            self._warn(s, f"play counts and positions not written to the player: {e}")

    async def _rebuild_index(
        self, s: DeviceSession | None = None, old_db: Path | None = None
    ) -> int | None:
        """The rebuilt index's track count, None when it failed."""
        known = await asyncio.to_thread(self._known_tags)
        try:
            st = await asyncio.to_thread(
                write_library_index, self.mount, self._backup_dir(), old_db, known, self._db_dir
            )
            log.info(
                "%s: library.db index rebuilt: %d tracks in %d folders (%d files read)",
                self.id, st.tracks, st.folders, st.read,
            )  # fmt: skip
            await asyncio.to_thread(self._save_known_tags)
            return st.tracks
        except (HotDatabase, OSError, sqlite3.Error) as e:
            msg = f"the player's music index was not rebuilt: {e}"
            if s is None:
                log.warning("%s: %s", self.id, msg)
            else:
                self._warn(s, msg)
            return None

    def _save_known_tags(self) -> None:
        try:
            save_known_tags(self._backup_dir().parent / TAG_CACHE, self._known_tags())
        except OSError as e:
            log.warning("%s: tag cache not saved: %s", self.id, e)

    async def _write_hearts(self, starred: dict[str, bool], manifest: Manifest) -> str | None:
        """Mirror changes of the library's favourites onto our files: an entry's `last_starred`
        is the value last mirrored, so only a star/unstar made in the library moves a heart (the plan's tracks may
        predate this job's reverse push); a heart the reverse sync has not seen yet is never removed. The seen set follows what we wrote, so the next reverse sync doesn't send it back."""
        if not starred:
            return None
        rules = self._rules()

        def work() -> str | None:
            have = {
                path_key(p, rules): p for p in read_databases(self.mount, self._db_dir).favourites
            }
            seen = self._seen_favourites()
            # no seen set yet: the first reverse sync has not pushed the player's hearts, so none may go
            seen_keys = {path_key(p, rules) for p in seen or ()}
            add: dict[str, tuple[str, str]] = {}
            remove: set[str] = set()
            for key, star in starred.items():
                e = manifest.items.get(key)
                if e is None or e.orphan:
                    continue
                last, e.last_starred = e.last_starred, star
                if star == last or (last is None and not star):
                    continue  # only a change in the library moves a heart (the plan may predate the reverse push)
                pk = path_key(e.path, rules)
                if star and pk not in have:
                    add[e.path] = (
                        e.meta.get("title") or PurePosixPath(e.path).stem,
                        e.meta.get("artist") or "",
                    )
                elif not star and pk in have and pk in seen_keys:
                    remove.add(have[pk])
            try:
                n_add, n_rm = write_favourites(
                    self.mount, add, remove, self._backup_dir(), self._db_dir
                )
            except (HotDatabase, OSError, sqlite3.Error) as e:
                return f"favourites not written to the player: {e}"
            if not (n_add or n_rm):
                return None
            log.info("%s: %d hearts added, %d removed in library.db", self.id, n_add, n_rm)
            rm_keys = {path_key(p, rules) for p in remove}

            def written(favs: set[str]) -> set[str]:
                return {p for p in favs if path_key(p, rules) not in rm_keys} | set(add)

            # no seen set: this job's reverse sync (it runs first) found no heart of ours to push, so the player's
            # current hearts are the base
            self._save_seen(written(seen if seen is not None else set(have.values())))
            if (
                self._favourites is not None
            ):  # read by this job's reverse sync, saved after the commit
                self._favourites = written(self._favourites)
            return None

        return await asyncio.to_thread(work)

    def _save_seen(self, favourites: set[str]) -> None:
        f = self._state_file()
        f.parent.mkdir(exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(favourites)), encoding="utf-8")
        tmp.replace(f)

    async def read_feedback(self, manifest: Manifest | None) -> DeviceFeedback:
        if manifest is None:
            return DeviceFeedback()
        rules = self._rules()

        def work() -> DeviceFeedback:
            data = read_databases(self.mount)
            self._favourites = data.favourites
            seen = self._seen_favourites()
            # first reverse sync: the hearts already there are all new (none are known yet) and star their songs
            return hiby_feedback(
                data,
                manifest,
                rules,
                seen if seen is not None else set(),
                now=dt.datetime.now(dt.UTC),
                device_id=self.id,
            )

        return await asyncio.to_thread(work)

    async def reset_feedback(self, s: DeviceSession, consumed: DeviceFeedback) -> None:
        """Remember the favourites the last `read_feedback` saw, so each change is sent only once."""
        favourites, self._favourites = self._favourites, None
        if favourites is None:
            return

        await asyncio.to_thread(self._save_seen, favourites)

    async def wipe(self) -> int:
        """Delete our files and the player's index of the card (library.db, audiobooks.db, podcast.db, thumbnails:
        play counts, hearts, positions and the device's playlists go with them, no reverse sync
        first), then recreate empty databases so the next sync can write playlists, hearts and audiobooks. The
        player's own settings in `.local` stay."""
        n = await super().wipe()
        await asyncio.to_thread(clear_index, self.mount)
        await self._rebuild_index()
        return n

    def _save_settings(self, root: Path, backup: Path) -> None:
        if rules := organize_rules(root):
            (backup / "organize_rules").write_text(rules)
        if (root / DB_DIR).is_dir():
            shutil.copytree(
                root / DB_DIR,
                backup / DB_DIR,
                ignore=shutil.ignore_patterns(*(f"{n}*" for n in INDEX_FILES)),
            )

    async def format(self, name: str, fs: str | None = None) -> str | None:
        """A new file system on the whole disk (exFAT by default, like the player ships), remounted at our
        mountpoint. The player's own settings in `/.local` (everything but its index, INDEX_FILES) are backed up
        to the cache dir first and restored, then empty databases are created (SonixOS never creates `/.local` itself). An unreadable old volume is formatted without the backup. Updates
        `config.identity.fs_uuid`."""
        guard = self.ctx.guard if self.ctx else None
        if guard is None or self.ctx is None or self.block_path is None:
            raise RuntimeError(
                f"{self.id}: format needs the block device (not mounted from a disk)"
            )
        fs = fs or self.format_filesystems[0]
        if fs not in self.format_filesystems:
            raise ValueError(f"unsupported file system {fs!r} (one of {self.format_filesystems})")
        cache = self.ctx.config.cache_dir
        await asyncio.to_thread(cache.mkdir, parents=True, exist_ok=True)
        backup = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="hiby-", dir=cache))
        try:
            src = await asyncio.to_thread(mount_source, self.mount.resolve())
            if src is not None and await asyncio.to_thread((self.mount / DB_DIR).is_dir):
                await asyncio.to_thread(self._save_settings, self.mount, backup)
            else:
                await probe_read(
                    guard,
                    self.block_path,
                    self.ctx.config.mount_root,
                    lambda root: self._save_settings(root, backup),
                    registered=self.config.identity.format_guard_ids(),
                )
        except Exception as e:  # noqa: BLE001 - a broken volume is what a format is for
            log.warning("%s: player settings not backed up before the format: %s", self.id, e)
        try:
            registered = self.config.identity.format_guard_ids()
            await make_filesystem(guard, self.block_path, fs, name, registered, whole_disk=True)  # type: ignore[arg-type]
            m = await mount_fs(
                guard, self.block_path, self.id, self.ctx.config.mount_root, registered=registered
            )
            self.mount = m.path
            self._path_rules = None

            def restore() -> None:
                if (backup / DB_DIR).is_dir():
                    shutil.copytree(backup / DB_DIR, self.mount / DB_DIR)
                (self.mount / STATE_DIR).mkdir(exist_ok=True)
                saved = backup / "organize_rules"
                clear_index(self.mount, saved.read_text() if saved.is_file() else None)

            await asyncio.to_thread(restore)
            await self._rebuild_index()
        except BaseException:
            log.error("%s: format failed; player settings kept in %s", self.id, backup)
            raise
        await asyncio.to_thread(shutil.rmtree, backup, True)
        disks = await asyncio.to_thread(scan_block_devices, False)
        _, node = find_disk_of(self.block_path, disks)
        self.config.identity = self.config.identity.model_copy(update={"fs_uuid": node.uuid})
        return self.block_path
