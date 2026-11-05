# SPDX-License-Identifier: GPL-3.0-or-later
"""Processor preview on one track: run a device's chain on one library item and compare.

The result goes through the processing cache like a sync (a later sync of that item reuses it); a copy of the file is
kept under `<cache_dir>/chain-preview/<token>/` for Download result - only the newest `KEEP` previews stay, older
ones are deleted when a new one is made. The comparison is a table
of rows {key, before, after, changed}: before from the library's track, after from ffprobe of the result."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.library import Library
from syncmusic.core.models import LibrarySnapshot, MediaKind, Track
from syncmusic.core.prepare import Chain, Preparer, initial_plan_item
from syncmusic.infra import ffmpeg
from syncmusic.infra.ffmpeg import ProbeResult

KEEP = 3
FOLDER = "chain-preview"
SAMPLES = 8


def sample_tracks(snapshots: Mapping[str, LibrarySnapshot], limit: int = SAMPLES) -> list[Track]:
    """Tracks to offer in the picker: one per format first (a FLAC, an MP3, …), then more music, by title."""
    tracks = sorted(
        (t for s in snapshots.values() for t in s.tracks.values()),
        key=lambda t: (t.kind is not MediaKind.MUSIC, t.title.casefold(), t.ref.key()),
    )
    picked: list[Track] = []
    formats: set[str] = set()
    for t in tracks:
        if t.format not in formats:
            formats.add(t.format)
            picked.append(t)
    for t in tracks:
        if len(picked) >= limit:
            break
        if t not in picked:
            picked.append(t)
    return picked[:limit]


def search_tracks(
    snapshots: Mapping[str, LibrarySnapshot], query: str, limit: int = 30
) -> list[Track]:
    """Any item whose title, artist, album, author or series contains `query` (case and accents as typed;
    the samples alone cannot reach a particular track, a podcast or one with lyrics), by title."""
    q = query.casefold().strip()
    found = [
        t
        for s in snapshots.values()
        for t in s.tracks.values()
        if q
        and any(q in (v or "").casefold() for v in (t.title, t.artist, t.album, t.author, t.series))
    ]
    found.sort(key=lambda t: (t.title.casefold(), t.ref.key()))
    return found[:limit]


def label(t: Track) -> str:
    """'Зозуля - Скрябін (FLAC)'."""
    who = t.artist or t.author or t.series
    return f"{t.title}{f' - {who}' if who else ''} ({(t.format or '?').upper()})"


def _kbps(bits: int | None) -> str:
    return f"{round(bits / 1000)} kbit/s" if bits else "-"


def _size(n: int | None) -> str:
    if not n:
        return "-"
    v, unit = float(n), "B"
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1000 or unit == "GB":
            break
        v /= 1000
    return f"{v:.0f} {unit}" if unit == "B" or v >= 100 else f"{v:.1f} {unit}"


def _audio(fmt: str | None, sample_rate: int | None, bit_depth: int | None) -> str:
    parts = [(fmt or "?").upper()]
    if sample_rate:
        parts.append(f"{sample_rate / 1000:g} kHz")
    if bit_depth:
        parts.append(f"{bit_depth} bit")
    return " · ".join(parts)


def _codec(probe: ProbeResult) -> str:
    codec = probe.codec or ""
    return {"pcm_s16le": "wav", "pcm_s24le": "wav", "vorbis": "ogg"}.get(codec, codec)


def rows(track: Track, after: ProbeResult, sidecars: list[str]) -> list[dict[str, Any]]:
    """The before / after table; `changed` marks the values the chain changed (shown bold)."""
    tags = after.tags
    track_no = (tags.get("track") or "").split("/")[0] or None
    # the lyric sidecar only: a cover.jpg next to the file is the cover step's
    lrc = [s for s in sidecars if s.lower() == ".lrc"]
    covers = [s for s in sidecars if s.lower() != ".lrc"]
    lyrics_after = (
        "in the file"
        if any(k in tags for k in ("lyrics", "unsyncedlyrics", "lyrics-xxx"))
        else ".lrc file"
        if lrc
        else "none"
    )
    cover_after = ", ".join(
        ([] if not after.has_cover else ["in the file"]) + [f"{c} file" for c in covers]
    )
    table = [
        (
            "Format",
            _audio(track.format, track.sample_rate, track.bit_depth),
            _audio(_codec(after), after.sample_rate, after.bit_depth),
        ),
        ("Bitrate", _kbps((track.bitrate_kbps or 0) * 1000), _kbps(after.audio_bitrate)),
        ("Size", _size(track.size_bytes), _size(after.size_bytes)),
        ("Cover", "in the file" if track.has_cover else "none", cover_after or "none"),
        ("Title", track.title, tags.get("title") or "-"),
        ("Artist", track.artist or "-", tags.get("artist") or "-"),
        ("Album", track.album or "-", tags.get("album") or "-"),
        ("Track", str(track.track_no) if track.track_no else "-", track_no or "-"),
        ("Lyrics", "yes" if track.has_lyrics else "none", lyrics_after),
    ]
    return [{"key": k, "before": a, "after": b, "changed": a != b} for k, a, b in table]


def prune(root: Path, keep: int = KEEP) -> None:
    """Delete all but the newest `keep` previews."""
    if not root.is_dir():
        return
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    for d in dirs[: max(0, len(dirs) - keep)]:
        shutil.rmtree(d, ignore_errors=True)


def result_file(cache_dir: Path, token: str, name: str) -> Path | None:
    """The kept result of a preview, or None (unknown token / name, or already pruned)."""
    root = (cache_dir / FOLDER).resolve()
    path = (root / token / name).resolve()
    if not path.is_relative_to(root) or len(path.relative_to(root).parts) != 2:
        return None
    return path if path.is_file() else None


async def run(
    preparer: Preparer,
    library: Library,
    track: Track,
    chain: Chain,
    caps: DeviceCapabilities,
    cache_dir: Path,
) -> dict[str, Any]:
    """Process `track` with `chain` for a device of `caps`; {rows, steps, file, token, size}."""
    item = chain.plan(initial_plan_item(track), caps)
    root = cache_dir / FOLDER
    token = uuid.uuid4().hex[:16]
    async with preparer.prepared(library, item, chain, caps) as work:
        name = f"{_safe(track.title)}.{item.file.ext or work.path.suffix.lstrip('.')}"
        out = root / token / name
        await asyncio.to_thread(out.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, work.path, out)
        sidecars = sorted(work.sidecars)
        steps = list(work.steps_done)
    await asyncio.to_thread(prune, root)
    probe = await ffmpeg.probe(out)
    return {
        "rows": rows(track, probe, sidecars),
        "steps": steps,
        "token": token,
        "file": name,
        "size": probe.size_bytes,
    }


def _safe(title: str) -> str:
    """A file name from a title (no path separators or control characters)."""
    cleaned = "".join("_" if c in '/\\:*?"<>|' or ord(c) < 32 else c for c in title).strip(" .")
    return cleaned[:80] or "track"
