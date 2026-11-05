# SPDX-License-Identifier: GPL-3.0-or-later
"""Rule tests and the live rule preview: what a rule set selects from the libraries, with or
without the device. Functions taking the SyncService; split out of core/sync.py."""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING, Any

from syncmusic.core import plan_view, registry
from syncmusic.core.models import ActionKind, ItemRef, LibrarySnapshot, Rule, RuleSet, Track
from syncmusic.core.rules import evaluate_ruleset, validate_ruleset
from syncmusic.core.rules.evaluator import resolve_rules
from syncmusic.core.rules.parser import fields_used
from syncmusic.core.space import ON_DEVICE
from syncmusic.core.sync import SyncError

if TYPE_CHECKING:
    from syncmusic.core.sync import SyncService

PREVIEW_SAMPLE = 20  # first matches shown by the live rule preview
RULE_EXAMPLES = 5  # examples per rule in a rule test
TEST_SAMPLE = 50  # the rule test's mixed sample


async def test_rules(
    svc: SyncService, device_id: str, rule_set: RuleSet | None = None
) -> dict[str, Any]:
    """Preview of what a rule set (default: the device's) selects from the device's libraries, without the
    device (`POST /devices/{id}/rules/test`)."""
    cfg = await svc.config(device_id)
    draft = cfg.model_copy(update={"rule_set": rule_set or cfg.rule_set})
    # the tested rules decide the libraries; none: they select nothing
    snaps = await svc._snapshots(draft) if await svc.libraries_of(draft) else {}
    return await test_selection(svc, draft.rule_set, snaps)


async def preview(svc: SyncService, device_id: str, rule_set: RuleSet) -> dict[str, Any]:
    """Live preview while editing a device's rules: the plan the unsaved `rule_set` would give, with
    space and the device limit, computed like `plan()` on the connected device (nothing is stored). A device
    that is not connected (or being ejected) gets the library-only selection (`connected: false`): sizes from
    the library, no space."""
    cfg = await svc.config(device_id)
    # the edited rules decide the libraries: a template just linked pulls its library at once
    draft = cfg.model_copy(update={"rule_set": rule_set})
    libs = await svc.libraries_of(draft)
    owners: dict[str, str] = {}  # item key -> the rule that selected it first
    test = await test_selection(
        svc, rule_set, snaps := await svc._snapshots(draft) if libs else {}, owners
    )
    if not libs:  # nothing pulls from a library: nothing selected, no plan
        return {**test, "connected": False, "reason": f"{cfg.name} has no templates"}
    clock = svc._clock_tasks.get(device_id)
    try:
        if (
            clock is not None and not clock.done()
        ):  # never wait 30 s for the iPod's clock settle while typing
            raise SyncError(f"{cfg.name} is setting its clock", "busy")
        device = await svc.open_device(device_id)
    except SyncError as e:
        if e.code not in ("not_connected", "busy", "unmountable"):
            raise
        return {
            **test,
            "connected": False,
            "reason": str(e),
            "sample": test["sample"][:PREVIEW_SAMPLE],
        }
    manifest = await device.read_manifest()
    plan = await svc._space_plan(draft, device, snaps, manifest, cfg.processors)
    on_device = [a for a in plan.actions if a.kind in ON_DEVICE and not a.orphan]
    by_kind: dict[str, int] = {}
    for a in on_device:
        if a.item is not None:
            k = a.item.track.kind.value
            by_kind[k] = by_kind.get(k, 0) + a.item.file.size + a.item.sidecar_bytes
    lost: dict[str, dict[str, Any]] = {}  # what each rule loses to the device limit
    for x in plan.limit.left_out if plan.limit else []:
        rid = owners.get(x.key)
        if rid is not None:
            loss = lost.setdefault(rid, {"rule_id": rid, "items": 0, "bytes": 0})
            loss["items"] += 1
            loss["bytes"] += x.size
    s = plan.space
    return {
        "connected": True,
        "items": len(on_device),
        # bytes of the selected items on the device after the plan (planned size, else the file as it is)
        "bytes": sum(
            a.item.file.size + a.item.sidecar_bytes
            if a.item is not None
            else (a.entry.size if a.entry else 0)
            for a in on_device
        ),
        "counts": {k.value: plan.count(k) for k in ActionKind},
        "fits": s.fits and not plan.does_not_fit,
        "needed": s.needed,
        # the device cannot tell its space (FTP without quota): no numbers
        "space_unknown": s.unknown,
        "free": None if s.unknown else s.free,
        "capacity": None if s.unknown else s.capacity,
        "free_after": None if s.unknown else s.free - s.needed,
        "reserve": s.reserve,  # part of `needed`: the preview says so
        "left_out": len(plan.limit.left_out) if plan.limit else 0,
        "left_out_per_rule": list(lost.values()),
        "by_kind": by_kind,
        "per_rule": test["per_rule"],
        "playlists": [p.name for p in plan.playlists],
        "warnings": merge_warnings(
            [], test["warnings"] + [w for w in plan.warnings if w not in test["warnings"]]
        ),
        "sample": [
            {
                "ref": a.ref.key(),
                "title": a.item.track.title if a.item is not None else a.title,
                "artist": a.item.track.artist if a.item is not None else None,
                "kind": a.item.track.kind.value if a.item is not None else "music",
                "action": a.kind.value,
            }
            for a in plan_view.spread(  # spread over artists/albums, not the first rule's
                on_device,
                PREVIEW_SAMPLE,
                lambda a: plan_view.track_bucket(a.item.track) if a.item else (a.ref.library_id,),
                lambda a: a.ref.key(),
            )
        ],
    }


