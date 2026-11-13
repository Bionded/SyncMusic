# SPDX-License-Identifier: GPL-3.0-or-later
"""Path layout and file-name sanitisation for file-based devices (Rockbox, flash drive).

Pure functions. A track's relative path comes from a per-kind template (`PathLayout`), each component is made
legal for the device filesystem (`PathRules`), the whole path is shortened to the filesystem's limit, and
`unique()` resolves collisions (case-insensitively where the filesystem is).

Templates use `{field}` / `{field:02}` over: title, artist, album_artist, album, year, disc, disc_total, track, genre,
series, author, chapter, date, episode_code (`S02E01` for a video episode). Missing values render empty; the separators around an empty value are
dropped (`{year} - {album}` without a year → `Album`). Fields that name a folder fall back to sensible
defaults (album artist → artist → "Unknown Artist", ...). The extension is appended by `render()`.
"""

from __future__ import annotations

import re
import string
import unicodedata
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, field_validator

from syncmusic.base.capabilities import PathRules
from syncmusic.core.models import MediaKind, Track

FIELDS = frozenset(
    {
        "title",
        "artist",
        "album_artist",
        "album",
        "year",
        "disc",
        "disc_total",
        "track",
        "genre",
        "series",
        "author",
        "chapter",
        "date",
        "episode_code",
        "library_path",
    }
)
# {library_path}: the file's path in its library without the extension (folders kept); a track whose library
# does not know its path falls back to the default template of its kind
LIBRARY_PATH = "{library_path}"
INT_FIELDS = frozenset({"year", "disc", "disc_total", "track", "chapter"})
RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
MIN_COMPONENT = 8  # shortening never cuts a component below this many characters
PLAYLIST_DIR = "Playlists"
# separators left dangling by an empty field
_EDGE = re.compile(r"^[\s\-–_,]+|[\s\-–_,]+$")
_GAP = re.compile(r"\s+[-–]\s+(?:[-–]\s+)+")
# ascii_only: Cyrillic transliteration (Latin accents are removed via NFKD)
_TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґў",
        [
            "a",
            "b",
            "v",
            "g",
            "d",
            "e",
            "e",
            "zh",
            "z",
            "i",
            "y",
            "k",
            "l",
            "m",
            "n",
            "o",
            "p",
            "r",
            "s",
            "t",
            "u",
            "f",
            "kh",
            "ts",
            "ch",
            "sh",
            "shch",
            "",
            "y",
            "",
            "e",
            "yu",
            "ya",
            "i",
            "yi",
            "ye",
            "g",
            "u",
        ],
        strict=True,
    )
)


class PathLayout(BaseModel):
    """Folder/file name templates per media kind (without extension)."""

    model_config = ConfigDict(extra="forbid")

    music: str = "Music/{album_artist}/{year} - {album}/{disc:02}{track:02} - {title}"
    # a release with 2+ discs: one folder per disc
    music_multi_disc: str = "Music/{album_artist}/{year} - {album}/CD{disc:02}/{track:02} - {title}"
    podcast: str = "Podcasts/{series}/{date} - {title}"
    audiobook: str = "Audiobooks/{author} - {series}/{chapter:03} - {title}"
    video: str = "Videos/{series}/{episode_code} - {title}"

    @field_validator("music", "music_multi_disc", "podcast", "audiobook", "video")
    @classmethod
    def _check(cls, v: str) -> str:
        for _, name, spec, conv in string.Formatter().parse(v):
            if name is None:
                continue
            if name not in FIELDS:
                raise ValueError(f"unknown path field {{{name}}} in {v!r}; known: {sorted(FIELDS)}")
            if conv or (spec and not re.fullmatch(r"0?\d", spec)):
                raise ValueError(f"unsupported format spec in {v!r} (only {{field:02}} style)")
            if spec and name not in INT_FIELDS:
                raise ValueError(f"{{{name}:{spec}}}: padding is only for {sorted(INT_FIELDS)}")
        if not v.strip("/ "):
            raise ValueError("empty path template")
        return v

    def template(self, kind: MediaKind, disc_total: int | None = None) -> str:
        if kind is MediaKind.MUSIC and (disc_total or 0) >= 2:
            return self.music_multi_disc
        return {
            MediaKind.MUSIC: self.music,
            MediaKind.PODCAST: self.podcast,
            MediaKind.VIDEO: self.video,
        }.get(kind, self.audiobook)


