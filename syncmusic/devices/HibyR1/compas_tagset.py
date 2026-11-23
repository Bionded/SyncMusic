# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: ASYNC240  (build runs in its own event loop inside a worker thread: blocking file work is fine)
"""The Compás player's library database, built on the server for a Wi-Fi client (the SyncMusic app on the player).

The app cannot run the player's indexer itself, so the server builds the database over exactly the files the card
holds: a mirror tree of hard links to them (compas-sdtool skips symlinks; a copy where a link cannot be made), each
file's mtime rounded down to an even second (FAT keeps 2 s steps, so the mtime the app gives the file on the card is
the same), the Podcasts/Audiobooks folders kept out of the music library like on the card (database.ignore), then
`compas-sdtool scan`. The new generation's files (generation 1: the app installs the set as a whole new database)
are the generated set; before it is offered, the set is opened with `compas-sdtool info` on a scratch card holding
only it, as the player would load it.

Incremental build: with the card's own current database (`base`, as the app uploaded it), the mirror also gets that
database and an empty sparse stand-in (`stubs`: the size and mtime the card's row was read at) for every card file
the server holds no copy of. The indexer reads a file only when its size or mtime differs from its row, so it reads
just the new files and keeps every other row; it writes generation G = card generation + 1, and the set is G's files,
its refs file and the older tables G reuses. A stand-in the indexer did read anyway would become a song without tags,
so such a build is refused.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
from pathlib import Path
from typing import BinaryIO

from syncmusic.devices.HibyR1 import compas_feedback as cf
from syncmusic.devices.HibyR1 import compas_sdtool as sd

GEN_FILE = re.compile(r"^database_(?:idx|\d+)\.tcd\.g(\d+)(?:\..+)?$")
IGNORE_FILE = "database.ignore"
IGNORED_DIRS = ("Podcasts", "Audiobooks")
MANIFEST = "manifest.json"
# a tag copy keeps the tags and this much audio after them (stream parameters, the first frames) and the file's end
TAG_COPY_AUDIO = 64 * 1024
TAG_COPY_TAIL = 128 * 1024


class TagsetError(Exception):
    """The set could not be built or does not load like the player would load it."""


class StandInRead(TagsetError):
    """The indexer read stand-ins of the card's database rows: those rows do not match the card's files."""


def even_mtime(p: Path) -> int:
    """Round the file's mtime down to an even second (in place) and return it."""
    st = p.stat()
    mt = int(st.st_mtime) // 2 * 2
    if st.st_mtime_ns != mt * 1_000_000_000:
        os.utime(p, (mt, mt))
    return mt


Stub = tuple[int, int]  # (size, mtime) of a card file the server holds no copy of
Synth = tuple[
    dict[str, str], float, int, int
]  # (metadata, duration s, size, mtime): see synthetic()
TCD_FILE = re.compile(
    r"^(?:database_(?:idx|\d+)\.tcd(?:\.g\d+)?(?:\..+)?|tagcache\.refs\.g\d+|tagcache\.gen)$"
)


def set_id(
    files: dict[str, Path],
    stubs: dict[str, Stub] | None = None,
    base: Path | None = None,
    synth: dict[str, Synth] | None = None,
) -> str:
    """The same card contents (path, size, even mtime) give the same id, so a set is built once."""
    rows: list[object] = [
        [rel, p.stat().st_size, even_mtime(p)] for rel, p in sorted(files.items())
    ]
    rows += [[rel, size, mtime, "stub"] for rel, (size, mtime) in sorted((stubs or {}).items())]
    rows += [[rel, *spec, "synth"] for rel, spec in sorted((synth or {}).items())]
    if base is not None:
        h = hashlib.sha256()
        for p in sorted(base.iterdir()):
            if p.is_file() and TCD_FILE.match(p.name):
                h.update(p.name.encode() + b"\0" + p.read_bytes())
        rows.append(h.hexdigest())
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:32]


