# SPDX-License-Identifier: GPL-3.0-or-later
"""Rule fields: name → type + getter on Track. Names are matched case-insensitively (nsp style)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from syncmusic.core.models import Track

FieldType = Literal["string", "number", "bool", "date", "list"]
Getter = Callable[[Track], Any]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: FieldType
    get: Getter
    aggregate: str | None = None  # album/artist aggregate fields are computed by the evaluator
    nsp: bool = True  # exists in Navidrome smart playlists


def _x(key: str) -> Getter:
    return lambda t: t.extra.get(key)


def _rating(t: Track) -> int:
    return t.rating or 0


def _artists(t: Track) -> list[str]:
    vals = t.extra.get("artists")
    if isinstance(vals, list) and vals:
        return [str(v) for v in vals]
    return [t.artist] if t.artist else []


def _albumartists(t: Track) -> list[str]:
    vals = t.extra.get("albumartists")
    if isinstance(vals, list) and vals:
        return [str(v) for v in vals]
    aa = t.album_artist or t.artist
    return [aa] if aa else []


_SPECS: list[FieldSpec] = [
    # strings
    FieldSpec("title", "string", lambda t: t.title),
    FieldSpec("album", "string", lambda t: t.album),
    FieldSpec("artist", "list", _artists),
    FieldSpec("albumartist", "list", _albumartists),
    FieldSpec("genre", "list", lambda t: t.genres),
    FieldSpec("comment", "string", lambda t: t.comment),
    FieldSpec("filetype", "string", lambda t: t.format),
    FieldSpec("codec", "string", lambda t: t.codec or t.format),
    FieldSpec("filepath", "string", lambda t: t.path),
    FieldSpec("lyrics", "string", _x("lyrics")),
    FieldSpec("sorttitle", "string", lambda t: t.extra.get("sorttitle") or t.title),
    FieldSpec("sortalbum", "string", lambda t: t.extra.get("sortalbum") or t.album),
    FieldSpec("sortartist", "string", lambda t: t.extra.get("sortartist") or t.artist),
    FieldSpec(
        "sortalbumartist",
        "string",
        lambda t: t.extra.get("sortalbumartist") or t.album_artist or t.artist,
    ),
    FieldSpec("discsubtitle", "string", _x("discsubtitle")),
    FieldSpec("albumcomment", "string", _x("albumcomment")),
    FieldSpec("catalognumber", "string", _x("catalognumber")),
    FieldSpec("explicitstatus", "string", _x("explicitstatus")),
    FieldSpec("albumtype", "string", _x("albumtype")),
    FieldSpec("mbz_album_id", "string", _x("mbz_album_id")),
    FieldSpec("mbz_album_artist_id", "string", _x("mbz_album_artist_id")),
    FieldSpec("mbz_artist_id", "string", _x("mbz_artist_id")),
    FieldSpec("mbz_recording_id", "string", _x("mbz_recording_id")),
    FieldSpec("mbz_release_track_id", "string", _x("mbz_release_track_id")),
    FieldSpec("mbz_release_group_id", "string", _x("mbz_release_group_id")),
    # numbers
    FieldSpec("tracknumber", "number", lambda t: t.track_no or 0),
    FieldSpec("discnumber", "number", lambda t: t.disc_no or 0),
    FieldSpec("year", "number", lambda t: t.year or 0),
    FieldSpec("originalyear", "number", lambda t: t.extra.get("originalyear") or t.year or 0),
    FieldSpec("releaseyear", "number", lambda t: t.extra.get("releaseyear") or t.year or 0),
    FieldSpec("size", "number", lambda t: t.size_bytes),
    FieldSpec("duration", "number", lambda t: t.duration_s or 0),
    FieldSpec("bitrate", "number", lambda t: t.bitrate_kbps or 0),
    FieldSpec("bitdepth", "number", lambda t: t.bit_depth or 0),
    FieldSpec("samplerate", "number", lambda t: t.sample_rate or 0),
    FieldSpec("channels", "number", lambda t: t.channels or 0),
    FieldSpec("bpm", "number", lambda t: t.extra.get("bpm") or 0),
    FieldSpec("playcount", "number", lambda t: t.play_count),
    FieldSpec("rating", "number", _rating),
    FieldSpec("averagerating", "number", lambda t: t.extra.get("averagerating") or _rating(t)),
    FieldSpec("rgtrackgain", "number", lambda t: t.extra.get("rgtrackgain") or 0),
    FieldSpec("rgtrackpeak", "number", lambda t: t.extra.get("rgtrackpeak") or 0),
    FieldSpec("rgalbumgain", "number", lambda t: t.extra.get("rgalbumgain") or 0),
    FieldSpec("rgalbumpeak", "number", lambda t: t.extra.get("rgalbumpeak") or 0),
    # booleans
    FieldSpec("loved", "bool", lambda t: t.starred),
    FieldSpec("compilation", "bool", lambda t: t.compilation),
    FieldSpec("hascoverart", "bool", lambda t: t.has_cover),
    FieldSpec("haslyrics", "bool", lambda t: t.has_lyrics, nsp=False),
    FieldSpec("missing", "bool", lambda t: bool(t.extra.get("missing"))),
    # dates
    FieldSpec("dateadded", "date", lambda t: t.date_added),
    FieldSpec("datemodified", "date", lambda t: t.date_modified),
    FieldSpec("lastplayed", "date", lambda t: t.last_played),
    FieldSpec("dateloved", "date", _x("dateloved")),
    FieldSpec("daterated", "date", _x("daterated")),
    FieldSpec("date", "date", _x("date")),
    FieldSpec("originaldate", "date", _x("originaldate")),
    FieldSpec("releasedate", "date", _x("releasedate")),
    # album / artist aggregates (computed over the snapshot by the evaluator)
    FieldSpec("albumrating", "number", _x("albumrating")),
    FieldSpec("albumloved", "bool", lambda t: bool(t.extra.get("albumloved"))),
    FieldSpec("albumplaycount", "number", lambda t: t.play_count, aggregate="album_sum"),
    FieldSpec("albumlastplayed", "date", lambda t: t.last_played, aggregate="album_max"),
    FieldSpec("albumdateloved", "date", _x("albumdateloved")),
    FieldSpec("albumdaterated", "date", _x("albumdaterated")),
    FieldSpec("albumdateadded", "date", lambda t: t.date_added, aggregate="album_min"),
    FieldSpec("albumdatemodified", "date", lambda t: t.date_modified, aggregate="album_max"),
    FieldSpec("albumduration", "number", lambda t: t.duration_s or 0, aggregate="album_sum"),
    FieldSpec("albumsongcount", "number", lambda t: 1, aggregate="album_sum"),
    FieldSpec("albumsize", "number", lambda t: t.size_bytes, aggregate="album_sum"),
    FieldSpec("artistrating", "number", _x("artistrating")),
    FieldSpec("artistloved", "bool", lambda t: bool(t.extra.get("artistloved"))),
    FieldSpec("artistplaycount", "number", lambda t: t.play_count, aggregate="artist_sum"),
    FieldSpec("artistlastplayed", "date", lambda t: t.last_played, aggregate="artist_max"),
    FieldSpec("artistdateloved", "date", _x("artistdateloved")),
    FieldSpec("artistdaterated", "date", _x("artistdaterated")),
    # ids
    FieldSpec("track_id", "string", lambda t: t.ref.item_id, nsp=False),
    FieldSpec("album_id", "string", lambda t: t.album_id, nsp=False),
    FieldSpec("artist_id", "string", lambda t: t.artist_id, nsp=False),
    # library_id matches the SyncMusic library id or the library's own (e.g. Navidrome) id
    FieldSpec(
        "library_id",
        "list",
        lambda t: (
            [t.ref.library_id]
            + ([str(t.extra["source_library_id"])] if "source_library_id" in t.extra else [])
        ),
    ),
    # podcasts / audiobooks (extensions)
    FieldSpec("kind", "string", lambda t: t.kind.value, nsp=False),
    FieldSpec("series", "string", lambda t: t.series, nsp=False),
    FieldSpec("author", "string", lambda t: t.author or t.artist, nsp=False),
    # an Audiobookshelf book's own series, e.g. "Discworld" (`series` is the book or show title)
    FieldSpec("bookseries", "list", lambda t: list(t.extra.get("abs_series") or []), nsp=False),
    FieldSpec("chapter", "number", lambda t: t.chapter_index or 0, nsp=False),
    FieldSpec("position", "number", lambda t: t.position_s or 0, nsp=False),
    FieldSpec("finished", "bool", lambda t: t.finished, nsp=False),
    FieldSpec("published", "date", lambda t: t.published, nsp=False),
    # video: from the file's tags or its folders ("Show/Season 2/S02E05 …")
    FieldSpec("show", "string", _x("show"), nsp=False),
    FieldSpec("season", "number", lambda t: t.extra.get("season") or 0, nsp=False),
    FieldSpec("episode", "number", lambda t: t.extra.get("episode") or 0, nsp=False),
]

FIELDS: dict[str, FieldSpec] = {s.name: s for s in _SPECS}

# What each kind of library fills in: the rule editor offers only these for a device's libraries.
# Music servers have the Navidrome smart-playlist fields (plus podcast series where they serve podcasts); an
# audiobook server has no albums, discs, MusicBrainz ids or ReplayGain but authors, series, chapters and progress.
COMMON_FIELDS = frozenset(
    {
        "title",
        "artist",
        "genre",
        "filetype",
        "codec",
        "filepath",
        "size",
        "duration",
        "bitrate",
        "channels",
    }
    | {"year", "dateadded", "datemodified", "hascoverart", "library_id", "kind", "comment"}
)
MUSIC_FIELDS = frozenset(s.name for s in _SPECS if s.nsp) | {"haslyrics", "kind"}
BOOK_FIELDS = COMMON_FIELDS | {
    "author",
    "series",
    "bookseries",
    "chapter",
    "position",
    "finished",
    "published",
    "tracknumber",
}
ALL_FIELDS = frozenset(FIELDS)

# Aliases used by Navidrome / friendlier names.
ALIASES: dict[str, str] = {
    "albumartists": "albumartist",
    "artists": "artist",
    "genres": "genre",
    "starred": "loved",
    "track": "tracknumber",
    "disc": "discnumber",
    "id": "track_id",
    "path": "filepath",
    "format": "filetype",
    "plays": "playcount",
    "date_added": "dateadded",
    "last_played": "lastplayed",
    "play_count": "playcount",
}


def normalize_name(name: str) -> str:
    return name.strip().lower()


def resolve_field(name: str) -> FieldSpec | None:
    """Known field, alias, or `extra.<key>`; None for unknown names."""
    n = normalize_name(name)
    n = ALIASES.get(n, n)
    if n in FIELDS:
        return FIELDS[n]
    if n.startswith("extra."):
        key = name.strip()[6:]
        return FieldSpec(n, "string", _x(key), nsp=False)
    return None


def custom_tag_field(name: str) -> FieldSpec:
    """Unknown field: treated as a custom tag looked up in Track.extra (Navidrome custom tags)."""
    key = name.strip()
    low = key.lower()

    def get(t: Track) -> Any:
        if key in t.extra:
            return t.extra[key]
        return t.extra.get(low)

    return FieldSpec(low, "string", get, nsp=True)
