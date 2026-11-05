# SPDX-License-Identifier: GPL-3.0-or-later
"""Readers for Rockbox's listening data. Pure functions on bytes/text;
`devices/Rockbox/rockbox.py` finds the files and turns the results into `DeviceFeedback`.

playback.log (Rockbox core, Settings → Playback → Logging; `apps/playback.c` `add_playbacklog`):
- `/.rockbox/playback.log`, appended; when it exceeds ~512 KB at boot it is renamed to
  `/.rockbox/playback_NNNN.log` (numbered, older) and a new one is started.
- Comment lines start with `#` (`# Started Ver. <version> [<date> Time <time>]` at every boot).
- One line per track change with more than 500 ms played: `<timestamp>:<elapsed ms>:<length ms>:<path>`.
  `path` is the absolute path on the player (`/Music/...`, `/<HDD0>/...` on multi-volume targets) and may
  contain `:`. With an RTC `timestamp` is `mktime()` of the player's clock, i.e. local wall-clock time
  counted as if it were UTC; without an RTC it is milliseconds since boot (not a date).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from syncmusic.base.capabilities import PathRules
from syncmusic.core.models import DeviceStats, Manifest, ManifestEntry, PlayEvent
from syncmusic.devices.paths import path_key
from syncmusic.devices.wallclock import wall_to_utc

# Clock stamps before this are an unset RTC (the mini 2G boots at 2000-01-01) or ticks since boot.
PLAUSIBLE_AFTER = dt.datetime(2001, 1, 1, tzinfo=dt.UTC)
_VOLUME = re.compile(r"^/<[^>/]+>")  # "/<HDD0>" / "/<MMC1>" prefix on multi-volume targets


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str | None:
    try:
        return sha256_hex(path.read_bytes())
    except OSError:
        return None


@dataclass(frozen=True)
class PlaybackEntry:
    timestamp: int
    elapsed_ms: int
    length_ms: int
    path: str  # as logged (absolute on the player)


def parse_playback_log(text: str) -> list[PlaybackEntry]:
    """Entries in file order; comments, blank and malformed lines are skipped."""
    out: list[PlaybackEntry] = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split(":", 3)
        if len(parts) != 4 or not parts[3].startswith("/"):
            continue
        try:
            ts, elapsed, length = (int(p) for p in parts[:3])
        except ValueError:
            continue
        out.append(PlaybackEntry(ts, elapsed, length, parts[3]))
    return out


def device_rel(path: str) -> str:
    """Player path → path relative to the mounted volume (volume prefix and leading '/' removed)."""
    return _VOLUME.sub("", path).lstrip("/")


def counts_as_play(
    elapsed_ms: int, length_ms: int, min_fraction: float, min_seconds: float
) -> bool:
    """Scrobble rule: played at least `min_fraction` of the track or `min_seconds`."""
    if elapsed_ms >= min_seconds * 1000:
        return True
    return length_ms > 0 and elapsed_ms >= min_fraction * length_ms


def local_wall_to_utc(stamp: int) -> dt.datetime:
    """Seconds of local wall-clock time counted as if UTC (Rockbox's mktime) → aware UTC datetime."""
    wall = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=stamp)
    return wall_to_utc(wall)


def entries_by_path(manifest: Manifest, rules: PathRules) -> dict[str, ManifestEntry]:
    """Manifest entries keyed by `path_key` of their path (case-insensitive on FAT)."""
    return {path_key(e.path, rules): e for e in manifest.items.values()}


