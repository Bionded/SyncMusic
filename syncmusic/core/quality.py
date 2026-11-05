# SPDX-License-Identifier: GPL-3.0-or-later
"""Quality profiles: a whole processor chain in one choice, adapted to what the device plays.

- `max` - Maximum quality: files the device plays stay as they are; lossless it cannot play becomes ALAC or FLAC when
  it plays one of them (else MP3 320), lossy it cannot play becomes AAC 256 or MP3 320.
- `space` - Save space: every lossless file becomes MP3 V2 (VBR ~190 kbit/s; AAC VBR when the device has no MP3),
  lossy files the device cannot play too; lossy files it plays stay.
- `weak` - For a weak player: everything becomes MP3 192 kbit/s CBR at 44.1 kHz / 16 bit, covers at most 300 px.

The rest of the chain (tags, covers, lyrics) is the device type's suggestion, so every profile keeps the tag step. Choosing a profile replaces the chain; editing a step by hand makes the chain "Custom" again."""

from __future__ import annotations

from typing import Any, Literal

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core import registry
from syncmusic.core.models import ProcessorSlot
from syncmusic.core.proc_templates import with_tag_step

Profile = Literal["max", "space", "weak"]
PROFILES: tuple[Profile, ...] = ("max", "space", "weak")
NAMES = {"max": "Maximum quality", "space": "Save space", "weak": "For a weak player"}
LOSSLESS_SOURCES = ["flac", "alac", "wav", "aiff", "ape", "wv"]
LOSSY_SOURCES = ["mp3", "aac", "opus", "ogg", "wma"]
WEAK_COVER_PX = 300


def _transcode(profile: Profile, caps: DeviceCapabilities) -> dict[str, Any] | None:
    fmts = caps.formats
    lossy_target = "aac" if "aac" in fmts else "mp3"
    if profile == "max":
        lossless_target = next((f for f in ("flac", "alac") if f in fmts), "mp3")
        q_lossless = {} if lossless_target != "mp3" else {"bitrate_kbps": 320}
        rules = [
            {
                "from": [f for f in LOSSLESS_SOURCES if f not in fmts],
                "to": lossless_target,
                "quality": q_lossless,
            },
            {
                "from": [f for f in LOSSY_SOURCES if f not in fmts],
                "to": lossy_target,
                "quality": {"bitrate_kbps": 256 if lossy_target == "aac" else 320},
            },
        ]
        rules = [r for r in rules if r["from"]]
        return {"rules": rules} if rules else None
    if profile == "space":
        target = "mp3" if "mp3" in fmts else lossy_target
        vbr = {"vbr": 2} if target == "mp3" else {"vbr": 4}
        rules = [{"from": LOSSLESS_SOURCES, "to": target, "when": "always", "quality": vbr}]
        unplayable = [f for f in LOSSY_SOURCES if f not in fmts]
        if unplayable:
            rules.append({"from": unplayable, "to": target, "quality": vbr})
        return {"rules": rules}
    return {
        "rules": [{"from": [], "to": "mp3", "when": "always", "quality": {"bitrate_kbps": 192}}],
        "max_sample_rate": 44100,
        "max_bit_depth": 16,
    }


def chain(
    profile: Profile, caps: DeviceCapabilities, suggested: list[ProcessorSlot]
) -> list[ProcessorSlot]:
    """The device's chain for `profile`: its suggested non-audio steps with the profile's conversion."""
    rest = [s.model_copy(deep=True) for s in suggested if s.type != "transcode"]
    if profile == "weak":
        rest = [
            s.model_copy(update={"settings": {**s.settings, "max_size": WEAK_COVER_PX}})
            if s.type == "covers"
            else s
            for s in rest
        ]
    settings = _transcode(profile, caps)
    slots = ([ProcessorSlot(type="transcode", settings=settings)] if settings else []) + rest
    # every profile is a valid chain for the device (contract with the processors' settings)
    for slot in slots:
        registry.processor_class(slot.type).Settings.model_validate(slot.settings)
    return with_tag_step(slots)
