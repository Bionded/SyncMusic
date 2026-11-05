# SPDX-License-Identifier: GPL-3.0-or-later
"""`covers` processor: prepare, embed, write or remove cover art.

Source image, in order: `WorkItem.cover` (the prepare pipeline puts the library's cover endpoint image there),
else the picture embedded in the file. The image is resized to fit `max_size` × `max_size` (never upscaled)
and encoded as baseline (or progressive) JPEG or PNG.

Modes:
- `embed`   - replace the file's embedded pictures with the prepared image;
- `sidecar` - write the prepared image as an album-folder file (`WorkItem.sidecars["cover.jpg"]`);
              optionally strip the embedded pictures;
- `remove`  - strip embedded pictures (saves space);
- `device`  - only prepare `WorkItem.cover` for a device that stores artwork in its own DB (iPod/pygpod).

`WorkItem.sidecars` keys starting with "." are per-track files (`<track basename><ext>`); other keys are
file names in the track's folder.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from pathlib import Path
from typing import Any, ClassVar, Literal

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from syncmusic.base.capabilities import CoverSpec, DeviceCapabilities
from syncmusic.base.processor import Processor
from syncmusic.core.models import MediaKind, PlanItem, ProcessorSlot, WorkItem
from syncmusic.processors.common import work_copy

log = logging.getLogger(__name__)

Mode = Literal["embed", "sidecar", "remove", "device"]
FORMATS = frozenset({"mp3", "aac", "alac", "flac", "ogg", "opus"})
LOSSLESS = frozenset({"alac", "flac", "wav", "aiff"})
JPEG_QUALITY = 90
BYTES_PER_PIXEL = {"jpeg": 0.25, "png": 1.5}  # rough size of a resized cover, for plan()
TAG_BYTES = 8192  # text tags + padding in the size estimate


def audio_bytes(item: PlanItem) -> int:
    """Estimated file size without embedded pictures: bitrate × duration + tags for lossy files.

    Lossless bitrates are unreliable (often the nominal PCM rate), so lossless files keep their size; after
    a planned transcode the size is already an audio-only prediction.
    """
    f, t = item.file, item.track
    if "transcode" in item.steps:
        return f.size
    if f.bitrate_kbps and t.duration_s and f.format not in LOSSLESS:
        estimate = int(f.bitrate_kbps * 125 * t.duration_s) + TAG_BYTES
        return min(f.size, estimate)
    return f.size


class CoversSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Mode = "embed"
    max_size: int | None = Field(None, ge=16, le=4096)  # None = device cover spec (default 500)
    format: Literal["jpeg", "png"] | None = None  # None = device cover spec
    progressive: bool | None = None  # None = device cover spec (Rockbox needs baseline)
    sidecar_name: str = "cover.jpg"
    strip_embedded: bool = False  # sidecar mode: also remove embedded pictures


def _spec(s: CoversSettings, caps: DeviceCapabilities) -> CoverSpec:
    base = caps.cover or CoverSpec()
    return CoverSpec(
        max_size=s.max_size or base.max_size,
        format=s.format or base.format,
        progressive=base.progressive if s.progressive is None else s.progressive,
        embed=base.embed,
        sidecar_name=base.sidecar_name,
    )


def prepare_image(data: bytes, spec: CoverSpec) -> bytes:
    """Resize to fit spec.max_size (no upscaling) and encode per spec. Raises ValueError for non-images."""
    try:
        img: Image.Image = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError(f"not an image: {e}") from e
    if max(img.size) > spec.max_size:
        img.thumbnail((spec.max_size, spec.max_size), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if spec.format == "png":
        img.save(out, "PNG", optimize=True)
    else:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(out, "JPEG", quality=JPEG_QUALITY, progressive=spec.progressive, optimize=True)
    return out.getvalue()


def embedded_picture(audio: Any) -> bytes | None:
    """Front cover (or first picture) embedded in a mutagen file object."""
    if type(audio).__name__ == "MP3" and audio.tags is not None:
        pics = audio.tags.getall("APIC")
        pics.sort(key=lambda p: p.type != 3)
        return bytes(pics[0].data) if pics else None
    if isinstance(audio, MP4) and audio.tags is not None:
        covr = audio.tags.get("covr")
        return bytes(covr[0]) if covr else None
    if isinstance(audio, FLAC):
        pics = sorted(audio.pictures, key=lambda p: p.type != 3)
        return bytes(pics[0].data) if pics else None
    if isinstance(audio, OggVorbis | OggOpus) and audio.tags is not None:
        for b64 in audio.tags.get("METADATA_BLOCK_PICTURE", []):
            try:
                return bytes(Picture(base64.b64decode(b64)).data)
            except (ValueError, mutagen.MutagenError):
                continue
    return None


def _handled(audio: Any) -> bool:
    return type(audio).__name__ == "MP3" or isinstance(audio, MP4 | FLAC | OggVorbis | OggOpus)


def set_pictures(path: Path, image: bytes | None, mime: str) -> None:
    """Replace all embedded pictures of `path` with `image` (None = remove them)."""
    audio: Any = mutagen.File(path)
    if type(audio).__name__ == "MP3":
        try:
            id3 = ID3(path)
        except mutagen.id3.ID3NoHeaderError:
            id3 = ID3()
        version = id3.version[1] if id3.version >= (2, 3, 0) else 4
        id3.delall("APIC")
        if image is not None:
            id3.add(APIC(encoding=3, mime=mime, type=3, desc="", data=image))
        id3.save(path, v2_version=3 if version == 3 else 4)
        return
    if audio.tags is None:
        audio.add_tags()
    if isinstance(audio, MP4):
        assert audio.tags is not None
        if image is None:
            audio.tags.pop("covr", None)
        else:
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio.tags["covr"] = [MP4Cover(image, imageformat=fmt)]
        audio.save()
        return
    pic = Picture()
    if image is not None:
        pic.data, pic.mime, pic.type = image, mime, 3
        with Image.open(io.BytesIO(image)) as img:
            pic.width, pic.height = img.size
            pic.depth = 24
    if isinstance(audio, FLAC):
        audio.clear_pictures()
        if image is not None:
            audio.add_picture(pic)
    else:  # Ogg Vorbis/Opus
        if "METADATA_BLOCK_PICTURE" in audio.tags:
            del audio.tags["METADATA_BLOCK_PICTURE"]
        if image is not None:
            audio.tags["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
    audio.save()


class Covers(Processor):
    type_name = "covers"
    display_name = "Cover art"
    Settings: ClassVar[type[BaseModel]] = CoversSettings
    stage: ClassVar[Literal["audio", "tags", "cover", "sidecar"]] = "cover"
    version: ClassVar[int] = 2  # 2: remove mode drops WorkItem.cover
    needs: ClassVar[frozenset[Literal["cover", "lyrics"]]] = frozenset({"cover"})

    def __init__(self, settings: BaseModel | dict[str, object], ctx: Any) -> None:
        super().__init__(settings, ctx)
        if self.s.mode == "remove":
            self.needs = frozenset()  # type: ignore[misc]  # only strips pictures: nothing to fetch

    @property
    def s(self) -> CoversSettings:
        settings: CoversSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def suggest(cls, caps: DeviceCapabilities) -> ProcessorSlot | None:
        if caps.needs_device_db:
            return ProcessorSlot(type=cls.type_name, settings={"mode": "device"})
        if caps.cover is None:
            return None
        if caps.cover.embed:
            return ProcessorSlot(type=cls.type_name, settings={"mode": "embed"})
        return ProcessorSlot(
            type=cls.type_name,
            settings={"mode": "sidecar", "sidecar_name": caps.cover.sidecar_name or "cover.jpg"},
        )

    def plan(self, item: PlanItem, caps: DeviceCapabilities) -> PlanItem:
        if item.track.kind is MediaKind.VIDEO:
            return item  # a video's picture is its own
        mode = self.s.mode
        if mode == "remove":
            if item.file.format not in FORMATS or not item.track.has_cover:
                return item
            file = item.file.model_copy(update={"size": audio_bytes(item)})
            return item.model_copy(update={"file": file, "steps": [*item.steps, self.type_name]})
        if not item.track.has_cover:
            return item
        spec = _spec(self.s, caps)
        image_bytes = int(spec.max_size**2 * BYTES_PER_PIXEL[spec.format])
        if mode == "embed":
            if item.file.format not in FORMATS:
                return item
            # the old embedded picture (if any) is replaced by the new one
            file = item.file.model_copy(update={"size": audio_bytes(item) + image_bytes})
            return item.model_copy(update={"file": file, "steps": [*item.steps, self.type_name]})
        if mode == "sidecar":
            file = item.file
            if self.s.strip_embedded and file.format in FORMATS:
                file = file.model_copy(update={"size": audio_bytes(item)})
            return item.model_copy(
                update={
                    "file": file,
                    "sidecars": sorted({*item.sidecars, self.s.sidecar_name}),
                    "sidecar_bytes": item.sidecar_bytes + image_bytes,
                    "steps": [*item.steps, self.type_name],
                }
            )
        return item.model_copy(update={"steps": [*item.steps, self.type_name]})  # device

    async def run(self, item: WorkItem, caps: DeviceCapabilities) -> WorkItem:
        if item.plan.track.kind is MediaKind.VIDEO:
            return item
        return await asyncio.to_thread(self._run, item, caps)

    def _run(self, item: WorkItem, caps: DeviceCapabilities) -> WorkItem:
        mode = self.s.mode
        try:
            audio = mutagen.File(item.path)
        except mutagen.MutagenError:
            audio = None
        handled = audio is not None and _handled(audio)
        if mode == "remove":
            item = item.model_copy(update={"cover": None})  # no artwork for the device either
            if not handled or embedded_picture(audio) is None:
                return item
            path = work_copy(item, self.type_name)
            set_pictures(path, None, "")
            return self._done(item, path)

        spec = _spec(self.s, caps)
        source = item.cover.read_bytes() if item.cover is not None and item.cover.exists() else None
        if source is None and handled:
            source = embedded_picture(audio)
        if source is None:
            return item
        try:
            image = prepare_image(source, spec)
        except ValueError as e:
            log.warning("covers: %s: %s", item.path.name, e)
            return item
        ext = "png" if spec.format == "png" else "jpg"
        cover = item.work_dir / f"{item.path.stem}.cover.{ext}"
        item.work_dir.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(image)
        update: dict[str, Any] = {"cover": cover}
        path = item.path
        if mode == "embed" and handled:
            path = work_copy(item, self.type_name)
            set_pictures(path, image, f"image/{spec.format}")
        elif mode == "sidecar":
            update["sidecars"] = {**item.sidecars, self.s.sidecar_name: cover}
            if self.s.strip_embedded and handled and embedded_picture(audio) is not None:
                path = work_copy(item, self.type_name)
                set_pictures(path, None, "")
        return self._done(item.model_copy(update=update), path)

    def _done(self, item: WorkItem, path: Path) -> WorkItem:
        file = item.plan.file.model_copy(update={"size": path.stat().st_size})
        return item.model_copy(
            update={
                "path": path,
                "plan": item.plan.model_copy(update={"file": file}),
                "steps_done": [*item.steps_done, self.type_name],
            }
        )
