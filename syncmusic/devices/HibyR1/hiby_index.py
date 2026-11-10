# SPDX-License-Identifier: GPL-3.0-or-later
"""The SonixOS music index (`.local/library.db`) rebuilt from the files on the card.

SonixOS's own "Detect changes" left a stale index after our syncs, so after every sync, wipe and format we write the
index part of library.db ourselves, the way a full scan of the player would: MEDIA_TABLE, the lookup tables
(ALBUM/ALBUM_GROUP/ARTIST/ALBUM_ARTIST/GENRE), the LINK tables, FOLDER_TABLE (so Detect changes finds nothing to
redo), COUNT_TABLE and the player's indexes. The user's tables (FAVOURITES, PLAY_COUNTS, PLAY_HISTORY, PLAYBACK_*,
M3U_*, PLAYLIST_*, MOUNT_SERIAL, ORGANIZE_STATE …) are kept.

The values follow the behaviour of SonixOS's library.c/metadata.c (GPL; reimplemented from a written description of
the behaviour, no code copied): see the formulas below. Paths in the tables are `/mnt/sd_0/<path on the volume>`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from syncmusic.base.progress import report_progress

log = logging.getLogger(__name__)

VOLUME = "/mnt/sd_0"
PLAYABLE = (".wav", ".mp3", ".flac", ".ogg", ".dsf", ".dff", ".aif", ".aiff", ".aifc", ".caf", ".opus", ".wv", ".m4a",
            ".alac", ".aac", ".ape", ".cue")  # fmt: skip
JUNK = {
    "__macosx",
    "$recycle.bin",
    "system volume information",
    ".ds_store",
    ".spotlight-v100",
    ".trashes",
    ".fseventsd",
}
ROOT_SKIP = {"audiobooks", "podcast"}  # indexed by their own pages, not in MEDIA_TABLE
MAX_DEPTH = 12
FORMAT_CODES = {
    ".mp3": 1,
    ".flac": 2,
    ".ogg": 3,
    ".dsf": 4,
    ".dff": 4,
    ".m4a": 5,
    ".alac": 5,
    ".opus": 6,
    ".wv": 7,
    ".ape": 9,
}
DEFAULT_RULES = "a0/g0/j0/x00000000"

# library.c SCHEMA_INDEXES + SORT_INDEXES: the player builds them at open when they are missing, but a database
# without them could not be opened after a Format, so we write them as the player does
INDEXES = (
    "CREATE INDEX IF NOT EXISTS media_path_idx ON MEDIA_TABLE(path)",
    "CREATE INDEX IF NOT EXISTS media_album_key_idx ON MEDIA_TABLE(album, album_key, year, ctime)",
    "CREATE INDEX IF NOT EXISTS media_artist_ctime_idx ON MEDIA_TABLE(artist, ctime)",
    "CREATE INDEX IF NOT EXISTS media_album_artist_ctime_idx ON MEDIA_TABLE(album_artist, ctime)",
    "CREATE INDEX IF NOT EXISTS artist_link_name_idx ON ARTIST_LINK(artist)",
    "CREATE INDEX IF NOT EXISTS artist_link_path_idx ON ARTIST_LINK(path)",
    "CREATE INDEX IF NOT EXISTS album_artist_link_name_idx ON ALBUM_ARTIST_LINK(album_artist)",
    "CREATE INDEX IF NOT EXISTS album_artist_link_path_idx ON ALBUM_ARTIST_LINK(path)",
    "CREATE INDEX IF NOT EXISTS genre_link_name_idx ON GENRE_LINK(genre)",
    "CREATE INDEX IF NOT EXISTS genre_link_path_idx ON GENRE_LINK(path)",
    "CREATE INDEX IF NOT EXISTS media_sort_idx ON MEDIA_TABLE(sortkey)",
    "CREATE INDEX IF NOT EXISTS media_genre_sort_idx ON MEDIA_TABLE(genre, sortkey)",
    "CREATE INDEX IF NOT EXISTS album_sort_idx ON ALBUM_TABLE(sortkey, album)",
    "CREATE INDEX IF NOT EXISTS album_group_sort_idx ON ALBUM_GROUP_TABLE(sortkey, album)",
    "CREATE INDEX IF NOT EXISTS artist_sort_idx ON ARTIST_TABLE(sortkey, artist)",
    "CREATE INDEX IF NOT EXISTS album_artist_sort_idx ON ALBUM_ARTIST_TABLE(sortkey, album_artist)",
    "CREATE INDEX IF NOT EXISTS genre_sort_idx ON GENRE_TABLE(sortkey, genre)",
)
INDEX_TABLES = (
    "MEDIA_TABLE",
    "ALBUM_TABLE",
    "ALBUM_GROUP_TABLE",
    "ARTIST_TABLE",
    "ALBUM_ARTIST_TABLE",
    "GENRE_TABLE",
    "ARTIST_LINK",
    "ALBUM_ARTIST_LINK",
    "GENRE_LINK",
    "FOLDER_TABLE",
    "CUE_STATE",
    "COUNT_TABLE",
)

# --- names: sort group, case folding, articles ---------------------------------------------------------------

EMPTY, SYMBOL, DIGIT, LATIN, GREEK, CYRILLIC, KANA, CJK, HANGUL, OTHER = range(10)


def sort_group(c: int) -> int:
    if c == 0:
        return EMPTY
    if 0x30 <= c <= 0x39:
        return DIGIT
    if 0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A:
        return LATIN
    if c < 0x80:
        return SYMBOL
    if 0xC0 <= c <= 0x24F:
        return LATIN
    if 0x370 <= c <= 0x3FF:
        return GREEK
    if 0x400 <= c <= 0x4FF:
        return CYRILLIC
    if 0x3040 <= c <= 0x30FF or 0x31F0 <= c <= 0x31FF or 0xFF66 <= c <= 0xFF9D:
        return KANA
    if 0x3400 <= c <= 0x4DBF or 0x4E00 <= c <= 0x9FFF or 0xF900 <= c <= 0xFAFF:
        return CJK
    if 0x1100 <= c <= 0x11FF or 0xAC00 <= c <= 0xD7AF:
        return HANGUL
    if 0x2000 <= c <= 0x2BFF:
        return SYMBOL
    return OTHER


# accented Latin to the plain letter: U+00C0-U+00FF ("*" = the maths signs, unchanged) and U+0100-U+017F
_LATIN1 = "aaaaaaaceeeeiiiidnooooo*ouuuuypsaaaaaaaceeeeiiiidnooooo*ouuuuypy"
_EXT_A = (
    "aaaaaa"
    "cccccccc"
    "dddd"
    "eeeeeeeeee"
    "gggggggg"
    "hhhh"
    "iiiiiiiiiiii"
    "jj"
    "kkk"
    "llllllllll"
    "nnnnnnnnn"
    "oooooooo"
    "rrrrrr"
    "ssssssss"
    "tttttt"
    "uuuuuuuuuuuu"
    "ww"
    "yyy"
    "zzzzzz"
    "s"
)


def fold_case(c: int) -> int:
    if 0x41 <= c <= 0x5A:
        return c + 32
    if 0xC0 <= c <= 0xFF:
        f = _LATIN1[c - 0xC0]
        return c if f == "*" else ord(f)
    if 0x100 <= c <= 0x17F:
        i = c - 0x100
        return ord(_EXT_A[i]) if i < len(_EXT_A) else c
    if 0x391 <= c <= 0x3A9 or 0x410 <= c <= 0x42F:
        return c + 32
    if c in (0x401, 0x451):
        return 0x435
    if 0x400 <= c <= 0x40F:
        return c + 80
    return c


ARTICLES = (
    b"the ",
    b"a ",
    b"an ",
    b"il ",
    b"lo ",
    b"la ",
    b"i ",
    b"gli ",
    b"le ",
    b"l'",
    b"un ",
    b"uno ",
    b"una ",
)
KEY_MAX = 512


def _skip_article(b: bytes) -> int:
    low = b[:5].lower()
    for a in ARTICLES:
        if len(b) > len(a) and low.startswith(a):
            return len(a)
    return 0


def _rest(text: str | None) -> str:
    """The text after a leading article."""
    b = (text or "").encode()
    return b[_skip_article(b) :].decode(errors="replace")


def sort_key(text: str | None) -> str:
    rest = _rest(text)
    out = bytearray(str(sort_group(ord(rest[0]) if rest else 0)).encode())
    for ch in rest:
        enc = chr(fold_case(ord(ch))).encode()
        if len(out) + len(enc) >= KEY_MAX:
            break
        out += enc
    return out.decode()


def index_char(text: str | None) -> tuple[str, int]:
    """(character, group): the A–Z strip letter of a name and its sort group (the `cn` of the lookup tables)."""
    if not text:
        return "#", EMPTY
    rest = _rest(text)
    if not rest:
        return "#", SYMBOL
    c = ord(rest[0])
    g = sort_group(c)
    if g == LATIN and c < 0x80:
        return rest[0].upper(), g
    if g == DIGIT:
        return "0-9", g
    return rest[0], g


def fold_text(text: str) -> bytes:
    out = bytearray()
    for ch in text:
        enc = chr(fold_case(ord(ch))).encode()
        if len(out) + len(enc) >= KEY_MAX:
            break
        out += enc
    return bytes(out)


def fnv1a32(b: bytes) -> int:
    h = 2166136261
    for x in b:
        h = ((h ^ x) * 16777619) & 0xFFFFFFFF
    return h


def _ascii_lower(b: bytes) -> bytes:
    return bytes(x + 32 if 0x41 <= x <= 0x5A else x for x in b)


def folder_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else path


def album_key(album_artist: str, path: str) -> str:
    if album_artist:
        return f"a{fnv1a32(fold_text(album_artist)):08x}"
    return f"d{fnv1a32(_ascii_lower(folder_of(path).encode())):08x}"


def entry_hash(name: str, is_dir: bool) -> int:
    """FNV-1a 64 of an ASCII-lower-cased directory entry name (+ '/' for a folder): FOLDER_TABLE.names adds them."""
    h = 14695981039346656037
    for x in _ascii_lower(name.encode()) + (b"/" if is_dir else b""):
        h = ((h ^ x) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def folder_signature(entries: list[tuple[str, bool]]) -> tuple[int, int]:
    """(files, names) of FOLDER_TABLE for the counted entries (name, is_dir) of one folder."""
    s = sum(entry_hash(n, d) for n, d in entries) & 0xFFFFFFFFFFFFFFFF
    return len(entries), s - (1 << 64) if s >= 1 << 63 else s


# --- splitting names (ORGANIZE_STATE rules) --------------------------------------------------------------------

SEMICOLON, SLASH, AMPERSAND, COMMA, FEAT, VS, X, PLUS, WITH, AND = (1 << i for i in range(10))
# the separators a genre may be split at (the player's Split genres choices); words are for artists only
GENRE_SEPARATORS = {
    "semicolon": SEMICOLON,
    "slash": SLASH,
    "ampersand": AMPERSAND,
    "comma": COMMA,
    "plus": PLUS,
}
_SYMBOLS = (
    (SEMICOLON, b";"),
    (SEMICOLON, "；".encode()),
    (SLASH, b"/"),
    (SLASH, "／".encode()),
    (AMPERSAND, b"&"),
    (AMPERSAND, "＆".encode()),
    (COMMA, b","),
    (COMMA, "，".encode()),
    (COMMA, "、".encode()),
    (PLUS, b"+"),
    (PLUS, "＋".encode()),
    (X, "×".encode()),
)
# (bit, word, may end in '.', case as written, outside brackets only)
_WORDS = (
    (FEAT, b"featuring", True, False, False),
    (FEAT, b"feat", True, False, False),
    (FEAT, b"ft", True, False, False),
    (VS, b"versus", True, False, False),
    (VS, b"vs", True, False, False),
    (X, b"x", False, True, True),
    (WITH, b"with", False, False, True),
    (AND, b"and", False, False, True),
)
SPLIT_MAX = 16
SPLIT_NAME_MAX = 256


def _word_at(b: bytes, i: int, seps: int, in_brackets: bool) -> int:
    if i == 0 or b[i - 1 : i] not in (b" ", b"(", b"["):
        return 0
    for bit, word, stop, exact, outside in _WORDS:
        if not seps & bit or (outside and in_brackets):
            continue
        n = len(word)
        got = b[i : i + n]
        if (got if exact else got.lower()) != word:
            continue
        if stop and b[i + n : i + n + 1] == b".":
            return n + 1
        if b[i + n : i + n + 1] == b" ":
            return n
    return 0


def _symbol_at(b: bytes, i: int, seps: int) -> int:
    for bit, sym in _SYMBOLS:
        if seps & bit and b.startswith(sym, i):
            return len(sym)
    return 0


def _trim(text: bytes, in_brackets: bool) -> bytes:
    while True:
        text = text.strip(b" \t")
        if text and text[-1:] in (b"(", b"[", b"-"):
            text = text[:-1]
            continue
        if in_brackets and text and text[-1:] in (b")", b"]"):
            text, in_brackets = text[:-1], False
            continue
        return text


def split_names(text: str, seps: int, exceptions: tuple[str, ...] = ()) -> list[str]:
    """The names of a multi-valued tag ("A feat. B", "Rock; Pop"), or [] when it is one name."""
    if not text or not seps:
        return []
    b = text.encode()
    exc = [e.encode().lower() for e in exceptions if e]
    parts: list[list[Any]] = [[bytearray(), False]]
    depth, word_in_brackets, i = 0, False, 0
    while i < len(b):
        keep = next((len(e) for e in exc if b[i : i + len(e)].lower() == e), 0)
        cut = 0
        if not keep:
            sym = _symbol_at(b, i, seps)
            if sym:
                cut = sym if depth == 0 or word_in_brackets else 0
            else:
                cut = _word_at(b, i, seps, depth > 0)
                word_in_brackets |= cut > 0 and depth > 0
        if cut:
            i += cut
            if len(parts) < SPLIT_MAX:
                parts.append([bytearray(), depth > 0])
            continue
        n = keep or 1
        if not keep:
            if b[i : i + 1] in (b"(", b"["):
                depth += 1
            elif b[i : i + 1] in (b")", b"]") and depth > 0:
                depth -= 1
                word_in_brackets = word_in_brackets and depth > 0
        cur = parts[-1][0]
        if len(cur) + n < SPLIT_NAME_MAX:
            cur += b[i : i + n]
        i += n
    out: list[str] = []
    for raw, in_br in parts:
        t = _trim(bytes(raw), in_br)
        if not t:
            continue
        name = t.decode(errors="replace")
        if all(
            o.lower() != name.lower() for o in out
        ):  # strcasecmp: ASCII only, like lower() for our use
            out.append(name)
    return out if len(out) > 1 else []


@dataclass(frozen=True)
class Rules:
    """ORGANIZE_STATE's `rules` string: separators per field and the album join."""

    artist: int = 0
    genre: int = 0
    album_artist: int = 0
    join: bool = False

    @classmethod
    def parse(cls, s: str | None) -> Rules:
        vals: dict[str, int] = {}
        for part in (s or DEFAULT_RULES).split("/"):
            if part[:1] in ("a", "g", "j", "b") and part[1:].isdigit():
                vals[part[0]] = int(part[1:])
        return cls(vals.get("a", 0), vals.get("g", 0), vals.get("b", 0), bool(vals.get("j", 0)))


