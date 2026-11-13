# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure rule evaluation over in-memory library snapshots."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from syncmusic.core.models import (
    DevicePlaylist,
    LibrarySnapshot,
    MediaKind,
    Playlist,
    Rule,
    RuleSet,
    RuleTemplate,
    Selection,
    Track,
)
from syncmusic.core.rules.fields import FieldSpec
from syncmusic.core.rules.parser import (
    Cond,
    Group,
    Node,
    RuleError,
    iter_conditions,
    parse_criteria,
    parse_date,
    parse_duration,
    parse_size,
    parse_sort,
)
from syncmusic.core.rules.templates import template_rules

Pred = Callable[[Track], bool]


@dataclass
class EvalContext:
    snapshots: Mapping[str, LibrarySnapshot]
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    # library id → the name of the account it is read as: its warnings name that user's library
    accounts: Mapping[str, str] = field(default_factory=dict)
    _aggregates: dict[str, dict[str, Any]] = field(default_factory=dict)

    def tracks(self, libraries: Iterable[str] | None) -> list[Track]:
        libs = list(self.snapshots) if libraries is None else [x for x in libraries]
        out: list[Track] = []
        for lib in libs:
            snap = self.snapshots.get(lib)
            if snap is not None:
                out.extend(snap.tracks.values())
        return out

    def playlist_members(self, ref: dict[str, Any], libraries: Iterable[str] | None) -> set[str]:
        """ItemRef keys of the tracks in the referenced playlist(s)."""
        members: set[str] = set()
        for pl in self.playlists(ref, libraries):
            members.update(r.key() for r in pl.track_refs)
        return members

    def playlists(self, ref: dict[str, Any], libraries: Iterable[str] | None) -> list[Playlist]:
        """The playlists a playlist reference ({id} / {name} / {path} / {any: true}) names."""
        libs = list(self.snapshots) if libraries is None else list(libraries)
        want_id = str(ref["id"]) if "id" in ref else None
        want_name = str(ref["name"]).casefold() if "name" in ref else None
        if want_id is None and want_name is None and "path" in ref:
            # "../favourites.nsp" → playlist named "favourites"
            base = str(ref["path"]).replace("\\", "/").rsplit("/", 1)[-1]
            want_name = base.rsplit(".", 1)[0].casefold()
        out: list[Playlist] = []
        for lib in libs:
            snap = self.snapshots.get(lib)
            if snap is None:
                continue
            for pid, pl in snap.playlists.items():
                if (
                    ref.get("any") is True
                    or (want_id is not None and want_id in (pid, pl.ref.key()))
                    or (want_name is not None and pl.name.casefold() == want_name)
                ):
                    out.append(pl)
        return out

    def aggregate(self, spec: FieldSpec) -> dict[str, Any]:
        """Per album/artist aggregate values, computed once per context."""
        assert spec.aggregate is not None
        cached = self._aggregates.get(spec.name)
        if cached is not None:
            return cached
        scope, fn = spec.aggregate.split("_")
        groups: dict[str, list[Any]] = defaultdict(list)
        for t in self.tracks(None):
            k = _group_key(t, scope)
            v = spec.get(t)
            if v is not None:
                groups[k].append(v)
        result: dict[str, Any] = {}
        for k, vals in groups.items():
            if not vals:
                continue
            result[k] = sum(vals) if fn == "sum" else (max(vals) if fn == "max" else min(vals))
        self._aggregates[spec.name] = result
        return result


def _group_key(t: Track, scope: str) -> str:
    if scope == "album":
        return f"{t.ref.library_id}/{t.album_id or (t.album_artist or t.artist or '') + '|' + (t.album or '')}"
    return f"{t.ref.library_id}/{t.artist_id or (t.artist or '')}"


# ---------------------------------------------------------------------------------------------
# condition compilation
# ---------------------------------------------------------------------------------------------


def _fold(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).casefold()


def _to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        if v.strip().lower() in ("true", "1", "yes"):
            return True
        if v.strip().lower() in ("false", "0", "no", ""):
            return False
    return None


def _to_num(v: Any) -> float | None:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _values(v: Any) -> list[Any]:
    """Field value as a list (multi-valued fields match if any value matches)."""
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [x for x in v if x is not None and x != ""]
    if v == "":
        return []
    return [v]