def device_layout(settings: object) -> PathLayout | None:
    """A file device's layout under its `base_folder` (flash drive): every template prefixed, so the
    manifest's layout of a file changes with the folder and the next sync moves it."""
    layout: PathLayout | None = getattr(settings, "layout", None)
    base = (getattr(settings, "base_folder", "") or "").strip("/")
    if layout is None or not base:
        return layout
    kinds = ("music", "music_multi_disc", "podcast", "audiobook", "video")
    return layout.model_copy(update={k: f"{base}/{getattr(layout, k)}" for k in kinds})


def _episode_code(t: Track) -> str | None:
    """`S02E01` for a video episode, so plain players sort episodes and seasons stay apart."""
    season, episode = t.extra.get("season"), t.extra.get("episode")
    if t.kind is not MediaKind.VIDEO or episode is None:
        return None
    code = f"S{season:02}E{episode:02}" if season is not None else f"E{episode:02}"
    return None if code.casefold() in (t.title or "").casefold() else code


def _values(t: Track) -> dict[str, str | int | None]:
    artist = t.artist or t.album_artist
    return {
        "title": t.title or "Untitled",
        "artist": artist or "Unknown Artist",
        "album_artist": t.album_artist or artist or "Unknown Artist",
        "album": t.album or ("Unknown Podcast" if t.kind is MediaKind.PODCAST else "Unknown Album"),
        "year": t.year,
        "disc": t.disc_no,
        "disc_total": t.disc_total,
        "track": t.track_no,
        "genre": t.genres[0] if t.genres else None,
        "series": t.series
        or t.album
        or ("Unknown Podcast" if t.kind is MediaKind.PODCAST else None),
        "author": t.author or artist or "Unknown Author",
        "chapter": t.chapter_index if t.chapter_index is not None else t.track_no,
        "date": t.published.strftime("%Y-%m-%d")
        if t.published
        else (str(t.year) if t.year else None),
        "episode_code": _episode_code(t),
    }


def _fill(component: str, values: dict[str, str | int | None]) -> str:
    out: list[str] = []
    for literal, name, spec, _ in string.Formatter().parse(component):
        out.append(literal)
        if name is None:
            continue
        v = values[name]
        if v is None or v == "":
            continue
        out.append(format(v, spec) if spec and isinstance(v, int) else str(v))
    text = _GAP.sub(" - ", "".join(out))
    return _EDGE.sub("", text)


# --- sanitisation ----------------------------------------------------------------------------


def _ascii(name: str) -> str:
    chars = []
    for c in name:
        low = c.lower()
        if low in _TRANSLIT:
            tr = _TRANSLIT[low]
            chars.append(tr.capitalize() if c != low else tr)
        else:
            chars.append(c)
    flat = unicodedata.normalize("NFKD", "".join(chars))
    return "".join(c if c.isascii() else ("" if unicodedata.combining(c) else "_") for c in flat)


def name_len(name: str, rules: PathRules) -> int:
    if rules.name_len_unit == "bytes":
        return len(name.encode("utf-8"))
    return len(name.encode("utf-16-le")) // 2


def _cut(text: str, limit: int, rules: PathRules) -> str:
    """Longest prefix of `text` within `limit` units (never splits a character)."""
    while text and name_len(text, rules) > limit:
        text = text[:-1]
    return text


def _split_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > 5 or " " in ext:
        return name, ""
    return stem, "." + ext


def sanitize_component(name: str, rules: PathRules, *, has_ext: bool = False) -> str:
    """One legal path component: forbidden/control chars → '_', no trailing dots/spaces, no leading dot,
    no reserved DOS names (non-case-sensitive filesystems), within `max_name_len` keeping the extension."""
    name = unicodedata.normalize("NFC", name)
    if rules.ascii_only:
        name = _ascii(name)
    name = "".join(
        "_" if c in rules.forbidden_chars or c == "/" or ord(c) < 32 or c == "\x7f" else c
        for c in name
    )
    stem, ext = _split_ext(name) if has_ext else (name, "")
    stem = stem.strip(" ").rstrip(rules.strip_trailing or "")
    if stem.startswith("."):
        stem = "_" + stem[1:]
    if not rules.case_sensitive and stem.split(".")[0].upper() in RESERVED:
        stem += "_"
    stem = _cut(stem, rules.max_name_len - name_len(ext, rules), rules)
    stem = stem.rstrip(rules.strip_trailing or "").rstrip(" ") or "_"
    return stem + ext


