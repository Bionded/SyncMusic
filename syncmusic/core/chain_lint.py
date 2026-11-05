# SPDX-License-Identifier: GPL-3.0-or-later
"""Warnings about processor chains that make no sense, each with a one-click fix.

`lint(slots, caps)` returns [{id, step, message, fix, processors}] - `processors` is the whole chain with
the fix applied, so the UI replaces its rows with it. The rules:
- `order`: a step that works on the finished file (tags, cover, lyrics) runs before Convert, which writes a new file;
  fix: put the steps in the order audio → tags → cover → sidecar.
- `duplicate`: the same step twice; fix: remove the later one.
- `unplayable`: Convert makes a format the device cannot play; fix: the best format it plays.
- `lossy_lossy`: Convert always re-encodes lossy files into another lossy format (MP3 → AAC loses quality twice);
  fix: only when the device cannot play them.
- `no_tags`: the tag step is missing or switched off, but every file needs the library's tags; fix:
  switch it on / add it.
- `lyrics_embedded`: lyrics are embedded for a device that reads .lrc files next to the track (Rockbox); fix: .lrc.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core import registry
from syncmusic.core.models import ProcessorSlot
from syncmusic.core.proc_templates import TAG_STEP, with_tag_step

LOSSY = frozenset({"mp3", "aac", "opus", "ogg", "wma"})
STAGES = ("audio", "tags", "cover", "sidecar")
NAMES = {
    "transcode": "Convert audio",
    "video": "Convert video",
    "retag": "Normalise tags",
    "covers": "Cover art",
    "lyrics": "Lyrics",
}


def _profile_rules(caps: DeviceCapabilities) -> list[dict[str, Any]]:
    """The Convert rules the quality profiles write for this device."""
    from syncmusic.core import quality

    out: list[dict[str, Any]] = []
    for p in quality.PROFILES:
        out += (quality._transcode(p, caps) or {}).get("rules", [])
    return [_plain_rule(r) for r in out]


def _plain_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """A Convert rule with its defaults filled in, so the form's serialisation (nulls, `false`s) compares equal
    to the stored one."""
    q = rule.get("quality") or {}
    return {
        "from": sorted(rule.get("from") or []),
        "to": rule.get("to"),
        "when": rule.get("when") or "not_supported",
        "quality": {k: v for k, v in q.items() if v is not None},
        "above_target_only": bool(rule.get("above_target_only")),
    }


def _name(type_name: str) -> str:
    return NAMES.get(type_name, type_name)


def _stage(slot: ProcessorSlot) -> int:
    try:
        return STAGES.index(registry.processor_class(slot.type).stage)
    except (KeyError, ValueError):
        return len(STAGES)


def _dump(slots: Sequence[ProcessorSlot]) -> list[dict[str, Any]]:
    return [s.model_dump(mode="json") for s in slots]


def _copy(slots: Sequence[ProcessorSlot]) -> list[ProcessorSlot]:
    return [s.model_copy(deep=True) for s in slots]


def _best_target(caps: DeviceCapabilities) -> str:
    return next((f for f in ("aac", "mp3", "opus", "ogg") if f in caps.formats), "mp3")


def lint(slots: Sequence[ProcessorSlot], caps: DeviceCapabilities) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def warn(id_: str, step: str, message: str, fix: str, fixed: list[ProcessorSlot]) -> None:
        out.append(
            {"id": id_, "step": step, "message": message, "fix": fix, "processors": _dump(fixed)}
        )

    on = [(i, s) for i, s in enumerate(slots) if s.enabled]
    # order: an enabled step of a later stage before one of an earlier stage
    stages = [_stage(s) for _, s in on]
    k = next((k for k in range(len(stages) - 1) if stages[k] > stages[k + 1]), None)
    if k is not None:
        late = on[k][1]
        warn(
            "order",
            late.type,
            f"{_name(late.type)} runs before a step that writes the file again, so its work is lost or redone",
            "Put the steps in order",
            sorted(_copy(slots), key=_stage),
        )
    seen: set[str] = set()
    for i, s in enumerate(slots):
        if s.type in seen:
            fixed = _copy(slots)
            del fixed[i]
            warn(
                "duplicate",
                s.type,
                f"{_name(s.type)} is in the chain twice",
                "Remove the second one",
                fixed,
            )
            break
        seen.add(s.type)
    for i, s in on:
        if s.type != "transcode":
            continue
        rules = s.settings.get("rules") or []
        for j, rule in enumerate(rules):
            to = rule.get("to")
            if caps.formats and to not in (None, "copy") and to not in caps.formats:
                fixed = _copy(slots)
                fixed[i].settings["rules"][j] = {**rule, "to": _best_target(caps)}
                warn(
                    "unplayable",
                    s.type,
                    f"Convert makes {str(to).upper()}, which this device cannot play",
                    f"Convert to {_best_target(caps).upper()} instead",
                    fixed,
                )
                break
            # no "from" = every format, the lossy ones too; a rule a quality profile writes is chosen on
            # purpose ("For a weak player" turns everything into MP3 192k) and is not warned
            sources = set(rule.get("from") or LOSSY)
            if (
                to in LOSSY
                and sources & LOSSY - {to}
                and rule.get("when", "not_supported") == "always"
                and not rule.get("above_target_only")
                and _plain_rule(rule) not in _profile_rules(caps)
            ):
                fixed = _copy(slots)
                fixed[i].settings["rules"][j] = {**rule, "when": "not_supported"}
                lossy = ", ".join(sorted(f.upper() for f in sources & LOSSY - {to}))
                warn(
                    "lossy_lossy",
                    s.type,
                    f"Convert always re-encodes {lossy} into {to.upper()}: lossy to lossy loses quality twice",
                    "Only when the device cannot play them",
                    fixed,
                )
                break
    tag = next((s for s in slots if s.type == TAG_STEP), None)
    if tag is None or not tag.enabled:
        fixed = _copy(slots)
        if tag is None:
            fixed = with_tag_step(fixed)
        else:
            next(s for s in fixed if s.type == TAG_STEP).enabled = True
        warn(
            "no_tags",
            TAG_STEP,
            "Without Normalise tags the files keep their own tags, not the library's",
            "Switch it on" if tag is not None else "Add it",
            fixed,
        )
    for i, s in on:
        if s.type == "lyrics" and caps.lyrics == "lrc_sidecar":
            embedded = [k for k in ("synced", "plain") if s.settings.get(k) == "embed"]
            if embedded:
                fixed = _copy(slots)
                fixed[i].settings = {**s.settings, **{k: "lrc" for k in embedded}}
                warn(
                    "lyrics_embedded",
                    s.type,
                    "This device shows lyrics from .lrc files next to the track, not from the file's tags",
                    "Write .lrc files",
                    fixed,
                )
    return out
