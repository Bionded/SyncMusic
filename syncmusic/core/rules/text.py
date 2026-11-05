# SPDX-License-Identifier: GPL-3.0-or-later
"""Rule text formats: Navidrome .nsp import/export, canonical JSON, YAML; validation."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, cast

import yaml
from pydantic import ValidationError

from syncmusic.core.models import Rule, RuleSet
from syncmusic.core.rules.fields import resolve_field
from syncmusic.core.rules.parser import (
    EXTENSION_OPERATORS,
    Cond,
    RuleError,
    iter_conditions,
    parse_criteria,
    parse_duration,
    parse_size,
    parse_sort,
)

_TRAILING_COMMA_RE = re.compile(r"(,\s*[}\]])")
_NSP_META = ("name", "comment", "sort", "order", "limit", "limitpercent")
_NSP_EXTRA = ("public", "refreshdelay")  # meaningless for devices, preserved for export


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_rule(rule: Rule) -> ValidationResult:
    res = ValidationResult()
    label = rule.name or rule.id
    try:
        parsed = parse_criteria(rule.criteria)
        res.warnings.extend(f"{label}: {w}" for w in parsed.warnings)
    except RuleError as exc:
        res.errors.append(f"{label}: {exc}")
    _, _, sort_warnings = parse_sort(rule.sort, rule.order)
    res.warnings.extend(f"{label}: {w}" for w in sort_warnings)
    try:
        if rule.limit_size:
            parse_size(rule.limit_size)
        if rule.limit_duration:
            parse_duration(rule.limit_duration)
    except RuleError as exc:
        res.errors.append(f"{label}: {exc}")
    if rule.limit is not None and rule.limit < 0:
        res.errors.append(f"{label}: limit must be >= 0")
    if rule.limit_percent is not None and not 1 <= rule.limit_percent <= 100:
        res.errors.append(f"{label}: limitPercent must be 1..100")
    return res


def validate_ruleset(rs: RuleSet, known: Collection[str] = ()) -> ValidationResult:
    """`known`: ids of the rules of the linked templates, which the priority order may list too."""
    res = ValidationResult()
    ids: set[str] = set()
    for r in [*rs.rules, *rs.excludes]:
        if r.id in ids:
            res.errors.append(f"duplicate rule id {r.id!r}")
        ids.add(r.id)
        sub = validate_rule(r)
        res.errors.extend(sub.errors)
        res.warnings.extend(sub.warnings)
    for rid in rs.priority:
        if rid not in ids and rid not in known:
            res.warnings.append(f"priority lists unknown rule id {rid!r}")
    return res


# ---------------------------------------------------------------------------------------------
# .nsp
# ---------------------------------------------------------------------------------------------


def _strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments and trailing commas outside strings (keeps line/column positions)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:end]))
            i = end
            continue
        else:
            out.append(c)
        i += 1
    return _TRAILING_COMMA_RE.sub(lambda m: " " + m.group(1)[1:], "".join(out))


def loads_lenient_json(text: str) -> Any:
    """JSON allowing // and /* */ comments and trailing commas (Navidrome docs use comments)."""
    try:
        return json.loads(_strip_json_comments(text))
    except json.JSONDecodeError as exc:
        raise RuleError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from None


def rule_from_nsp(data: dict[str, Any] | str, rule_id: str | None = None) -> Rule:
    if isinstance(data, str):
        data = cast(dict[str, Any], loads_lenient_json(data))
    if not isinstance(data, dict):
        raise RuleError("an .nsp document must be a JSON object")
    lower = {k.lower(): k for k in data}
    criteria = {k: v for k, v in data.items() if k.lower() not in _NSP_META + _NSP_EXTRA}
    criteria = _lists_from_nsp(criteria)
    if len(criteria) == 1 and next(iter(criteria)) in ("in", "notIn"):
        criteria = {"all": [criteria]}  # the whole rule was one list
    order = str(data.get(lower.get("order", ""), "asc") or "asc").lower()
    limit = data.get(lower["limit"]) if "limit" in lower else None
    pct = data.get(lower["limitpercent"]) if "limitpercent" in lower else None
    rule = Rule(
        id=rule_id or uuid.uuid4().hex[:8],
        name=str(data.get(lower.get("name", ""), "") or ""),
        comment=str(data.get(lower.get("comment", ""), "") or ""),
        criteria=criteria,
        sort=data.get(lower["sort"]) if "sort" in lower else None,
        order="desc" if order == "desc" else "asc",
        limit=int(limit) if limit is not None else None,
        limit_percent=int(pct) if pct is not None else None,
        nsp_extra={k: v for k, v in data.items() if k.lower() in _NSP_EXTRA},
    )
    parse_criteria(rule.criteria)  # raises RuleError when invalid
    return rule


def _nsp_list(op: str, fields: Any) -> Any:
    """`in` / `notIn` of Navidrome fields as plain .nsp: any of `is`, all of `isNot`; None when a
    field is not one Navidrome knows (track_id lists of template picks) - then it is stripped."""
    if not isinstance(fields, dict) or not fields:
        return None
    one, group = ("is", "any") if op == "in" else ("isNot", "all")
    parts = []
    for name, values in fields.items():
        spec = resolve_field(str(name))
        if spec is None or not spec.nsp or not isinstance(values, list):
            return None
        parts.append({group: [{one: {name: v}} for v in values]})
    return parts[0] if len(parts) == 1 else {"all": parts}