def with_genre_separators(rules: str | None, bits: int) -> str:
    """`rules` (ORGANIZE_STATE) with the genre separators set to `bits`; the artist, album-artist, join and
    exception parts stay exactly as the player wrote them."""
    parts = (rules or DEFAULT_RULES).split("/")
    for i, part in enumerate(parts):
        if part[:1] == "g" and part[1:].isdigit():
            parts[i] = f"g{bits}"
            break
    else:
        parts.insert(1 if parts and parts[0][:1] == "a" else 0, f"g{bits}")
    return "/".join(parts)


# --- reading a file ------------------------------------------------------------------------------------------


@dataclass
class Tags:
    title: str = ""
    album: str = ""
    artist: str = ""
    album_artist: str = ""
    genre: str = ""
    year: int = 0
    track: int = 0
    disc: int = 0
    sample_rate: int = 0
    bits: int = 0
    alac: bool = False


def _clean(v: Any) -> str:
    s = str(v)
    return "".join(" " if ord(c) < 0x20 or ord(c) == 0x7F else c for c in s).strip(" ")


def _join(values: list[Any]) -> str:
    """Repeated values joined with "; ", repeats (case-insensitive) dropped, as the player's reader does."""
    out: list[str] = []
    for v in values:
        s = _clean(v)
        if s and all(o.lower() != s.lower() for o in out):
            out.append(s)
    return "; ".join(out)


