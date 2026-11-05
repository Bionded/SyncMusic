# SPDX-License-Identifier: GPL-3.0-or-later
"""Linked rule templates and the auto-managed "Manual selection" rule."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from syncmusic.core.models import DeviceConfig, Rule, RuleSet, RuleTemplate

# a device has templates only: what is picked for it ("Add to device") or removed from it (Player
# contents) goes into its own template, a normal template linked to it on first use
OWN_TEMPLATE_PREFIX = "own-"
MANUAL_RULE_ID = "manual-selection"
MANUAL_RULE_NAME = "Manual selection"
IdField = Literal["track_id", "album_id", "artist_id"]


def own_template_id(device_id: str) -> str:
    return f"{OWN_TEMPLATE_PREFIX}{device_id}"


def own_template(
    device: DeviceConfig, existing: RuleTemplate | None, rules: RuleSet
) -> RuleTemplate:
    """The device's own template "<Device> - own" with these rules and excludes (from all libraries: each rule
    names its own)."""
    tpl = existing or RuleTemplate(id=own_template_id(device.id), name=f"{device.name} - own")
    return tpl.model_copy(update={"rules": rules.rules, "excludes": rules.excludes}, deep=True)


def template_usage(template_id: str, devices: Iterable[DeviceConfig]) -> list[DeviceConfig]:
    return [d for d in devices if template_id in d.rule_set.template_ids]


def _bound(rule: Rule, library_ids: list[str]) -> Rule:
    if not library_ids:
        return rule
    libs = (
        list(library_ids)
        if rule.libraries is None
        else [x for x in rule.libraries if x in library_ids]
    )
    return rule.model_copy(update={"libraries": libs})


def template_rules(template: RuleTemplate) -> tuple[list[Rule], list[Rule]]:
    """(rules, excludes) of a template, limited to its libraries unless it draws from all."""
    libs = template.library_ids
    return [_bound(r, libs) for r in template.rules], [_bound(r, libs) for r in template.excludes]


def device_libraries(
    ruleset: RuleSet, templates: Mapping[str, RuleTemplate], enabled: Sequence[str]
) -> list[str]:
    """The libraries a device pulls from: those of the enabled rules of its linked templates (and
    of a draft's rules), in the order of `enabled` (the enabled libraries). A rule without libraries in a template
    without libraries draws from all of them. Excludes pull nothing."""
    used: set[str] = set()
    for rule in all_rules(ruleset, templates):
        if rule.enabled:
            used.update(enabled if rule.libraries is None else rule.libraries)
    return [lid for lid in enabled if lid in used]


def all_rules(ruleset: RuleSet, templates: Mapping[str, RuleTemplate]) -> list[Rule]:
    out: list[Rule] = []
    for tid in dict.fromkeys(ruleset.template_ids):
        if tid in templates:
            out.extend(template_rules(templates[tid])[0])
    out.extend(ruleset.rules)
    return out


def _manual_ids(rule: Rule, field: IdField) -> list[str]:
    for cond in rule.criteria.get("any", []):
        if isinstance(cond, dict) and "in" in cond and field in cond["in"]:
            return [str(x) for x in cond["in"][field]]
    return []


def add_to_manual_selection(
    ruleset: RuleSet, field: IdField, ids: Iterable[str], library_id: str | None = None
) -> RuleSet:
    """Append ids to the device's `Manual selection` rule (created on first use)."""
    rs = ruleset.model_copy(deep=True)
    rule = next((r for r in rs.rules if r.id == MANUAL_RULE_ID), None)
    if rule is None:  # bound to its library: it must not pull every library onto the device
        rule = Rule(
            id=MANUAL_RULE_ID,
            name=MANUAL_RULE_NAME,
            criteria={"any": []},
            libraries=[library_id] if library_id else None,
        )
        rs.rules.append(rule)
    conds: list[dict[str, dict[str, list[str]]]] = rule.criteria.setdefault("any", [])
    existing = next((c for c in conds if "in" in c and field in c["in"]), None)
    if existing is None:
        existing = {"in": {field: []}}
        conds.append(existing)
    current = existing["in"][field]
    for i in ids:
        if i not in current:
            current.append(i)
    if library_id and rule.libraries is not None and library_id not in rule.libraries:
        rule.libraries.append(library_id)
    return rs


def remove_from_manual_selection(ruleset: RuleSet, field: IdField, ids: Iterable[str]) -> RuleSet:
    """Take ids out of the `Manual selection` rule; a list that runs empty goes, and so does the rule once it
    selects nothing (an Undo leaves the rules as they were before the add)."""
    rs = ruleset.model_copy(deep=True)
    drop = set(ids)
    for rule in rs.rules:
        if rule.id != MANUAL_RULE_ID:
            continue
        conds = rule.criteria.get("any", [])
        for cond in conds:
            if "in" in cond and field in cond["in"]:
                cond["in"][field] = [x for x in cond["in"][field] if x not in drop]
                if not cond["in"][field]:
                    del cond["in"][field]
        rule.criteria["any"] = [c for c in conds if not ("in" in c and not c["in"])]
    kept = [r for r in rs.rules if r.id != MANUAL_RULE_ID or r.criteria.get("any")]
    if len(kept) < len(rs.rules):
        rs.rules = kept
        rs.priority = [p for p in rs.priority if p != MANUAL_RULE_ID]
    return rs


EXCLUDE_RULE_ID = "removed-from-device"
EXCLUDE_RULE_NAME = "Removed from the device"


def add_device_exclusions(ruleset: RuleSet, ids: Iterable[str]) -> RuleSet:
    """Append track ids to the device's `Removed from the device` exclude rule (created on first use): items a rule
    or template still selects stay off the device after "Remove selected" in Player contents."""
    ids = list(dict.fromkeys(ids))
    if not ids:
        return ruleset
    rs = ruleset.model_copy(deep=True)
    rule = next((r for r in rs.excludes if r.id == EXCLUDE_RULE_ID), None)
    if rule is None:
        rule = Rule(id=EXCLUDE_RULE_ID, name=EXCLUDE_RULE_NAME, criteria={"any": []})
        rs.excludes.append(rule)
    conds: list[dict[str, dict[str, list[str]]]] = rule.criteria.setdefault("any", [])
    existing = next((c for c in conds if "in" in c and "track_id" in c["in"]), None)
    if existing is None:
        existing = {"in": {"track_id": []}}
        conds.append(existing)
    current = existing["in"]["track_id"]
    current.extend(i for i in ids if i not in current)
    return rs


def as_rules(template: RuleTemplate | None) -> RuleSet:
    """A template's rules and excludes as a rule set, for the helpers above (empty without one)."""
    if template is None:
        return RuleSet()
    return RuleSet(rules=template.rules, excludes=template.excludes).model_copy(deep=True)


def manual_ids(ruleset: RuleSet, field: IdField) -> list[str]:
    """Ids of `field` in the device's Manual selection rule."""
    rule = next((r for r in ruleset.rules if r.id == MANUAL_RULE_ID), None)
    return _manual_ids(rule, field) if rule else []
