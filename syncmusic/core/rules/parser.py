# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse and validate nsp-compatible criteria trees into a small AST.

Criteria grammar (JSON):
    node      := group | condition
    group     := {"all": [node, ...]} | {"any": [node, ...]}
    condition := {<operator>: {<field>: <value>}}       (several fields in one operator = implicit "all")
Special operators: inPlaylist/notInPlaylist ({"id"|"name"|"path": ...} or the extension {"any": true}), latestEpisodes ({"per_series": N[, "unplayed": true]}).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from syncmusic.core.rules.fields import FieldSpec, custom_tag_field, resolve_field

GROUP_OPS = ("all", "any")
NSP_OPERATORS = (
    "is",
    "isNot",
    "gt",
    "lt",
    "contains",
    "notContains",
    "startsWith",
    "endsWith",
    "inTheRange",
    "before",
    "after",
    "inTheLast",
    "notInTheLast",
    "inPlaylist",
    "notInPlaylist",
    "isMissing",
    "isPresent",
)
EXTENSION_OPERATORS = ("in", "notIn", "latestEpisodes")
OPERATORS = NSP_OPERATORS + EXTENSION_OPERATORS
_OP_BY_LOWER = {op.lower(): op for op in OPERATORS}

# Top-level keys of an .nsp file that are not criteria.
NSP_META_KEYS = {
    "name",
    "comment",
    "sort",
    "order",
    "limit",
    "limitpercent",
    "public",
    "refreshdelay",
    "offset",
}


class RuleError(ValueError):
    """Invalid rule (human-readable message incl. the path inside the criteria)."""


@dataclass
class Cond:
    op: str
    field: str  # field name as written (or "" for playlist/latestEpisodes conditions)
    value: Any
    spec: FieldSpec | None = None
    known: bool = True  # False: unknown field (custom tag lookup in Track.extra)


@dataclass
class Group:
    op: Literal["all", "any"]
    children: list[Node] = field(default_factory=list)


Node = Group | Cond


@dataclass
class ParseResult:
    node: Node
    warnings: list[str] = field(default_factory=list)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_date(v: Any) -> datetime | None:
    """'YYYY-MM-DD' / ISO datetime / epoch seconds → aware datetime (UTC)."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return datetime.fromtimestamp(float(v), UTC)
    if isinstance(v, str) and _DATE_RE.match(v.strip()):
        s = v.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_value(op: str, spec: FieldSpec | None, fname: str, value: Any, path: str) -> None:
    def bad(msg: str) -> RuleError:
        return RuleError(f"{path}: {op} {fname!r}: {msg}")

    if op in ("is", "isNot"):
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise bad("value must be a string, number or boolean")
        if spec is not None and spec.type == "bool" and not isinstance(value, bool):
            raise bad("value must be true or false")
    elif op in ("gt", "lt"):
        if not _is_number(value) and parse_date(value) is None:
            raise bad("value must be a number or a date (YYYY-MM-DD)")
    elif op in ("contains", "notContains", "startsWith", "endsWith"):
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise bad("value must be a string")
    elif op == "inTheRange":
        if not isinstance(value, list) or len(value) != 2:
            raise bad("value must be a list of two numbers or dates")
        if not all(_is_number(x) or parse_date(x) is not None for x in value):
            raise bad("range bounds must be numbers or dates (YYYY-MM-DD)")
    elif op in ("before", "after"):
        if parse_date(value) is None:
            raise bad("value must be a date (YYYY-MM-DD)")
    elif op in ("inTheLast", "notInTheLast"):
        if not _is_number(value) or value < 0:
            raise bad("value must be a non-negative number of days")
        if spec is not None and spec.type != "date":
            raise bad(f"field is not a date (type {spec.type})")
    elif op in ("in", "notIn"):
        if not isinstance(value, list):
            raise bad("value must be a list")
    elif op in ("isMissing", "isPresent") and not isinstance(value, bool):
        raise bad("value must be true or false")


_TEXT_OPS = ("contains", "notContains", "startsWith", "endsWith")


def _parse(node: Any, path: str, warnings: list[str]) -> Node:
    if not isinstance(node, dict):
        raise RuleError(f"{path}: expected an object, got {type(node).__name__}")
    keys = [k for k in node if k.lower() not in NSP_META_KEYS] if path == "$" else list(node)
    if not keys:
        if path == "$":
            return Group("all", [])  # empty criteria = match everything
        raise RuleError(f"{path}: empty condition")
    if len(keys) > 1:
        # {"is": {...}, "gt": {...}} at one level: implicit all
        return Group("all", [_parse({k: node[k]}, f"{path}.{k}", warnings) for k in keys])
    key = keys[0]
    body = node[key]
    low = key.lower()
    if low in GROUP_OPS:
        if not isinstance(body, list):
            raise RuleError(f"{path}.{key}: expected a list of conditions")
        return Group(
            "all" if low == "all" else "any",
            [_parse(c, f"{path}.{key}[{i}]", warnings) for i, c in enumerate(body)],
        )
    op = _OP_BY_LOWER.get(low)
    if op is None:
        raise RuleError(f"{path}: unknown operator {key!r}")
    if not isinstance(body, dict) or not body:
        raise RuleError(f"{path}.{key}: expected an object {{field: value}}")
    if op in ("inPlaylist", "notInPlaylist"):
        ref = {k.lower(): v for k, v in body.items()}
        if ref.get("any") is True:  # extension: a track in any playlist (built-in "All playlists")
            return Cond(op, "", {"any": True})
        if not any(k in ref for k in ("id", "name", "path")):
            raise RuleError(
                f"{path}.{key}: expected {{'id': ...}}, {{'name': ...}} or {{'any': true}}"
            )
        if "path" in ref and "id" not in ref and "name" not in ref:
            warnings.append(
                f"{path}.{key}: playlist references by .nsp path are not supported; "
                "the playlist is matched by file name instead"
            )
        return Cond(op, "", ref)
    if op == "latestEpisodes":
        n = body.get("per_series")
        if not isinstance(n, (int, float)) or isinstance(n, bool) or n < 1:
            raise RuleError(f"{path}.{key}: expected {{'per_series': N}} with N >= 1")
        unplayed = body.get("unplayed", False)
        if not isinstance(unplayed, bool) or set(body) - {"per_series", "unplayed"}:
            raise RuleError(f"{path}.{key}: expected {{'per_series': N, 'unplayed': true|false}}")
        # unplayed: the N newest of the episodes not finished yet, so a played one makes room
        return Cond(op, "", (int(n), unplayed))
    conds: list[Node] = []
    for fname, value in body.items():
        spec = resolve_field(str(fname))
        known = spec is not None
        if spec is None:
            spec = custom_tag_field(str(fname))
            warnings.append(
                f"{path}.{key}: unknown field {fname!r} (looked up as a custom tag; "
                "no match when absent)"
            )
        _check_value(op, spec, str(fname), value, f"{path}.{key}")
        if op in _TEXT_OPS and value == "":
            # usually an untouched builder row; Navidrome matches the same way, so only warn.
            # Only the empty string: a blank like " " is a real substring and matches literally
            warnings.append(
                f"{path}.{key}: {fname!r} has an empty value, so it matches "
                f"{'no item' if op == 'notContains' else 'every item'}"
            )
        conds.append(Cond(op, str(fname), value, spec, known))
    return conds[0] if len(conds) == 1 else Group("all", conds)


def parse_criteria(criteria: dict[str, Any]) -> ParseResult:
    if not isinstance(criteria, dict):
        raise RuleError("$: criteria must be an object")
    warnings: list[str] = []
    node = _parse(criteria, "$", warnings)
    return ParseResult(node, warnings)


def fields_used(criteria: dict[str, Any]) -> set[str]:
    """Names of the known fields a rule's criteria test (warn about fields a library does not have)."""
    try:
        node = parse_criteria(criteria).node
    except RuleError:
        return set()
    return {c.spec.name for c in iter_conditions(node) if c.spec is not None and c.known}


