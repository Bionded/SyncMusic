# SPDX-License-Identifier: GPL-3.0-or-later
"""`retag` processor: write the library's metadata onto the file in a form the device reads.

The library's view of a track (title, artists, album, numbers, year, genres, compilation) is written to the
file; fields the library does not know are left as they are in the file - including track/disc totals
('2/17') and a full release date whose year matches the library's year. Options: album artist falls back to
the artist, ID3v2.3 for old players, strip private ID3 blobs, rating/play count tags, podcast/audiobook
markers (MP4 `stik`; iPods take the media type from iTunesDB instead), and field templates such as
`title: "{title} ({year})"`.

ReplayGain: music without a track gain tag (ReplayGain, iTunNORM, Opus R128) is measured with
ffmpeg's `replaygain` filter and gets `replaygain_track_gain` / `replaygain_track_peak` in the copy; `always`
measures every music file and replaces the file's own value. Measurements are kept in the `replaygain` table per
library item and source state, so each file is analysed once. Track gain only (no album gain: the chain sees one
file at a time; the developer notes).

Formats: MP3 (ID3), MP4/M4A (AAC/ALAC), FLAC, Ogg Vorbis/Opus. Anything else passes through unchanged.
The input is never modified in place unless it already lives in the work dir.
"""

from __future__ import annotations

import asyncio
import logging
import string
from pathlib import Path
from typing import Any, ClassVar, Literal

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import (
    COMM,
    ID3,
    POPM,
    TALB,
    TCMP,
    TCON,
    TDRC,
    TDRL,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TXXX,
)
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from pydantic import BaseModel, ConfigDict, field_validator

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.processor import Processor
from syncmusic.core.models import MediaKind, PlanItem, ProcessorSlot, Track, WorkItem, utcnow
from syncmusic.infra import ffmpeg
from syncmusic.processors.common import work_copy

log = logging.getLogger(__name__)

FORMATS = frozenset({"mp3", "aac", "alac", "flac", "ogg", "opus"})
TEMPLATE_TARGETS = frozenset({"title", "artist", "album", "album_artist", "genre", "comment"})
# Track fields usable in templates
TEMPLATE_FIELDS = frozenset(
    {
        "title",
        "artist",
        "album_artist",
        "album",
        "track_no",
        "disc_no",
        "year",
        "genre",
        "comment",
        "series",
        "author",
        "chapter_index",
        "kind",
    }
)
TAG_OVERHEAD = 2048  # predicted growth of the file (tag frames + padding), bytes
ID3_PRIVATE = ("PRIV", "GEOB", "UFID", "NCON")
UFID_MUSICBRAINZ = "http://musicbrainz.org"
POPM_EMAIL = "SyncMusic"  # our rating frame
POPM_STARS = [0, 1, 64, 128, 196, 255]  # 0–5 stars, Windows Media Player scale
MP4_MEDIA_KIND = {MediaKind.AUDIOBOOK: 2, MediaKind.PODCAST: 21}
GAIN_TAGS = ("replaygain_track_gain", "replaygain_track_peak")
# tag keys (casefolded) that already tell a player the track's loudness
HAS_GAIN = ("replaygain_track_gain", "itunnorm", "r128_track_gain")


class RetagSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    album_artist_fallback: bool = True  # album artist := artist when the library has none
    id3v23: bool = False  # save ID3 as v2.3 (old players) instead of v2.4
    strip_private: bool = (
        True  # drop PRIV/GEOB/UFID ID3 frames (binary blobs players ignore; not MusicBrainz's UFID)
    )
    write_rating: bool = False  # POPM / FMPS_Rating
    write_play_count: bool = False  # POPM counter / FMPS_Playcount
    kind_markers: bool = True  # MP4 media kind for podcasts/audiobooks
    templates: dict[str, str] = {}  # target field -> template over Track fields
    # music without a track gain tag is measured (missing), every music file is (always), or none (off)
    replaygain: Literal["off", "missing", "always"] = "missing"

    @field_validator("templates")
    @classmethod
    def _check_templates(cls, v: dict[str, str]) -> dict[str, str]:
        for target, tpl in v.items():
            if target not in TEMPLATE_TARGETS:
                raise ValueError(f"template target {target!r} not in {sorted(TEMPLATE_TARGETS)}")
            for _, name, spec, conv in string.Formatter().parse(tpl):
                if name is None:
                    continue
                if name not in TEMPLATE_FIELDS:
                    raise ValueError(f"unknown template field {{{name}}} in {tpl!r}")
                if conv or spec not in ("", "d", "02d", "03d"):
                    raise ValueError(f"unsupported conversion/format spec in {tpl!r}")
        return v