def _getter(cond: Cond, ctx: EvalContext) -> Callable[[Track], Any]:
    spec = cond.spec
    assert spec is not None
    if spec.aggregate is None:
        return spec.get
    agg = ctx.aggregate(spec)
    scope = spec.aggregate.split("_")[0]
    return lambda t: agg.get(_group_key(t, scope))


def compile_node(node: Node, ctx: EvalContext, libraries: Iterable[str] | None) -> Pred:
    if isinstance(node, Group):
        preds = [compile_node(c, ctx, libraries) for c in node.children]
        if not preds:
            # Navidrome: empty And{} matches all, empty Or{} is (1=0) and matches nothing.
            return (lambda t: True) if node.op == "all" else (lambda t: False)
        if len(preds) == 1:
            return preds[0]
        if node.op == "all":
            return lambda t: all(p(t) for p in preds)
        return lambda t: any(p(t) for p in preds)
    return _compile_cond(node, ctx, libraries)


def _compile_cond(c: Cond, ctx: EvalContext, libraries: Iterable[str] | None) -> Pred:
    op = c.op
    if op in ("inPlaylist", "notInPlaylist"):
        members = ctx.playlist_members(c.value, libraries)
        if op == "inPlaylist":
            return lambda t: t.ref.key() in members
        return lambda t: t.ref.key() not in members
    if op == "latestEpisodes":
        n, unplayed = c.value
        latest = _latest_per_series(ctx.tracks(libraries), n, unplayed)
        return lambda t: t.ref.key() in latest

    assert c.spec is not None
    get = _getter(c, ctx)
    ftype = c.spec.type
    val = c.value
    known = c.known

    def guard(p: Pred) -> Pred:
        # Unknown field (custom tag) that the track doesn't have → no match, also for negations.
        if known:
            return p
        return lambda t: get(t) not in (None, "") and p(t)

    if op in ("isMissing", "isPresent"):
        want_missing = bool(val) if op == "isMissing" else not bool(val)

        return lambda t: (not _values(get(t))) == want_missing

    if op in ("is", "isNot"):
        if ftype == "bool":
            want = _to_bool(val)
            p: Pred = lambda t: _to_bool(get(t)) is want and want is not None
        elif ftype == "number":
            n = _to_num(val)
            p = lambda t: n is not None and _to_num(get(t)) == n
        elif ftype == "date":
            d = parse_date(val)
            p = lambda t: d is not None and _same_day(parse_date(get(t)), d)
        else:
            f = _fold(val)
            p = lambda t: any(_fold(x) == f for x in _values(get(t)))
        if op == "is":
            return guard(p)
        return guard(lambda t: not p(t))

    if op in ("contains", "notContains", "startsWith", "endsWith"):
        f = _fold(val)
        if op == "startsWith":
            p = lambda t: any(_fold(x).startswith(f) for x in _values(get(t)))
        elif op == "endsWith":
            p = lambda t: any(_fold(x).endswith(f) for x in _values(get(t)))
        else:
            p = lambda t: any(f in _fold(x) for x in _values(get(t)))
        if op == "notContains":
            return guard(lambda t: not p(t))
        return guard(p)

    if op in ("gt", "lt", "inTheRange", "before", "after"):
        bounds = val if op == "inTheRange" else [val]
        numeric_bounds = all(isinstance(x, (int, float)) for x in bounds)
        as_date = ftype == "date" or op in ("before", "after") or not numeric_bounds
        conv: Callable[[Any], Any] = _date_key if as_date else _to_num
        if op == "inTheRange":
            lo, hi = conv(val[0]), conv(val[1])
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            return guard(lambda t: _between(conv(get(t)), lo, hi))
        bound = conv(val)
        if op in ("gt", "after"):
            return guard(lambda t: _cmp(conv(get(t)), bound) > 0)
        return guard(lambda t: _cmp(conv(get(t)), bound) < 0)

    if op in ("inTheLast", "notInTheLast"):
        cutoff = ctx.now - timedelta(days=float(val))
        if op == "inTheLast":
            return guard(lambda t: (d := parse_date(get(t))) is not None and d >= cutoff)
        # Navidrome: "not in the last N days" includes never (NULL)
        return guard(lambda t: (d := parse_date(get(t))) is None or d < cutoff)

    if op in ("in", "notIn"):
        wanted = {_fold(x) for x in val}
        p = lambda t: any(_fold(x) in wanted for x in _values(get(t)))
        if op == "in":
            return guard(p)
        return guard(lambda t: not p(t))

    raise RuleError(f"operator {op!r} not implemented")


