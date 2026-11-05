# SPDX-License-Identifier: GPL-3.0-or-later
"""Suggested processor template at registration.

Every offered template's chain plans each library item (the planner's own size estimate); the template that plays
everything and fits with the fewest conversions wins. When none does, a chain is generated from the type's default:
a `not_supported` rule per unplayable audio format, then Convert-to-fit until it fits. Plain rules, no I/O here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core.fit import TARGETS as FIT_TARGETS
from syncmusic.core.fit import with_fit_rule
from syncmusic.core.models import MediaKind, ProcessorSlot, ProcessorTemplate, Track
from syncmusic.core.prepare import Chain, initial_plan_item
from syncmusic.core.proc_templates import with_tag_step
from syncmusic.processors.transcode import LOSSLESS


@dataclass
class Score:
    id: str
    name: str
    needed: int = 0
    unplayable: Counter[str] = field(
        default_factory=Counter
    )  # planned format -> items the device cannot play
    converted: int = 0
    converted_lossless: int = 0

    def fits(self, free: int | None) -> bool:
        return free is None or self.needed <= free


@dataclass
class Suggestion:
    template_id: str | None
    draft: ProcessorTemplate | None
    reason: dict[str, object]  # {key, **params}: the UI words it (i18n)
    candidates: list[Score]
    variants: list[Variant] = field(default_factory=list)
    best_variant: int | None = None


def score(
    tpl_id: str,
    name: str,
    processors: Sequence[ProcessorSlot],
    tracks: Sequence[Track],
    caps: DeviceCapabilities,
) -> Score:
    chain = Chain.from_slots(list(processors))
    s = Score(tpl_id, name)
    for t in tracks:
        planned = chain.plan(initial_plan_item(t), caps).file
        s.needed += planned.size
        if caps.formats and planned.format not in caps.formats:
            s.unplayable[planned.format] += 1
        if planned.format != t.format:
            s.converted += 1
            s.converted_lossless += t.format in LOSSLESS
    return s


def _target(fmt: str, caps: DeviceCapabilities) -> tuple[str, int | None]:
    """What a format the device cannot play becomes: lossless → ALAC if played, lossy → AAC 256 if played,
    else MP3 320."""
    if fmt in LOSSLESS and (not caps.formats or "alac" in caps.formats):
        return "alac", None
    if fmt not in LOSSLESS and (not caps.formats or "aac" in caps.formats):
        return "aac", 256
    return "mp3", 320


def generate(
    base: Sequence[ProcessorSlot],
    unplayable: Sequence[str],
    caps: DeviceCapabilities,
) -> list[ProcessorSlot]:
    """`base` with a transcode rule for each unplayable format (first in its transcode slot)."""
    out = [s.model_copy(deep=True) for s in base]
    if not unplayable:
        return out
    slot = next((s for s in out if s.type == "transcode"), None)
    if slot is None:
        slot = ProcessorSlot(type="transcode", settings={})
        out.insert(0, slot)
    rules = []
    for fmt in sorted(unplayable):
        to, kbps = _target(fmt, caps)
        rule: dict[str, object] = {"from": [fmt], "to": to, "when": "not_supported"}
        if kbps:
            rule["quality"] = {"bitrate_kbps": kbps}
        rules.append(rule)
    slot.settings = {**slot.settings, "rules": [*rules, *slot.settings.get("rules", [])]}
    slot.enabled = True
    return with_tag_step(out)


def suggest(
    templates: Sequence[ProcessorTemplate],
    tracks: Sequence[Track],
    caps: DeviceCapabilities,
    free: int | None,
    *,
    default_id: str | None = None,
    device_type: str = "",
    device_name: str = "",
) -> Suggestion:
    """The best template for `tracks` on a device with `caps` and `free` bytes (None: unknown), or a generated
    draft when none plays everything and fits."""
    items = [t for t in tracks if caps.supports_kind(t.kind)]
    scores = [score(t.id, t.name, t.processors, items, caps) for t in templates]
    by_id = {t.id: t for t in templates}
    n = len(items)
    good = [s for s in scores if not s.unplayable and s.fits(free)]
    base_tpl = by_id.get(default_id or "") or (templates[0] if templates else None)
    base = list(base_tpl.processors) if base_tpl else [ProcessorSlot(type="retag")]
    vs = variants(base, items, caps, free) if items else []
    best_v = best_variant(vs, free) if vs else None
    if good:

        def rank(s: Score) -> tuple[int, int, int, str]:
            tpl = by_id[s.id]
            return (s.converted, s.id != default_id, not tpl.builtin, s.name)

        best = min(good, key=rank)
        return Suggestion(best.id, None, _reason(best, n, free, scores), scores, vs, best_v)
    # generate: the type's default chain (else the first template's), plus rules for what it cannot play
    first = score("", "", base, items, caps)
    audio = [f for f in first.unplayable if f not in _video_formats(items)]
    procs = generate(base, audio, caps)
    s = score("", "", procs, items, caps)
    if not s.fits(free):
        for to, kbps in FIT_TARGETS:
            if to != "mp3":
                continue
            procs = with_fit_rule(generate(base, audio, caps), to, kbps)
            s = score("", "", procs, items, caps)
            if s.fits(free):
                break
    draft = ProcessorTemplate(
        id="",
        name=f"{device_name}: suggested" if device_name else "Suggested",
        device_type=device_type or None,
        processors=procs,
    )
    s.name = draft.name
    reason = _reason(s, n, free, scores)
    reason["generated"] = True
    reason["formats"] = sorted(audio)
    return Suggestion(None, draft, reason, scores, vs, best_v)


@dataclass
class Variant:
    """A generated chain to choose from: `key` + `to`/`quality` name it (the UI words it),
    `rank` orders by loss (0 = files as they are)."""

    key: str  # copy | lossless | lossy | fit
    to: str | None
    quality: dict[str, int]
    rank: int
    processors: list[ProcessorSlot]
    score: Score


# lossy targets of the variants, least loss first: AAC 256 / MP3 320 / MP3 V0 / Opus / Vorbis
LOSSY_VARIANTS: list[tuple[str, dict[str, int]]] = [
    ("aac", {"bitrate_kbps": 256}),
    ("mp3", {"bitrate_kbps": 320}),
    ("mp3", {"vbr": 0}),
    ("opus", {"bitrate_kbps": 160}),
    ("ogg", {"vbr": 6}),
]


def _with_rule(base: Sequence[ProcessorSlot], rule: Mapping[str, object]) -> list[ProcessorSlot]:
    """`base` with `rule` first in its (enabled) transcode slot."""
    out = with_tag_step([s.model_copy(deep=True) for s in base])
    slot = next(s for s in out if s.type == "transcode" and s.enabled)
    slot.settings = {**slot.settings, "rules": [rule, *slot.settings.get("rules", [])]}
    return out


def variants(
    base: Sequence[ProcessorSlot],
    tracks: Sequence[Track],
    caps: DeviceCapabilities,
    free: int | None,
) -> list[Variant]:
    """Chains generated from `base` (the type's default chain), each scored on `tracks`: the files as they are
    (unplayable formats converted), lossless kept as ALAC/FLAC, lossless → each lossy target the device plays,
    and "convert the lossless files only when they do not fit" (the planner's space step: all of them or none).
    Variants that come out identical to an earlier one are dropped."""
    items = [t for t in tracks if caps.supports_kind(t.kind)]

    def plays(fmt: str) -> bool:
        return not caps.formats or fmt in caps.formats

    lossless = sorted(LOSSLESS)
    out: list[Variant] = []

    def add(
        key: str, to: str | None, quality: dict[str, int], rank: int, procs: list[ProcessorSlot]
    ) -> None:
        sc = score("", "", procs, items, caps)
        if any((v.score.needed, v.score.converted) == (sc.needed, sc.converted) for v in out):
            return
        out.append(Variant(key, to, quality, rank, procs, sc))

    copy = with_tag_step([s.model_copy(deep=True) for s in base])
    add("copy", None, {}, 0, copy)
    for to in ("alac", "flac"):
        if plays(to):
            rule: dict[str, object] = {
                "from": [f for f in lossless if f != to],
                "to": to,
                "when": "always",
            }
            add("lossless", to, {}, 1, _with_rule(base, rule))
    targets = [(to, q) for to, q in LOSSY_VARIANTS if plays(to)]
    if targets:
        to, q = next(((t, q) for t, q in targets if t == "mp3"), targets[0])
        fit: dict[str, object] = {"from": lossless, "to": to, "when": "space_needed", "quality": q}
        procs = _with_rule(base, fit)
        sc = score("", "", copy, items, caps)
        if not sc.fits(free):  # the space step converts every lossless file (core/space.py)
            sc = score("", "", _with_rule(base, {**fit, "when": "always"}), items, caps)
        out.append(Variant("fit", to, q, 2, procs, sc))
    for i, (to, q) in enumerate(targets):
        lossy: dict[str, object] = {"from": lossless, "to": to, "when": "always", "quality": q}
        add("lossy", to, q, 3 + i, _with_rule(base, lossy))
    return out


def best_variant(vs: Sequence[Variant], free: int | None) -> int:
    """Index of the variant that fits with the least loss; none fits → the smallest."""
    fitting = [i for i, v in enumerate(vs) if v.score.fits(free)]
    if fitting:
        return min(fitting, key=lambda i: vs[i].rank)
    return min(range(len(vs)), key=lambda i: vs[i].score.needed)


def _video_formats(items: Sequence[Track]) -> set[str]:
    return {t.format for t in items if t.kind == MediaKind.VIDEO}


def _reason(s: Score, n: int, free: int | None, scores: Sequence[Score]) -> dict[str, object]:
    out: dict[str, object] = {"files": n, "needed": s.needed, "free": free, "fits": s.fits(free)}
    if s.unplayable:
        out["key"] = "unplayable"
        out["unplayable"] = sum(s.unplayable.values())
    elif not s.converted:
        out["key"] = "copy"
    else:
        out["key"] = "convert"
        out["converted"] = s.converted
        # what copying as it is would need: the candidate converting least
        copies = [c for c in scores if not c.converted]
        if copies:
            out["as_is"] = min(c.needed for c in copies)
    return out
