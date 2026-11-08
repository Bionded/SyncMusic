# SPDX-License-Identifier: GPL-3.0-or-later
"""Processor templates: reusable processor chains, applied to a device or linked to it.

Built-in templates are the suggested chains of each device type (read-only, can be copied). A linked device keeps a
copy of the template's chain in `DeviceConfig.processors` - the sync engine only ever reads that - and the copy is
renewed whenever the template changes (`linked_devices` + the facade).
"""

from __future__ import annotations

import functools
from collections.abc import Iterable

from syncmusic.core import registry
from syncmusic.core.models import DeviceConfig, ProcessorSlot, ProcessorTemplate

TAG_STEP = "retag"
CONVERT_STEP = "transcode"
# the convert step a chain without one gets: no rules, no sample-rate/bit-depth limits - it
# only converts formats the device cannot play (transcode's fallback), so files it plays stay untouched
FALLBACK_CONVERT = {"max_sample_rate": None, "max_bit_depth": None}


def with_tag_step(slots: list[ProcessorSlot]) -> list[ProcessorSlot]:
    """Every chain writes the library's tags into the file before it goes to the device: a chain
    without the tag step gets one, right after its audio step(s) (transcode) and before covers and sidecars.

    Every chain also converts what the device cannot play, never leaving it off: a chain
    without a convert step gets a fallback-only one first."""
    if not any(s.type == CONVERT_STEP and s.enabled for s in slots):
        slots = [ProcessorSlot(type=CONVERT_STEP, settings=dict(FALLBACK_CONVERT)), *slots]
    if any(s.type == TAG_STEP for s in slots):
        return slots
    pos = max((i + 1 for i, s in enumerate(slots) if _stage(s.type) == "audio"), default=0)
    return [*slots[:pos], ProcessorSlot(type=TAG_STEP), *slots[pos:]]


def normalised_chain(slots: list[ProcessorSlot]) -> list[tuple[str, dict[str, object]]]:
    """A chain as the sync engine runs it: the enabled slots (plus the implied tag/convert steps), each with its
    settings validated and the defaults filled in. Two chains that are equal here process every file alike."""
    out: list[tuple[str, dict[str, object]]] = []
    for s in with_tag_step(slots):
        if not s.enabled:
            continue
        try:
            settings = registry.processor_class(s.type).Settings.model_validate(s.settings)
        except (KeyError, ValueError):  # unknown type / invalid settings: compare as written
            out.append((s.type, dict(s.settings)))
            continue
        out.append((s.type, settings.model_dump(mode="json")))
    return out


def same_chain(a: list[ProcessorSlot], b: list[ProcessorSlot]) -> bool:
    return normalised_chain(a) == normalised_chain(b)


def _stage(type_name: str) -> str | None:
    try:
        return registry.processor_class(type_name).stage
    except KeyError:  # an unknown type: validation names it
        return None


BUILTIN_NAMES = {
    "ipod": "iPod: ALAC/AAC",
    "rockbox": "Rockbox: MP3 320",  # the id stays `builtin-rockbox`: devices linked to it keep their chain
    "flashdrive": "Flash drive: copy",
    "ftp": "FTP server: copy",
    "hiby": "HiBy: copy",
    "smb": "SMB share: copy",
}

# the processor template a new device of the type links when Register names none; other types get
# their suggested chain, unlinked
DEFAULT_PROCESSOR_TEMPLATES = {"rockbox": "builtin-rockbox-copy"}


@functools.cache
def builtin_processor_templates() -> dict[str, ProcessorTemplate]:
    from syncmusic.core.sync import suggested_processors

    out = {}
    for cls in registry.device_classes():
        if cls.type_name == "rockbox":
            # files as they are (Rockbox plays FLAC/ALAC), only tags, covers and lyrics; the
            # default for new Rockbox devices, listed before "Rockbox: MP3 320"
            copy = cls.capabilities.model_copy(update={"prefer_lossy": False})
            out["builtin-rockbox-copy"] = ProcessorTemplate(
                id="builtin-rockbox-copy",
                name="Rockbox: copy",
                device_type=cls.type_name,
                processors=suggested_processors(copy),
                builtin=True,
            )
        tid = f"builtin-{cls.type_name}"
        out[tid] = ProcessorTemplate(
            id=tid,
            name=BUILTIN_NAMES.get(cls.type_name, f"{cls.display_name}: suggested"),
            device_type=cls.type_name,
            processors=suggested_processors(cls.capabilities),
            builtin=True,
        )
    return out


def linked_devices(template_id: str, devices: Iterable[DeviceConfig]) -> list[DeviceConfig]:
    return [d for d in devices if d.processor_template_id == template_id]
