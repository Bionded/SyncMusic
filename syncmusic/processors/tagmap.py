# SPDX-License-Identifier: GPL-3.0-or-later
"""Tags of a media file in one vocabulary, readable from and writable to every format SyncMusic writes.

ffmpeg's `-map_metadata 0` drops or mis-maps many fields between containers (FLAC Vorbis comments → MP4 atoms or
ID3: album artist, track/disc totals, sort names, MusicBrainz ids, ReplayGain, lyrics, BPM, compilation, ...). The
transcode processor therefore reads the source's tags here and writes all of them to its output with an explicit
map; retag later puts the library's values on top.

The vocabulary is mutagen's "easy" key names (lowercase, Picard's), with the numbers split: `tracknumber`,
`tracktotal`, `discnumber`, `disctotal`. Values are lists of strings (multi-value fields). On disk the names follow
MusicBrainz Picard: ID3 frames / `TXXX:<Name>`, MP4 atoms / `----:com.apple.iTunes:<Name>`, Vorbis `UPPERCASE`. A
field without a dedicated frame or atom still survives as TXXX / freeform. The front cover travels with the tags.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mutagen
from mutagen.apev2 import APEv2
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TXXX, UFID, USLT, Frames
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

Tags = dict[str, list[str]]

# ID3 text frames with a field of their own
ID3_TEXT = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "albumartist": "TPE2",
    "composer": "TCOM",
    "lyricist": "TEXT",
    "conductor": "TPE3",
    "remixer": "TPE4",
    "genre": "TCON",
    "date": "TDRC",
    "originaldate": "TDOR",
    "bpm": "TBPM",
    "compilation": "TCMP",
    "grouping": "TIT1",
    "subtitle": "TIT3",
    "discsubtitle": "TSST",
    "titlesort": "TSOT",
    "artistsort": "TSOP",
    "albumsort": "TSOA",
    "albumartistsort": "TSO2",
    "composersort": "TSOC",
    "copyright": "TCOP",
    "encodedby": "TENC",
    "isrc": "TSRC",
    "label": "TPUB",
    "media": "TMED",
    "mood": "TMOO",
    "language": "TLAN",
    "key": "TKEY",
}
ID3_TEXT_BACK = {v: k for k, v in ID3_TEXT.items()}
ID3_TEXT_BACK.update({"TYER": "date", "TORY": "originaldate", "GRP1": "grouping"})

# names of fields kept as TXXX (ID3) / freeform (MP4), as Picard writes them
CUSTOM_NAMES = {
    "musicbrainz_trackid": "MusicBrainz Track Id",  # MP4 only; ID3 keeps it in UFID
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id",
    "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
    "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
    "musicbrainz_releasetrackid": "MusicBrainz Release Track Id",
    "musicbrainz_workid": "MusicBrainz Work Id",
    "musicbrainz_discid": "MusicBrainz Disc Id",
    "musicbrainz_albumtype": "MusicBrainz Album Type",
    "musicbrainz_albumstatus": "MusicBrainz Album Status",
    "releasecountry": "MusicBrainz Album Release Country",
    "acoustid_id": "Acoustid Id",
    "replaygain_track_gain": "replaygain_track_gain",
    "replaygain_track_peak": "replaygain_track_peak",
    "replaygain_album_gain": "replaygain_album_gain",
    "replaygain_album_peak": "replaygain_album_peak",
}
CUSTOM_BACK = {v.casefold(): k for k, v in CUSTOM_NAMES.items()}
UFID_OWNER = "http://musicbrainz.org"

MP4_TEXT = {
    "title": "©nam",
    "artist": "©ART",
    "album": "©alb",
    "albumartist": "aART",
    "composer": "©wrt",
    "genre": "©gen",
    "date": "©day",
    "grouping": "©grp",
    "comment": "©cmt",
    "lyrics": "©lyr",
    "description": "desc",
    "copyright": "cprt",
    "encodedby": "©too",
    "titlesort": "sonm",
    "artistsort": "soar",
    "albumsort": "soal",
    "albumartistsort": "soaa",
    "composersort": "soco",
}
MP4_TEXT_BACK = {v: k for k, v in MP4_TEXT.items()}
MP4_FREEFORM = "----:com.apple.iTunes:"

# Vorbis / APE spellings of the same fields
ALIASES = {
    "album artist": "albumartist",
    "album_artist": "albumartist",
    "totaltracks": "tracktotal",
    "totaldiscs": "disctotal",
    "unsyncedlyrics": "lyrics",
    "unsynced lyrics": "lyrics",
    "year": "date",
    "track": "tracknumber",
    "disc": "discnumber",
    "organization": "label",
    "publisher": "label",
    "musicbrainz_releasetrackid": "musicbrainz_releasetrackid",
}
# never carried over: what the encoder writes about itself, and data that no longer fits the new audio
DROP = frozenset(
    {"encoder", "encoder_options", "itunsmpb", "itunnorm", "itunes_cddb_1", "length", "vendor"}
)


@dataclass
class FileTags:
    tags: Tags = field(default_factory=dict)
    cover: bytes | None = None  # the front cover (or the first picture)


def _add(tags: Tags, key: str, values: Any) -> None:
    key = ALIASES.get(key.casefold().strip(), key.casefold().strip())
    if key in DROP or not key:
        return
    if not isinstance(values, list | tuple):
        values = [values]
    clean = [str(v).strip() for v in values if str(v).strip()]
    if not clean:
        return
    if key in ("tracknumber", "discnumber"):
        number, _, total = clean[0].partition("/")
        if number.strip():
            tags[key] = [number.strip()]
        if total.strip():
            tags["tracktotal" if key == "tracknumber" else "disctotal"] = [total.strip()]
        return
    tags.setdefault(key, [])
    tags[key] += [v for v in clean if v not in tags[key]]


def _mime(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


# --- read --------------------------------------------------------------------------------------------------------


def read(path: Path) -> FileTags:
    """Every tag of the file in the shared vocabulary; empty for a file mutagen cannot read."""
    try:
        audio = mutagen.File(path)
    except Exception:  # noqa: BLE001 - unreadable: nothing to carry over
        return FileTags()
    if audio is None:
        return FileTags()
    out = FileTags()
    tags = audio.tags
    if isinstance(tags, ID3):
        _read_id3(tags, out)
    elif isinstance(audio, MP4):
        _read_mp4(audio, out)
    elif isinstance(tags, APEv2):
        for k, v in tags.items():
            if k.casefold().startswith("cover art"):
                data = bytes(v.value)
                out.cover = out.cover or data.split(b"\x00", 1)[-1]
            elif v.kind == 0:  # text
                _add(out.tags, k, list(v))
    elif tags is not None and hasattr(tags, "keys"):  # Vorbis comments (FLAC, Ogg)
        for k in tags.keys():  # noqa: SIM118 - a VCommentDict iterates as (key, value) pairs
            if k.casefold() == "metadata_block_picture":
                for b64 in tags[k]:
                    pic = Picture(base64.b64decode(b64))
                    if out.cover is None or pic.type == 3:
                        out.cover = bytes(pic.data)
                continue
            _add(out.tags, k, list(tags[k]))
    pictures = getattr(audio, "pictures", None)  # FLAC picture blocks
    if pictures:
        front = next((p for p in pictures if p.type == 3), pictures[0])
        out.cover = bytes(front.data)
    return out


def _read_id3(id3: ID3, out: FileTags) -> None:
    for frame in id3.values():
        fid = frame.FrameID
        if fid in ID3_TEXT_BACK:
            _add(out.tags, ID3_TEXT_BACK[fid], [str(t) for t in frame.text])
        elif fid == "TRCK":
            _add(out.tags, "tracknumber", str(frame.text[0]) if frame.text else "")
        elif fid == "TPOS":
            _add(out.tags, "discnumber", str(frame.text[0]) if frame.text else "")
        elif fid == "TXXX":
            key = CUSTOM_BACK.get(frame.desc.casefold(), frame.desc)
            _add(out.tags, key, [str(t) for t in frame.text])
        elif fid == "UFID" and frame.owner == UFID_OWNER:
            _add(out.tags, "musicbrainz_trackid", frame.data.decode("ascii", "replace"))
        elif fid == "COMM" and not frame.desc:
            _add(out.tags, "comment", [str(t) for t in frame.text])
        elif fid == "USLT" and "lyrics" not in out.tags:
            _add(out.tags, "lyrics", frame.text)
        elif fid == "APIC" and (out.cover is None or frame.type == 3):
            out.cover = bytes(frame.data)


def _read_mp4(audio: MP4, out: FileTags) -> None:
    tags: Any = audio.tags or {}
    for atom, values in tags.items():
        if atom in MP4_TEXT_BACK:
            _add(out.tags, MP4_TEXT_BACK[atom], list(values))
        elif atom in ("trkn", "disk") and values:
            number, total = values[0]
            key = "track" if atom == "trkn" else "disc"
            if number:
                _add(out.tags, f"{key}number", str(number))
            if total:
                _add(out.tags, f"{key}total", str(total))
        elif atom == "tmpo" and values:
            _add(out.tags, "bpm", str(values[0]))
        elif atom == "cpil":
            _add(out.tags, "compilation", "1" if values else "0")
        elif atom == "covr" and values:
            out.cover = bytes(values[0])
        elif atom.startswith(MP4_FREEFORM):
            name = atom[len(MP4_FREEFORM) :]
            key = CUSTOM_BACK.get(name.casefold(), name)
            _add(out.tags, key, [bytes(v).decode("utf-8", "replace") for v in values])


# --- write -------------------------------------------------------------------------------------------------------


def write(path: Path, data: FileTags) -> None:
    """Replace the file's tags with `data` (MP3, MP4/M4A, FLAC, Ogg Vorbis/Opus); other formats are left alone."""
    audio = mutagen.File(path)
    if audio is None:
        return
    if isinstance(audio, MP4):
        _write_mp4(audio, data)
    elif isinstance(audio.tags, ID3) or path.suffix.lower() == ".mp3":
        _write_id3(path, data)
        return
    elif isinstance(audio, FLAC):
        audio.delete()
        audio.clear_pictures()
        _write_vorbis(audio, data)
        if data.cover:
            audio.add_picture(_picture(data.cover))
    elif hasattr(audio, "tags") and audio.tags is not None and hasattr(audio.tags, "clear"):
        audio.tags.clear()
        _write_vorbis(audio, data)
        if data.cover:
            audio["metadata_block_picture"] = [
                base64.b64encode(_picture(data.cover).write()).decode("ascii")
            ]
    else:
        return
    audio.save()