def iter_conditions(node: Node) -> list[Cond]:
    if isinstance(node, Cond):
        return [node]
    out: list[Cond] = []
    for c in node.children:
        out.extend(iter_conditions(c))
    return out


# ---------------------------------------------------------------------------------------------
# sort / limits
# ---------------------------------------------------------------------------------------------


@dataclass
class SortKey:
    field: str
    spec: FieldSpec | None
    desc: bool


def parse_sort(sort: str | None, order: str = "asc") -> tuple[list[SortKey], bool, list[str]]:
    """Return (keys, random, warnings). `order: desc` reverses every key (Navidrome semantics)."""
    warnings: list[str] = []
    if not sort or not sort.strip():
        return [], False, warnings
    if sort.strip().lower() == "random":
        return [], True, warnings
    keys: list[SortKey] = []
    for part in sort.split(","):
        p = part.strip()
        if not p:
            continue
        desc = p.startswith("-")
        name = p.lstrip("+-").strip()
        spec = resolve_field(name)
        if spec is None:
            warnings.append(f"sort: unknown field {name!r} (looked up as a custom tag)")
            spec = custom_tag_field(name)
        if order == "desc":
            desc = not desc
        keys.append(SortKey(name, spec, desc))
    return keys, False, warnings


_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([kmgt]?i?b?)?\s*$", re.I)
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "tib": 1024**4,
}


def parse_size(text: str) -> int:
    m = _SIZE_RE.match(text)
    unit = (m.group(2) or "").lower() if m else ""
    if not m or unit not in _SIZE_UNITS:
        raise RuleError(f"invalid size {text!r} (examples: 500MB, 10GB, 2GiB)")
    return int(float(m.group(1)) * _SIZE_UNITS[unit])


_DUR_RE = re.compile(r"([\d.]+)\s*([wdhms])", re.I)
_DUR_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> float:
    """'48h', '1h30m', '2d', '90m', '3600s' (or plain seconds) → seconds."""
    t = text.strip().lower()
    if re.fullmatch(r"[\d.]+", t):
        return float(t)
    parts = _DUR_RE.findall(t)
    if not parts or _DUR_RE.sub("", t).strip():
        raise RuleError(f"invalid duration {text!r} (examples: 48h, 1h30m, 2d)")
    return sum(float(n) * _DUR_UNITS[u] for n, u in parts)