def _template_values(t: Track) -> dict[str, Any]:
    return {
        "title": t.title,
        "artist": t.artist or "",
        "album_artist": t.album_artist or "",
        "album": t.album or "",
        "track_no": t.track_no or 0,
        "disc_no": t.disc_no or 0,
        "year": t.year or "",
        "genre": t.genres[0] if t.genres else "",
        "comment": t.comment or "",
        "series": t.series or "",
        "author": t.author or "",
        "chapter_index": t.chapter_index or 0,
        "kind": t.kind.value,
    }


def wanted_tags(t: Track, s: RetagSettings) -> dict[str, Any]:
    """Field → value to write (None-valued fields of the library are not written)."""
    album_artist = t.album_artist or (t.artist if s.album_artist_fallback else None)
    tags: dict[str, Any] = {
        "title": t.title,
        "artist": t.artist,
        "album": t.album,
        "album_artist": album_artist,
        "track_no": t.track_no,
        "disc_no": t.disc_no,
        "year": t.year,
        "genre": list(t.genres) or None,
        "comment": t.comment,
        "compilation": t.compilation,
    }
    if s.templates:
        values = _template_values(t)
        for target, tpl in s.templates.items():
            text = tpl.format_map(values).strip()
            tags[target] = [text] if target == "genre" else text or tags[target]
    if s.write_rating:
        tags["rating"] = t.rating
    if s.write_play_count:
        tags["play_count"] = t.play_count
    if t.published is not None and t.kind in (MediaKind.PODCAST, MediaKind.AUDIOBOOK):
        # the full release date: what the iPod's Podcasts view and other players show
        tags["released"] = t.published.astimezone().date().isoformat()
    if s.kind_markers and t.kind in MP4_MEDIA_KIND:
        tags["media_kind"] = MP4_MEDIA_KIND[t.kind]
    return {k: v for k, v in tags.items() if v is not None}


# --- writers per tag format ----------------------------------------------------------------------


def _number(new: int, old: object) -> str:
    """'n' or 'n/total': a total the file already has is kept (the libraries do not know totals)."""
    _, _, total = str(old or "").partition("/")
    total = total.strip()
    return f"{new}/{total}" if total.isdigit() and int(total) >= new else str(new)


def _date(year: int, old: object) -> str:
    """The year, or the file's full date when it falls in that year ('2011-03-04' stays)."""
    text = str(old or "").strip()
    return text if text[:4] == str(year) and len(text) > 4 else str(year)


def _first(values: Any) -> object:
    return values[0] if values else None


def _write_id3(path: Path, tags: dict[str, Any], s: RetagSettings) -> None:
    try:
        id3 = ID3(path)
    except mutagen.id3.ID3NoHeaderError:
        id3 = ID3()
    text = {"title": TIT2, "artist": TPE1, "album": TALB, "album_artist": TPE2}
    for key, frame in text.items():
        if key in tags:
            id3.setall(frame.__name__, [frame(encoding=3, text=[tags[key]])])

    def old(frame_id: str) -> object:
        frame = id3.get(frame_id)
        return _first(frame.text) if frame is not None else None

    if "track_no" in tags:
        id3.setall("TRCK", [TRCK(encoding=3, text=[_number(tags["track_no"], old("TRCK"))])])
    if "disc_no" in tags:
        id3.setall("TPOS", [TPOS(encoding=3, text=[_number(tags["disc_no"], old("TPOS"))])])
    if "year" in tags:
        id3.setall("TDRC", [TDRC(encoding=3, text=[_date(tags["year"], old("TDRC"))])])
    if "released" in tags:
        id3.setall("TDRC", [TDRC(encoding=3, text=[tags["released"]])])
        id3.setall("TDRL", [TDRL(encoding=3, text=[tags["released"]])])
    if "genre" in tags:
        id3.setall("TCON", [TCON(encoding=3, text=tags["genre"])])
    if "comment" in tags:
        id3.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=[tags["comment"]])])
    if tags.get("compilation"):  # never removed: "not a compilation" is also the library's default
        id3.setall("TCMP", [TCMP(encoding=3, text=["1"])])
    if "rating" in tags or "play_count" in tags:
        old = id3.get(f"POPM:{POPM_EMAIL}")
        rating = POPM_STARS[tags["rating"]] if "rating" in tags else getattr(old, "rating", 0)
        count = tags.get("play_count", getattr(old, "count", 0))
        id3.setall("POPM", [POPM(email=POPM_EMAIL, rating=rating, count=count)])
    for key in GAIN_TAGS:
        if key in tags:
            for frame in id3.getall("TXXX"):
                if frame.desc.casefold() == key:
                    id3.delall(frame.HashKey)
            id3.add(TXXX(encoding=3, desc=key, text=[tags[key]]))
    if s.strip_private:
        # the MusicBrainz recording id lives in a UFID frame: it is a tag, not a blob
        keep = [f for f in id3.getall("UFID") if f.owner == UFID_MUSICBRAINZ]
        for frame_id in ID3_PRIVATE:
            id3.delall(frame_id)
        for frame in keep:
            id3.add(frame)
    if s.id3v23:
        id3.update_to_v23()  # converts v2.4-only frames (TDRC → TYER, ...); save() alone does not
    id3.save(path, v2_version=3 if s.id3v23 else 4)


