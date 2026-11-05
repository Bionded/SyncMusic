# SPDX-License-Identifier: GPL-3.0-or-later
"""`video` processor: fit a video into the stock iPod's 320×240 profile.

The profile every video iPod plays (Apple, support.apple.com/111923; 320×240, the Classic's screen):
H.264 Baseline, level 1.3, ≤ 768 kbit/s, at most 320×240 (aspect kept, even sizes, never upscaled), ≤ 30 fps,
yuv420p; AAC-LC stereo ≤ 160 kbit/s at ≤ 48 kHz; MP4 with the moov atom at the front (faststart), `.m4v`.

Decision (`plan` predicts it from the library's metadata, `run` re-decides from ffprobe and wins):
- `copy`: the file already fits the profile and starts with its moov atom - left as it is;
- `remux`: the streams fit, the container does not (moov at the end, other streams, not MP4) - `-c copy` into an
  `.m4v` with faststart, no quality lost;
- `encode`: anything else - libx264 + AAC at the settings' bitrates.
Items that are not videos pass through untouched (and the audio processors leave videos alone).
"""

from __future__ import annotations

import asyncio
import logging
from fractions import Fraction
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.processor import Processor
from syncmusic.core.models import MediaKind, PlanItem, PlannedFile, ProcessorSlot, WorkItem
from syncmusic.infra import ffmpeg

log = logging.getLogger(__name__)

MAX_W, MAX_H = 320, 240
MAX_VIDEO_KBPS = 768  # H.264 Baseline level 1.3
MAX_AUDIO_KBPS = 160
MAX_FPS = 30.0
MAX_AUDIO_RATE = 48000
MP4_CONTAINERS = ("mp4", "m4v", "mov")
CONTAINER_OVERHEAD = 1.03
# an output shorter than the source by more than max(1 s, 2 %) means a truncated/corrupt source (as transcode)
MIN_SHORTFALL_S = 1.0
SHORTFALL_FRACTION = 0.02

Action = Literal["copy", "remux", "encode"]


class VideoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_kbps: int = Field(600, ge=200, le=MAX_VIDEO_KBPS)  # average; the peak is capped at 768
    audio_kbps: int = Field(128, ge=64, le=MAX_AUDIO_KBPS)
    always: bool = False  # re-encode files that already fit the profile too


def fit_size(width: int, height: int, sar: Fraction = Fraction(1)) -> tuple[int, int]:
    """The frame size inside 320×240 with the display aspect kept (square pixels out), even, never upscaled."""
    dw = width * sar
    scale = min(Fraction(1), Fraction(MAX_W) / dw, Fraction(MAX_H, height))
    return min(_even(dw * scale), MAX_W), min(_even(height * scale), MAX_H)


def _even(v: Fraction) -> int:
    return max(2, round(v / 2) * 2)


def target_fps(fps: float | None) -> float | None:
    """The output frame rate when the source is above 30 fps (50 → 25, 60 → 30); None = keep it."""
    if not fps or fps <= MAX_FPS + 0.01:
        return None
    return fps / 2 if fps / 2 <= MAX_FPS + 0.01 else MAX_FPS


def _rate(value: Any) -> float | None:
    try:
        f = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return float(f) if f > 0 else None