def fit_path(parts: list[str], rules: PathRules) -> list[str]:
    """Shorten components until the joined path fits `max_path_len` (characters): the file stem first, then
    the longest folders; the top folder and the extension are kept."""
    parts = list(parts)

    def total() -> int:
        return len("/".join(parts))

    while total() > rules.max_path_len:
        excess = total() - rules.max_path_len
        candidates = []
        for i, p in enumerate(parts):
            if i == 0 and len(parts) > 1:
                continue  # top folder (Music/Podcasts/...)
            stem, ext = _split_ext(p) if i == len(parts) - 1 else (p, "")
            if len(stem) > MIN_COMPONENT:
                candidates.append((len(stem), i, stem, ext))
        if not candidates:
            break  # cannot shorten further; the device write will fail with a clear error
        _, i, stem, ext = max(candidates)
        keep = max(MIN_COMPONENT, len(stem) - excess)
        stem = stem[:keep].rstrip(rules.strip_trailing or "").rstrip(" ") or "_"
        parts[i] = stem + ext
    return parts


def render(track: Track, ext: str, rules: PathRules, layout: PathLayout | None = None) -> str:
    """Relative POSIX path of `track` on the device (not yet de-duplicated, see `unique`)."""
    tpl = (layout or PathLayout()).template(track.kind, track.disc_total)
    if LIBRARY_PATH in tpl:
        lib = str(PurePosixPath(track.path).with_suffix("")) if track.path else ""
        if lib.strip("/. "):
            tpl = tpl.replace(LIBRARY_PATH, lib.replace("{", "{{").replace("}", "}}"))
        else:
            tpl = PathLayout().template(track.kind, track.disc_total)
    values = _values(track)
    raw = [c for c in tpl.split("/") if c.strip()]
    parts: list[str] = []
    for n, comp in enumerate(raw):
        last = n == len(raw) - 1
        text = _fill(comp, values)
        if last:
            text = (text or values["title"] or "Untitled") + "." + ext.lstrip(".")
        elif not text:  # a folder with only empty fields (a movie has no series)
            continue
        parts.append(sanitize_component(text, rules, has_ext=last))
    return "/".join(fit_path(parts, rules))


def playlist_path(
    name: str, rules: PathRules, folder: str = PLAYLIST_DIR, ext: str = ".m3u8"
) -> str:
    if ":" in rules.forbidden_chars:  # "Rock: 80s" reads better as "Rock - 80s" than "Rock_ 80s"
        name = re.sub(r"\s*:\s*", " - ", name).strip(" -") or name
    comp = sanitize_component(f"{name}{ext}", rules, has_ext=True)
    return f"{folder}/{comp}"


def clean_folder(value: str) -> str:
    """A device-relative folder from a setting: slashes trimmed, no `..`, no hidden or state folder."""
    parts = [p for p in value.replace("\\", "/").split("/") if p.strip()]
    if not parts:
        raise ValueError("the folder must not be empty")
    for p in parts:
        if p in (".", "..") or p.startswith("."):
            raise ValueError(f"invalid folder {value!r}: no '.', '..' or hidden folders")
    return "/".join(p.strip() for p in parts)


def path_key(rel: str, rules: PathRules) -> str:
    """Comparison key for collisions: case-folded where the filesystem is case-insensitive."""
    rel = unicodedata.normalize("NFC", rel)
    return rel if rules.case_sensitive else rel.casefold()


def spill(rel: str, counts: dict[str, int], need: int, rules: PathRules) -> str:
    """`rel`, or `rel` in "<folder> (2)", "(3)" ... when its folder holds `rules.max_files_per_folder` entries and
    `need` more would not fit (`counts`: entries per folder key, `path_key` of the parent)."""
    limit = rules.max_files_per_folder
    p = PurePosixPath(rel)
    if limit is None or p.parent == PurePosixPath("."):
        return rel
    folder, n = p.parent, 1
    while True:
        cand = (
            folder
            if n == 1
            else folder.with_name(
                _cut(folder.name, rules.max_name_len - len(f" ({n})"), rules) + f" ({n})"
            )
        )
        if counts.get(path_key(str(cand), rules), 0) + need <= limit:
            return str(cand / p.name)
        n += 1


def unique(rel: str, taken: set[str], rules: PathRules) -> str:
    """`rel`, or `rel` with ' (2)', ' (3)' ... before the extension if its key is in `taken` (keys from
    `path_key`). The caller adds the key of the result to `taken`."""
    if path_key(rel, rules) not in taken:
        return rel
    p = PurePosixPath(rel)
    stem, ext = _split_ext(p.name)
    n = 2
    while True:
        suffix = f" ({n})"
        name = _cut(stem, rules.max_name_len - name_len(suffix + ext, rules), rules) + suffix + ext
        cand = str(p.with_name(name))
        if path_key(cand, rules) not in taken:
            return cand
        n += 1