def _audio_start(f: BinaryIO, size: int) -> int:
    """Where the audio data starts after an ID3v2 tag or FLAC metadata blocks (0 for other files)."""
    f.seek(0)
    head = f.read(10)
    if head[:3] == b"ID3" and len(head) == 10:
        n = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        return 10 + n + (10 if head[5] & 0x10 else 0)
    if head[:4] == b"fLaC":
        pos = 4
        while pos + 4 <= size:
            f.seek(pos)
            b = f.read(4)
            pos += 4 + int.from_bytes(b[1:4], "big")
            if b[0] & 0x80:
                break
        return pos
    return 0


def _mp4_ranges(f: BinaryIO, size: int) -> list[tuple[int, int]]:
    """Every top-level MP4 box whole (the tags and the sample tables, wherever they are) but the media data."""
    out: list[tuple[int, int]] = []
    pos = 0
    while pos + 8 <= size:
        f.seek(pos)
        b = f.read(16)
        n, kind = struct.unpack(">I4s", b[:8])
        if n == 1 and len(b) == 16:
            n = struct.unpack(">Q", b[8:16])[0]
        elif n == 0:
            n = size - pos
        if n < 8:
            return [(0, size)]
        out.append((pos, pos + (min(n, TAG_COPY_AUDIO) if kind == b"mdat" else n)))
        pos += n
    return out


def tag_ranges(src: Path) -> list[tuple[int, int]]:
    """The byte ranges of an audio file the player's indexer reads: its tags, the stream headers and first frames
    and the end of the file (ID3v1/APE tags, the last Ogg page)."""
    size = src.stat().st_size
    with src.open("rb") as f:
        f.seek(4)
        if f.read(4) == b"ftyp":
            ranges = _mp4_ranges(f, size)
        else:
            ranges = [(0, _audio_start(f, size) + TAG_COPY_AUDIO)]
    ranges.append((size - TAG_COPY_TAIL, size))
    return [(max(0, a), min(size, b)) for a, b in ranges if min(size, b) > max(0, a)]