def _int(v: Any) -> int:
    """C strtol: leading digits ("3/12" → 3, "2003-05-01" → 2003)."""
    s = str(v).strip()
    digits = ""
    for ch in s.lstrip("+"):
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else 0


def read_tags(path: Path) -> Tags:
    import mutagen
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4

    t = Tags()
    try:
        audio = mutagen.File(path)
    except Exception as e:  # noqa: BLE001 - an unreadable file is indexed by its name, like the player does
        log.debug("%s: tags not read: %s", path, e)
        return t
    if audio is None:
        return t
    info = getattr(audio, "info", None)
    tags = audio.tags
    if isinstance(audio, FLAC):
        t.sample_rate, t.bits = int(info.sample_rate), int(info.bits_per_sample)
    elif isinstance(audio, MP4):
        t.sample_rate = int(getattr(info, "sample_rate", 0) or 0)
        t.bits = (
            int(getattr(info, "bits_per_sample", 0) or 0)
            if getattr(info, "codec", "") == "alac"
            else 0
        )
        t.alac = getattr(info, "codec", "") == "alac"
    elif path.suffix.lower() in (".wav", ".dsf", ".ape"):
        t.sample_rate = int(getattr(info, "sample_rate", 0) or 0)
        t.bits = (
            1 if path.suffix.lower() == ".dsf" else int(getattr(info, "bits_per_sample", 0) or 0)
        )
    if tags is None:
        return t
    if isinstance(tags, ID3):

        def text(fid: str) -> list[Any]:
            frame = tags.get(fid)
            return list(frame.text) if frame is not None else []

        t.title = _join(text("TIT2")[:1])
        t.album = _join(text("TALB")[:1])
        t.artist = _join(text("TPE1"))
        t.album_artist = _join(text("TPE2"))
        t.genre = _join([g for f in tags.getall("TCON") for g in f.genres])
        t.track = _int((text("TRCK") or [""])[0])
        t.disc = _int((text("TPOS") or [""])[0])
        for fid in ("TYER", "TDRC", "TORY", "TDOR"):
            if y := _int((text(fid) or [""])[0]):
                t.year = y
                break
    elif isinstance(audio, MP4):

        def atom(key: str) -> list[Any]:
            return list(tags.get(key) or [])

        t.title = _join(atom("\xa9nam")[:1])
        t.album = _join(atom("\xa9alb")[:1])
        t.artist = _join(atom("\xa9ART"))
        t.album_artist = _join(atom("aART")[:1])
        t.genre = _join(atom("\xa9gen"))
        t.track = int((atom("trkn") or [(0, 0)])[0][0] or 0)
        t.disc = int((atom("disk") or [(0, 0)])[0][0] or 0)
        t.year = _int((atom("\xa9day") or [""])[0])
    else:  # Vorbis comments (FLAC, Ogg, Opus) and APEv2/others with text keys

        def vc(*keys: str) -> list[Any]:
            out: list[Any] = []
            for k in keys:
                v = tags.get(k) if hasattr(tags, "get") else None
                if v is None:
                    continue
                out += list(v) if isinstance(v, list) else [v]
            return out

        t.title = _join(vc("TITLE", "title")[:1])
        t.album = _join(vc("ALBUM", "album")[:1])
        t.artist = _join(vc("ARTIST", "artist"))
        aa = vc(
            "ALBUMARTIST",
            "albumartist",
            "ALBUM ARTIST",
            "album artist",
            "ALBUM_ARTIST",
            "album_artist",
        )
        t.album_artist = _clean(aa[-1]) if aa else ""
        t.genre = _join(vc("GENRE", "genre"))
        t.track = _int((vc("TRACKNUMBER", "tracknumber") or vc("TRACK", "track") or [""])[0])
        t.disc = _int((vc("DISCNUMBER", "discnumber") or vc("DISC", "disc") or [""])[0])
        t.year = _int((vc("DATE", "date") or vc("YEAR", "year") or [""])[0])
    return t


