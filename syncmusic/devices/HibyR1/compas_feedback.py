# SPDX-License-Identifier: GPL-3.0-or-later
"""Listening data of a Compás card, read-only: play counts, last played and favourites from the player's
tagcache, positions from the Audiobooks and Podcasts plugins' state files on the card.

Tagcache (database format 8, a Rockbox tagcache descendant) in `.compas/`:
- `tagcache.gen` holds the current generation N as text; the files carry the suffix `.gN` (none for N = 0).
- `database_idx.tcd.gN`: header `magic, datasize, entry_count, serial, commitid, dirty` (int32, little endian;
  magic 0x54434810 or 0x54434811 for an indexed generation, dirty must be 0), then one entry per slot of 23 int32
  tag values followed by an int32 flag word (0x1 = deleted).
- the file name is a per-slot string tag (tag 4): its value in the entry is the offset into
  `database_4.tcd.gM`, where it starts with `length, slot` (int32) and the NUL-terminated UTF-8 path, absolute
  on the player (`/data/mnt/sd_0/...`). M is N unless `tagcache.refs.gN` (magic 0x54435231, version 1,
  generation, 10 source generations, FNV-1a checksum) names an older generation for the file name tag.
- numeric tags used: 15 playcount, 16 rating (1 = favourite), 18 last played (unix seconds, player clock).

Audiobooks plugin: `.plugins/.audiobooks_state_v3`, tab-separated, percent-encoded fields; `P` lines are
`P, book key, file, position s, duration s, finished, last played, ...` (key = folder under Audiobooks/,
file relative to it; a single-file book has key = file name). Podcasts plugin: `Podcasts/.progress.tsv`,
`P, key, position s, duration s, played, new, absolute path, last played, ...`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote

log = logging.getLogger(__name__)

COMPAS_DIR = ".compas"
PLAYER_ROOT = "/data/mnt/sd_0/"  # the card's mount point on the player
MAGICS = (0x54434810, 0x54434811)
REFS_MAGIC, REFS_VERSION, REFS_COUNT = 0x54435231, 1, 10
REFS_FILENAME_INDEX = 9  # position of the file name tag in the refs' source list
TAG_COUNT = 23
TAG_FILENAME, TAG_PLAYCOUNT, TAG_RATING, TAG_LASTPLAYED = 4, 15, 16, 18
FLAG_DELETED = 0x1
MASTER_HEADER, TAG_HEADER = 24, 12
ENTRY_SIZE = (TAG_COUNT + 1) * 4
FAVOURITE = 1
AUDIOBOOK_STATE = ".plugins/.audiobooks_state_v3"
PODCAST_PROGRESS = "Podcasts/.progress.tsv"
# the plugins' readers stop at these (Audiobooks.lua writes 8 KiB / 10 lines below them, so do we)
STATE_MAX_BYTES, STATE_MAX_LINES = 2097152 - 8192, 20000 - 10


class CompasDbError(ValueError):
    """The tagcache is missing, of another format or damaged."""


@dataclass
class CompasData:
    """What the card holds (paths relative to the card root). `indexed` = every path the tagcache knows."""

    play_counts: dict[str, tuple[int, int | None]] = field(default_factory=dict)
    favourites: set[str] = field(default_factory=set)
    positions: dict[str, tuple[float, bool, int | None]] = field(default_factory=dict)
    indexed: set[str] = field(default_factory=set)


def card_rel(path: str) -> str | None:
    """Card-relative path of a path on the player; None outside the card."""
    return path[len(PLAYER_ROOT) :] if path.startswith(PLAYER_ROOT) else None


def generation(db: Path) -> int:
    try:
        return max(int((db / "tagcache.gen").read_text("ascii").strip() or 0), 0)
    except (OSError, ValueError):
        return 0


def _suffix(gen: int) -> str:
    return f".g{gen}" if gen > 0 else ""


def filename_generation(db: Path, gen: int) -> int:
    """The generation whose file holds the file name strings (refs file; missing or invalid = `gen`)."""
    try:
        raw = (db / f"tagcache.refs.g{gen}").read_bytes()
    except OSError:
        return gen
    fmt = f"<IIi{REFS_COUNT}iI"
    if gen == 0 or len(raw) != struct.calcsize(fmt):
        return gen
    magic, version, g, *rest = struct.unpack(fmt, raw)
    sources = rest[:REFS_COUNT]
    if magic != REFS_MAGIC or version != REFS_VERSION or g != gen or rest[-1] != _fnv1a(raw[:-4]):
        return gen
    source = sources[REFS_FILENAME_INDEX]
    return source if 0 <= source <= gen else gen


def _fnv1a(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


@dataclass(frozen=True)
class CompasEntry:
    path: str  # card-relative
    playcount: int
    rating: int
    last_played: int
    slot: int = -1  # its row in the master index


def parse_tagcache(master: bytes, names: bytes) -> list[CompasEntry]:
    """Live entries of the master index with their card-relative paths."""
    if len(master) < MASTER_HEADER:
        raise CompasDbError("truncated master index")
    magic, _size, count, _serial, _commit, dirty = struct.unpack_from("<6i", master)
    if magic not in MAGICS:
        raise CompasDbError("not a Compás tagcache of a known format")
    if dirty:
        raise CompasDbError("the tagcache is being written")
    if count < 0 or MASTER_HEADER + count * ENTRY_SIZE > len(master):
        raise CompasDbError(f"master index too short for {count} entries")
    if len(names) < TAG_HEADER or struct.unpack_from("<i", names)[0] != MAGICS[0]:
        raise CompasDbError("file name tag file missing or damaged")
    out: list[CompasEntry] = []
    for slot in range(count):
        e = struct.unpack_from(f"<{TAG_COUNT + 1}i", master, MASTER_HEADER + slot * ENTRY_SIZE)
        if e[TAG_COUNT] & FLAG_DELETED:
            continue
        seek = e[TAG_FILENAME]
        if seek < TAG_HEADER or seek + 8 > len(names):
            continue
        length, idx = struct.unpack_from("<ii", names, seek)
        if idx != slot or length <= 0:
            continue
        raw = names[seek + 8 : seek + 8 + length].split(b"\0", 1)[0]
        rel = card_rel(raw.decode("utf-8", errors="replace"))
        if rel:
            out.append(
                CompasEntry(rel, max(e[TAG_PLAYCOUNT], 0), e[TAG_RATING], e[TAG_LASTPLAYED], slot)
            )
    return out


def master_path(mount: Path) -> Path:
    """The master index of the card's current generation."""
    db = mount / COMPAS_DIR
    return db / f"database_idx.tcd{_suffix(generation(db))}"