def _lists_from_nsp(node: Any) -> Any:
    """The way back: an `any` of `is` (an `all` of `isNot`) on one string field with 2+ values is `in` (`notIn`)."""
    if isinstance(node, list):
        return [_lists_from_nsp(n) for n in node]
    if not isinstance(node, dict) or len(node) != 1:
        return node
    ((key, value),) = node.items()
    if key.lower() not in ("all", "any") or not isinstance(value, list):
        return node
    one = "is" if key.lower() == "any" else "isNot"
    names = {
        next(iter(c[one]))
        if isinstance(c, dict) and len(c) == 1 and isinstance(c.get(one), dict) and len(c[one]) == 1
        else None
        for c in value
    }
    name = next(iter(names)) if len(names) == 1 else None
    spec = resolve_field(name) if isinstance(name, str) else None
    if len(value) >= 2 and spec is not None and spec.type in ("string", "list"):
        values = [c[one][name] for c in value]
        if all(isinstance(v, str) for v in values):
            return {"in" if one == "is" else "notIn": {name: values}}
    return {key: _lists_from_nsp(value)}


def _strip_extensions(node: Any, removed: list[str]) -> Any:
    if isinstance(node, list):
        out = [_strip_extensions(n, removed) for n in node]
        return [n for n in out if n is not None]
    if not isinstance(node, dict):
        return node
    res: dict[str, Any] = {}
    for k, v in node.items():
        if k in ("in", "notIn") and (plain := _nsp_list(k, v)) is not None:
            if len(node) == 1:
                res.update(plain)
            else:
                res.setdefault("all", []).append(plain)
            continue
        if k in EXTENSION_OPERATORS:
            removed.append(k)
            continue
        if k.lower() in ("all", "any"):
            res[k] = _strip_extensions(v, removed)
        else:
            res[k] = v
    return res or None


def rule_to_nsp(rule: Rule) -> tuple[dict[str, Any], list[str]]:
    """Export as Navidrome .nsp. Returns (document, warnings about stripped extensions)."""
    warnings: list[str] = []
    removed: list[str] = []
    criteria = _strip_extensions(rule.criteria, removed) or {"all": []}
    if removed:
        warnings.append(f"extension operators removed: {', '.join(sorted(set(removed)))}")
    try:
        conds: list[Cond] = iter_conditions(parse_criteria(criteria).node)
    except RuleError:
        conds = []
    ext_fields = sorted(
        {c.field for c in conds if c.spec is not None and not c.spec.nsp and c.known}
    )
    if ext_fields:
        warnings.append(f"fields unknown to Navidrome kept: {', '.join(ext_fields)}")
    for attr in ("limit_size", "limit_duration", "kind", "libraries", "as_playlist"):
        if getattr(rule, attr):
            warnings.append(f"extension setting {attr!r} dropped")
    if rule.include_playlists:
        warnings.append("extension setting 'include_playlists' dropped")
    if rule.skip_playlists:
        warnings.append("extension setting 'skip_playlists' dropped")
    if rule.random_period != "none":
        warnings.append("extension setting 'random_period' dropped")
    doc: dict[str, Any] = {}
    if rule.name:
        doc["name"] = rule.name
    if rule.comment:
        doc["comment"] = rule.comment
    doc.update(rule.nsp_extra)
    doc.update(criteria)
    if rule.sort:
        doc["sort"] = rule.sort
    if rule.order == "desc":
        doc["order"] = "desc"
    if rule.limit is not None:
        doc["limit"] = rule.limit
    if rule.limit_percent is not None:
        doc["limitPercent"] = rule.limit_percent
    return doc, warnings


# ---------------------------------------------------------------------------------------------
# canonical JSON / YAML of rules and rule sets
# ---------------------------------------------------------------------------------------------


def rule_to_dict(rule: Rule) -> dict[str, Any]:
    return rule.model_dump(mode="json", exclude_defaults=True) | {"id": rule.id}


def rule_to_json(rule: Rule) -> str:
    return json.dumps(rule_to_dict(rule), indent=2, ensure_ascii=False)


def rule_to_yaml(rule: Rule) -> str:
    return yaml.safe_dump(rule_to_dict(rule), allow_unicode=True, sort_keys=False)


def ruleset_to_yaml(rs: RuleSet) -> str:
    return yaml.safe_dump(
        rs.model_dump(mode="json", exclude_defaults=True), allow_unicode=True, sort_keys=False
    )


def _load_text(text: str) -> Any:
    s = text.strip()
    if s.startswith(("{", "[")):
        return loads_lenient_json(s)
    try:
        return yaml.safe_load(s)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise RuleError(f"invalid YAML{where}: {problem}") from None


def pydantic_message(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in exc.errors()
    )


def rule_from_text(text: str) -> Rule:
    """Rule from JSON or YAML. Accepts our Rule format or a bare .nsp document."""
    data = _load_text(text)
    if not isinstance(data, dict):
        raise RuleError("rule text must be an object")
    if "criteria" in data or "id" in data and not any(k in data for k in ("all", "any")):
        unknown = sorted(set(data) - set(Rule.model_fields))
        if unknown:
            raise RuleError(f"unknown key(s) in rule: {', '.join(unknown)}")
        try:
            rule = Rule.model_validate(data)
        except ValidationError as exc:
            raise RuleError(pydantic_message(exc)) from None
        parse_criteria(rule.criteria)
        return rule
    return rule_from_nsp(data)


def ruleset_from_text(text: str) -> RuleSet:
    data = _load_text(text)
    if isinstance(data, list):
        data = {"rules": data}
    if not isinstance(data, dict):
        raise RuleError("rule set text must be an object")
    if "rules" not in data and "excludes" not in data and "template_ids" not in data:
        return RuleSet(rules=[rule_from_text(json.dumps(data))])
    try:
        rs = RuleSet.model_validate(data)
    except ValidationError as exc:
        raise RuleError(pydantic_message(exc)) from None
    res = validate_ruleset(rs)
    if not res.ok:
        raise RuleError("; ".join(res.errors))
    return rs