# --- the walk --------------------------------------------------------------------------------------------------


def _playable(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in PLAYABLE)


def _skipped(name: str) -> bool:
    return name.startswith(".") or name.lower() in JUNK


@dataclass
class Folder:
    rel: str  # "" for the card root
    mtime: int
    files: list[os.DirEntry[str]]  # playable files, in readdir order
    entries: list[tuple[str, bool]]  # what FOLDER_TABLE counts
    has_cue: bool


def walk(mount: Path) -> Iterator[Folder]:
    """The folders a full scan of the player enters, depth first, files before subfolders."""

    def visit(rel: str, depth: int) -> Iterator[Folder]:
        d = mount / rel if rel else mount
        subdirs: list[str] = []
        files: list[os.DirEntry[str]] = []
        entries: list[tuple[str, bool]] = []
        with os.scandir(d) as it:
            for e in it:
                if _skipped(e.name) or len(f"{VOLUME}/{rel}/{e.name}".encode()) >= 512:
                    continue
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                    is_file = e.is_file(follow_symlinks=True)
                except OSError:
                    continue
                if is_dir:
                    entries.append((e.name, True))
                    if not (depth == 0 and e.name.lower() in ROOT_SKIP):
                        subdirs.append(e.name)
                elif is_file and _playable(e.name):
                    entries.append((e.name, False))
                    files.append(e)
        yield Folder(
            rel,
            int(d.stat().st_mtime),
            files,
            entries,
            any(f.name.lower().endswith(".cue") for f in files),
        )
        if depth < MAX_DEPTH:
            for s in subdirs:
                yield from visit(f"{rel}/{s}" if rel else s, depth + 1)

    yield from visit("", 0)