def read_tagcache(mount: Path) -> list[CompasEntry] | None:
    """Entries of the card's tagcache; None when there is none yet (the player has not indexed the card).
    Raises CompasDbError when it is damaged."""
    db = mount / COMPAS_DIR
    gen = generation(db)
    try:
        master = (db / f"database_idx.tcd{_suffix(gen)}").read_bytes()
    except FileNotFoundError:
        return None
    try:
        names = (
            db / f"database_{TAG_FILENAME}.tcd{_suffix(filename_generation(db, gen))}"
        ).read_bytes()
    except FileNotFoundError as e:
        raise CompasDbError("file name tag file missing") from e
    return parse_tagcache(master, names)


def _tsv(path: Path) -> list[list[str]]:
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return []
    return [line.split("\t") for line in text.splitlines() if line.startswith("P\t")]


def _num(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def _listened(pos: float, finished: bool, last: float | None) -> bool:
    """A record at 0 s, not finished and never played is just a listed item (ours or the plugin's), no listening
    data: sending it back would create a 0-second progress in the library."""
    return pos > 0 or finished or bool(last)


def audiobook_positions(mount: Path) -> dict[str, tuple[float, bool, int | None]]:
    out: dict[str, tuple[float, bool, int | None]] = {}
    for p in _tsv(mount / AUDIOBOOK_STATE):
        if len(p) < 7 or (pos := _num(p[3])) is None:
            continue
        key, file = unquote(p[1]), unquote(p[2])
        if not key or not file:
            continue
        rel = f"Audiobooks/{file}" if key == file else f"Audiobooks/{key}/{file}"
        last = _num(p[6])
        if _listened(pos, p[5] == "1", last):
            out[rel] = (pos, p[5] == "1", int(last) if last else None)
    return out


def podcast_positions(mount: Path) -> dict[str, tuple[float, bool, int | None]]:
    out: dict[str, tuple[float, bool, int | None]] = {}
    for p in _tsv(mount / PODCAST_PROGRESS):
        if len(p) < 8 or (pos := _num(p[2])) is None:
            continue
        last = _num(p[7])
        if (rel := card_rel(unquote(p[6]))) and _listened(pos, p[4] == "1", last):
            out[rel] = (pos, p[4] == "1", int(last) if last else None)
    return out


def read_card(mount: Path) -> CompasData:
    """Everything listening-related on the card. A damaged tagcache gives no counts and no favourites
    (logged), positions are still read."""
    data = CompasData()
    try:
        entries = read_tagcache(mount) or []
    except CompasDbError as e:
        log.warning("Compás tagcache not readable: %s", e)
        entries = []
    for t in entries:
        data.indexed.add(t.path)
        if t.playcount:
            data.play_counts[t.path] = (t.playcount, t.last_played or None)
        if t.rating == FAVOURITE:
            data.favourites.add(t.path)
    data.positions = {**podcast_positions(mount), **audiobook_positions(mount)}
    return data


# --- positions to the player ------------------------------------------------------------------------------


def encode(text: str) -> str:
    """Percent-encoding as the plugins write it: everything but letters, digits, `._- ` and space."""
    return re.sub(r"[^A-Za-z0-9._\- ]", lambda m: "".join(f"%{b:02X}" for b in m[0].encode()), text)


def _audiobook_key(rel: str) -> tuple[str, str] | None:
    """(book key, file) of a card-relative path under Audiobooks/: the first folder is the book, a file directly
    in Audiobooks/ is a single-file book (key = file name)."""
    parts = rel.split("/")
    if len(parts) < 2 or parts[0] != "Audiobooks":
        return None
    return (parts[1], parts[1]) if len(parts) == 2 else (parts[1], "/".join(parts[2:]))


def _fits(lines: list[str]) -> bool:
    return (
        len(lines) <= STATE_MAX_LINES and sum(len(x.encode()) + 1 for x in lines) <= STATE_MAX_BYTES
    )


class Position(NamedTuple):
    """What the library says about an audiobook file or podcast episode on the card."""

    position_s: float
    finished: bool = False
    duration_s: float | None = None  # the file's length (the plugins keep the current file's)


def _flag(b: bool) -> str:
    return "1" if b else "0"


def _secs(v: float | None) -> str:
    return str(int(v or 0))


def merge_audiobook_positions(
    text: str, updates: dict[str, Position], now: int
) -> tuple[str, list[str]]:
    """The Audiobooks plugin state with the positions of `updates` (card-relative path → Position): a book's `P`
    line gets the file, position, the file's duration and the finished flag (last played = `now`, never finished
    by hand), a missing book gets a new line; every other line stays byte for byte. Returns the new text and the
    paths that did not fit the plugin's limits."""
    lines = text.splitlines()
    index = {}
    for i, line in enumerate(lines):
        p = line.split("\t")
        if p[0] == "P" and len(p) >= 7:
            index[unquote(p[1])] = i
    dropped: list[str] = []
    for rel, pos in sorted(updates.items()):
        kf = _audiobook_key(rel)
        if kf is None:
            continue
        key, file = kf
        if key in index:
            p = lines[index[key]].split("\t")
            if pos.duration_s:
                p[4] = _secs(pos.duration_s)
            elif unquote(p[2]) != file:
                p[4] = "0"  # another chapter file of unknown length
            p[2], p[3], p[5], p[6] = (
                encode(file),
                _secs(pos.position_s),
                _flag(pos.finished),
                str(now),
            )
            if len(p) >= 8:
                p[7] = "0"  # finished_manual
            lines[index[key]] = "\t".join(p)
        else:
            new = "\t".join(
                (
                    "P",
                    encode(key),
                    encode(file),
                    _secs(pos.position_s),
                    _secs(pos.duration_s),
                    _flag(pos.finished),
                    str(now),
                    "0",
                    "0",
                )
            )
            if not _fits([*lines, new]):
                dropped.append(rel)
                continue
            # P lines first: the plugin reads them before its bookmarks and history
            at = max(index.values()) + 1 if index else 0
            lines.insert(at, new)
            index = {k: (v + 1 if v >= at else v) for k, v in index.items()}
            index[key] = at
    return "".join(f"{x}\n" for x in lines), dropped


def merge_podcast_positions(text: str, updates: dict[str, Position], now: int) -> str:
    """The Podcasts plugin's `.progress.tsv` with the positions of the episodes it lists (matched by their path on
    the player); episodes it does not list are left out, every other line stays byte for byte."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        p = line.split("\t")
        if p[0] != "P" or len(p) < 8:
            continue
        rel = card_rel(unquote(p[6]))
        if rel is not None and rel in updates:
            u = updates[rel]
            p[2], p[4], p[7] = _secs(u.position_s), _flag(u.finished), str(now)
            if u.duration_s:
                p[3] = _secs(u.duration_s)
            lines[i] = "\t".join(p)
    return "".join(f"{x}\n" for x in lines)


def _replace(path: Path, text: str) -> None:
    """Write next to the file, then rename over it (as the plugins do), so a pulled cable leaves the old file."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def write_positions(mount: Path, updates: dict[str, Position], now: int) -> list[str]:
    """Put library positions (card-relative path → Position) into the plugins' state files; a file that does not
    exist is created only for audiobooks. Returns the paths that did not fit the plugin's limits."""
    books = {k: v for k, v in updates.items() if k.startswith("Audiobooks/")}
    episodes = {k: v for k, v in updates.items() if k.startswith("Podcasts/")}
    dropped: list[str] = []
    if books:
        path = mount / AUDIOBOOK_STATE
        old = path.read_text("utf-8", errors="surrogateescape") if path.is_file() else ""
        new, dropped = merge_audiobook_positions(old, books, now)
        if new != old:
            path.parent.mkdir(exist_ok=True)
            _replace(path, new)
    path = mount / PODCAST_PROGRESS
    if episodes and path.is_file():
        old = path.read_text("utf-8", errors="surrogateescape")
        new = merge_podcast_positions(old, episodes, now)
        if new != old:
            _replace(path, new)
    return dropped


# --- our podcast episodes in the Podcasts plugin's list ---------------------------------------------------

OUR_GUID = "syncmusic:"  # the guid part of our records' keys (`<show key>|syncmusic:<card path>`)
MAX_PODCAST_RECORDS, MAX_PODCAST_BYTES = 5000, 2097152


@dataclass(frozen=True)
class Episode:
    """A podcast episode SyncMusic put on the card."""

    path: str  # card-relative, under Podcasts/
    show: str
    title: str
    published: dt.datetime | None = None
    duration_s: float | None = None


def _show_key(show: str) -> str:
    # the plugin keys a show by md5(feed URL)[:16]; ours by the show name, so it never meets a subscription's key
    return hashlib.md5(f"{OUR_GUID}{show}".encode()).hexdigest()[:16]  # noqa: S324 - a key, not security


def merge_podcast_episodes(text: str, episodes: list[Episode]) -> tuple[str, int]:
    """`.progress.tsv` listing exactly our `episodes` among our records (key guid `syncmusic:…`): missing ones
    added as new downloads, ours no longer on the card removed; the plugin's own records and every other line stay
    byte for byte, as do the position and played flags of ours. Returns the text and how many did not fit."""
    lines = text.splitlines()
    want = {e.path: e for e in episodes}
    listed: set[str] = set()
    kept: list[str] = []
    for line in lines:
        p = line.split("\t")
        if p[0] == "P" and len(p) >= 2 and unquote(p[1]).partition("|")[2].startswith(OUR_GUID):
            rel = card_rel(unquote(p[6])) if len(p) >= 7 else None
            if rel not in want or rel in listed:
                continue  # an episode of ours that left the card (or a duplicate)
            listed.add(rel)
        else:
            rel = card_rel(unquote(p[6])) if p[0] == "P" and len(p) >= 7 else None
            if rel is not None:
                listed.add(rel)  # the plugin lists it already (its own download)
        kept.append(line)
    records = sum(1 for x in kept if x.startswith("P\t"))
    size = sum(len(x.encode()) + 1 for x in kept)
    dropped = 0
    for e in sorted(want.values(), key=lambda e: e.path):
        if e.path in listed:
            continue
        epoch = int(e.published.timestamp()) if e.published else 0
        new = "\t".join(
            (
                "P",
                encode(f"{_show_key(e.show)}|{OUR_GUID}{e.path}"),
                "0",
                str(int(e.duration_s or 0)),
                "0",
                "1",
                encode(PLAYER_ROOT + e.path),
                "0",
                encode(e.title),
                encode(e.published.strftime("%Y-%m-%d") if e.published else ""),
                str(epoch),
            )
        )
        if records + 1 > MAX_PODCAST_RECORDS or size + len(new.encode()) + 1 > MAX_PODCAST_BYTES:
            dropped += 1
            continue
        kept.append(new)
        records, size = records + 1, size + len(new.encode()) + 1
    return "".join(f"{x}\n" for x in kept), dropped


def write_podcast_episodes(mount: Path, episodes: list[Episode]) -> int:
    """Refresh our records in `Podcasts/.progress.tsv` (created only when there is an episode to list)."""
    path = mount / PODCAST_PROGRESS
    old = path.read_text("utf-8", errors="surrogateescape") if path.is_file() else ""
    if not old and not episodes:
        return 0
    new, dropped = merge_podcast_episodes(old, episodes)
    if new != old:
        path.parent.mkdir(exist_ok=True)
        _replace(path, new)
    return dropped