def tag_copy(src: Path, dest: Path) -> None:
    """A sparse copy of `src` holding only what the indexer reads (`tag_ranges`), with the same size and mtime:
    the database built over it is the one built over the whole file, at a fraction of the disk."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    st = src.stat()
    with src.open("rb") as i, tmp.open("wb") as o:
        for a, b in tag_ranges(src):
            i.seek(a)
            o.seek(a)
            o.write(i.read(b - a))
        o.truncate(st.st_size)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    tmp.replace(dest)


def shrink_tag_copies(root: Path) -> int:
    """Turn whole files under `root` (kept by older versions) into tag copies; the bytes freed. Blocking."""
    freed = 0
    if not root.is_dir():
        return 0
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink() or p.name.startswith("."):
            continue
        st = p.stat()
        kept = sum(b - a for a, b in tag_ranges(p))
        if st.st_blocks * 512 <= kept + 64 * 1024:
            continue  # already a tag copy (or a small file)
        tag_copy(p, p)
        if st.st_nlink == 1:  # a hard link's data stays with its other name
            freed += st.st_blocks * 512 - p.stat().st_blocks * 512
    return freed


def _link(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


async def build(
    files: dict[str, Path],
    out: Path,
    tool: Path,
    base: Path | None = None,
    stubs: dict[str, Stub] | None = None,
    synth: dict[str, Synth] | None = None,
) -> dict[str, object]:
    """Build the set for `files` (card path -> file on the server) into `out`, incrementally over the card's
    database `base` with `stubs` for the card files the server holds no copy of, and stand-ins made from `synth`
    (see synthetic) for files neither has; its manifest."""
    out.mkdir(parents=True, exist_ok=True)
    info = await sd.info(tool, out)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".mirror-") as tmp:
        mirror = Path(tmp)
        db = mirror / cf.COMPAS_DIR
        start = 0
        if base is not None:
            db.mkdir()
            for p in base.iterdir():
                if p.is_file() and TCD_FILE.match(p.name):
                    shutil.copyfile(p, db / p.name)
            start = cf.generation(db)
        for rel, src in files.items():
            even_mtime(src)
            _link(src, mirror / rel)
        for rel, (size, mtime) in (stubs or {}).items():
            stub = mirror / rel
            stub.parent.mkdir(parents=True, exist_ok=True)
            with stub.open("wb") as f:
                f.truncate(size)
            os.utime(stub, (mtime, mtime))
        made = sum(synthetic(mirror / rel, *spec) for rel, spec in (synth or {}).items())
        for folder in IGNORED_DIRS:
            (mirror / folder).mkdir(parents=True, exist_ok=True)
            (mirror / folder / IGNORE_FILE).touch()
        done = await sd.scan(tool, mirror)
        gen = cf.generation(db)
        if gen != start + 1:
            raise TagsetError(f"compas-sdtool built generation {gen}, expected {start + 1}")
        read = int(done.get("parsed") or 0) + int(done.get("failed") or 0)
        if read > len(files) + made:
            raise StandInRead(
                f"compas-sdtool read {read} files, only {len(files) + made} are real: the card's database does "
                "not match the files it lists"
            )
        keep = {gen} | {g for g in (cf.ref_sources(db, gen) or []) if 0 < g < gen}
        names = []
        for p in sorted(db.iterdir()):
            m = GEN_FILE.match(p.name)
            ok = m is not None and (
                int(m.group(1)) == gen
                or (int(m.group(1)) in keep and not p.name.startswith("database_idx"))
            )
            if ok or (gen > 1 and p.name == f"tagcache.refs.g{gen}"):
                shutil.copyfile(p, out / p.name)
                names.append(p.name)
    await _check(tool, out, names, done.get("songs"), gen)
    manifest = {
        "database_format": info.database_format,
        "sdtool": f"{info.version} {info.build}",
        "songs": done.get("songs"),
        "generation": gen,
        "read": read,
        "files": names,
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1))
    return manifest


async def _check(tool: Path, out: Path, names: list[str], songs: object, gen: int = 1) -> None:
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".check-") as tmp:
        db = Path(tmp) / cf.COMPAS_DIR
        db.mkdir()
        for n in names:
            shutil.copyfile(out / n, db / n)
        (db / "tagcache.gen").write_text(str(gen))
        events = await sd._run(tool, ["info", tmp], sd.INFO_TIMEOUT_S)
    got = next((e for e in reversed(events) if "generation" in e), {})
    if got.get("generation") != gen or got.get("songs") != songs:
        raise TagsetError(
            f"the built database does not load: {got!r}, expected {gen} / {songs} songs"
        )


def build_cached(
    files: dict[str, Path],
    cache: Path,
    tool: Path,
    base: Path | None = None,
    stubs: dict[str, Stub] | None = None,
    synth: dict[str, Synth] | None = None,
) -> tuple[str, Path, list[str]]:
    """The set for `files` (and `base`/`stubs`, see build) from `cache/<set id>/`, built when missing (older sets
    are removed). Blocking: run it in a worker thread."""
    sid = set_id(files, stubs, base, synth)
    out = cache / sid
    delivered = _delivered_unchanged(files, cache, base, stubs, synth)
    if delivered is not None:
        sid, out = delivered.name, delivered
    if not (out / MANIFEST).is_file():
        cache.mkdir(parents=True, exist_ok=True)
        tmp = cache / f".{sid}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            asyncio.run(build(files, tmp, tool, base, stubs, synth))
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        manifest = json.loads((tmp / MANIFEST).read_text())
        manifest["inputs"] = set_id(files, stubs, None, synth)
        (tmp / MANIFEST).write_text(json.dumps(manifest, indent=1))
        tmp.rename(out)
        for old in cache.iterdir():
            if old != out and old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
    names = json.loads((out / MANIFEST).read_text())["files"]
    return sid, out, list(names)


def _delivered_unchanged(
    files: dict[str, Path],
    cache: Path,
    base: Path | None,
    stubs: dict[str, Stub] | None,
    synth: dict[str, Synth] | None,
) -> Path | None:
    """The cached set when the card's database is that very set (the client uploads it again after installing it)
    and the card's files are the ones it was built for: nothing to rebuild."""
    if base is None or not cache.is_dir():
        return None
    for old in cache.iterdir():
        try:
            manifest = json.loads((old / MANIFEST).read_text())
        except (OSError, ValueError):
            continue
        if manifest.get("inputs") != set_id(files, stubs, None, synth):
            continue
        names = set(manifest["files"])
        on_card = {p.name for p in base.iterdir() if p.is_file() and TCD_FILE.match(p.name)}
        if on_card - {"tagcache.gen"} != names - {"tagcache.gen"}:
            continue
        if all((base / n).read_bytes() == (old / n).read_bytes() for n in names - {"tagcache.gen"}):
            return old
    return None