# --- building the rows -----------------------------------------------------------------------------------------

MEDIA_COLUMNS = (
    "id, path, name, album, artist, genre, year, dis_id, ck_id, has_child_file, begin_time, end_time, cue_id,"
    " character, size, sample_rate, bit_rate, bit, channel, format, quality, album_pic_path, lrc_path, track_gain,"
    " track_peak, album_artist, ctime, mtime, sortkey, disc, album_key"
)


def media_row(n: int, path: str, st: os.stat_result, t: Tags) -> tuple[Any, ...]:
    name = t.title or Path(path).stem
    ext = Path(path).suffix.lower()
    fmt = 8 if t.alac else FORMAT_CODES.get(ext, 0)
    album_artist = t.album_artist or t.artist
    return (
        n,
        path,
        name,
        t.album,
        t.artist,
        t.genre,
        t.year,
        t.track,
        0,
        0,
        0,
        0,
        0,
        index_char(name)[0],
        st.st_size,
        t.sample_rate,
        0,
        t.bits,
        0,
        fmt,
        "",
        None,
        None,
        0.0,
        0.0,
        album_artist,
        int(st.st_ctime),
        int(st.st_mtime),
        sort_key(name),
        t.disc if t.disc > 0 else 1,
        album_key(album_artist, path),
    )