def _sar(stream: dict[str, Any]) -> Fraction:
    """The pixel aspect ratio ("16:15"); 1 when unknown ("0:1", "N/A")."""
    try:
        sar = Fraction(str(stream.get("sample_aspect_ratio") or "1:1").replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return Fraction(1)
    return sar if sar > 0 else Fraction(1)


def _kbps(value: Any) -> float | None:
    try:
        return int(value) / 1000
    except (TypeError, ValueError):
        return None


def moov_first(path: Path) -> bool:
    """True when the MP4's moov atom comes before its media data (the player can start without seeking)."""
    with open(path, "rb") as f:
        while True:
            head = f.read(8)
            if len(head) < 8:
                return False
            size, kind = int.from_bytes(head[:4], "big"), head[4:8]
            if kind == b"moov":
                return True
            if kind == b"mdat":
                return False
            if size == 1:
                size = int.from_bytes(f.read(8), "big") - 8
            elif size == 0:
                return False
            f.seek(size - 8, 1)


def assess(info: dict[str, Any], path: Path) -> tuple[Action, list[str]]:
    """What a probed file needs for the profile, and why (the reasons name what does not fit)."""
    streams = info.get("streams") or []
    video = [
        s
        for s in streams
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
    ]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        return "encode", ["no video stream"]
    v = video[0]
    why: list[str] = []
    if v.get("codec_name") != "h264" or "baseline" not in str(v.get("profile", "")).lower():
        why.append(f"{v.get('codec_name')} {v.get('profile') or ''}".strip())
    if (v.get("level") or 99) > 13:
        why.append(f"level {v.get('level')}")
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    sar = _sar(v)
    if fit_size(w, h, sar) != (w, h) or sar != 1:
        why.append(f"{w}×{h}")
    if v.get("pix_fmt") not in (None, "yuv420p"):
        why.append(str(v.get("pix_fmt")))
    if target_fps(_rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate"))) is not None:
        why.append(f"{v.get('avg_frame_rate')} fps")
    fmt_kbps = _kbps((info.get("format") or {}).get("bit_rate"))
    a_kbps = sum(_kbps(a.get("bit_rate")) or 0 for a in audio[:1])
    v_kbps = _kbps(v.get("bit_rate")) or ((fmt_kbps - a_kbps) if fmt_kbps else None)
    if v_kbps is None or v_kbps > MAX_VIDEO_KBPS * 1.05:
        why.append(f"{round(v_kbps)} kbit/s" if v_kbps else "unknown bitrate")
    if audio:
        a = audio[0]
        if a.get("codec_name") != "aac" or str(a.get("profile", "LC")) != "LC":
            why.append(f"audio {a.get('codec_name')}")
        if (a.get("channels") or 2) > 2 or int(a.get("sample_rate") or 0) > MAX_AUDIO_RATE:
            why.append("audio channels / rate")
        if (_kbps(a.get("bit_rate")) or 0) > MAX_AUDIO_KBPS * 1.05:
            why.append(f"audio {round(_kbps(a.get('bit_rate')) or 0)} kbit/s")
    if why:
        return "encode", why
    container = str((info.get("format") or {}).get("format_name", ""))
    others = len(streams) - 1 - min(len(audio), 1)
    if "mp4" not in container and "mov" not in container:
        return "remux", [f"container {container}"]
    if others or len(audio) > 1:
        return "remux", ["extra streams"]
    if not moov_first(path):
        return "remux", ["moov atom at the end"]
    return "copy", []


class Video(Processor):
    type_name = "video"
    display_name = "Convert video"
    Settings: ClassVar[type[BaseModel]] = VideoSettings
    stage: ClassVar[Literal["audio", "tags", "cover", "sidecar"]] = (
        "audio"  # writes a new media file
    )

    @property
    def s(self) -> VideoSettings:
        settings: VideoSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def suggest(cls, caps: DeviceCapabilities) -> ProcessorSlot | None:
        # a device that plays video only in the iPod profile (stock iPod)
        return ProcessorSlot(type=cls.type_name) if caps.video_profile == "ipod" else None

    # --- plan ------------------------------------------------------------------------------------

    def predict(self, item: PlanItem) -> Action:
        """From the library's metadata: an H.264 MP4 within 320×240 and the bitrate is copied (run checks the
        profile, level and moov position and may still remux or encode), anything else is encoded."""
        t = item.track
        w, h = t.extra.get("width"), t.extra.get("height")
        fits = (
            not self.s.always
            and t.codec == "h264"
            and t.format in MP4_CONTAINERS
            and isinstance(w, int)
            and isinstance(h, int)
            and fit_size(w, h) == (w, h)
            and (t.bitrate_kbps or 10**6) <= MAX_VIDEO_KBPS + MAX_AUDIO_KBPS
        )
        return "copy" if fits else "encode"

    def plan(self, item: PlanItem, caps: DeviceCapabilities) -> PlanItem:
        if item.track.kind is not MediaKind.VIDEO or self.predict(item) == "copy":
            return item
        kbps = self.s.video_kbps + self.s.audio_kbps
        size = int(kbps * 1000 / 8 * (item.track.duration_s or 0) * CONTAINER_OVERHEAD)
        planned = PlannedFile(
            format="m4v", codec="h264", ext="m4v", size=size, bitrate_kbps=kbps, encoding="ipod"
        )
        return item.model_copy(update={"file": planned, "steps": [*item.steps, self.type_name]})

    # --- run -------------------------------------------------------------------------------------

    async def run(self, item: WorkItem, caps: DeviceCapabilities) -> WorkItem:
        if item.plan.track.kind is not MediaKind.VIDEO:
            return item
        info = await asyncio.to_thread(ffmpeg.probe_json, item.path)
        action, why = await asyncio.to_thread(assess, info, item.path)
        if self.s.always and action != "encode":
            action, why = "encode", ["always"]
        if action == "copy":
            return item
        out = item.work_dir / f"{item.path.stem}.{self.type_name}.m4v"
        args = self._args(action, info, item.path, out)
        try:
            await ffmpeg.run_ffmpeg(args)
            res = await asyncio.to_thread(ffmpeg.probe_json, out)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        expected = _rate((info.get("format") or {}).get("duration"))
        got = _rate((res.get("format") or {}).get("duration"))
        if (
            expected
            and got is not None
            and got < expected - max(MIN_SHORTFALL_S, SHORTFALL_FRACTION * expected)
        ):
            out.unlink(missing_ok=True)
            raise RuntimeError(
                f"{item.path.name}: output is {got:.1f} s, source should be {expected:.1f} s"
            )
        planned = PlannedFile(
            format="m4v",
            codec="h264",
            ext="m4v",
            size=out.stat().st_size,
            bitrate_kbps=round(_kbps((res.get("format") or {}).get("bit_rate")) or 0) or None,
            encoding="ipod",
        )
        log.debug("video %s: %s (%s)", item.path.name, action, ", ".join(why))
        return item.model_copy(
            update={
                "path": out,
                "plan": item.plan.model_copy(update={"file": planned}),
                "steps_done": [*item.steps_done, self.type_name],
                "notes": {**item.notes, "video": f"{action}: {', '.join(why)}"},
            }
        )

    def _args(self, action: Action, info: dict[str, Any], src: Path, out: Path) -> list[str]:
        # 0:V = video that is not an attached cover picture
        args = ["-i", ffmpeg.fpath(src), "-map", "0:V:0", "-map", "0:a:0?", "-map_metadata", "0"]
        if action == "remux":
            return [*args, "-c", "copy", "-movflags", "+faststart", "-f", "mp4", ffmpeg.fpath(out)]
        v = next(
            s
            for s in info.get("streams") or []
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        )
        w, h = fit_size(int(v["width"]), int(v["height"]), _sar(v))
        fps = target_fps(_rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate")))
        audio = next((s for s in info.get("streams") or [] if s.get("codec_type") == "audio"), None)
        args += [
            "-c:v", "libx264", "-profile:v", "baseline", "-level", "1.3", "-preset", "medium",
            "-b:v", f"{self.s.video_kbps}k", "-maxrate", f"{MAX_VIDEO_KBPS}k", "-bufsize", f"{2 * MAX_VIDEO_KBPS}k",
            "-vf", f"scale={w}:{h},setsar=1", "-pix_fmt", "yuv420p",
            "-threads", "2",  # the Pi runs other conversions next to it
        ]  # fmt: skip
        if fps is not None:
            args += ["-r", f"{fps:g}"]
        if audio is not None:
            args += ["-c:a", ffmpeg.aac_encoder(), "-b:a", f"{self.s.audio_kbps}k", "-ac", "2"]
            if int(audio.get("sample_rate") or 0) > MAX_AUDIO_RATE:
                args += ["-ar", str(MAX_AUDIO_RATE)]
        return [*args, "-movflags", "+faststart", "-f", "mp4", ffmpeg.fpath(out)]
