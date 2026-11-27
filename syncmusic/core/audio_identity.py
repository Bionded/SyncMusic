# SPDX-License-Identifier: GPL-3.0-or-later
"""The identity of a file's audio, without its tags: two files with the same identity carry the same audio, so a
library file whose size and time changed only because a tagger rewrote its tags is not a new source.

The reader asks only for small byte ranges (`read(offset, length)`), so a library can answer it with HTTP Range
requests instead of downloading the whole file:

- FLAC: the STREAMINFO MD5 of the decoded audio; when the encoder left it zero, a hash of the first audio
  frames and the audio length (after the metadata blocks);
- MP3: the audio between the ID3v2 tag and the ID3v1/APE tags at the end - its length and a hash of its start;
- MP4 (M4A, ALAC, AAC): the `mdat` atom's length and a hash of its start;
- Ogg (Vorbis, Opus, FLAC in Ogg): the payload of the first audio pages (granule position > 0) and the stream's
  last granule position (the length in samples).

`None`: an unknown format or a file too broken to read - the caller then treats a size/time change as a new
source, as before."""

from __future__ import annotations

import asyncio
import hashlib
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path

SLICE = 64 * 1024  # bytes of audio hashed
MAX_HEADERS = 64  # atoms / metadata blocks / pages walked before giving up

Reader = Callable[[int, int], Awaitable[bytes]]


def _digest(*parts: bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.hexdigest()[:32]


async def audio_identity(read: Reader, size: int) -> str | None:
    """`format:hash` of the audio of a file of `size` bytes, or None."""
    if size < 16:
        return None
    head = await read(0, 12)
    try:
        if head[:4] == b"fLaC":
            return await _flac(read, size)
        if head[:4] == b"OggS":
            return await _ogg(read, size)
        if head[4:8] == b"ftyp":
            return await _mp4(read, size)
        if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
            return await _mp3(read, size, head)
    except (struct.error, ValueError, IndexError):
        return None
    return None


async def file_identity(path: Path) -> str | None:
    """The identity of a local file."""

    def reader(offset: int, length: int) -> bytes:
        with path.open("rb") as f:
            f.seek(offset)
            return f.read(length)

    async def read(offset: int, length: int) -> bytes:
        return await asyncio.to_thread(reader, offset, length)

    try:
        size = (await asyncio.to_thread(path.stat)).st_size
        return await audio_identity(read, size)
    except OSError:
        return None


async def _flac(read: Reader, size: int) -> str | None:
    pos = 4
    md5 = b""
    for _ in range(MAX_HEADERS):
        hdr = await read(pos, 4)
        if len(hdr) < 4:
            return None
        last, kind, length = hdr[0] & 0x80, hdr[0] & 0x7F, int.from_bytes(hdr[1:4], "big")
        if kind == 0:  # STREAMINFO
            info = await read(pos + 4, 34)
            if len(info) < 34:
                return None
            md5 = info[18:34]
            if any(md5):
                # the MD5 covers the decoded samples; the sample count and format make it exact
                return "flac:" + md5.hex() + info[10:18].hex()
        pos += 4 + length
        if last:
            break
    else:
        return None
    if pos >= size:
        return None
    return "flac~:" + _digest(await read(pos, SLICE), str(size - pos).encode())


async def _mp3(read: Reader, size: int, head: bytes) -> str | None:
    start = 0
    if head[:3] == b"ID3":
        sz = head[6:10]
        if any(b & 0x80 for b in sz):
            return None
        start = 10 + ((sz[0] << 21) | (sz[1] << 14) | (sz[2] << 7) | sz[3])
        if head[5] & 0x10:  # footer
            start += 10
    end = size
    tail = await read(max(0, size - 128), 128)
    if tail[:3] == b"TAG":
        end -= 128
    ape = await read(max(0, end - 32), 32)
    if ape[:8] == b"APETAGEX":
        (ape_size,) = struct.unpack("<I", ape[12:16])
        flags = struct.unpack("<I", ape[20:24])[0]
        end -= ape_size + (32 if flags & 0x80000000 else 0)
    if end - start < 4:
        return None
    first = await read(start, min(SLICE, end - start))
    if not (first[0] == 0xFF and first[1] & 0xE0 == 0xE0):
        return None  # a padding or a second tag we do not know: no reliable start
    return "mp3:" + _digest(first, str(end - start).encode())


async def _mp4(read: Reader, size: int) -> str | None:
    pos = 0
    for _ in range(MAX_HEADERS):
        if pos + 8 > size:
            return None
        hdr = await read(pos, 16)
        length, kind = struct.unpack(">I4s", hdr[:8])
        body = 8
        if length == 1:
            (length,) = struct.unpack(">Q", hdr[8:16])
            body = 16
        elif length == 0:
            length = size - pos
        if length < body:
            return None
        if kind == b"mdat":
            data = length - body
            return "mp4:" + _digest(await read(pos + body, min(SLICE, data)), str(data).encode())
        pos += length
    return None


async def _ogg(read: Reader, size: int) -> str | None:
    pos = 0
    payload = b""
    for _ in range(MAX_HEADERS * 16):
        if pos + 27 > size or len(payload) >= SLICE:
            break
        hdr = await read(pos, 27)
        if hdr[:4] != b"OggS":
            return None
        (granule,) = struct.unpack("<q", hdr[6:14])
        segs = await read(pos + 27, hdr[26])
        body = sum(segs)
        if granule > 0:  # an audio page (the header pages have granule 0)
            payload += await read(pos + 27 + hdr[26], body)
        pos += 27 + hdr[26] + body
    if not payload:
        return None
    # the last page's granule position: the stream's length in samples
    tail = await read(max(0, size - 65307), 65307)
    last = tail.rfind(b"OggS")
    if last < 0 or last + 14 > len(tail):
        return None
    (length,) = struct.unpack("<q", tail[last + 6 : last + 14])
    return "ogg:" + _digest(payload[:SLICE], str(length).encode())