def playback_plays(
    entries: Iterable[PlaybackEntry],
    manifest: Manifest,
    rules: PathRules,
    *,
    has_rtc: bool | None,
    now: dt.datetime,
    device_id: str,
    min_fraction: float = 0.5,
    min_seconds: float = 240,
) -> tuple[list[PlayEvent], int]:
    """(plays of managed tracks, number of plays of unmanaged paths) from playback.log entries in file order.

    A stamp is used as the play time when the target has an RTC (or it is unknown) and the stamp is a
    plausible date; otherwise the play time is estimated: walking back from `now` (the last entry) by each
    later entry's elapsed time."""
    by_path = entries_by_path(manifest, rules)
    plays: list[PlayEvent] = []
    unmapped = 0
    cursor = now
    for e in reversed(list(entries)):
        counted = counts_as_play(e.elapsed_ms, e.length_ms, min_fraction, min_seconds)
        played_at: dt.datetime | None = None
        if has_rtc is not False and e.timestamp > 0:
            stamp = local_wall_to_utc(e.timestamp) if e.timestamp < 2**32 else None
            if stamp is not None and PLAUSIBLE_AFTER <= stamp <= now + dt.timedelta(days=1):
                played_at = stamp
        estimated = played_at is None
        if played_at is None:
            played_at = cursor
        cursor = min(cursor, played_at) - dt.timedelta(milliseconds=e.elapsed_ms)
        if not counted:
            continue
        entry = by_path.get(path_key(device_rel(e.path), rules))
        if entry is None:
            unmapped += 1
            continue
        plays.append(
            PlayEvent(
                ref=entry.ref,
                played_at=played_at,
                played_at_estimated=estimated,
                source_device=device_id,
            )
        )
    plays.reverse()
    return plays, unmapped


# --- .scrobbler.log ------------------------------------------------------------------------------

# Audioscrobbler "portable player logging" 1.1, written by Rockbox's lastfm_scrobbler plugin
# (`apps/plugins/lastfm_scrobbler.c`) to `/.scrobbler.log` (targets with an RTC) or
# `/.scrobbler-timeless.log` (without; timestamp always 0). Header lines start with `#`: `#AUDIOSCROBBLER/1.1`,
# `#TZ/UNKNOWN` (Rockbox: local wall time) or `#TZ/UTC`, `#CLIENT/Rockbox <target> <version>`, and a column
# header `#ARTIST\t#ALBUM...`. Then one TAB-separated line per track: artist, album (may be empty), title,
# track number (-1 = none), length in s, `L` (listened, ≥ the plugin's threshold) or `S` (skipped), unix
# timestamp, MusicBrainz track id (may be empty). A file without readable tags is logged as artist
# `<UNTAGGED>` and its file name as title. No paths: entries are mapped by the tags of the managed files.

UNTAGGED = "<UNTAGGED>"


@dataclass(frozen=True)
class ScrobblerEntry:
    artist: str
    album: str
    title: str
    length_s: int
    listened: bool
    timestamp: int


def parse_scrobbler_log(text: str) -> tuple[str | None, list[ScrobblerEntry]]:
    """(`#TZ` value or None, entries in file order); malformed lines are skipped."""
    tz: str | None = None
    out: list[ScrobblerEntry] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if line.startswith("#TZ/"):
                tz = line[4:].strip()
            continue
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) < 7 or fields[5] not in ("L", "S") or not fields[2]:
            continue
        try:
            length, stamp = int(fields[4]), int(fields[6])
        except ValueError:
            continue
        out.append(ScrobblerEntry(fields[0], fields[1], fields[2], length, fields[5] == "L", stamp))
    return tz, out


@dataclass(frozen=True)
class TrackTags:
    """What the scrobbler log can be matched on for one managed file."""

    artist: str
    album: str
    title: str
    filename: str  # last path component (UNTAGGED entries carry it as title)


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


class TagIndex:
    """Managed entries by (artist, album, title), by (artist, title) and by file name; ambiguous keys
    (several entries) never match."""

    def __init__(self, tags: dict[str, TrackTags], entries: dict[str, ManifestEntry]) -> None:
        self._maps: list[dict[tuple[str, ...], ManifestEntry | None]] = [{}, {}, {}]
        for key, t in tags.items():
            entry = entries[key]
            a, b, c = _norm(t.artist), _norm(t.album), _norm(t.title)
            for m, k in zip(self._maps, ((a, b, c), (a, c), (_norm(t.filename),)), strict=True):
                m[k] = None if k in m else entry

    def find(self, e: ScrobblerEntry) -> ManifestEntry | None:
        if e.artist == UNTAGGED:
            return self._maps[2].get((_norm(e.title),))
        a, b, c = _norm(e.artist), _norm(e.album), _norm(e.title)
        if b and (a, b, c) in self._maps[0]:
            return self._maps[0][(a, b, c)]
        return self._maps[1].get((a, c))


