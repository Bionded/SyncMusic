# SPDX-License-Identifier: GPL-3.0-or-later
"""Small cover thumbnails for the library browser and Player contents.

A thumbnail is the cover scaled and centre-cropped to a SIZE×SIZE JPEG (twice the 40 px it is shown at), cached in
`<cache_dir>/thumbs/<scope>/` as a hard link to one shared entry per picture in `<cache_dir>/thumbs/shared/`  (the same album on every player and in the library browser is scaled and stored once); the browser's artwork grid asks for LARGE (twice its 168 px cards). A source without a cover (or one that fails) leaves a `.none` marker for a day, so a
page of rows does not ask the library again on every view. At most PARALLEL covers are fetched and scaled at once:
the browser lazy-loads a whole page of rows together.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import time
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SIZE = 80
LARGE = 336
PARALLEL = 4
NONE_TTL_S = 24 * 3600
SHARED = "shared"  # content-addressed thumbnails, linked from every scope

_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _slots:
        _slots[loop] = asyncio.Semaphore(PARALLEL)
    return _slots[loop]


def embedded_cover(audio: Any) -> bytes | None:
    """The first picture in a mutagen file's tags (FLAC, ID3, MP4, Ogg), or None."""
    pictures = getattr(audio, "pictures", None)  # FLAC
    if pictures:
        return bytes(pictures[0].data)
    tags = audio.tags
    if tags is None:
        return None
    if hasattr(tags, "getall"):  # ID3
        frames = tags.getall("APIC")
        return bytes(frames[0].data) if frames else None
    covr = tags.get("covr") if hasattr(tags, "get") else None  # MP4
    if covr:
        return bytes(covr[0])
    block = tags.get("metadata_block_picture") if hasattr(tags, "get") else None  # Ogg
    if block:
        import base64

        from mutagen.flac import Picture

        return bytes(Picture(base64.b64decode(block[0])).data)  # type: ignore[no-untyped-call]
    return None


COVER_FILES = ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg")


def file_cover(path: Path) -> bytes | None:
    """The picture embedded in a media file, else a cover image next to it."""
    import mutagen

    try:
        audio = mutagen.File(path)
        data = embedded_cover(audio) if audio is not None else None
    except Exception:  # noqa: BLE001 - unreadable tags: try the folder image
        data = None
    if data:
        return data
    names = {p.name.casefold(): p for p in path.parent.iterdir() if p.is_file()}
    for name in COVER_FILES:
        if name in names:
            return names[name].read_bytes()
    return None


def _scaled(data: bytes, size: int = SIZE) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as img:
        small = ImageOps.fit(img.convert("RGB"), (size, size), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    small.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _fresh_none(marker: Path) -> bool:
    try:
        return time.time() - marker.stat().st_mtime < NONE_TTL_S
    except FileNotFoundError:
        return False


async def thumbnail(
    cache_dir: Path,
    scope: str,
    key: str,
    load: Callable[[], Awaitable[bytes | None]],
    size: int = SIZE,
) -> Path | None:
    """The cached `size`×`size` thumbnail of `key` in `scope` (a library or device id), made from `load()` (the
    cover bytes, None when there is none) on the first request; None when there is no usable cover."""
    sized = key if size == SIZE else f"{key}\0{size}"
    digest = hashlib.sha256(f"{scope}\0{sized}".encode()).hexdigest()[:32]
    folder = cache_dir / "thumbs" / hashlib.sha256(scope.encode()).hexdigest()[:16]
    path, marker = folder / f"{digest}.jpg", folder / f"{digest}.none"
    if await asyncio.to_thread(path.is_file):
        return path
    if await asyncio.to_thread(_fresh_none, marker):
        return None
    async with _slot():
        if await asyncio.to_thread(path.is_file):  # made while this request waited
            return path
        try:
            data = await load()
            small = await asyncio.to_thread(_scaled, data, size) if data else None
        except Exception as e:  # noqa: BLE001 - a broken or unreachable cover is just no cover
            log.debug("thumbnail %s/%s: %s", scope, key, e)
            small = None
        if small is None or not data:
            await asyncio.to_thread(_write, marker, b"")
            return None
        await asyncio.to_thread(_link_shared, cache_dir, data, size, small, path)
        return path


def _link_shared(cache_dir: Path, data: bytes, size: int, small: bytes, path: Path) -> None:
    """`path` becomes a hard link to the shared thumbnail of these cover bytes (made now when it is the first)."""
    shared = cache_dir / "thumbs" / SHARED / f"{hashlib.sha256(data).hexdigest()[:32]}-{size}.jpg"
    if not shared.is_file():
        _write(shared, small)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.lnk")
    try:
        tmp.unlink(missing_ok=True)
        os.link(shared, tmp)
        os.replace(tmp, path)
    except OSError:  # a file system without hard links: a copy of its own
        _write(path, small)
