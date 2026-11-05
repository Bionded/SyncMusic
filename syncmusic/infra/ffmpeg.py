# SPDX-License-Identifier: GPL-3.0-or-later
"""Thin async wrapper around the `ffmpeg` / `ffprobe` binaries.

- Arguments are always passed as a list (never through a shell); paths are made absolute so a file name
  that starts with `-` or contains `:` is never read as an option or protocol.
- A failing run raises `FfmpegError` with the tail of stderr.
- Cancelling the awaiting task kills the subprocess (no orphan ffmpeg keeps writing into the work dir).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syncmusic.infra.config import env_value

log = logging.getLogger(__name__)
DEFAULT_FFMPEG = env_value("FFMPEG") or "ffmpeg"
DEFAULT_FFPROBE = env_value("FFPROBE") or "ffprobe"
FFMPEG = DEFAULT_FFMPEG
FFPROBE = DEFAULT_FFPROBE
NICE = 0  # conversions run at this nice level (Settings → Conversion); 0 = as the app
# encoders SyncMusic's processors may use; Settings → Conversion lists the ones this build has
WANTED_ENCODERS = (
    "aac",
    "libfdk_aac",
    "alac",
    "libmp3lame",
    "flac",
    "libopus",
    "libvorbis",
    "mjpeg",
    "png",
)
STDERR_TAIL = 2000


class FfmpegError(RuntimeError):
    """ffmpeg/ffprobe exited non-zero (or could not be started)."""

    def __init__(self, cmd: list[str], returncode: int | None, stderr: str) -> None:
        tail = stderr.strip()[-STDERR_TAIL:]
        exe = cmd[3] if cmd[:2] == ["nice", "-n"] and len(cmd) > 3 else cmd[0]
        super().__init__(f"{Path(exe).name} failed (exit {returncode}): {tail}")
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr


@dataclass
class ProbeResult:
    """What SyncMusic needs to know about one media file."""

    container: str  # ffprobe format_name, e.g. "flac", "mov,mp4,m4a,3gp,3g2,mj2", "mp3"
    codec: str | None  # audio codec name, e.g. "flac", "alac", "aac", "mp3", "opus"
    duration_s: float | None
    sample_rate: int | None
    channels: int | None
    bit_depth: int | None  # lossless only (bits_per_raw_sample / sample format)
    bitrate: int | None  # overall bit rate in bit/s
    size_bytes: int | None
    has_cover: bool  # an attached picture stream
    tags: dict[str, str] = field(
        default_factory=dict
    )  # format + audio stream tags, keys lowercased
    audio_bitrate: int | None = None  # audio stream bit rate (bit/s), else the overall one


def fpath(path: str | os.PathLike[str]) -> str:
    """Absolute path string for an ffmpeg argument."""
    return os.path.abspath(os.fspath(path))


async def _run(cmd: list[str], timeout_s: float | None) -> tuple[bytes, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise FfmpegError(cmd, None, str(e)) from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except BaseException:  # cancellation or timeout: never leave the process running
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode != 0:
        raise FfmpegError(cmd, proc.returncode, err.decode(errors="replace"))
    return out, err.decode(errors="replace")


def is_ffmpeg(path: str) -> bool:
    """True for a file named ffmpeg… that is not a link to some other program: the path comes from the settings and
    the API, and whatever it names is run."""
    p = Path(path)
    return p.name.startswith("ffmpeg") and p.resolve().name.startswith("ffmpeg")


def configure(path: str | None, nice: int = 0) -> None:
    """Use the ffmpeg at `path` (its ffprobe next to it; None: the default from the environment / PATH) at the
    `nice` level (Settings → Conversion)."""
    global FFMPEG, FFPROBE, NICE
    if path and not is_ffmpeg(path):
        log.warning("ffmpeg_path %s is not an ffmpeg binary: using the default", path)
        path = None
    if path:
        FFMPEG = path
        FFPROBE = str(Path(path).with_name(Path(path).name.replace("ffmpeg", "ffprobe")))
    else:
        FFMPEG, FFPROBE = DEFAULT_FFMPEG, DEFAULT_FFPROBE
    NICE = nice
    encoders.cache_clear()


def nice_prefix() -> list[str]:
    return ["nice", "-n", str(NICE)] if NICE else []


def check(path: str | None = None) -> dict[str, Any]:
    """{path, version, encoders (the WANTED ones it has), missing, error} of the ffmpeg at `path` (default: the one
    in use) - Settings → Conversion's "Found ffmpeg 6.1.1 · codecs: …"."""
    exe = path or FFMPEG
    out: dict[str, Any] = {
        "path": exe,
        "version": None,
        "encoders": [],
        "missing": [],
        "error": None,
    }
    if path and not is_ffmpeg(path):
        out["error"] = f"not an ffmpeg binary: {exe}"
        return out
    try:
        first = subprocess.run(
            [exe, "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = f"ffmpeg does not run at {exe}: {type(e).__name__}"
        return out
    words = first[0].split() if first else []
    out["version"] = words[2] if len(words) > 2 and words[:2] == ["ffmpeg", "version"] else None
    have = _encoders(exe)
    out["encoders"] = [e for e in WANTED_ENCODERS if e in have]
    out["missing"] = [e for e in ("aac", "alac", "libmp3lame", "flac") if e not in have]
    return out


def probe_json(path: str | os.PathLike[str], timeout_s: float = 60) -> dict[str, Any]:
    """ffprobe's format + streams of one file as JSON, blocking (a library scan's worker thread).
    Raises FfmpegError when ffprobe fails."""
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        fpath(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as e:
        raise FfmpegError(cmd, None, str(e)) from e
    if out.returncode != 0:
        raise FfmpegError(cmd, out.returncode, out.stderr)
    data: dict[str, Any] = json.loads(out.stdout or "{}")
    return data


def decode_problem(path: str | os.PathLike[str], timeout_s: float = 600) -> str | None:
    """Decode the whole file and discard the result (the integrity check's deep read): the first error
    ffmpeg reports, or None when it decodes cleanly."""
    cmd = [
        *nice_prefix(),
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        fpath(path),
        "-f",
        "null",
        "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as e:
        return f"cannot be decoded: {type(e).__name__}"
    lines = [x for x in out.stderr.splitlines() if x.strip()]
    if out.returncode != 0 or decode_errors(out.stderr):
        return f"cannot be decoded: {lines[0] if lines else f'exit {out.returncode}'}"[:300]
    return None


async def run_ffmpeg(args: list[str], *, timeout_s: float | None = None) -> str:
    """Run `ffmpeg <args>` (quiet, no stdin, overwrite outputs) at the configured nice level. Raises FfmpegError
    on failure; returns the error-level messages of a successful run (e.g. decoding errors ffmpeg skipped over),
    usually empty."""
    cmd = [*nice_prefix(), FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", *args]
    _, err = await _run(cmd, timeout_s)
    return err


async def run_ffmpeg_progress(args: list[str], on_time: Callable[[float], None]) -> str:
    """`run_ffmpeg` that calls `on_time(seconds of output written)` as ffmpeg reports it (`-progress`, about
    twice a second) - for long encodes the UI shows."""
    cmd = [*nice_prefix(), FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-nostats"]
    cmd += ["-progress", "pipe:1", "-y", *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise FfmpegError(cmd, None, str(e)) from e
    assert proc.stdout is not None and proc.stderr is not None
    err_task = asyncio.create_task(proc.stderr.read())
    try:
        async for raw in proc.stdout:
            key, _, value = raw.decode(errors="replace").strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                on_time(int(value) / 1e6)
        err = (await err_task).decode(errors="replace")
        await proc.wait()
    except BaseException:  # cancellation: never leave the process running
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        err_task.cancel()
        raise
    if proc.returncode != 0:
        raise FfmpegError(cmd, proc.returncode, err)
    return err


REPLAYGAIN_LINE = re.compile(r"track_(gain|peak)\s*=\s*([+-]?[0-9.]+)")
REPLAYGAIN_BRANCH = "replaygain,anullsink"  # an `asplit` output that only measures


def _gain(err: str) -> tuple[float, float] | None:
    found = dict(REPLAYGAIN_LINE.findall(err))
    if "gain" not in found or "peak" not in found:
        return None
    return float(found["gain"]), float(found["peak"])


async def run_ffmpeg_measured(
    args: list[str], *, timeout_s: float | None = None
) -> tuple[str, tuple[float, float] | None]:
    """`run_ffmpeg` for a graph with a `REPLAYGAIN_BRANCH`: the audio is decoded once for the encode and the
    measurement (a second decode was a third of a hi-res FLAC → ALAC prepare). Returns the error-level
    messages and (track gain dB, peak) or None."""
    cmd = [
        *nice_prefix(),
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "level+info",
    ]
    _, err = await _run([*cmd, "-y", *args], timeout_s)
    errors = "\n".join(line for line in err.splitlines() if "[error]" in line or "[fatal]" in line)
    return errors, _gain(err)


async def replaygain(
    path: str | os.PathLike[str], *, duration_s: float | None = None
) -> tuple[float, float] | None:
    """ReplayGain of the file's first audio stream, measured by ffmpeg's `replaygain` filter:
    (track gain in dB, track peak as a fraction of full scale), or None when ffmpeg reports no values. Raises
    FfmpegError when ffmpeg fails. The Pi 5 measures ~150x realtime, so the timeout grows with the duration."""
    cmd = [
        *nice_prefix(),
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-v",
        "info",
        "-i",
        fpath(path),
    ]
    cmd += ["-map", "0:a:0", "-af", "replaygain", "-f", "null", "-"]
    _, err = await _run(cmd, 60 + (duration_s or 0) / 20)
    return _gain(err)


DECODE_ERROR_MARKERS = (
    "Decoding error",
    "decode_frame() failed",
    "Error while decoding",
    "Invalid data found",
)


def decode_errors(stderr: str) -> list[str]:
    """Lines of an ffmpeg stderr that report damaged input data."""
    return [
        line.strip() for line in stderr.splitlines() if any(m in line for m in DECODE_ERROR_MARKERS)
    ]


async def probe(path: str | os.PathLike[str], *, timeout_s: float | None = 60) -> ProbeResult:
    """ffprobe one file. Raises FfmpegError when the file is missing or not media."""
    out, _ = await _run(
        [
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            fpath(path),
        ],
        timeout_s,
    )
    return parse_probe(json.loads(out))


# mutagen stream info → ffprobe codec name, for the formats `quick_probe` answers
_QUICK_CODECS = {
    "MPEGInfo": "mp3",
    "StreamInfo": "flac",
    "OggVorbisInfo": "vorbis",
    "OggOpusInfo": "opus",
}
_LOSSLESS_CODECS = ("flac", "alac")


def quick_probe(path: str | os.PathLike[str]) -> ProbeResult | None:
    """The stream facts of an MP3, FLAC, MP4 (AAC/ALAC), Ogg Vorbis or Opus file read by mutagen in-process, or None
    (another format, or mutagen cannot tell) - then call `probe`. An ffprobe run costs ~100 ms of CPU on the Pi, a
    first sync of a big library runs one per item. No tags, no cover: what deciding a conversion needs."""
    import mutagen  # noqa: PLC0415 - the module is imported where ffmpeg alone is enough too

    try:
        f = mutagen.File(fpath(path))
    except Exception:  # noqa: BLE001 - not readable by mutagen: ffprobe decides
        return None
    info = getattr(f, "info", None)
    if info is None:
        return None
    name = type(info).__name__
    codec = _QUICK_CODECS.get(name)
    if name == "MP4Info":
        c = str(getattr(info, "codec", ""))
        codec = "aac" if c.startswith("mp4a.40") else "alac" if c == "alac" else None
    if codec is None or (codec == "flac" and type(f).__name__ != "FLAC"):
        return None
    rate = getattr(info, "sample_rate", None) or (48000 if codec == "opus" else None)
    bitrate = getattr(info, "bitrate", None) or None
    return ProbeResult(
        container=codec,
        codec=codec,
        duration_s=getattr(info, "length", None) or None,
        sample_rate=rate,
        channels=getattr(info, "channels", None),
        bit_depth=getattr(info, "bits_per_sample", None) if codec in _LOSSLESS_CODECS else None,
        bitrate=bitrate,
        size_bytes=os.path.getsize(path),
        has_cover=False,
        audio_bitrate=bitrate,
    )


def _int(value: Any) -> int | None:
    try:
        return int(value) or None
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bit_depth(stream: dict[str, Any]) -> int | None:
    if depth := _int(stream.get("bits_per_raw_sample")):
        return depth
    if stream.get("codec_name") in ("flac", "alac", "wavpack", "ape") or str(
        stream.get("codec_name", "")
    ).startswith("pcm_"):
        fmt = str(stream.get("sample_fmt", ""))
        return {"s16": 16, "s16p": 16, "s32": 32, "s32p": 32, "u8": 8}.get(fmt) or _int(
            stream.get("bits_per_sample")
        )
    return None


def parse_probe(data: dict[str, Any]) -> ProbeResult:
    """ProbeResult from ffprobe's JSON (`-show_format -show_streams`)."""
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    audio: dict[str, Any] = next((s for s in streams if s.get("codec_type") == "audio"), {})
    has_cover = any(
        s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic")
        for s in streams
    )
    tags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
    for k, v in (audio.get("tags") or {}).items():  # Ogg/Opus keep tags on the stream
        tags.setdefault(str(k).lower(), str(v))
    return ProbeResult(
        container=str(fmt.get("format_name", "")),
        codec=audio.get("codec_name"),
        duration_s=_float(fmt.get("duration")) or _float(audio.get("duration")),
        sample_rate=_int(audio.get("sample_rate")),
        channels=_int(audio.get("channels")),
        bit_depth=_bit_depth(audio) if audio else None,
        bitrate=_int(fmt.get("bit_rate")),
        size_bytes=_int(fmt.get("size")),
        has_cover=has_cover,
        tags=tags,
        audio_bitrate=_int(audio.get("bit_rate")) or _int(fmt.get("bit_rate")),
    )


@functools.cache
def encoders() -> frozenset[str]:
    """Names of the audio/image encoders this ffmpeg build offers (cached; runs ffmpeg once)."""
    return _encoders(FFMPEG)


def _encoders(exe: str) -> frozenset[str]:
    try:
        out = subprocess.run(
            [exe, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names: set[str] = set()
    started = False
    for line in out.splitlines():
        if line.strip().startswith("------"):
            started = True
            continue
        parts = line.split()
        if started and len(parts) >= 2 and parts[0][0] in "AV":
            names.add(parts[1])
    return frozenset(names)


def aac_encoder() -> str:
    """Best available AAC encoder: libfdk_aac when compiled in, else ffmpeg's native `aac`."""
    return "libfdk_aac" if "libfdk_aac" in encoders() else "aac"