def scrobbler_plays(
    entries: Iterable[ScrobblerEntry],
    tz: str | None,
    index: TagIndex,
    *,
    now: dt.datetime,
    device_id: str,
) -> tuple[list[PlayEvent], int]:
    """(plays of managed tracks, number of unmatched `L` entries); only `L` entries count. `#TZ/UTC` stamps
    are UTC, others local wall time; 0 / implausible stamps are estimated backwards from `now` by the
    lengths of the later entries."""
    plays: list[PlayEvent] = []
    unmapped = 0
    cursor = now
    for e in reversed(list(entries)):
        played_at: dt.datetime | None = None
        if 0 < e.timestamp < 2**32:
            stamp = (
                dt.datetime.fromtimestamp(e.timestamp, dt.UTC)
                if tz == "UTC"
                else local_wall_to_utc(e.timestamp)
            )
            if PLAUSIBLE_AFTER <= stamp <= now + dt.timedelta(days=1):
                played_at = stamp
        estimated = played_at is None
        if played_at is None:
            played_at = cursor
        cursor = min(cursor, played_at) - dt.timedelta(seconds=max(e.length_s, 0))
        if not e.listened:
            continue
        entry = index.find(e)
        if entry is None:
            unmapped += 1
            continue
        plays.append(
            PlayEvent(
                ref=entry.ref,
                played_at=played_at,
                played_at_estimated=estimated,
                source_device=device_id,
            )
        )
    plays.reverse()
    return plays, unmapped


# --- tagcache (database_*.tcd) -------------------------------------------------------------------

# Layout from Rockbox `apps/tagcache.c` / `tagcache.h` (header version TCH 0x10), native byte order of the
# player (little-endian on ARM; a byte-swapped magic means big-endian). Files in `/.rockbox`:
# - `database_idx.tcd` (master index): header `int32 magic, datasize, entry_count, serial, commitid, dirty`
#   (24 bytes), then `entry_count` × `struct index_entry { int32 tag_seek[23]; int32 flag; }` (96 bytes).
#   For string tags `tag_seek[t]` is an offset into `database_<t>.tcd`; numeric tags hold the value itself.
#   Tags: 0 artist, 1 album, 2 genre, 3 title, 4 filename, 5 composer, 6 comment, 7 albumartist, 8 grouping,
#   9 year, 10 discnumber, 11 tracknumber, 12 canonicalartist, 13 bitrate, 14 length (ms), 15 playcount,
#   16 rating (0-10), 17 playtime (ms), 18 lastplayed (a serial counter - the master header's `serial` at the
#   time of the play, *not* a date), 19 commitid, 20 mtime, 21 lastelapsed, 22 lastoffset. Flags: 0x1
#   deleted, 0x2 filename is a dircache pointer (RAM only), 0x4 numeric data modified, 0x8 track number
#   generated, 0x10 statistics resurrected.
# - `database_<t>.tcd` (string tags): header `int32 magic, datasize, entry_count` (12 bytes), then entries
#   `int32 tag_length (incl. NUL), int32 idx_id, char data[tag_length]`.
# Rockbox counts a play (playcount + 1, lastplayed = serial++) when a track ends after ≥ 15 s or by itself,
# only with "Gather Runtime Data" on and only for files already in the database.

TAGCACHE_MAGIC = 0x54434810
TAG_COUNT = 23
TAG_FILENAME, TAG_LENGTH, TAG_PLAYCOUNT, TAG_RATING, TAG_PLAYTIME, TAG_LASTPLAYED = (
    4,
    14,
    15,
    16,
    17,
    18,
)
FLAG_DELETED, FLAG_DIRCACHE = 0x1, 0x2
MASTER_FILE = "database_idx.tcd"
MASTER_HEADER = 24
TAG_HEADER = 12
ENTRY_SIZE = (TAG_COUNT + 1) * 4