def _picture(cover: bytes) -> Picture:
    pic = Picture()
    pic.data, pic.mime, pic.type = cover, _mime(cover), 3
    return pic


def _write_vorbis(audio: Any, data: FileTags) -> None:
    for key, values in data.tags.items():
        audio[key.upper()] = values


def _write_id3(path: Path, data: FileTags) -> None:
    id3 = ID3()
    t = data.tags
    for key, values in t.items():
        if key in ID3_TEXT:
            id3.add(Frames[ID3_TEXT[key]](encoding=3, text=values))
        elif key in ("tracknumber", "discnumber"):
            total = t.get("tracktotal" if key == "tracknumber" else "disctotal")
            text = f"{values[0]}/{total[0]}" if total else values[0]
            id3.add(Frames["TRCK" if key == "tracknumber" else "TPOS"](encoding=3, text=[text]))
        elif key in ("tracktotal", "disctotal"):
            number_key = "tracknumber" if key == "tracktotal" else "discnumber"
            if number_key not in t:  # a total without a number: keep it as "/total"
                frame = "TRCK" if key == "tracktotal" else "TPOS"
                id3.add(Frames[frame](encoding=3, text=[f"/{values[0]}"]))
        elif key == "comment":
            id3.add(COMM(encoding=3, lang="eng", desc="", text=values))
        elif key == "lyrics":
            id3.add(USLT(encoding=3, lang="eng", desc="", text="\n".join(values)))
        elif key == "musicbrainz_trackid":
            id3.add(UFID(owner=UFID_OWNER, data=values[0].encode("ascii", "replace")))
        else:
            id3.add(TXXX(encoding=3, desc=CUSTOM_NAMES.get(key, key.upper()), text=values))
    if data.cover:
        id3.add(APIC(encoding=3, mime=_mime(data.cover), type=3, desc="", data=data.cover))
    id3.save(path, v2_version=4)  # v2.3 would join multi-values with "/"


