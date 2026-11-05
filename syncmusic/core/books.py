# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-file audiobooks as one book.

A library lists an audiobook as one track per file (ABS `{item}:{ino}`, folder: one per file, grouped by
`album_id`). A stock iPod shows every file as a separate book, so for devices with `combine_audiobooks` the planner
swaps the selected files of a book for one **book track**: `item_id` = the book's `album_id`, the parts (as track
dumps, in chapter order) under `extra["book_parts"]`. The preparer downloads the parts and merges them into one
AAC `.m4b` with a chapter per file (`merge_book`); the processor chain then runs on that file as usual.

Book tracks are derived, never listed in the library: `LibrarySnapshot.books` holds them (rules, browsing and
counts see the files), `LibrarySnapshot.track()` finds either. Reverse sync works on the book's time: ABS maps a
book id (no `:ino`) to the whole item (start 0)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from syncmusic.core.models import ItemRef, MediaKind, Track
from syncmusic.infra import ffmpeg

BOOK_PARTS = "book_parts"
BOOK_PART = "book_part"  # [k, n] of a book split into n files
# FAT32 holds files below 4 GiB: a bigger book is split into parts at file boundaries (margin for the estimate)
MAX_BOOK_BYTES = 3_800 * 1000**2
# speech profile: a re-encoded book is mono AAC at most 64 kbps - 2.5x faster to encode on the Pi 5
# than 128k stereo (63x vs 25x realtime) and half the size; the iPod plays mono AAC books with bookmarks
DEFAULT_KBPS = 64
MAX_KBPS = 64


def _order(t: Track) -> tuple[int, int, str]:
    n = t.chapter_index if t.chapter_index is not None else t.track_no
    return (t.disc_no or 0, n if n is not None else 10**6, t.path or t.title)


def book_tracks(tracks: Mapping[str, Track]) -> dict[str, Track]:
    """The book track of every audiobook of `tracks` (by item id) that has more than one file; a book above
    MAX_BOOK_BYTES as several part tracks (`split`)."""
    groups: dict[str, list[Track]] = {}
    for t in tracks.values():
        if t.kind is MediaKind.AUDIOBOOK and t.album_id:
            groups.setdefault(t.album_id, []).append(t)
    out: dict[str, Track] = {}
    for book_id, parts in groups.items():
        if len(parts) < 2 or book_id in tracks:
            continue
        parts.sort(key=_order)
        if sum(p.size_bytes for p in parts) <= MAX_BOOK_BYTES:
            out[book_id] = _book(book_id, parts)
            continue
        groups_ = split(parts)
        if groups_ is None:  # a single file above the limit: the book stays as files
            continue
        for k, group in enumerate(groups_, 1):
            part = _book(book_id, group, (k, len(groups_)))
            out[part.ref.item_id] = part
    return out


