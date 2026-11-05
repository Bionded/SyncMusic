# SPDX-License-Identifier: GPL-3.0-or-later
"""Transcode to fit: when a plan does not fit the device's free space, convert lossless files and lossy
files above a target bitrate, MP3 320 CBR first.

The offer is one transcode rule (`above_target_only`) put first into the device's `transcode` processor slot (a slot
is added at the front of the chain when there is none). Estimating plans with that chain instead of the device's
(duration x bitrate per converted file, `processors/transcode.py`); accepting stores it in the device config.
"""

from __future__ import annotations

from typing import Literal

from syncmusic.core.models import ProcessorSlot

FitTarget = Literal["mp3", "aac"]
# offered in this order, the first is the default
TARGETS: list[tuple[FitTarget, int]] = [
    ("mp3", 320),
    ("mp3", 256),
    ("mp3", 192),
    ("aac", 256),
    ("aac", 128),
]


def fit_rule(to: FitTarget, kbps: int) -> dict[str, object]:
    return {
        "from": [],
        "to": to,
        "when": "always",
        "quality": {"bitrate_kbps": kbps},
        "above_target_only": True,
    }


def with_fit_rule(processors: list[ProcessorSlot], to: FitTarget, kbps: int) -> list[ProcessorSlot]:
    """The device's processors with the fit rule first in its transcode slot (an earlier fit rule is replaced); a
    disabled transcode slot is enabled, a missing one is added first."""
    if (to, kbps) not in TARGETS:
        raise ValueError(
            f"no such target {to} {kbps}k (offered: {', '.join(f'{t} {k}k' for t, k in TARGETS)})"
        )
    out = [s.model_copy(deep=True) for s in processors]
    slot = next((s for s in out if s.type == "transcode"), None)
    if slot is None:
        slot = ProcessorSlot(type="transcode", settings={})
        out.insert(0, slot)
    rules = [r for r in slot.settings.get("rules", []) if not r.get("above_target_only")]
    slot.settings = {**slot.settings, "rules": [fit_rule(to, kbps), *rules]}
    slot.enabled = True
    return out