def _lookup(table: str, col: str, value: str) -> tuple[str, tuple[Any, ...]]:
    ch, g = index_char(value)
    if table == "ALBUM_ARTIST_TABLE":
        return (
            f"INSERT OR IGNORE INTO {table}(id, {col}, character, cn, ctime, mtime, mqa, pinyin_charater, sortkey)"
            " VALUES (0, ?, ?, ?, 0, 0, 0, '', ?)",
            (value, ch, g, sort_key(value)),
        )
    return (
        f"INSERT OR IGNORE INTO {table}(id, {col}, character, cn, sortkey) VALUES (0, ?, ?, ?, ?)",
        (value, ch, g, sort_key(value)),
    )


def _file_names(
    con: sqlite3.Connection, path: str, value: str, table: str, col: str, seps: int
) -> None:
    if not value:
        return
    names = split_names(value, seps, ("AC/DC",) if table == "ARTIST_TABLE" else ())
    for name in names or [value]:
        con.execute(*_lookup(table, col, name))
    link = table.replace("_TABLE", "_LINK")
    for name in names:
        con.execute(f"INSERT INTO {link}(path, {col}) VALUES (?, ?)", (path, name))


def _find(parent: dict[str, str], k: str) -> str:
    while parent[k] != k:
        parent[k] = parent[parent[k]]
        k = parent[k]
    return k