def _write_mp4(audio: MP4, tags: dict[str, Any]) -> None:
    if audio.tags is None:
        audio.add_tags()
    t: Any = audio.tags
    assert t is not None
    for key, atom in (
        ("title", "©nam"),
        ("artist", "©ART"),
        ("album", "©alb"),
        ("album_artist", "aART"),
        ("comment", "©cmt"),
    ):
        if key in tags:
            t[atom] = [tags[key]]
    if "year" in tags:
        t["©day"] = [_date(tags["year"], _first(t.get("©day")))]
    if "released" in tags:
        t["©day"] = [tags["released"]]
    if "genre" in tags:
        t["©gen"] = tags["genre"]
    if "track_no" in tags:
        total = t.get("trkn", [(0, 0)])[0][1]
        t["trkn"] = [(tags["track_no"], total)]
    if "disc_no" in tags:
        total = t.get("disk", [(0, 0)])[0][1]
        t["disk"] = [(tags["disc_no"], total)]
    if tags.get("compilation"):
        t["cpil"] = True
    if "media_kind" in tags:
        t["stik"] = [tags["media_kind"]]
        if tags["media_kind"] == MP4_MEDIA_KIND[MediaKind.PODCAST]:
            t["pcst"] = True
    if "rating" in tags:
        t["----:com.apple.iTunes:FMPS_Rating"] = [MP4FreeForm(f"{tags['rating'] / 5:.1f}".encode())]
    if "play_count" in tags:
        t["----:com.apple.iTunes:FMPS_Playcount"] = [MP4FreeForm(str(tags["play_count"]).encode())]
    for key in GAIN_TAGS:
        if key in tags:
            for old in [k for k in t if k.casefold() == f"----:com.apple.itunes:{key}"]:
                del t[old]
            t[f"----:com.apple.iTunes:{key}"] = [MP4FreeForm(tags[key].encode())]
    audio.save()


def _write_vorbis(audio: FLAC | OggVorbis | OggOpus, tags: dict[str, Any]) -> None:
    if audio.tags is None:
        audio.add_tags()
    t: Any = audio.tags
    assert t is not None
    for key, name in (
        ("title", "TITLE"),
        ("artist", "ARTIST"),
        ("album", "ALBUM"),
        ("album_artist", "ALBUMARTIST"),
        ("comment", "COMMENT"),
    ):
        if key in tags:
            t[name] = [tags[key]]
    for key, name in (("track_no", "TRACKNUMBER"), ("disc_no", "DISCNUMBER")):
        if key in tags:
            t[name] = [_number(tags[key], _first(t.get(name)))]
    if "year" in tags:
        t["DATE"] = [_date(tags["year"], _first(t.get("DATE")))]
    if "released" in tags:
        t["DATE"] = [tags["released"]]
    if "genre" in tags:
        t["GENRE"] = tags["genre"]
    if tags.get("compilation"):
        t["COMPILATION"] = ["1"]
    if "rating" in tags:
        t["FMPS_RATING"] = [f"{tags['rating'] / 5:.1f}"]
    if "play_count" in tags:
        t["FMPS_PLAYCOUNT"] = [str(tags["play_count"])]
    for key in GAIN_TAGS:
        if key in tags:
            t[key.upper()] = [tags[key]]
    audio.save()


def _handled(audio: Any) -> bool:
    return type(audio).__name__ == "MP3" or isinstance(audio, MP4 | FLAC | OggVorbis | OggOpus)


def has_gain(audio: Any) -> bool:
    """The file already carries a track loudness value a player can use (ReplayGain, iTunNORM, Opus R128)."""
    keys = [str(k).casefold() for k in (audio.tags.keys() if audio.tags else [])]
    return any(mark in k for k in keys for mark in HAS_GAIN)


def gain_tags(gain_db: float, peak: float) -> dict[str, str]:
    """The tag values as ReplayGain scanners write them ('-6.18 dB', '0.988553')."""
    return {"replaygain_track_gain": f"{gain_db:+.2f} dB", "replaygain_track_peak": f"{peak:.6f}"}