# --- from scratch: stand-ins built from what the manifest knows (no audio on the server) -------------------------

SYNTH_FORMATS = (".flac", ".mp3")  # what synthetic() can make
SYNTH_RATE = 44_100
MP3_FRAME_SAMPLES = 1152


def _vorbis_tags(meta: dict[str, str]) -> list[tuple[str, str]]:
    keys = {
        "title": "TITLE",
        "artist": "ARTIST",
        "album_artist": "ALBUMARTIST",
        "album": "ALBUM",
        "track_no": "TRACKNUMBER",
        "disc_no": "DISCNUMBER",
        "year": "DATE",
        "genres": "GENRE",
    }
    return [(k, meta[f]) for f, k in keys.items() if meta.get(f)]


def _flac_head(meta: dict[str, str], duration_s: float) -> bytes:
    from mutagen._vorbis import VComment

    samples = max(int(duration_s * SYNTH_RATE), 1)
    # STREAMINFO: block sizes 4096, frame sizes unknown, 44.1 kHz, 2 channels, 16 bit, total samples, no MD5
    packed = (SYNTH_RATE << 44) | (1 << 41) | (15 << 36) | samples
    info = struct.pack(">HH", 4096, 4096) + b"\0" * 6 + packed.to_bytes(8, "big") + b"\0" * 16
    vc = VComment()
    vc.extend(_vorbis_tags(meta))
    comment = vc.write(framing=False)

    def block(kind: int, data: bytes, last: bool) -> bytes:
        return bytes([(0x80 if last else 0) | kind]) + len(data).to_bytes(3, "big") + data

    return b"fLaC" + block(0, info, False) + block(4, comment, True)


def _mp3_head(meta: dict[str, str], duration_s: float, size: int) -> bytes:
    import io

    from mutagen import id3

    frames = max(round(duration_s * SYNTH_RATE / MP3_FRAME_SAMPLES), 1)
    # one MPEG-1 Layer III frame (128 kbit/s, 44.1 kHz, stereo: 417 bytes) carrying a Xing header with the frame
    # and byte counts: the length the player shows comes from it
    xing = b"Xing" + struct.pack(">III", 0x3, frames, size)
    frame = (b"\xff\xfb\x90\x00" + b"\0" * 32 + xing).ljust(417, b"\0")
    tags = id3.ID3()
    names = {
        "title": id3.TIT2,
        "artist": id3.TPE1,
        "album_artist": id3.TPE2,
        "album": id3.TALB,
        "track_no": id3.TRCK,
        "disc_no": id3.TPOS,
        "year": id3.TDRC,
        "genres": id3.TCON,
    }
    for f, cls in names.items():
        if meta.get(f):
            tags.add(cls(encoding=3, text=[meta[f]]))
    buf = io.BytesIO()
    tags.save(buf, v2_version=4, padding=lambda _info: 0)
    return buf.getvalue() + frame


def synthetic(
    dest: Path, meta: dict[str, str], duration_s: float | None, size: int, mtime: int
) -> bool:
    """Write a stand-in for a card file the server holds no copy of and the card's database does not list: a real
    header with the tags SyncMusic wrote and the stream length, sparse up to the file's size, with its mtime. The
    indexer builds the same row from it as from the file (FLAC and MP3; False for other formats)."""
    ext = dest.suffix.lower()
    if not duration_s or size <= 0:
        return False
    if ext == ".flac":
        head = _flac_head(meta, duration_s)
    elif ext == ".mp3":
        head = _mp3_head(meta, duration_s, size)
    else:
        return False
    if len(head) > size:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        f.write(head)
        f.truncate(size)
    os.utime(dest, (mtime, mtime))
    return True