def _same_day(a: datetime | None, b: datetime) -> bool:
    return a is not None and a.date() == b.date()


def _date_key(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 1000 <= v <= 9999:
        return datetime(int(v), 1, 1, tzinfo=UTC).timestamp()  # a year compared with dates
    d = parse_date(v)
    return d.timestamp() if d is not None else None


def _cmp(a: Any, b: Any) -> int:
    if a is None or b is None:
        return 0  # incomparable → neither gt nor lt
    return int(a > b) - int(a < b)


def _between(v: Any, lo: Any, hi: Any) -> bool:
    return v is not None and lo is not None and hi is not None and lo <= v <= hi


def _latest_per_series(tracks: list[Track], n: int, unplayed: bool = False) -> set[str]:
    """The `n` newest episodes of each podcast; `unplayed`: of those not finished.
    Podcasts only: "latest N" of an audiobook would cut a book into arbitrary chapters."""
    groups: dict[str, list[Track]] = defaultdict(list)
    for t in tracks:
        if t.series and t.kind == MediaKind.PODCAST and not (unplayed and t.finished):
            groups[f"{t.ref.library_id}|{t.series}"].append(t)
    out: set[str] = set()
    floor = datetime.min.replace(tzinfo=UTC)
    for items in groups.values():
        items.sort(
            key=lambda t: (t.published or t.date_added or floor, t.chapter_index or 0),
            reverse=True,
        )
        out.update(t.ref.key() for t in items[:n])
    return out


# ---------------------------------------------------------------------------------------------
# sorting / limiting
# ---------------------------------------------------------------------------------------------


def default_order_key(t: Track) -> tuple[Any, ...]:
    return (
        (t.album_artist or t.artist or "").casefold(),
        t.year or 0,
        (t.album or "").casefold(),
        t.disc_no or 0,
        t.track_no or 0,
        t.chapter_index or 0,
        t.title.casefold(),
        t.ref.key(),
    )


def _sort_value(spec: FieldSpec, t: Track, ctx: EvalContext) -> tuple[int, Any]:
    """Sort key: (0, 0) empty < (1, number/date) < (2, string)."""
    if spec.aggregate is not None:
        v = ctx.aggregate(spec).get(_group_key(t, spec.aggregate.split("_")[0]))
    else:
        v = spec.get(t)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None or v == "":
        return (0, 0)  # empty first in ascending order (SQL NULLs-first)
    if isinstance(v, datetime):
        return (1, v.timestamp())
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):
        return (1, float(v))
    d = parse_date(v) if spec.type == "date" else None
    if d is not None:
        return (1, d.timestamp())
    return (2, str(v).casefold())  # strings after numbers (mixed custom tags must not crash)


def random_seed(rule: Rule, now: datetime) -> str:
    """The rule id, plus the ISO week or the month with a `random_period`: the order stays the same
    within a period and is shuffled anew when the next one starts."""
    if rule.random_period == "week":
        year, week, _ = now.isocalendar()
        return f"{rule.id}|{year}-W{week:02d}"
    if rule.random_period == "month":
        return f"{rule.id}|{now.year}-{now.month:02d}"
    return rule.id


def sort_tracks(tracks: list[Track], rule: Rule, ctx: EvalContext) -> tuple[list[Track], list[str]]:
    keys, random, warnings = parse_sort(rule.sort, rule.order)
    out = sorted(tracks, key=default_order_key)
    if rule.order == "desc" and not keys and not random:
        out.reverse()
    if random:
        seed = random_seed(rule, ctx.now)

        def rkey(t: Track) -> str:
            return hashlib.sha1(f"{seed}|{t.ref.key()}".encode()).hexdigest()

        out.sort(key=rkey)  # stable "random": an item keeps its place across runs
        return out, warnings
    for k in reversed(keys):
        spec = k.spec
        assert spec is not None

        def skey(t: Track, s: FieldSpec = spec) -> tuple[int, Any]:
            return _sort_value(s, t, ctx)

        out.sort(key=skey, reverse=k.desc)
    return out, warnings