def split(parts: Sequence[Track]) -> list[list[Track]] | None:
    """The files of a too-big book cut into consecutive groups of about equal size, each at most
    MAX_BOOK_BYTES; None when one file alone is bigger."""
    if any(p.size_bytes > MAX_BOOK_BYTES for p in parts):
        return None
    total = sum(p.size_bytes for p in parts)
    target = total / -(-total // MAX_BOOK_BYTES)
    out: list[list[Track]] = [[]]
    size = 0
    for p in parts:
        if out[-1] and (size + p.size_bytes > MAX_BOOK_BYTES or size >= target):
            out.append([])
            size = 0
        out[-1].append(p)
        size += p.size_bytes
    return out


def part_id(book_id: str, files: Sequence[Track]) -> str:
    """A part's item id: `{book}:+{sub}+{sub}…` (the `+` keeps a one-file part apart from that file) when the files are `{book}:{sub}` (ABS: their inos, so the
    library maps positions onto the part's span), else `{book}#{first file}`."""
    prefix = f"{book_id}:"
    subs = [f.ref.item_id.removeprefix(prefix) for f in files]
    if all(f.ref.item_id.startswith(prefix) for f in files) and not any("+" in s for s in subs):
        return f"{book_id}:+{'+'.join(subs)}"
    return f"{book_id}#{files[0].ref.item_id}"


def _book(book_id: str, parts: list[Track], part: tuple[int, int] | None = None) -> Track:
    first = parts[0]
    title = first.album or first.series or first.title
    extra: dict[str, object] = {
        **first.extra,
        BOOK_PARTS: [p.model_dump(mode="json") for p in parts],
    }
    if part is not None:
        title = f"{title} (part {part[0]}/{part[1]})"
        book_id = part_id(book_id, parts)
        extra[BOOK_PART] = list(part)
    durations = [p.duration_s or 0.0 for p in parts]
    positions = [p.position_s for p in parts]
    folder = (first.path or "").rpartition("/")[0]
    state = hashlib.sha256("|".join(f"{p.ref.item_id}={p.source_state}" for p in parts).encode())
    return first.model_copy(
        update={
            "ref": ItemRef(library_id=first.ref.library_id, item_id=book_id),
            "title": title,
            "track_no": part[0] if part else None,  # parts in order (core/positions.py)
            "disc_no": None,
            "disc_total": None,
            "chapter_index": None,
            "duration_s": sum(durations) or None,
            "size_bytes": sum(p.size_bytes for p in parts),
            "format": "aac",
            "codec": "aac",
            "bitrate_kbps": book_kbps(parts),
            "sample_rate": None,
            "bit_depth": None,
            "path": f"{folder}/{title.replace('/', ' of ')}.m4b".lstrip("/"),
            "has_cover": any(p.has_cover for p in parts),
            "has_lyrics": False,
            # the library clamps each file's position to the file: their sum is the book's position
            "position_s": sum(p or 0.0 for p in positions)
            if any(p is not None for p in positions)
            else None,
            "finished": all(p.finished for p in parts),
            "play_count": max(p.play_count for p in parts),
            "source_state": state.hexdigest()[:24],
            "extra": extra,
        }
    )


def parts_of(book: Track) -> list[Track]:
    return [Track.model_validate(d) for d in book.extra.get(BOOK_PARTS) or []]


def is_book(track: Track) -> bool:
    return bool(track.extra.get(BOOK_PARTS))


def book_kbps(parts: Sequence[Track]) -> int:
    """AAC bit rate of the merged book: the parts' highest (lossy) bit rate, at most MAX_KBPS."""
    rates = [p.bitrate_kbps for p in parts if p.bitrate_kbps]
    return min(max(rates), MAX_KBPS) if rates else DEFAULT_KBPS


def merge_selection(
    items: Mapping[str, list[str]], books_by_part: Mapping[str, Track]
) -> dict[str, list[str]]:
    """Selected keys (`lib/item` → rule ids) with every file of a book replaced by the book's key, the rules of
    its files united. A book is taken when any of its files is selected."""
    out: dict[str, list[str]] = {}
    for key, rules in items.items():
        book = books_by_part.get(key)
        k = book.ref.key() if book is not None else key
        merged = out.setdefault(k, [])
        merged.extend(r for r in rules if r not in merged)
    return out


def chapter_title(part: Track, n: int) -> str:
    return part.title or f"Chapter {n}"


def _meta_escape(s: str) -> str:
    for c in "\\=;#\n":
        s = s.replace(c, "\\" + c)
    return s


def _concat_quote(path: str) -> str:
    return path.replace("'", "'\\''")


def ffmetadata(book: Track, chapters: Sequence[tuple[str, float]]) -> str:
    """ffmpeg metadata file: the book's tags and one chapter per (title, duration)."""
    lines = [";FFMETADATA1"]
    tags = {
        "title": book.title,
        "album": book.album or book.title,
        "artist": book.author or book.artist,
        "album_artist": book.author or book.album_artist,
        "genre": "Audiobook",
        "date": str(book.year) if book.year else None,
        "media_type": "2",  # iTunes stik: audiobook
    }
    lines += [f"{k}={_meta_escape(v)}" for k, v in tags.items() if v]
    start = 0
    for title, duration in chapters:
        end = start + max(int(round(duration * 1000)), 1)
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}"]
        lines.append(f"title={_meta_escape(title)}")
        start = end
    return "\n".join(lines) + "\n"


async def merge_book(
    book: Track,
    files: Sequence[Path],
    out: Path,
    on_time: Callable[[float], None] | None = None,
) -> list[tuple[str, float]]:
    """Merge the parts' `files` (chapter order) into the AAC `.m4b` `out`, one chapter per file. Copies the AAC
    streams when every part is AAC with the same sample rate and channels, else re-encodes at the speech profile
    (mono, at most MAX_KBPS). `on_time` gets the seconds of the book merged so far. Returns the chapters
    (title, duration)."""
    parts = parts_of(book)
    probes = await asyncio.gather(*(ffmpeg.probe(f) for f in files))
    chapters = [
        (chapter_title(p, n), pr.duration_s or p.duration_s or 0.0)
        for n, (p, pr) in enumerate(zip(parts, probes, strict=True), 1)
    ]
    meta = out.with_name("chapters.txt")
    await asyncio.to_thread(meta.write_text, ffmetadata(book, chapters), "utf-8")
    copy = (
        len({(pr.codec, pr.sample_rate, pr.channels) for pr in probes}) == 1
        and probes[0].codec == "aac"
    )
    if copy:
        listing = out.with_name("parts.txt")
        text = "".join(f"file '{_concat_quote(str(f.resolve()))}'\n" for f in files)
        await asyncio.to_thread(listing.write_text, text, "utf-8")
        args = ["-f", "concat", "-safe", "0", "-i", ffmpeg.fpath(listing)]
        args += ["-i", ffmpeg.fpath(meta), "-map", "0:a", "-c:a", "copy"]
        meta_idx = 1
    else:
        args = []
        for f in files:
            args += ["-i", ffmpeg.fpath(f)]
        meta_idx = len(files)
        args += ["-i", ffmpeg.fpath(meta)]
        chains = [
            f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=mono[a{i}]"
            for i in range(len(files))
        ]
        joined = "".join(f"[a{i}]" for i in range(len(files)))
        graph = ";".join([*chains, f"{joined}concat=n={len(files)}:v=0:a=1[out]"])
        args += ["-filter_complex", graph, "-map", "[out]"]
        args += ["-c:a", ffmpeg.aac_encoder(), "-b:a", f"{book.bitrate_kbps or DEFAULT_KBPS}k"]
    args += ["-map_metadata", str(meta_idx), "-map_chapters", str(meta_idx), "-vn"]
    args += ["-movflags", "+faststart", "-f", "ipod", ffmpeg.fpath(out)]
    if on_time is None:
        await ffmpeg.run_ffmpeg(args)
    else:
        await ffmpeg.run_ffmpeg_progress(args, on_time)
    return chapters