async def test_selection(
    svc: SyncService,
    rs: RuleSet,
    snaps: dict[str, LibrarySnapshot],
    owners: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What `rs` selects from `snaps`: items, bytes, per rule, by kind, playlists, warnings, a sample (no device, templates are tested against chosen libraries). `owners` gets each item's first rule."""
    templates = await svc.store.templates()
    linked, _, _ = resolve_rules(RuleSet(template_ids=rs.template_ids), templates)
    result = validate_ruleset(rs, {r.id for r in linked})
    if not result.ok:
        raise SyncError("invalid rules: " + "; ".join(result.errors), "invalid")
    selection = evaluate_ruleset(rs, snaps, templates)
    rules, excludes, _ = resolve_rules(rs, templates)
    names = {r.id: r.name or r.id for r in rules}
    fw = await field_warnings(svc, [*rules, *excludes], list(snaps))
    tracks = {
        k: snaps[r.library_id].tracks[r.item_id]
        for k, r in ((k, ItemRef.parse(k)) for k in selection.items)
        if r.library_id in snaps and r.item_id in snaps[r.library_id].tracks
    }
    # every enabled include rule, also one that matches nothing
    per_rule: dict[str, dict[str, Any]] = {
        r.id: {"rule_id": r.id, "name": names[r.id], "items": 0, "bytes": 0, "matched": 0}
        for r in rules
        if r.enabled
    }
    by_rule: dict[str, list[Track]] = {}
    by_kind: dict[str, int] = {}
    for t in tracks.values():
        by_kind[t.kind.value] = by_kind.get(t.kind.value, 0) + t.size_bytes
    for key, rule_ids in selection.items.items():
        if owners is not None and rule_ids:
            owners[key] = rule_ids[0]
        for n, rid in enumerate(rule_ids):
            e = per_rule.setdefault(
                rid,
                {
                    "rule_id": rid,
                    "name": names.get(rid, rid),
                    "items": 0,
                    "bytes": 0,
                    "matched": 0,
                },
            )
            e["matched"] += 1  # everything the rule matches, also what a rule above brings
            if n == 0:  # items and bytes count for the rule that selected it first
                e["items"] += 1
                e["bytes"] += tracks[key].size_bytes if key in tracks else 0
            if key in tracks:
                by_rule.setdefault(rid, []).append(tracks[key])

    def brief(t: Track) -> dict[str, Any]:
        return {
            "ref": t.ref.key(),
            "title": t.title,
            "artist": t.artist or t.author,
            "album": t.album,
            "kind": t.kind.value,
        }

    def diverse(ts: list[Track], n: int) -> list[Track]:
        return plan_view.spread(ts, n, plan_view.track_bucket, lambda t: t.ref.key())

    for (
        rid,
        e,
    ) in per_rule.items():  # a few examples per rule, spread over artists/albums
        e["examples"] = [brief(t) for t in diverse(by_rule.get(rid, []), RULE_EXAMPLES)]
        e["matches_nothing"] = e["matched"] == 0
    # the mixed sample: each rule's examples first (in rule order), then spread over everything
    sample: dict[str, Track] = {}
    for rid in per_rule:
        for t in diverse(by_rule.get(rid, []), RULE_EXAMPLES):
            sample.setdefault(t.ref.key(), t)
    for t in diverse(list(tracks.values()), TEST_SAMPLE):
        if len(sample) >= TEST_SAMPLE:
            break
        sample.setdefault(t.ref.key(), t)
    return {
        "items": len(selection.items),
        "bytes": sum(t.size_bytes for t in tracks.values()),
        "per_rule": list(per_rule.values()),
        "by_kind": by_kind,
        "playlists": [p.name for p in selection.playlists],
        "warnings": merge_warnings(result.warnings, selection.warnings + fw),
        "sample": [brief(t) for t in list(sample.values())[:TEST_SAMPLE]],
    }


async def matches_csv(svc: SyncService, rs: RuleSet, snaps: dict[str, LibrarySnapshot]) -> str:
    """Every item `rs` selects as CSV, with the rules that select it (the full match list)."""
    templates = await svc.store.templates()
    selection = evaluate_ruleset(rs, snaps, templates)
    rules, _, _ = resolve_rules(rs, templates)
    names = {r.id: r.name or r.id for r in rules}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rules", "title", "artist", "album", "kind", "size", "library_id", "item_id"])
    for key, rule_ids in selection.items.items():
        ref = ItemRef.parse(key)
        t = snaps[ref.library_id].tracks.get(ref.item_id) if ref.library_id in snaps else None
        if t is None:
            continue
        w.writerow(
            [
                "; ".join(names.get(r, r) for r in rule_ids),
                t.title,
                t.artist or t.author or "",
                t.album or "",
                t.kind.value,
                t.size_bytes,
                ref.library_id,
                ref.item_id,
            ]
        )
    return buf.getvalue()


async def field_warnings(svc: SyncService, rules: list[Rule], library_ids: list[str]) -> list[str]:
    """A rule testing a field one of the libraries does not have matches nothing there."""
    configs = {c.id: c for c in await svc.store.libraries() if c.id in library_ids}
    out = []
    for rule in rules:
        used = fields_used(rule.criteria)
        for cfg in configs.values():
            if rule.libraries and cfg.id not in rule.libraries:
                continue  # the rule never draws from this library
            missing = sorted(used - registry.library_class(cfg.type).rule_fields)
            if missing:
                out.append(
                    f"rule {rule.name or rule.id!r}: {', '.join(missing)} not available in library "
                    f"{cfg.name!r}, so the rule matches nothing there"
                )
    return out


def merge_warnings(validation: list[str], selection: list[str]) -> list[str]:
    """Validation warnings ("<rule>: ...") the evaluation already reported as "rule <rule>: ..." or
    "exclude <rule>: ..." appear once."""
    seen = set(selection)
    extra = [w for w in validation if f"rule {w}" not in seen and f"exclude {w}" not in seen]
    return extra + selection