def apply_limits(tracks: list[Track], rule: Rule) -> list[Track]:
    out = tracks
    if rule.limit is not None and rule.limit >= 0:
        out = out[: rule.limit]
    if rule.limit_percent is not None and out:
        pct = max(1, min(100, rule.limit_percent))
        out = out[: max(1, math.floor(len(out) * pct / 100))]
    if rule.limit_size:
        budget = parse_size(rule.limit_size)
        acc, cut = 0, 0
        for t in out:
            if acc + t.size_bytes > budget:
                break
            acc += t.size_bytes
            cut += 1
        out = out[:cut]
    if rule.limit_duration:
        budget_s = parse_duration(rule.limit_duration)
        acc_s, cut = 0.0, 0
        for t in out:
            if acc_s + (t.duration_s or 0) > budget_s:
                break
            acc_s += t.duration_s or 0
            cut += 1
        out = out[:cut]
    return out


# ---------------------------------------------------------------------------------------------
# rules and rule sets
# ---------------------------------------------------------------------------------------------


@dataclass
class RuleResult:
    rule: Rule
    tracks: list[Track]
    warnings: list[str]
    playlist_refs: list[dict[str, Any]]  # inPlaylist references (their playlists go to the device)


def evaluate_rule(
    rule: Rule,
    ctx: EvalContext,
    attached: Iterable[str] | None = None,
) -> RuleResult:
    attached_list = list(ctx.snapshots) if attached is None else list(attached)
    libs = [x for x in attached_list if rule.libraries is None or x in rule.libraries]
    parsed = parse_criteria(rule.criteria)
    pred = compile_node(parsed.node, ctx, libs)
    kinds = set(rule.kind) if rule.kind else None
    matched = [t for t in ctx.tracks(libs) if (kinds is None or t.kind in kinds) and pred(t)]
    ordered, sort_warnings = sort_tracks(matched, rule, ctx)
    result = apply_limits(ordered, rule)
    playlist_refs = [c.value for c in iter_conditions(parsed.node) if c.op == "inPlaylist"]
    # a playlist that no attached library has (deleted, renamed, an id from another server) matches nothing
    # silently otherwise
    users = [ctx.accounts[x] for x in libs if x in ctx.accounts]
    where = f" in {', '.join(users)}'s library" if users else ""
    missing = [
        f"no playlist matches {c.op} {c.value}{where}"
        for c in iter_conditions(parsed.node)
        if c.op in ("inPlaylist", "notInPlaylist")
        and c.value.get("any") is not True
        and not ctx.playlists(c.value, libs)
    ]
    if users:  # picks the other user's library has but this one does not
        missing += _missing_picks(parsed.node, ctx, libs, where)
    return RuleResult(rule, result, parsed.warnings + sort_warnings + missing, playlist_refs)


_PICK_FIELDS = {"track_id", "album_id", "artist_id", "album", "artist", "albumartist"}


def _missing_picks(node: Node, ctx: EvalContext, libs: list[str], where: str) -> list[str]:
    """`is` picks (ids, artists, albums) no track of these libraries has: "2 picked items are not in Anna's
    library"."""
    picks = [
        c
        for c in iter_conditions(node)
        if c.op == "is" and c.field in _PICK_FIELDS and c.spec is not None
    ]
    if not picks:
        return []
    have: dict[str, set[str]] = {}
    for t in ctx.tracks(libs):
        for c in picks:
            assert c.spec is not None
            have.setdefault(c.field, set()).update(_fold(x) for x in _values(c.spec.get(t)))
    gone = [c for c in picks if _fold(c.value) not in have.get(c.field, set())]
    if not gone:
        return []
    what = "picked item is" if len(gone) == 1 else "picked items are"
    names = ", ".join(str(c.value) for c in gone[:3]) + (", …" if len(gone) > 3 else "")
    return [f"{len(gone)} {what} not{where} ({names})"]


