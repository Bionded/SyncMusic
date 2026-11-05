"""Synthetic Rockbox tagcache writer for tests (layout in syncmusic/devices/Rockbox/rockbox_feedback.py)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from syncmusic.devices.Rockbox import rockbox_feedback as rf


@dataclass
class Row:
    filename: str  # absolute on the player, e.g. "/Music/a.mp3"
    playcount: int = 0
    rating: int = 0
    lastplayed: int = 0
    length_ms: int = 200_000
    playtime_ms: int = 0
    flag: int = 0


def write_tagcache(rb_dir: Path, rows: list[Row], order: str = "<") -> None:
    """database_idx.tcd + database_4.tcd (file names) + empty files for the other string tags."""
    rb_dir.mkdir(parents=True, exist_ok=True)
    names = b""
    seeks = []
    for i, r in enumerate(rows):
        data = r.filename.encode() + b"\0"
        data += b"X" * (-len(data) % 4)  # Rockbox pads to 4-byte alignment
        seeks.append(rf.TAG_HEADER + len(names))
        names += struct.pack(f"{order}ii", len(data), i) + data
    hdr = struct.pack(f"{order}iii", rf.TAGCACHE_MAGIC, len(names), len(rows))
    (rb_dir / f"database_{rf.TAG_FILENAME}.tcd").write_bytes(hdr + names)
    empty = struct.pack(f"{order}iii", rf.TAGCACHE_MAGIC, 0, 0)
    for t in (0, 1, 2, 3, 5, 6, 7, 8, 12):
        (rb_dir / f"database_{t}.tcd").write_bytes(empty)
    body = b""
    for r, seek in zip(rows, seeks, strict=True):
        tags = [rf.TAG_HEADER] * rf.TAG_COUNT
        tags[rf.TAG_FILENAME] = seek
        for t in (9, 10, 11, 13, 19, 20, 21, 22):
            tags[t] = 0
        tags[rf.TAG_LENGTH] = r.length_ms
        tags[rf.TAG_PLAYCOUNT] = r.playcount
        tags[rf.TAG_RATING] = r.rating
        tags[rf.TAG_PLAYTIME] = r.playtime_ms
        tags[rf.TAG_LASTPLAYED] = r.lastplayed
        body += struct.pack(f"{order}{rf.TAG_COUNT + 1}i", *tags, r.flag)
    serial = max((r.lastplayed for r in rows), default=0) + 1
    master = struct.pack(f"{order}6i", rf.TAGCACHE_MAGIC, len(body), len(rows), serial, 1, 0)
    (rb_dir / rf.MASTER_FILE).write_bytes(master + body)