class TagcacheError(ValueError):
    """The tagcache files are missing, of another version or damaged."""


@dataclass(frozen=True)
class TagcacheEntry:
    filename: str  # as stored (absolute on the player)
    playcount: int
    rating: int  # 0-10
    playtime_ms: int
    lastplayed: int  # serial: higher = played later
    length_ms: int


def _byte_order(data: bytes) -> str:
    if len(data) >= 4:
        if int.from_bytes(data[:4], "little") == TAGCACHE_MAGIC:
            return "<"
        if int.from_bytes(data[:4], "big") == TAGCACHE_MAGIC:
            return ">"
    raise TagcacheError("not a Rockbox tagcache file of a known version")


def parse_tagcache(master: bytes, filenames: bytes) -> list[TagcacheEntry]:
    """Entries of `database_idx.tcd` with their file names from `database_4.tcd` (deleted entries and
    entries without a readable file name are left out)."""
    order = _byte_order(master)
    if _byte_order(filenames) != order:
        raise TagcacheError("tagcache files of different byte order")
    if len(master) < MASTER_HEADER:
        raise TagcacheError("truncated master index")
    count = struct.unpack_from(f"{order}i", master, 8)[0]
    if count < 0 or MASTER_HEADER + count * ENTRY_SIZE > len(master):
        raise TagcacheError(f"master index too short for {count} entries")
    out: list[TagcacheEntry] = []
    for i in range(count):
        e = struct.unpack_from(f"{order}{TAG_COUNT + 1}i", master, MASTER_HEADER + i * ENTRY_SIZE)
        flag = e[TAG_COUNT]
        if flag & (FLAG_DELETED | FLAG_DIRCACHE):
            continue
        seek = e[TAG_FILENAME]
        if seek < TAG_HEADER or seek + 8 > len(filenames):
            continue
        length = struct.unpack_from(f"{order}i", filenames, seek)[0]
        raw = filenames[seek + 8 : seek + 8 + max(length, 0)]
        name = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        if not name:
            continue
        out.append(
            TagcacheEntry(
                filename=name,
                playcount=max(e[TAG_PLAYCOUNT], 0),
                rating=min(max(e[TAG_RATING], 0), 10),
                playtime_ms=e[TAG_PLAYTIME],
                lastplayed=e[TAG_LASTPLAYED],
                length_ms=max(e[TAG_LENGTH], 0),
            )
        )
    return out


def stats_by_path(entries: Iterable[TagcacheEntry], rules: PathRules) -> dict[str, DeviceStats]:
    """Tagcache values per `path_key` of the device-relative path (the baseline for new manifest entries)."""
    return {
        path_key(device_rel(t.filename), rules): DeviceStats(
            playcount=t.playcount, rating=rating_stars(t.rating)
        )
        for t in entries
    }


def rating_stars(rating: int) -> int | None:
    """Rockbox rating 0-10 → 0-5 stars, round half up (0 = unrated → None)."""
    return (rating + 1) // 2 if rating > 0 else None


@dataclass
class TagcacheFeedback:
    plays: list[PlayEvent]
    ratings: dict[str, int | None]  # ItemRef.key() -> stars (only changed ones)
    play_counts: dict[str, int]  # ItemRef.key() -> absolute tagcache playcount
    unmapped: int  # tagcache entries with plays/ratings that are not managed files