def _join_albums(con: sqlite3.Connection) -> None:
    """The album join ([library] join_albums): one album name spread over several keys whose tracks share a folder
    becomes one album, on the key with the most tracks (a tie: the key that sorts first)."""
    albums = [a for (a,) in con.execute("SELECT DISTINCT album FROM MEDIA_TABLE WHERE album <> ''")]
    for album in albums:
        rows = con.execute(
            "SELECT rowid, path, album_key FROM MEDIA_TABLE WHERE album = ?", (album,)
        ).fetchall()
        keys = {k for _, _, k in rows}
        if len(keys) < 2:
            continue
        parent = {k: k for k in keys}

        by_folder: dict[bytes, str] = {}
        for _, path, k in rows:
            f = _ascii_lower(folder_of(path).encode())
            if f in by_folder:
                parent[_find(parent, k)] = _find(parent, by_folder[f])
            else:
                by_folder[f] = k
        count: dict[str, int] = {}
        for _, _, k in rows:
            count[k] = count.get(k, 0) + 1
        best: dict[str, str] = {}
        for k in sorted(keys):
            r = _find(parent, k)
            if r not in best or count[k] > count[best[r]]:
                best[r] = k
        for rowid, _, k in rows:
            new = best[_find(parent, k)]
            if new != k:
                con.execute("UPDATE MEDIA_TABLE SET album_key = ? WHERE rowid = ?", (new, rowid))
        con.execute("DELETE FROM ALBUM_GROUP_TABLE WHERE album = ?", (album,))
        con.execute(
            "INSERT OR IGNORE INTO ALBUM_GROUP_TABLE(album, album_key, sortkey)"
            " SELECT album, album_key, ? FROM MEDIA_TABLE WHERE album = ? GROUP BY album_key",
            (sort_key(album), album),
        )


@dataclass
class IndexStats:
    tracks: int = 0
    read: int = (
        0  # files whose tags were read from the card (the rest: an unchanged row or `known`)
    )
    folders: int = 0


# path in the database -> (size, mtime, tags) of a file whose tags are known without reading the card
KnownTags = dict[str, tuple[int, int, Tags]]
PROGRESS_EVERY = 200  # files


