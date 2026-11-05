# SPDX-License-Identifier: GPL-3.0-or-later
"""Browser playback of an on-device file (Player contents ▶).

Every current browser plays MP3, AAC (in MP4 or ADTS), FLAC, Opus, Vorbis and PCM WAV; Firefox does not decode ALAC,
and nothing plays WMA or AIFF everywhere. A playable file is served as it is, with an explicit Content-Type; any
other one is transcoded once to MP3 into `<cache_dir>/preview/` (a few recent files are kept) and that copy is
served, so seeking works through ordinary range requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

from syncmusic.infra import ffmpeg

log = logging.getLogger(__name__)

KEEP = 20  # transcoded previews kept in the cache (the newest by access)

# codec → Content-Type of a file the browsers play as it is
_PLAYABLE: dict[str, str] = {
    "mp3": "audio/mpeg",
    "aac": "audio/mp4",
    "flac": "audio/flac",
    "opus": "audio/ogg",
    "vorbis": "audio/ogg",
    "pcm_s16le": "audio/wav",
    "pcm_s24le": "audio/wav",
    "pcm_u8": "audio/wav",
}

_locks: dict[Path, asyncio.Lock] = {}


def playable_type(codec: str | None, container: str) -> str | None:
    """The Content-Type to serve a file with as it is, or None when it needs a transcode."""
    if codec is None:
        return None
    media_type = _PLAYABLE.get(codec)
    if media_type is None:
        return None
    if codec == "aac" and container.startswith("aac"):
        return "audio/aac"  # ADTS
    if codec == "flac" and "ogg" in container:
        return "audio/ogg"
    return media_type


async def browser_file(path: Path, cache_dir: Path) -> tuple[Path, str]:
    """(file to serve, Content-Type) for playing `path` in a browser. Raises ffmpeg.FfmpegError when the file is
    not media or cannot be converted."""
    info = await ffmpeg.probe(path)
    media_type = playable_type(info.codec, info.container)
    if media_type is not None:
        return path, media_type
    st = await asyncio.to_thread(path.stat)
    digest = hashlib.sha256(f"{path}\0{st.st_size}\0{st.st_mtime_ns}".encode()).hexdigest()[:24]
    out = cache_dir / "preview" / f"{digest}.mp3"
    # a browser sends several (range) requests for one file: convert it once
    lock = _locks.setdefault(out, asyncio.Lock())
    async with lock:
        if not await asyncio.to_thread(out.is_file):
            await asyncio.to_thread(out.parent.mkdir, parents=True, exist_ok=True)
            tmp = out.with_suffix(".part.mp3")
            log.info("preview: %s (%s) → MP3", path.name, info.codec)
            try:
                await ffmpeg.run_ffmpeg(
                    ["-i", ffmpeg.fpath(path), "-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "2"]
                    + ["-f", "mp3", ffmpeg.fpath(tmp)]
                )
            except BaseException:
                await asyncio.to_thread(tmp.unlink, missing_ok=True)
                raise
            await asyncio.to_thread(os.replace, tmp, out)
            await asyncio.to_thread(_prune, out.parent)
        else:
            await asyncio.to_thread(os.utime, out)
    return out, "audio/mpeg"


def _prune(folder: Path) -> None:
    files = sorted(
        (p for p in folder.glob("*.mp3") if not p.name.endswith(".part.mp3")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[KEEP:]:
        old.unlink(missing_ok=True)