def tagcache_feedback(
    entries: Iterable[TagcacheEntry],
    manifest: Manifest,
    rules: PathRules,
    *,
    now: dt.datetime,
    device_id: str,
) -> TagcacheFeedback:
    """Deltas against the manifest: plays = playcount − last_playcount, or playcount when it dropped below
    last_playcount (counter reset) (all estimated: the tagcache has no
    dates; most recent first by `lastplayed`, walking back from `now` by the track length), ratings that
    differ from last_rating."""
    by_path = entries_by_path(manifest, rules)
    fb = TagcacheFeedback([], {}, {}, 0)
    played: list[tuple[TagcacheEntry, ManifestEntry, int]] = []
    for t in entries:
        entry = by_path.get(path_key(device_rel(t.filename), rules))
        if entry is None:
            if t.playcount or t.rating:
                fb.unmapped += 1
            continue
        key = entry.ref.key()
        fb.play_counts[key] = t.playcount
        # a count below the last-known one was reset (Rockbox re-added the file): all of it is new
        last = entry.last_playcount if t.playcount >= entry.last_playcount else 0
        if (delta := t.playcount - last) > 0:
            played.append((t, entry, delta))
        stars = rating_stars(t.rating)
        if stars != entry.last_rating and (stars is not None or entry.last_rating):
            fb.ratings[key] = stars
    cursor = now
    for t, entry, delta in sorted(played, key=lambda p: -p[0].lastplayed):
        step = dt.timedelta(seconds=entry.duration_s or t.length_ms / 1000 or 180)
        for _ in range(delta):
            fb.plays.append(
                PlayEvent(
                    ref=entry.ref,
                    played_at=cursor,
                    played_at_estimated=True,
                    source_device=device_id,
                )
            )
            cursor -= step
    fb.plays.reverse()
    return fb


# /.rockbox/config.cfg keys of the settings that make Rockbox record listening data (apps/settings_list.c);
# Rockbox writes only settings that differ from their default (both default to off)
CFG_PLAY_LOG = "play log"  # Settings > Playback Settings > Logging -> /.rockbox/playback.log
CFG_RUNTIME_DATA = (
    "gather runtime data"  # Settings > General Settings > Database -> tagcache counts/ratings
)
# no plays or ratings recorded with the device setting `enable_play_log` on: SyncMusic switches the log on itself the next
# time it writes the device (a reverse sync without changes does not write it)
PLAY_LOG_PENDING_WARNING = (
    "Rockbox records no plays or ratings yet: SyncMusic switches Playback Logging on the next "
    "time it writes the device (Device settings); the player starts logging after it restarts"
)
# the device setting is off: SyncMusic keeps the play log off
PLAY_LOG_OFF_WARNING = (
    "Rockbox records no plays or ratings: its play log is off (Device settings > Rockbox play log); "
    "Gather Runtime Data on the player would also record them"
)


def parse_config(text: str) -> dict[str, str]:
    """`key: value` lines of a Rockbox .cfg file (comments and blank lines skipped; keys lower-case)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.lstrip().startswith("#"):
            out[key.strip().lower()] = value.strip()
    return out


def records_listening_data(cfg: dict[str, str]) -> bool:
    return any(cfg.get(k, "off").lower() == "on" for k in (CFG_PLAY_LOG, CFG_RUNTIME_DATA))


def play_log_state(text: str | None) -> str:
    """`on`, `off` (an explicit line, the last one wins as in Rockbox) or `unset` (no line / no file: Rockbox's
    default, off)."""
    value = parse_config(text or "").get(CFG_PLAY_LOG)
    if value is None:
        return "unset"
    return "on" if value.lower() == "on" else "off"


def with_play_log(text: str, on: bool) -> str | None:
    """config.cfg text with `play log: on` / `off`, or None when it already is (no rewrite). Every other line is
    kept as it is; an existing `play log` line is changed in place (the last one wins in Rockbox, so all are set),
    otherwise the line is appended."""
    want = "on" if on else "off"
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    found = changed = False
    for i, line in enumerate(lines):
        key, sep, value = line.partition(":")
        if sep and not line.lstrip().startswith("#") and key.strip().lower() == CFG_PLAY_LOG:
            found = True
            if value.strip().lower() != want:
                lines[i] = f"{key}: {want}{newline}"
                changed = True
    if not found:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        lines.append(f"{CFG_PLAY_LOG}: {want}{newline}")
        changed = True
    return "".join(lines) if changed else None


def with_play_log_on(text: str) -> str | None:
    return with_play_log(text, True)