def rebuild(
    con: sqlite3.Connection,
    mount: Path,
    old_db: Path | None = None,
    known: KnownTags | None = None,
) -> IndexStats:
    """Replace the index tables of an open library.db (inside the caller's transaction) with the files on the
    card. A file whose path, size and mtime match its old MEDIA_TABLE row (of `old_db` if given: a copy of the
    database a full sync deleted) keeps that row's tag values; else one in `known` (tags read when we wrote the
    file) takes those. Only the rest is read from the card (13k files over USB took over an hour), and
    goes into `known` so the next rebuild doesn't read it again. Reports `report_progress(files, total)`."""
    stats = IndexStats()
    known = {} if known is None else known
    rules = Rules.parse(
        (con.execute("SELECT rules FROM ORGANIZE_STATE WHERE id = 0").fetchone() or (None,))[0]
    )
    old: dict[str, tuple[Any, ...]] = {}
    old_folders: dict[str, tuple[int, int, int]] = {}
    src = con
    if old_db is not None:
        src = sqlite3.connect(f"file:{old_db}?mode=ro", uri=True)
    try:
        for row in src.execute(f"SELECT {MEDIA_COLUMNS} FROM MEDIA_TABLE"):
            old[row[1]] = row
        for path, mtime, files, names in src.execute(
            "SELECT path, mtime, files, names FROM FOLDER_TABLE"
        ):
            old_folders[path] = (mtime, files, names)
    except sqlite3.Error:
        if src is con:
            raise
    finally:
        if src is not con:
            src.close()
    for table in INDEX_TABLES:
        con.execute(f"DELETE FROM {table}")
    folders = list(walk(mount))
    total = sum(len(f.files) for f in folders)
    report_progress(0, total)
    seen = 0
    on_card: set[str] = set()
    for folder in folders:
        stats.folders += 1
        for e in folder.files:
            seen += 1
            if seen % PROGRESS_EVERY == 0:
                report_progress(seen, total)
            if e.name.lower().endswith(".cue"):
                continue  # CUE sheets: the player's Detect changes indexes them (no FOLDER_TABLE row below)
            rel = f"{folder.rel}/{e.name}" if folder.rel else e.name
            path = f"{VOLUME}/{rel}"
            on_card.add(path)
            st = e.stat()
            prev = old.get(path)
            if prev is not None and prev[14] == st.st_size and prev[27] == int(st.st_mtime):
                t = Tags(
                    title=prev[2], album=prev[3], artist=prev[4], genre=prev[5], year=prev[6], track=prev[7],
                    sample_rate=prev[15], bits=prev[17], alac=prev[19] == 8, disc=prev[29],
                    album_artist="" if prev[25] == prev[4] else prev[25],
                )  # fmt: skip
                if t.title == Path(path).stem:
                    t.title = ""
            elif (
                (k := known.get(path)) is not None
                and k[0] == st.st_size
                and k[1] == int(st.st_mtime)
            ):
                t = k[2]
            else:
                t = read_tags(Path(e.path))
                known[path] = (st.st_size, int(st.st_mtime), t)
                stats.read += 1
            stats.tracks += 1
            row = media_row(stats.tracks, path, st, t)
            con.execute(
                f"INSERT OR REPLACE INTO MEDIA_TABLE({MEDIA_COLUMNS}) VALUES ({','.join('?' * 31)})",
                row,
            )
            album, artist, genre, album_artist, key = row[3], row[4], row[5], row[25], row[30]
            if album:
                con.execute(*_lookup("ALBUM_TABLE", "album", album))
                con.execute(
                    "INSERT OR IGNORE INTO ALBUM_GROUP_TABLE(album, album_key, sortkey) VALUES (?, ?, ?)",
                    (album, key, sort_key(album)),
                )
            _file_names(con, path, artist, "ARTIST_TABLE", "artist", rules.artist)
            _file_names(con, path, genre, "GENRE_TABLE", "genre", rules.genre)
            _file_names(
                con, path, album_artist, "ALBUM_ARTIST_TABLE", "album_artist", rules.album_artist
            )
        if not folder.has_cue:
            files, names = folder_signature(folder.entries)
            fpath = f"{VOLUME}/{folder.rel}" if folder.rel else VOLUME
            mtime = folder.mtime
            prev_folder = old_folders.get(fpath)
            if prev_folder is not None and prev_folder[1:] == (files, names):
                # same entries: keep the stored time, so writing our own files (manifest, databases) into a
                # folder does not change the database and force a rewrite on the card
                mtime = prev_folder[0]
            con.execute(
                "INSERT OR REPLACE INTO FOLDER_TABLE(path, mtime, files, names) VALUES (?, ?, ?, ?)",
                (fpath, mtime, files, names),
            )
    if rules.join:
        _join_albums(con)
    for gone in [p for p in known if p not in on_card]:
        del known[gone]  # files no longer on the card: the cache never outgrows it
    report_progress(total, total)
    con.execute("INSERT INTO COUNT_TABLE(cn) VALUES (?)", (stats.tracks,))
    for sql in INDEXES:
        con.execute(sql)
    return stats
