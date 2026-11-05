# SPDX-License-Identifier: GPL-3.0-or-later
"""`transcode` processor: convert audio to a format/sample rate the device plays.

Decision (shared by `plan` and `run` so both agree):
1. Rules, first match wins. A rule matches when the source format is in `from` (empty = any) and its `when`
   holds: `always`; `not_supported` (format not in `caps.formats`); `space_needed` (only when the planner
   enabled space steps for the item). An `always` rule converts even formats the device plays.
   `to: copy` keeps the file as it is (only a format the device plays). A rule with `above_target_only` (transcode to fit) matches only
   lossless sources and lossy ones above its target bitrate.
2. No rule matched but the source is unsupported → a fallback target the device plays (lossless → ALAC/FLAC,
   else AAC/MP3/...). Nothing playable → unchanged (the planner reports the item as unsupported).
3. A supported lossless source over the device's sample-rate/bit-depth limit is re-encoded in its own codec.
4. Lossy → lossy at a fixed bitrate (default or `bitrate_kbps`): capped at the source's bitrate rounded up to a
   standard step (`cap_to_source`), so a 128k podcast does not become a 320k MP3. VBR qualities are not capped.

`plan` predicts from library metadata (no I/O); `run` re-decides from an ffprobe of the actual file, which wins
when they disagree. Tags and an embedded cover are carried over (no cover for Ogg/Opus outputs).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.processor import Processor
from syncmusic.core.models import MediaKind, PlanItem, PlannedFile, ProcessorSlot, WorkItem
from syncmusic.infra import ffmpeg
from syncmusic.processors import tagmap

log = logging.getLogger(__name__)

Target = Literal["alac", "aac", "mp3", "opus", "ogg", "flac", "copy"]
LOSSLESS = frozenset({"flac", "alac", "wav", "aiff", "ape", "wv"})
EXT = {"alac": "m4a", "aac": "m4a", "mp3": "mp3", "opus": "opus", "ogg": "ogg", "flac": "flac"}
# lossless sizes relative to FLAC (observed on music; refined later from real conversions)
LOSSLESS_RATIO = {"flac": 1.0, "alac": 1.03, "wav": 1.7, "aiff": 1.7, "ape": 0.97, "wv": 0.99}
# default quality per lossy target (kbit/s) and the average bitrate of LAME -V0..-V9 / Vorbis -q0..-q10
# MP3 defaults to CBR: LAME VBR sizes vary ±10 % per track (measured on music)
DEFAULT_KBPS = {"aac": 256, "mp3": 320, "opus": 160, "ogg": 192}
LAME_VBR_KBPS = [245, 225, 190, 175, 165, 130, 115, 100, 85, 65]
VORBIS_Q_KBPS = [64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 500]
# fixed bitrates a lossy→lossy target is capped to (smallest step ≥ CAP_TOLERANCE × source bitrate)
STANDARD_KBPS = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
CAP_TOLERANCE = 0.9  # VBR sources read a few percent over their nominal rate (129k for a 128k AAC)
CONTAINER_OVERHEAD = 1.02
# an output shorter than the source by more than max(1 s, 2 %) means a truncated/corrupt source
MIN_SHORTFALL_S = 1.0
SHORTFALL_FRACTION = 0.02
# ffmpeg's native AAC encoder overshoots its nominal bitrate by ~4 % (measured on music)
BITRATE_FACTOR = {"aac": 1.05}
# probe codec name -> our format name
CODEC_FORMAT = {
    "vorbis": "ogg",
    "wmav2": "wma",
    "wavpack": "wv",
    "pcm_s16be": "aiff",
    "pcm_s24be": "aiff",
}


class Quality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vbr: int | None = Field(None, ge=0, le=10)  # LAME -V / libfdk -vbr / Vorbis -q
    bitrate_kbps: int | None = Field(None, ge=8, le=512)


class TranscodeRule(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: list[str] = Field(default=[], alias="from")  # source formats; empty = any
    to: Target
    when: Literal["always", "not_supported", "space_needed"] = "not_supported"
    quality: Quality = Quality()
    # transcode to fit: only lossless sources and lossy ones above the target bitrate; the others are
    # left to the next rules (copied as they are when nothing else matches).
    above_target_only: bool = False


class TranscodeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[TranscodeRule] = []
    max_sample_rate: int | Literal["device"] | None = "device"
    max_bit_depth: int | Literal["device"] | None = "device"
    cap_to_source: bool = (
        True  # lossy sources: target bitrate ≤ source bitrate (rounded up to a step)
    )


class CorruptSource(RuntimeError):
    """The source has damaged audio data (ffmpeg reported decoding errors)."""


class TruncatedSource(CorruptSource):
    """The converted file is much shorter than the source claims: the source is truncated or corrupt."""


@dataclass(frozen=True)
class Decision:
    to: str  # target codec (never "copy")
    quality: Quality
    sample_rate: int | None  # output rate, None = keep
    bit_depth: int | None  # output depth (lossless targets), None = keep
    reason: str
    max_kbps: int | None = None  # cap from a lossy source's bitrate, None = no cap


def source_cap(fmt: str, kbps: float | None) -> int | None:
    """Bitrate cap for a lossy source: the smallest standard step ≥ CAP_TOLERANCE × its bitrate."""
    if fmt in LOSSLESS or not kbps or kbps <= 0:
        return None
    return next((k for k in STANDARD_KBPS if k >= kbps * CAP_TOLERANCE), None)


def _format_of(codec: str | None, container: str = "") -> str:
    if not codec:
        return ""
    if codec.startswith("pcm_"):
        return "aiff" if "aiff" in container or codec.endswith("be") else "wav"
    return CODEC_FORMAT.get(codec, codec)


def _size_kbps(size: int, duration_s: float | None) -> float | None:
    return size * 8 / duration_s / 1000 if size and duration_s else None


class Transcode(Processor):
    type_name = "transcode"
    display_name = "Convert audio"
    Settings: ClassVar[type[BaseModel]] = TranscodeSettings
    stage: ClassVar[Literal["audio", "tags", "cover", "sidecar"]] = "audio"
    # 2: bitrate cap; 3: duration check; 4: decode errors
    version: ClassVar[int] = 5  # 5: every source tag + cover carried over

    @property
    def s(self) -> TranscodeSettings:
        settings: TranscodeSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def suggest(cls, caps: DeviceCapabilities) -> ProcessorSlot | None:
        # A device that plays ALAC/AAC but not FLAC (stock iPod): FLAC → ALAC, Opus/Vorbis/WMA → AAC.
        if {"alac", "aac"} <= caps.formats and "flac" not in caps.formats:
            return ProcessorSlot(
                type=cls.type_name,
                settings={
                    "rules": [
                        {"from": ["flac", "wav", "aiff", "ape", "wv"], "to": "alac"},
                        {"from": ["opus", "ogg", "wma"], "to": "aac"},
                    ]
                },
            )
        # A player that decodes lossless only at a battery cost (Rockbox): lossless → MP3 (default
        # quality, CBR 320k), lossy sources stay as they are.
        if caps.prefer_lossy:
            return ProcessorSlot(
                type=cls.type_name,
                settings={
                    "rules": [
                        {
                            "from": ["flac", "alac", "wav", "aiff", "ape", "wv"],
                            "to": "mp3",
                            "when": "always",
                        }
                    ],
                },
            )
        return None

    # --- decision -------------------------------------------------------------------------------

    def _limit(self, value: int | str | None, device: int | None) -> int | None:
        return device if value == "device" else value  # type: ignore[return-value]

    def decide(
        self,
        fmt: str,
        sample_rate: int | None,
        bit_depth: int | None,
        caps: DeviceCapabilities,
        space_steps: bool = False,
        source_kbps: float | None = None,
    ) -> Decision | None:
        """What to convert a `fmt` source to for this device; None = keep the file as it is."""
        supported = not caps.formats or fmt in caps.formats
        target: str | None = None
        quality = Quality()
        reason = ""
        shrink = False
        for i, rule in enumerate(self.s.rules):
            if rule.from_ and fmt not in rule.from_:
                continue
            if rule.when == "not_supported" and supported:
                continue
            if rule.when == "space_needed" and not space_steps:
                continue
            if rule.above_target_only and not self._above_target(fmt, source_kbps, rule):
                continue
            if rule.to == "copy":
                if not supported:
                    continue  # never copy a file the device cannot play: the fallback converts it
                return None
            target, quality, reason = rule.to, rule.quality, f"rule {i + 1} ({rule.when})"
            # same format at a lower bitrate (mp3 320k -> mp3 192k) still converts; a rule into the source's own
            # format does so when the source is above its bitrate ('weak' turns every 320k MP3 into 192k)
            shrink = rule.above_target_only or (
                rule.to == fmt and self._above_target(fmt, source_kbps, rule)
            )
            break
        if target is None and not supported:
            target = self._fallback(fmt, caps)
            reason = "unsupported format"
            if target is None:
                return None
        max_sr = self._limit(self.s.max_sample_rate, caps.max_sample_rate)
        max_bd = self._limit(self.s.max_bit_depth, caps.max_bit_depth)
        out_sr = sample_rate if sample_rate and max_sr and sample_rate > max_sr else None
        if out_sr is not None and max_sr is not None:
            out_sr = 44100 if out_sr % 44100 == 0 and max_sr >= 44100 else max_sr
        if target is None and fmt in LOSSLESS:
            over_bd = bool(bit_depth and max_bd and bit_depth > max_bd)
            if out_sr is None and not over_bd:
                return None
            target, reason = fmt, "over the device's sample rate/bit depth limit"
        if target is None:
            return None
        if target == "mp3" and (out_sr or sample_rate or 0) > 48000:
            out_sr = 48000 if (out_sr or sample_rate or 0) % 44100 else 44100
        out_bd: int | None = None
        if target in ("alac", "flac"):
            src_bd = bit_depth or 16
            out_bd = min(src_bd, max_bd) if max_bd else src_bd
            if out_bd not in (16, 24):
                out_bd = 16 if out_bd < 24 else 24
        if target == fmt and out_sr is None and out_bd in (None, bit_depth) and not shrink:
            return None
        cap = (
            source_cap(fmt, source_kbps)
            if self.s.cap_to_source and target not in LOSSLESS
            else None
        )
        return Decision(target, quality, out_sr, out_bd, reason, cap)

    @staticmethod
    def _above_target(fmt: str, source_kbps: float | None, rule: TranscodeRule) -> bool:
        """A lossless source, or a lossy one whose bitrate (rounded up to a standard step) is above the rule's
        target: converting it makes it smaller. A lossy source of unknown bitrate is not known to be above."""
        if fmt in LOSSLESS or rule.to == "copy" or rule.to in LOSSLESS:
            return fmt in LOSSLESS and rule.to not in LOSSLESS
        target = rule.quality.bitrate_kbps or DEFAULT_KBPS[rule.to]
        cap = source_cap(fmt, source_kbps)
        return cap is not None and cap > target

    @staticmethod
    def _fallback(fmt: str, caps: DeviceCapabilities) -> str | None:
        order = ["alac", "flac", "aac", "mp3", "opus", "ogg"] if fmt in LOSSLESS else []
        order += ["aac", "mp3", "opus", "ogg"]
        return next((t for t in order if t in caps.formats), None)

    # --- plan ------------------------------------------------------------------------------------

    def plan(self, item: PlanItem, caps: DeviceCapabilities) -> PlanItem:
        if item.track.kind is MediaKind.VIDEO:
            return item  # the `video` processor's: an audio conversion would drop the picture
        f = item.file
        d = self.decide(
            f.format,
            f.sample_rate,
            f.bit_depth,
            caps,
            space_steps=item.enable_space_steps,
            source_kbps=f.bitrate_kbps or _size_kbps(f.size, item.track.duration_s),
        )
        if d is None:
            return item
        size = self._predict_size(item, d)
        planned = PlannedFile(
            format=d.to,
            codec=d.to,
            ext=EXT[d.to],
            size=size,
            bitrate_kbps=self._kbps(d) if d.to not in LOSSLESS else None,
            sample_rate=48000 if d.to == "opus" else d.sample_rate or f.sample_rate,
            bit_depth=d.bit_depth if d.to in LOSSLESS else None,
            encoding=self._encoding(d),
        )
        return item.model_copy(update={"file": planned, "steps": [*item.steps, self.type_name]})

    @staticmethod
    def _encoding(d: Decision) -> str | None:
        """The encoder mode `_args` picks that the planned bitrate does not show (VBR vs CBR)."""
        if d.to in LOSSLESS:
            return None
        if d.quality.vbr is not None:
            return f"vbr {d.quality.vbr}"
        return "q 6" if d.to == "ogg" and not d.quality.bitrate_kbps else "cbr"

    def _kbps(self, d: Decision) -> int:
        q = d.quality
        if q.vbr is None and d.max_kbps:
            return min(q.bitrate_kbps or DEFAULT_KBPS[d.to], d.max_kbps)
        if q.bitrate_kbps:
            return q.bitrate_kbps
        if q.vbr is not None:
            if d.to == "mp3":
                return LAME_VBR_KBPS[min(q.vbr, 9)]
            if d.to == "ogg":
                return VORBIS_Q_KBPS[q.vbr]
            if d.to == "aac":
                return [96, 96, 128, 160, 192, 256][min(q.vbr, 5)]
        return DEFAULT_KBPS[d.to]

    def _predict_size(self, item: PlanItem, d: Decision) -> int:
        f, t = item.file, item.track
        if d.to in LOSSLESS:
            src_rate = f.sample_rate or 44100
            src_bd = f.bit_depth or 16
            if f.format in LOSSLESS:
                flac_equiv = f.size / LOSSLESS_RATIO.get(f.format, 1.0)
            else:  # lossy → lossless: from PCM size at ~60 % compression
                pcm = (t.duration_s or 0) * src_rate * (t.channels or 2) * src_bd / 8
                flac_equiv = pcm * 0.6
            scale = ((d.sample_rate or src_rate) / src_rate) * ((d.bit_depth or src_bd) / src_bd)
            return int(flac_equiv * scale * LOSSLESS_RATIO[d.to])
        factor = CONTAINER_OVERHEAD * BITRATE_FACTOR.get(d.to, 1.0)
        return int(self._kbps(d) * 1000 / 8 * (t.duration_s or 0) * factor)

    def cache_key(self, item: PlanItem) -> str | None:
        """The planned output: everything `run` derives from the decision for this item.

        Not the whole settings: a rule that does not match this item (opus → mp3 in a FLAC library) must not
        change its key. Target, rate, depth and bitrate are in the planned file; the encoder mode in
        `encoding`. Called with an item this processor did not plan, the key falls back to the full settings.
        """
        f = item.file
        output = f"{f.format}:{f.sample_rate}:{f.bit_depth}:{f.bitrate_kbps}:{f.encoding}"
        if not item.steps or item.steps[-1] != self.type_name:
            return f"{self.settings_hash()}:{output}"
        return f"decision:{output}"

    # --- run -------------------------------------------------------------------------------------

    def _decide(
        self, info: ffmpeg.ProbeResult, item: WorkItem, caps: DeviceCapabilities
    ) -> Decision | None:
        return self.decide(
            _format_of(info.codec, info.container),
            info.sample_rate,
            info.bit_depth,
            caps,
            space_steps=item.plan.enable_space_steps,
            source_kbps=info.audio_bitrate / 1000 if info.audio_bitrate else None,
        )

    async def run(self, item: WorkItem, caps: DeviceCapabilities) -> WorkItem:
        if item.plan.track.kind is MediaKind.VIDEO:
            return item
        # the cheap in-process probe first: an item kept as it is needs no ffprobe run; one that is
        # converted is decided again on ffprobe's answer, as before
        quick = await asyncio.to_thread(ffmpeg.quick_probe, item.path)
        if quick is not None and self._decide(quick, item, caps) is None:
            return item
        info = await ffmpeg.probe(item.path)
        d = self._decide(info, item, caps)
        if d is None:
            return item
        fmt = _format_of(info.codec, info.container)
        out = item.work_dir / f"{item.path.stem}.{self.type_name}.{EXT[d.to]}"
        # music is measured for ReplayGain in the same pass (retag uses it instead of decoding again)
        measure = item.plan.track.kind is MediaKind.MUSIC
        args, encoder = self._args(d, item.path, out, measure)
        gain: tuple[float, float] | None = None
        try:
            if measure:
                stderr, gain = await ffmpeg.run_ffmpeg_measured(args)
            else:
                stderr = await ffmpeg.run_ffmpeg(args)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        damaged = ffmpeg.decode_errors(stderr)
        if damaged:
            out.unlink(missing_ok=True)
            raise CorruptSource(
                f"{item.path.name}: damaged audio data ({len(damaged)} errors): {damaged[0]}"
            )
        try:  # every tag and the cover of the source, mapped explicitly (ffmpeg loses many)
            await asyncio.to_thread(tagmap.carry_over, item.path, out)
        except Exception as e:  # noqa: BLE001 - the audio is fine; ffmpeg's own copy of the tags stays
            log.warning("transcode %s: tags not carried over: %s", item.path.name, e)
        res = await ffmpeg.probe(out)
        expected = (
            info.duration_s
        )  # what the source file itself claims (library metadata can be off)
        if (
            expected
            and res.duration_s is not None
            and res.duration_s < expected - max(MIN_SHORTFALL_S, SHORTFALL_FRACTION * expected)
        ):
            out.unlink(missing_ok=True)
            raise TruncatedSource(
                f"{item.path.name}: output is {res.duration_s:.1f} s, source should be {expected:.1f} s "
                "(truncated or corrupt source)"
            )
        planned = PlannedFile(
            format=d.to,
            codec=res.codec,
            ext=EXT[d.to],
            size=out.stat().st_size,
            bitrate_kbps=round(res.bitrate / 1000)
            if res.bitrate and d.to not in LOSSLESS
            else None,
            sample_rate=res.sample_rate,
            bit_depth=res.bit_depth if d.to in LOSSLESS else None,
        )
        log.debug("transcode %s: %s → %s (%s, %s)", item.path.name, fmt, d.to, d.reason, encoder)
        return item.model_copy(
            update={
                "path": out,
                "plan": item.plan.model_copy(update={"file": planned}),
                "steps_done": [*item.steps_done, self.type_name],
                "notes": {
                    **item.notes,
                    "encoder": encoder,
                    "transcode": f"{fmt}→{d.to}",
                    **({"ffmpeg_messages": stderr.strip()[-500:]} if stderr.strip() else {}),
                    **({"replaygain_measured": list(gain)} if gain else {}),
                },
            }
        )

    def _args(
        self, d: Decision, src: Path, out: Path, measure: bool = False
    ) -> tuple[list[str], str]:
        args = ["-i", ffmpeg.fpath(src), "-map_metadata", "0"]
        if d.to in ("opus", "ogg"):
            args += ["-vn"]  # no attached pictures in Ogg via ffmpeg
        else:
            args += ["-map", "0:v:0?", "-c:v", "copy", "-disposition:v", "attached_pic"]
        resample: list[str] = []
        if d.sample_rate:
            resample.append(str(d.sample_rate))
        if d.to in ("alac", "flac") and d.bit_depth:
            fmt = {"alac": {16: "s16p", 24: "s32p"}, "flac": {16: "s16", 24: "s32"}}[d.to][
                d.bit_depth
            ]
            resample.append(f"out_sample_fmt={fmt}")
            if d.bit_depth == 16:
                resample.append("dither_method=triangular")
        audio = "aresample=" + ":".join(resample) if resample else ""
        if (
            measure
        ):  # an asplit branch measures; the encoded audio is bit-identical to the plain graph
            enc = f"[o]{audio}[a]" if audio else "[o]anull[a]"
            graph = f"[0:a:0]asplit[o][m];{enc};[m]{ffmpeg.REPLAYGAIN_BRANCH}"
            args += ["-filter_complex", graph, "-map", "[a]"]
        else:
            args += ["-map", "0:a:0", *(["-af", audio] if audio else [])]
        encoder = {
            "alac": "alac",
            "flac": "flac",
            "mp3": "libmp3lame",
            "opus": "libopus",
            "ogg": "libvorbis",
            "aac": ffmpeg.aac_encoder(),
        }[d.to]
        args += ["-c:a", encoder]
        if d.to == "flac" and d.bit_depth == 24:
            args += ["-bits_per_raw_sample", "24"]
        q = d.quality
        if d.to not in LOSSLESS:
            if q.vbr is not None and d.to == "mp3":
                args += ["-q:a", str(min(q.vbr, 9))]
            elif q.vbr is not None and d.to == "ogg":
                args += ["-q:a", str(q.vbr)]
            elif q.vbr is not None and encoder == "libfdk_aac":
                args += ["-vbr", str(max(1, min(q.vbr, 5)))]
            elif d.to == "ogg" and not q.bitrate_kbps:
                args += ["-q:a", "6"]
            else:
                args += ["-b:a", f"{self._kbps(d)}k"]
        if d.to == "mp3":
            args += ["-id3v2_version", "3"]
        args.append(ffmpeg.fpath(out))
        return args, encoder