def write_tags(path: Path, tags: dict[str, Any], s: RetagSettings) -> None:
    """Write `tags` to `path` (MP3/MP4/FLAC/Ogg, see `_handled`) in its native tag format."""
    audio = mutagen.File(path)
    if type(audio).__name__ == "MP3":
        _write_id3(path, tags, s)
    elif isinstance(audio, MP4):
        _write_mp4(audio, tags)
    elif isinstance(audio, FLAC | OggVorbis | OggOpus):
        _write_vorbis(audio, tags)
    else:
        raise ValueError(f"{path.name}: tag format not handled")


class Retag(Processor):
    type_name = "retag"
    display_name = "Normalise tags"
    Settings: ClassVar[type[BaseModel]] = RetagSettings
    stage: ClassVar[Literal["audio", "tags", "cover", "sidecar"]] = "tags"
    version: ClassVar[int] = (
        4  # 2: keeps track/disc totals and full dates; 3: removes nothing; 4: ReplayGain (470)
    )

    @property
    def s(self) -> RetagSettings:
        settings: RetagSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def suggest(cls, caps: DeviceCapabilities) -> ProcessorSlot | None:
        # Every device: the file carries the library's tags - iPods show their iTunesDB, but
        # the files keep the tags too
        return ProcessorSlot(type=cls.type_name)

    def plan(self, item: PlanItem, caps: DeviceCapabilities) -> PlanItem:
        if item.file.format not in FORMATS:
            return item
        file = item.file.model_copy(update={"size": item.file.size + TAG_OVERHEAD})
        return item.model_copy(update={"file": file, "steps": [*item.steps, self.type_name]})

    async def run(self, item: WorkItem, caps: DeviceCapabilities) -> WorkItem:
        tags = wanted_tags(item.track, self.s)

        def inspect() -> tuple[bool, bool]:  # (tag format handled, already has a gain tag)
            try:
                audio = mutagen.File(item.path)
            except mutagen.MutagenError:
                return False, False
            return _handled(audio), _handled(audio) and has_gain(audio)

        handled, tagged = await asyncio.to_thread(inspect)
        if not handled:  # not a tag format we handle (or unreadable): unchanged
            log.debug("retag: %s passes through", item.path.name)
            return item
        mode = self.s.replaygain
        if item.track.kind == MediaKind.MUSIC and (
            mode == "always" or (mode == "missing" and not tagged)
        ):
            tags.update(await self.measured_gain(item))

        def work() -> Path:
            path = work_copy(item, self.type_name)
            write_tags(path, tags, self.s)
            return path

        path = await asyncio.to_thread(work)
        file = item.plan.file.model_copy(update={"size": path.stat().st_size})
        notes = (
            {**item.notes, "replaygain": tags["replaygain_track_gain"]}
            if GAIN_TAGS[0] in tags
            else item.notes
        )
        return item.model_copy(
            update={
                "path": path,
                "plan": item.plan.model_copy(update={"file": file}),
                "steps_done": [*item.steps_done, self.type_name],
                "notes": notes,
            }
        )

    async def measured_gain(self, item: WorkItem) -> dict[str, str]:
        """The item's ReplayGain tags from the cache or, the first time, from ffmpeg (empty when it cannot be
        measured: the file is synced without them)."""
        db = self.ctx.db if self.ctx is not None else None
        key, state = item.track.ref.key(), item.track.source_state
        if db is not None and state:
            row = await db.fetchone(
                "SELECT gain_db, peak FROM replaygain WHERE item_key = ? AND source_state = ?",
                (key, state),
            )
            if row is not None:
                return gain_tags(row["gain_db"], row["peak"])
        # measured by the transcode pass; else one decode of its own
        measured: tuple[float, float] | None = None
        if (pre := item.notes.get("replaygain_measured")) is not None:
            measured = (float(pre[0]), float(pre[1]))
        else:
            try:
                measured = await ffmpeg.replaygain(item.path, duration_s=item.track.duration_s)
            except ffmpeg.FfmpegError as e:
                log.warning("ReplayGain of %s could not be measured: %s", item.track.title, e)
                return {}
        if measured is None:
            log.warning(
                "ReplayGain of %s could not be measured: ffmpeg gave no value", item.track.title
            )
            return {}
        if db is not None and state:
            await db.execute(
                "INSERT OR REPLACE INTO replaygain (item_key, source_state, gain_db, peak, analysed_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, state, *measured, utcnow().isoformat()),
            )
        return gain_tags(*measured)