def _write_mp4(audio: MP4, data: FileTags) -> None:
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    tags.clear()
    t = data.tags
    for key, values in t.items():
        if key in MP4_TEXT:
            tags[MP4_TEXT[key]] = ["\n".join(values)] if key == "lyrics" else values
        elif key in ("tracknumber", "discnumber", "tracktotal", "disctotal"):
            kind = "track" if key.startswith("track") else "disc"
            atom = "trkn" if kind == "track" else "disk"
            if atom not in tags:
                tags[atom] = [(_int(t.get(f"{kind}number")), _int(t.get(f"{kind}total")))]
        elif key == "bpm" and _int(values):
            tags["tmpo"] = [_int(values)]
        elif key == "compilation":
            tags["cpil"] = values[0] not in ("0", "", "false")
        else:
            name = CUSTOM_NAMES.get(key, key.upper())
            tags[MP4_FREEFORM + name] = [MP4FreeForm(v.encode("utf-8")) for v in values]
    if data.cover:
        fmt = MP4Cover.FORMAT_PNG if data.cover.startswith(b"\x89PNG") else MP4Cover.FORMAT_JPEG
        tags["covr"] = [MP4Cover(data.cover, imageformat=fmt)]


def _int(values: list[str] | None) -> int:
    if not values:
        return 0
    digits = str(values[0]).split(".")[0].strip()
    return int(digits) if digits.isdigit() else 0


def carry_over(src: Path, dst: Path) -> None:
    """Give `dst` (a conversion of `src`) every tag and the cover of `src`."""
    write(dst, read(src))
