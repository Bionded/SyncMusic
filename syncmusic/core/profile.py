# SPDX-License-Identifier: GPL-3.0-or-later
"""Device profile: what a device really handles - codecs, sample rate
and bit depth, track limit, cover size, playlists, media kinds - and where each value comes from.

Resolution, later wins: the device class's defaults → the known-models database (`known_models.yaml`, the entry
of the detected model, else the type's `generic` entry) → what the device reports itself (pygpod's model table,
the installed Rockbox codecs and features) → what the device's settings choose (Rockbox/flash/FTP formats and
playlists, iPod covers) → a overrides (`DeviceConfig.capability_overrides`, the profile card).
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from syncmusic.base.capabilities import COVER_SIZE, DeviceCapabilities

KNOWN_MODELS = Path(__file__).with_name("known_models.yaml")
GENERIC = "generic"

# the fields a profile shows and the database may set; all are capability fields except `cover_size`
PROFILE_FIELDS: tuple[str, ...] = (
    "formats",
    "max_sample_rate",
    "max_bit_depth",
    "max_tracks",
    COVER_SIZE,
    "supports_playlists",
    "supports_music",
    "supports_podcasts",
    "supports_audiobooks",
    "has_rtc",
    "prefer_lossy",
)

Source = Literal["default", "known_model", "detected", "setting", "override"]


class KnownModel(BaseModel):
    type_name: str
    model: str  # model id, or GENERIC
    label: str
    values: dict[str, Any] = {}


class ProfileValue(BaseModel):
    value: Any
    source: Source


class DeviceProfile(BaseModel):
    model: str | None = None  # detected model id
    label: str | None = None  # name from the database
    # the detected model has its own database entry (not only the type's generic one)
    known: bool = False
    fields: dict[str, ProfileValue] = {}

    def values(self, *sources: Source) -> dict[str, Any]:
        """Field values from the given sources (all when none given)."""
        return {k: f.value for k, f in self.fields.items() if not sources or f.source in sources}


def _profile_value(caps: DeviceCapabilities, name: str) -> Any:
    if name == COVER_SIZE:
        return caps.cover.max_size if caps.cover else None
    return getattr(caps, name)


def _check(type_name: str, model: str, values: dict[str, Any]) -> None:
    unknown = set(values) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(
            f"{KNOWN_MODELS.name}: {type_name}/{model}: unknown fields {sorted(unknown)}"
        )
    DeviceCapabilities().with_overrides(values)  # values of the right type (raises ValidationError)


@cache
def known_models() -> dict[str, dict[str, KnownModel]]:
    """The database: type -> model id -> entry (validated once)."""
    raw = yaml.safe_load(KNOWN_MODELS.read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, KnownModel]] = {}
    for type_name, models in raw.items():
        out[type_name] = {}
        for model, entry in (models or {}).items():
            values = dict(entry or {})
            label = str(values.pop("label", model))
            _check(type_name, model, values)
            out[type_name][str(model)] = KnownModel(
                type_name=type_name, model=str(model), label=label, values=values
            )
    return out


def known_model(type_name: str, model: str | None) -> KnownModel | None:
    """The database entry of `model` (case-insensitive), else the type's generic one, else None."""
    models = known_models().get(type_name, {})
    wanted = (model or GENERIC).casefold()
    hit = next((m for k, m in models.items() if k.casefold() == wanted), None)
    return hit or models.get(GENERIC)


def resolve_profile(
    type_name: str,
    model: str | None,
    defaults: DeviceCapabilities,
    detected: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> DeviceProfile:
    """The profile of a `type_name` device of `model`: defaults, then the database entry, then `detected`, then
    `settings`, then `overrides` (only profile fields count; other keys are ignored here)."""
    fields = {
        n: ProfileValue(value=_profile_value(defaults, n), source="default") for n in PROFILE_FIELDS
    }
    entry = known_model(type_name, model)
    layers: list[tuple[Source, Mapping[str, Any]]] = [
        ("known_model", entry.values if entry else {}),
        ("detected", detected or {}),
        ("setting", settings or {}),
        ("override", overrides or {}),
    ]
    for source, values in layers:
        for name, value in values.items():
            if name in fields:
                fields[name] = ProfileValue(value=value, source=source)
    return DeviceProfile(
        model=model,
        label=entry.label if entry else None,
        known=entry is not None and entry.model != GENERIC,
        fields=fields,
    )


def apply_profile(caps: DeviceCapabilities, profile: DeviceProfile) -> DeviceCapabilities:
    """`caps` with every profile value that does not come from the defaults."""
    return caps.with_overrides(profile.values("known_model", "detected", "setting", "override"))