def resolve_rules(
    ruleset: RuleSet, templates: Mapping[str, RuleTemplate]
) -> tuple[list[Rule], list[Rule], list[str]]:
    """Linked templates (in order) + inline rules; returns (rules, excludes, warnings)."""
    rules: list[Rule] = []
    excludes: list[Rule] = []
    warnings: list[str] = []
    for tid in dict.fromkeys(ruleset.template_ids):  # a template linked twice counts once
        tpl = templates.get(tid)
        if tpl is None:
            warnings.append(f"linked template {tid!r} does not exist")
            continue
        t_rules, t_excludes = template_rules(tpl)
        rules.extend(t_rules)
        excludes.extend(t_excludes)
    rules.extend(ruleset.rules)
    excludes.extend(ruleset.excludes)
    return rules, excludes, warnings


def evaluate_ruleset(
    ruleset: RuleSet,
    snapshots: Mapping[str, LibrarySnapshot],
    templates: Mapping[str, RuleTemplate] | None = None,
    *,
    attached: Iterable[str] | None = None,
    allowed_kinds: set[MediaKind] | None = None,
    now: datetime | None = None,
    accounts: Mapping[str, str] | None = None,
) -> Selection:
    ctx = EvalContext(snapshots, now or datetime.now(UTC), accounts or {})
    rules, excludes, warnings = resolve_rules(ruleset, templates or {})
    attached_list = list(snapshots) if attached is None else list(attached)

    prio = {rid: i for i, rid in enumerate(ruleset.priority)}
    enabled = [r for r in rules if r.enabled]
    ordered_rules = sorted(
        enumerate(enabled), key=lambda ir: (prio.get(ir[1].id, len(prio)), ir[0])
    )

    excluded: dict[str, str] = {}  # key -> the first exclude rule that matched it
    for ex in excludes:
        if not ex.enabled:
            continue
        res = evaluate_rule(ex, ctx, attached_list)
        warnings.extend(f"exclude {ex.name or ex.id}: {w}" for w in res.warnings)
        for t in res.tracks:
            excluded.setdefault(t.ref.key(), ex.id)

    kinds: set[MediaKind] = set()
    for r in enabled:  # a rule without a kind selects every kind
        kinds |= set(r.kind) if r.kind else set(MediaKind)
    items: dict[str, list[str]] = {}
    results: list[RuleResult] = []
    skipped_kinds = 0
    for _, rule in ordered_rules:
        res = evaluate_rule(rule, ctx, attached_list)
        results.append(res)
        warnings.extend(f"rule {rule.name or rule.id}: {w}" for w in res.warnings)
        for t in res.tracks:
            key = t.ref.key()
            if key in excluded:
                continue
            if allowed_kinds is not None and t.kind not in allowed_kinds:
                skipped_kinds += 1
                continue
            by = items.setdefault(key, [])
            if rule.id not in by:
                by.append(rule.id)
    if skipped_kinds:
        warnings.append(
            f"{skipped_kinds} selected items skipped: media kind not supported by device"
        )

    playlists: list[DevicePlaylist] = []
    seen_names: set[str] = set()
    for res in results:
        rule = res.rule
        if rule.as_playlist:
            refs = [t.ref for t in res.tracks if t.ref.key() in items]
            playlists.append(DevicePlaylist(name=rule.as_playlist, refs=refs))
            seen_names.add(rule.as_playlist.casefold())
        # inPlaylist playlists go to the device by default (opt-out skip_playlists)
        if not rule.skip_playlists and (rule.include_playlists or res.playlist_refs):
            libs = [x for x in attached_list if rule.libraries is None or x in rule.libraries]
            # no inPlaylist condition (e.g. built-in All): every playlist of the rule's libraries
            for ref in res.playlist_refs or [{"any": True}]:
                for snap_id in libs:
                    snap = snapshots.get(snap_id)
                    if snap is None:
                        continue
                    for pid, pl in snap.playlists.items():
                        if not (
                            ref.get("any") is True
                            or ("id" in ref and str(ref["id"]) in (pid, pl.ref.key()))
                            or ("name" in ref and pl.name.casefold() == str(ref["name"]).casefold())
                        ):
                            continue
                        if pl.name.casefold() in seen_names:
                            continue
                        seen_names.add(pl.name.casefold())
                        refs = [r for r in pl.track_refs if r.key() in items]
                        playlists.append(
                            DevicePlaylist(name=pl.name, refs=refs, source=pl.ref, smart=pl.smart)
                        )
    return Selection(
        items=items, excluded=excluded, playlists=playlists, warnings=warnings, kinds=kinds
    )
