# SPDX-License-Identifier: GPL-3.0-or-later
"""Space calculation: does the plan fit, and what to do when it does not.

`needed = Σ size_delta (adds + replaces − removals) + reserve`, `reserve = safety margin (SyncSettings: % of
capacity, at least min, at most max bytes) + Device.commit_reserve(items after the plan)` (device DB + manifest).
If `needed > free`:
1. re-plan with the transcode `when: space_needed` rules enabled; keep that plan if it needs less;
2. still too big → `does_not_fit` with a SpaceReport (bytes per rule, suggestions); auto mode must not apply it;
3. `force` → adds/replaces are taken in priority order (selection order: rule priority, then each rule's own
   sort) while they fit; the rest is dropped (`SpaceReport.dropped`). A dropped replace keeps the old version.

Per-rule totals count every item under the first (highest-priority) rule that selected it, so they add up.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from typing import Literal

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.device import UNKNOWN_SPACE
from syncmusic.core.models import (
    ActionKind,
    DbSize,
    DeviceConfig,
    LeftOut,
    LibrarySnapshot,
    Manifest,
    PlanAction,
    RuleSpace,
    RuleTemplate,
    SpaceReport,
    SyncPlan,
    SyncSettings,
)
from syncmusic.core.planner import human_size as _human
from syncmusic.core.planner import plan_sync
from syncmusic.core.prepare import LOSSLESS, Chain
from syncmusic.core.rules.evaluator import resolve_rules

ON_DEVICE = (ActionKind.ADD, ActionKind.REPLACE, ActionKind.KEEP, ActionKind.UPDATE_META)


def safety_margin(capacity: int, s: SyncSettings) -> int:
    """`pct` of the capacity, clamped to [min, max] (at most ~500 MB by default; min wins over max)."""
    return max(
        min(int(capacity * s.safety_margin_pct / 100), s.safety_margin_max_bytes),
        s.safety_margin_min_bytes,
    )


def _final_size(a: PlanAction) -> int:
    """Bytes the item occupies on the device after the plan."""
    if a.kind in (ActionKind.ADD, ActionKind.REPLACE) and a.item is not None:
        return a.item.file.size + a.item.sidecar_bytes
    return a.entry.size if a.entry is not None else 0


DB_WARN_FRACTION = 0.9  # a device database at this share of its limit is warned about


def db_level(size: int, limit: int) -> Literal["ok", "warn", "over"]:
    return "over" if size > limit else "warn" if size >= DB_WARN_FRACTION * limit else "ok"


def check_db_size(plan: SyncPlan, current: int | None, estimated: int, limit: int) -> DbSize:
    """Set `plan.db`: near the limit → a warning; over it → the plan needs an explicit confirmation (never applied
    automatically; `apply` wants `confirm_db_limit`), and a note that transcoding does not help."""
    level = db_level(estimated, limit)
    plan.db = DbSize(current=current, estimated=estimated, limit=limit, level=level)
    what = f"the device database would grow to {_human(estimated)} (limit {_human(limit)})"
    if level == "warn":
        plan.warnings.append(f"{what}: close to the limit")
    elif level == "over":
        msg = f"{what}: the device may fail to read it - sync fewer items"
        plan.warnings.append(msg)
        plan.needs_confirmation = True
        plan.confirmation_reasons.append(msg)
        plan.space.suggestions.append(
            "the device database limit is the constraint, not free space: transcoding does not make it "
            "smaller - lower rule limits or remove items"
        )
    return plan.db


def items_after(plan: SyncPlan) -> int:
    return sum(1 for a in plan.actions if a.kind in ON_DEVICE)


def space_report(
    plan: SyncPlan,
    *,
    capacity: int,
    free: int,
    reserve: int,
    rule_names: Mapping[str, str] | None = None,
) -> SpaceReport:
    """`rule_names` in priority order: `per_rule` follows it (the plan review explains priority decisions);
    rules it does not name come last, biggest first."""
    needed = sum(a.size_delta for a in plan.actions) + reserve
    names = rule_names or {}
    rank = {rid: i for i, rid in enumerate(names)}
    per: dict[str, RuleSpace] = {}
    for a in plan.actions:
        if a.kind not in ON_DEVICE or not a.rules:
            continue
        rid = a.rules[0]
        rs = per.setdefault(rid, RuleSpace(rule_id=rid, name=names.get(rid, rid)))
        rs.items += 1
        rs.bytes += _final_size(a)
    return SpaceReport(
        capacity=capacity,
        free=free,
        needed=needed,
        reserve=reserve,
        fits=needed <= free,
        unknown=capacity >= UNKNOWN_SPACE,
        per_rule=sorted(per.values(), key=lambda r: (rank.get(r.rule_id, len(rank)), -r.bytes)),
    )


def suggestions(plan: SyncPlan, report: SpaceReport, caps: DeviceCapabilities) -> list[str]:
    out = [f"the plan needs {_human(report.needed - report.free)} more than is free on the device"]
    if report.per_rule:
        big = max(report.per_rule, key=lambda r: r.bytes)
        out.append(f"lower the limit of rule '{big.name}' ({big.items} items, {_human(big.bytes)})")
    lossless = [
        a
        for a in plan.actions
        if a.item is not None and a.item.file.format in LOSSLESS and a.kind in ON_DEVICE
    ]
    if lossless and not report.space_steps_used and caps.formats & {"aac", "mp3", "opus", "ogg"}:
        size = sum(_final_size(a) for a in lossless)
        target = "AAC 256k" if "aac" in caps.formats else "a lossy format"
        out.append(
            f"convert lossless files to {target} when space is needed ({len(lossless)} item{'' if len(lossless) == 1 else 's'}, "
            f"{_human(size)}): add a transcode rule with `when: space_needed`"
        )
    genres: Counter[str] = Counter()
    for a in plan.actions:
        if a.kind in ON_DEVICE and a.item is not None and a.item.track.genres:
            genres[a.item.track.genres[0]] += _final_size(a)
    if len(genres) > 1:
        g, size = genres.most_common(1)[0]
        out.append(f"exclude genre '{g}' ({_human(size)})")
    return out


def trim_to_fit(plan: SyncPlan, budget: int) -> list[str]:
    """Keep adds/replaces in plan (= priority) order while `budget` (free − reserve) allows; returns the
    dropped ItemRef keys. Removals always happen and free space first."""
    budget -= sum(a.size_delta for a in plan.actions if a.kind is ActionKind.REMOVE)
    dropped: list[str] = []
    actions: list[PlanAction] = []
    for a in plan.actions:
        if a.kind not in (ActionKind.ADD, ActionKind.REPLACE) or a.size_delta <= 0:
            if a.kind in (ActionKind.ADD, ActionKind.REPLACE):
                budget -= a.size_delta
            actions.append(a)
            continue
        if a.size_delta <= budget:
            budget -= a.size_delta
            actions.append(a)
            continue
        dropped.append(a.ref.key())
        if a.kind is ActionKind.REPLACE:  # the old version stays on the device
            actions.append(
                a.model_copy(
                    update={
                        "kind": ActionKind.KEEP,
                        "item": None,
                        "size_delta": 0,
                        "reason": "skipped for space",
                    }
                )
            )
    plan.actions = actions
    gone = set(dropped) - {a.ref.key() for a in actions}
    plan.playlists = [
        p.model_copy(update={"refs": [r for r in p.refs if r.key() not in gone]})
        for p in plan.playlists
    ]
    return dropped


def plan_with_space(
    device: DeviceConfig,
    snapshots: Mapping[str, LibrarySnapshot],
    manifest: Manifest | None,
    caps: DeviceCapabilities,
    chain: Chain,
    *,
    capacity: int,
    free: int,
    commit_reserve: Callable[[int], int],
    templates: Mapping[str, RuleTemplate] | None = None,
    auto: bool = False,
    force: bool = False,
    missing: Collection[str] = (),
    now: datetime | None = None,
    library_names: Mapping[str, str] | None = None,
    account_names: Mapping[str, str] | None = None,
    attached: Collection[str] | None = None,
) -> SyncPlan:
    """Plan + space check (+ space steps, + forced trimming). `commit_reserve(n)` = Device.commit_reserve."""
    margin = safety_margin(capacity, device.sync)
    rules, _, _ = resolve_rules(device.rule_set, templates or {})
    prio = {rid: i for i, rid in enumerate(device.rule_set.priority)}
    ordered = sorted(enumerate(rules), key=lambda ir: (prio.get(ir[1].id, len(prio)), ir[0]))
    names = {
        r.id: r.name or r.id for _, r in ordered
    }  # priority order, as the evaluator ranks them

    def build(space_steps: bool) -> SyncPlan:
        p = plan_sync(
            device,
            snapshots,
            manifest,
            caps,
            chain,
            templates=templates,
            auto=auto,
            space_steps=space_steps,
            missing=missing,
            now=now,
            library_names=library_names,
            account_names=account_names,
            attached=attached,
        )
        # no margin on a space nobody knows (a % of the UNKNOWN_SPACE sentinel would be terabytes)
        reserve = (0 if capacity >= UNKNOWN_SPACE else margin) + commit_reserve(items_after(p))
        p.space = space_report(p, capacity=capacity, free=free, reserve=reserve, rule_names=names)
        p.space.space_steps_used = space_steps
        return p

    plan = build(False)
    if not plan.space.fits:
        smaller = build(True)
        if smaller.space.needed < plan.space.needed:
            plan = smaller
    if plan.space.fits:
        return plan
    plan.space.suggestions = suggestions(plan, plan.space, caps)
    if not force:
        plan.does_not_fit = True
        plan.warnings.append(
            f"does not fit: needs {_human(plan.space.needed)}, {_human(plan.space.free)} free"
        )
        return plan
    full = {r.rule_id: r for r in plan.space.per_rule}
    reserve = plan.space.reserve
    wanted = {
        a.ref.key(): LeftOut(key=a.ref.key(), title=a.title, size=a.size_delta)
        for a in plan.actions
        if a.kind in (ActionKind.ADD, ActionKind.REPLACE)
    }
    dropped = trim_to_fit(plan, free - reserve)
    trimmed = space_report(plan, capacity=capacity, free=free, reserve=reserve, rule_names=names)
    kept = {r.rule_id: r for r in trimmed.per_rule}
    trimmed.per_rule = []
    for rid, before in full.items():
        after = kept.get(rid)
        items, size = (after.items, after.bytes) if after else (0, 0)
        trimmed.per_rule.append(
            before.model_copy(
                update={
                    "items": items,
                    "bytes": size,
                    "dropped_items": before.items - items,
                    "dropped_bytes": before.bytes - size,
                }
            )
        )
    trimmed.dropped = dropped
    trimmed.dropped_detail = [wanted[k] for k in dropped]
    trimmed.suggestions = []  # how to fit the untrimmed plan: no longer true once it is trimmed
    trimmed.space_steps_used = plan.space.space_steps_used
    plan.space = trimmed
    plan.warnings.append(
        f"forced sync: {len(dropped)} item{'' if len(dropped) == 1 else 's'} skipped for space"
    )
    return plan


def usage_breakdown(
    manifest: Manifest | None,
    *,
    used: int,
    db_size: int | None,
    rule_names: Mapping[str, str],
    library_names: Mapping[str, str],
) -> dict[str, object]:
    """Device space map, from the manifest (no device scan): SyncMusic's bytes per library and per
    rule (an item counts under its first rule, so the rows add up), and the rest of the used space split into the
    device database and files SyncMusic did not write (sidecars, foreign files)."""
    libs: Counter[str] = Counter()
    lib_items: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    rule_items: Counter[str] = Counter()
    for e in manifest.items.values() if manifest else ():
        libs[e.ref.library_id] += e.size
        lib_items[e.ref.library_id] += 1
        rid = e.rules[0] if e.rules else ""
        rules[rid] += e.size
        rule_items[rid] += 1
    synced = sum(libs.values())
    database = min(db_size or 0, max(0, used - synced))

    def rows(
        sizes: Counter[str], items: Counter[str], names: Mapping[str, str]
    ) -> list[dict[str, object]]:
        return [
            {"id": k, "name": names.get(k, k), "items": items[k], "bytes": b}
            for k, b in sizes.most_common()
        ]

    return {
        "by_library": rows(libs, lib_items, library_names),
        "by_rule": rows(rules, rule_items, rule_names),
        "database": database,
        "foreign": max(0, used - synced - database),
    }


def left_off(plan: SyncPlan, limit_rows: int = 200) -> dict[str, object] | None:
    """What the last plan left off and why: space (does not fit, or dropped by a forced sync) and
    the device limit, grouped by rule, with the plan's advice. None when nothing was left off."""
    s = plan.space
    dropped_rules = [r.model_dump() for r in s.per_rule if r.dropped_items]
    limit = plan.limit
    if not plan.does_not_fit and not s.dropped and not (limit and limit.left_out):
        return None
    return {
        "does_not_fit": plan.does_not_fit,
        "short_by": max(0, s.needed - s.free) if plan.does_not_fit else 0,
        # does not fit: what each rule wants (the user trims one of them); forced: what each rule lost
        "per_rule": dropped_rules if s.dropped else [r.model_dump() for r in s.per_rule],
        "dropped": [d.model_dump() for d in s.dropped_detail[:limit_rows]],
        "dropped_total": len(s.dropped),
        "dropped_bytes": sum(d.size for d in s.dropped_detail),
        "limit_left_out": len(limit.left_out) if limit else 0,
        "limit_left_out_bytes": limit.left_out_bytes if limit else 0,
        "suggestions": list(s.suggestions),
    }
