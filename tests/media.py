"""Synthetic test media (the developer notes level 3): short ffmpeg sine tones tagged with mutagen.

Encoded templates are cached per format in a temp dir for the test session; `make_track` copies a template and
writes tags, so creating many files stays cheap on the Pi.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import mutagen
from mutagen.id3 import APIC, ID3, SYLT, USLT
from mutagen.mp4 import MP4Cover

# ffmpeg encoder arguments per extension
ENCODERS: dict[str, list[str]] = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "128k"],
    ".flac": ["-c:a", "flac"],
    ".m4a": ["-c:a", "aac", "-b:a", "96k"],
    ".opus": ["-c:a", "libopus", "-b:a", "64k"],
}
_TEMPLATES = Path(tempfile.mkdtemp(prefix="sd-media-"))
# one dir per session; removed at exit so runs do not pile up in /tmp
atexit.register(shutil.rmtree, _TEMPLATES, ignore_errors=True)
# 1×1 JPEG
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a"
    "1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f"
    "0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d010203"
    "00041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a343536373839"
    "3a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4"
    "a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9fa"
    "ffda0008010100003f00fbd3ffd9"
)


def _template(ext: str, seconds: float) -> Path:
    path = _TEMPLATES / f"tone-{seconds}{ext}"
    if not path.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={seconds}",
                *ENCODERS[ext],
                str(path),
            ],
            check=True,
        )
    return path


def make_track(
    path: Path, *, seconds: float = 1.0, cover: bool = False, lyrics: str | None = None, **tags: Any
) -> Path:
    """Create an audio file at `path` (format from the suffix) with easy-tag `tags` (title=..., artist=...)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_template(path.suffix.lower(), seconds), path)
    audio = mutagen.File(path, easy=True)
    if audio.tags is None:
        audio.add_tags()
    for key, value in tags.items():
        audio.tags[key] = [str(v) for v in value] if isinstance(value, list) else str(value)
    audio.save()
    if cover or lyrics:
        _add_extras(path, cover, lyrics)
    return path


def _add_extras(path: Path, cover: bool, lyrics: str | None) -> None:
    ext = path.suffix.lower()
    if ext == ".mp3":
        id3 = ID3(path)
        if cover:
            id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=TINY_JPEG))
        if lyrics:
            id3.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
        id3.save()
        return
    audio = mutagen.File(path)
    if ext == ".flac":
        if cover:
            from mutagen.flac import Picture

            pic = Picture()
            pic.data, pic.mime, pic.type = TINY_JPEG, "image/jpeg", 3
            audio.add_picture(pic)
        if lyrics:
            audio["LYRICS"] = lyrics
    elif ext == ".m4a":
        if cover:
            audio["covr"] = [MP4Cover(TINY_JPEG, imageformat=MP4Cover.FORMAT_JPEG)]
        if lyrics:
            audio["©lyr"] = lyrics
    audio.save()


def add_synced_lyrics_id3(path: Path, lines: list[tuple[str, int]]) -> None:
    """SYLT frame with (text, milliseconds) lines on an MP3."""
    id3 = ID3(path)
    id3.add(SYLT(encoding=3, lang="eng", format=2, type=1, text=lines))
    id3.save()


def make_music(
    path: Path,
    *,
    seconds: float = 3.0,
    sample_rate: int = 44100,
    bit_depth: int = 16,
    cover: bool = False,
    lyrics: str | None = None,
    **tags: Any,
) -> Path:
    """Stereo pink noise at the given rate/depth: two independent channels, so encoders size it roughly like
    real music (a copied mono channel is coded as joint stereo at about 60 % of the bitrate), unlike a sine.

    Supported suffixes: .flac, .wav, .m4a (ALAC), .mp3, .opus. Templates are cached like `make_track`'s.
    """
    ext = path.suffix.lower()
    template = _TEMPLATES / f"noise-stereo-{seconds}-{sample_rate}-{bit_depth}{ext}"
    if not template.exists():
        fmt = "s16" if bit_depth == 16 else "s32"
        codec = {
            ".flac": ["-c:a", "flac", "-sample_fmt", fmt],
            ".wav": ["-c:a", "pcm_s16le" if bit_depth == 16 else "pcm_s24le"],
            ".m4a": ["-c:a", "alac", "-sample_fmt", fmt + "p"],
            ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
            ".opus": ["-c:a", "libopus", "-b:a", "96k"],
        }[ext]
        if ext == ".flac" and bit_depth == 24:
            codec += ["-bits_per_raw_sample", "24"]
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                # fixed seeds: the same bytes every run (random noise made a FLAC sometimes parse as MP3)
                *(
                    "-f",
                    "lavfi",
                    "-i",
                    f"anoisesrc=d={seconds}:c=pink:r={sample_rate}:a=0.3:seed=42",
                ),
                *(
                    "-f",
                    "lavfi",
                    "-i",
                    f"anoisesrc=d={seconds}:c=pink:r={sample_rate}:a=0.3:seed=43",
                ),
                "-filter_complex",
                "[0:a][1:a]join=inputs=2:channel_layout=stereo",
                *codec,
                str(template),
            ],
            check=True,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, path)
    if tags and ext != ".wav":
        audio = mutagen.File(path, easy=True)
        if audio.tags is None:
            audio.add_tags()
        for key, value in tags.items():
            audio.tags[key] = [str(v) for v in value] if isinstance(value, list) else str(value)
        audio.save()
    if cover or lyrics:
        _add_extras(path, cover, lyrics)
    return path


_EXT_FORMAT = {".flac": "flac", ".mp3": "mp3", ".opus": "opus", ".ogg": "ogg", ".wav": "wav"}


def media_work_item(
    path: Path, work_dir: Path, *, space_steps: bool = False, has_cover: bool = False
) -> Any:
    """A WorkItem for a real media file, with Track/PlannedFile filled from mutagen like a library would."""
    from syncmusic.core.models import ItemRef, PlanItem, PlannedFile, Track, WorkItem

    audio = mutagen.File(path)
    info = audio.info
    ext = path.suffix.lower()
    fmt = _EXT_FORMAT.get(ext) or (
        "alac" if str(getattr(info, "codec", "")).startswith("alac") else "aac"
    )
    tags = mutagen.File(path, easy=True).tags or {}
    track = Track(
        ref=ItemRef(library_id="lib", item_id=path.name),
        title=(tags.get("title") or [path.stem])[0],
        artist=(tags.get("artist") or [None])[0],
        format=fmt,
        size_bytes=path.stat().st_size,
        duration_s=info.length,
        sample_rate=getattr(info, "sample_rate", None),
        bit_depth=getattr(info, "bits_per_sample", None) or None,
        channels=getattr(info, "channels", None),
        bitrate_kbps=round(getattr(info, "bitrate", 0) / 1000) or None,
        has_cover=has_cover,
    )
    file = PlannedFile(
        format=fmt,
        codec=fmt,
        ext=ext.lstrip("."),
        size=track.size_bytes,
        bitrate_kbps=track.bitrate_kbps,
        sample_rate=track.sample_rate,
        bit_depth=track.bit_depth if fmt in ("flac", "alac", "wav") else None,
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    plan = PlanItem(track=track, file=file, enable_space_steps=space_steps)
    return WorkItem(plan=plan, path=path, work_dir=work_dir)


def make_video(
    path: Path,
    *,
    size: str = "640x480",
    seconds: float = 1.0,
    rate: int = 25,
    video: tuple[str, ...] = ("-c:v", "libx264", "-preset", "ultrafast"),
    faststart: bool = False,
    noise: bool = False,
    **tags: str,
) -> Path:
    """A test-pattern video with a sine tone: H.264 + AAC in the container of the suffix. `noise`: grain
    on every frame, so an encoder reaches its target bitrate like on a real film (size predictions)."""
    pattern = f"testsrc2=size={size}:rate={rate}:duration={seconds}"
    if noise:
        pattern += ",noise=alls=30:allf=t"
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            pattern if noise else f"testsrc=size={size}:rate={rate}:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            *video,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-shortest",
            *(["-movflags", "+faststart"] if faststart else []),
            *meta,
            str(path),
        ],  # fmt: skip
        check=True,
    )
    return path


def video_work_item(path: Path, work_dir: Path) -> Any:
    """A WorkItem for a real video file, filled from ffprobe like the folder library does."""
    from syncmusic.core.models import ItemRef, MediaKind, PlanItem, Track, WorkItem
    from syncmusic.core.prepare import initial_plan_item
    from syncmusic.infra.ffmpeg import probe_json

    info = probe_json(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    track = Track(
        ref=ItemRef(library_id="lib", item_id=path.name),
        kind=MediaKind.VIDEO,
        title=path.stem,
        format=path.suffix.lower().lstrip("."),
        codec=v["codec_name"],
        size_bytes=path.stat().st_size,
        duration_s=float(info["format"]["duration"]),
        bitrate_kbps=int(info["format"]["bit_rate"]) // 1000,
        path=path.name,
        extra={"width": v["width"], "height": v["height"]},
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    plan: PlanItem = initial_plan_item(track)
    return WorkItem(plan=plan, path=path, work_dir=work_dir)


def big_cover(
    width: int = 1200, height: int = 1200, fmt: str = "JPEG", progressive: bool = False
) -> bytes:
    """A photo-like test cover (gradient + noise), encoded as JPEG or PNG."""
    import io
    import random

    from PIL import Image

    rnd = random.Random(width * 7 + height)
    img = Image.linear_gradient("L").resize((width, height)).convert("RGB")
    noise = Image.frombytes("L", (width, height), rnd.randbytes(width * height)).convert("RGB")
    img = Image.blend(img, noise, 0.15)
    out = io.BytesIO()
    img.save(out, fmt, **({"quality": 92, "progressive": progressive} if fmt == "JPEG" else {}))
    return out.getvalue()


def embed_cover(path: Path, data: bytes, mime: str = "image/jpeg") -> None:
    """Embed `data` as the front cover of an MP3/M4A/FLAC/Ogg file (replacing existing pictures)."""
    from syncmusic.processors.covers import set_pictures

    set_pictures(path, data, mime)
