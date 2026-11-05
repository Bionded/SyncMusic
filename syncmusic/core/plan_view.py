# SPDX-License-Identifier: GPL-3.0-or-later
"""What the plan page gets of a big plan: totals per group, a diverse sample of at most
`SAMPLE_MAX` items, and the full list as a separate, paged or downloaded view. Apply always uses the whole plan."""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Callable, Iterator
from typing import Any

from syncmusic.core.models import ActionKind, LeftOut, MediaKind, PlanAction, SyncPlan, Track

SAMPLE_MAX = 100  # items the plan summary carries
PAGE_MAX = 1000  # items one GET /plans/{id}/items page returns at most

# the plan page's groups: the action kinds plus "left" (left out by the device limit or for space)
GROUPS = ["add", "replace", "update_meta", "remove", "keep", "left"]
# the Metadata tab: items whose metadata or listening data the sync changes, any action kind
META = "meta"


def _bucket(a: PlanAction) -> tuple[str, ...]:
    """What makes two items "alike" for the sample: library, kind, artist, album and the reason."""
    tr = a.item.track if a.item else None
    kind = tr.kind if tr else a.entry.kind if a.entry else MediaKind.MUSIC
    artist = (tr.album_artist or tr.artist or tr.author or "") if tr else (a.subtitle or "")
    album = (tr.album or "") if tr else ""
    return (a.ref.library_id, kind.value, artist.casefold(), album.casefold(), a.reason)


def _kind(a: PlanAction) -> str:
    return (a.item.track.kind if a.item else a.entry.kind if a.entry else MediaKind.MUSIC).value


def facets(plan: SyncPlan) -> dict[str, dict[str, int]]:
    """What the plan list's filters offer: items per library, media kind and rule id."""
    out: dict[str, dict[str, int]] = {"library": {}, "kind": {}, "rule": {}}
    for a in plan.actions:
        for name, value in (("library", a.ref.library_id), ("kind", _kind(a))):
            out[name][value] = out[name].get(value, 0) + 1
        for r in a.rules:
            out["rule"][r] = out["rule"].get(r, 0) + 1
    return out


def _filter_match(a: PlanAction, library: str, kind: str, rule: str) -> bool:
    return (
        (not library or a.ref.library_id == library)
        and (not kind or _kind(a) == kind)
        and (not rule or rule in a.rules)
    )


def _stable(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()


def spread[T](
    items: list[T], n: int, bucket: Callable[[T], tuple[str, ...]], key: Callable[[T], str]
) -> list[T]:
    """`n` of `items` taken round-robin over their buckets (in a stable, hashed order), so a sample of 12 000
    adds shows many artists and albums instead of the first album's tracks; kept in the items' order. Also the rule
    preview's examples."""
    if len(items) <= n:
        return items
    buckets: dict[tuple[str, ...], list[int]] = {}
    for i, x in enumerate(items):
        buckets.setdefault(bucket(x), []).append(i)
    order = sorted(buckets.values(), key=lambda ix: _stable(key(items[ix[0]])))
    picked: list[int] = []
    depth = 0
    while len(picked) < n:
        for ix in order:
            if depth < len(ix):
                picked.append(ix[depth])
                if len(picked) == n:
                    break
        depth += 1
    return [items[i] for i in sorted(picked)]


def track_bucket(t: Track) -> tuple[str, ...]:
    return (
        t.ref.library_id,
        t.kind.value,
        (t.album_artist or t.artist or t.author or "").casefold(),
        (t.album or "").casefold(),
    )


def _spread(actions: list[PlanAction], n: int) -> list[PlanAction]:
    return spread(actions, n, _bucket, lambda a: a.ref.key())


def _quotas(sizes: dict[str, int], total: int) -> dict[str, int]:
    """Share `total` between the groups evenly; what a small group does not use goes to the others."""
    quota = {g: 0 for g in sizes}
    left = total
    open_ = [g for g in sizes if sizes[g] > 0]
    while left > 0 and open_:
        each = max(1, left // len(open_))
        for g in list(open_):
            take = min(each, sizes[g] - quota[g], left)
            quota[g] += take
            left -= take
            if quota[g] >= sizes[g]:
                open_.remove(g)
            if left == 0:
                break
    return quota


def left_items(plan: SyncPlan) -> list[tuple[str, LeftOut]]:
    """The items left out: ("lim", ...) by the device limit, ("space", ...) for space."""
    out = [("lim", x) for x in (plan.limit.left_out if plan.limit else [])]
    detail = plan.space.dropped_detail or [LeftOut(key=k, title=k) for k in plan.space.dropped]
    return out + [("space", x) for x in detail]


def group_actions(plan: SyncPlan) -> dict[str, list[PlanAction]]:
    out: dict[str, list[PlanAction]] = {k.value: [] for k in ActionKind}
    for a in plan.actions:
        out[a.kind.value].append(a)
    return out


def sample(plan: SyncPlan, n: int = SAMPLE_MAX) -> dict[str, Any]:
    """The summary's lists cut to a diverse sample, and the totals the page shows instead of counting them:
    {actions, left_out, dropped_detail, totals: {group: n}, bytes: {group: size}, sampled: bool}."""
    groups = group_actions(plan)
    left = left_items(plan)
    sizes = {g: len(v) for g, v in groups.items()} | {"left": len(left)}
    quota = _quotas(sizes, n)
    picked = {id(a) for g, acts in groups.items() for a in _spread(acts, quota[g])}
    lefts = left[: quota["left"]]  # left-out items carry only a title: no buckets to spread over
    return {
        "actions": [a.model_dump(mode="json") for a in plan.actions if id(a) in picked],
        "left_out": [x.model_dump(mode="json") for why, x in lefts if why == "lim"],
        "dropped_detail": [x.model_dump(mode="json") for why, x in lefts if why == "space"],
        "totals": sizes,
        "bytes": {g: sum(a.size_delta for a in acts) for g, acts in groups.items()}
        | {"left": sum(x.size for _, x in left)},
        "sampled": sum(sizes.values()) > n,
    }


def meta_view(plan: SyncPlan, n: int = SAMPLE_MAX) -> dict[str, Any]:
    """The Metadata tab's start: {total, changes: {"direction:field": n}, actions: a diverse
    sample of the items with changes}."""
    acts = [a for a in plan.actions if a.changes]
    counts: dict[str, int] = {}
    for a in acts:
        for c in a.changes:
            k = f"{c.direction}:{c.field}"
            counts[k] = counts.get(k, 0) + 1
    return {
        "total": len(acts),
        "changes": counts,
        "conflicts": sum(1 for a in acts for c in a.changes if c.conflict),
        "actions": [a.model_dump(mode="json") for a in _spread(acts, n)],
    }


def _matches(text: str, q: str) -> bool:
    return not q or q.casefold() in text.casefold()


def _action_text(a: PlanAction) -> str:
    tr = a.item.track if a.item else None
    parts = [a.title, a.subtitle or "", a.reason]
    if tr:
        parts += [tr.title or "", tr.artist or tr.author or "", tr.album or ""]
    return " ".join(parts)


def _meta_match(a: PlanAction, field: str, direction: str) -> bool:
    return any(
        (not field or c.field == field) and (not direction or c.direction == direction)
        for c in a.changes
    )


def items_page(
    plan: SyncPlan,
    group: str | None,
    q: str,
    offset: int,
    limit: int,
    field: str = "",
    direction: str = "",
    library: str = "",
    kind: str = "",
    rule: str = "",
) -> dict[str, Any]:
    """One page of the full list (JSON): {total, offset, limit, actions, left} for one group or all of them,
    filtered by `q` (title, artist, album, reason). `actions` are the plan's actions as the summary has them,
    `left` are {why: lim|space, key, title, size}. Group `meta`: the items with metadata changes, filtered by the
    change's `field` and `direction`. `library`, `kind` and `rule` narrow every group ; left-out items carry only their key, so they match a library filter and no kind or rule filter."""
    limit = max(1, min(limit, PAGE_MAX))
    if group == META:
        acts = [
            a
            for a in plan.actions
            if a.changes and _meta_match(a, field, direction) and _matches(_action_text(a), q)
        ]
        return {
            "total": len(acts),
            "offset": offset,
            "limit": limit,
            "actions": [a.model_dump(mode="json") for a in acts[offset : offset + limit]],
            "left": [],
        }
    acts = [
        a
        for a in plan.actions
        if (group in (None, a.kind.value))
        and _filter_match(a, library, kind, rule)
        and _matches(_action_text(a), q)
    ]
    lefts = [
        (why, x)
        for why, x in left_items(plan)
        if group in (None, "left")
        and not kind
        and not rule
        and (not library or x.key.partition("/")[0] == library)
        and _matches(f"{x.title} {x.key}", q)
    ]
    total = len(acts) + len(lefts)
    page_acts = acts[offset : offset + limit]
    rest = limit - len(page_acts)
    start = max(0, offset - len(acts))
    page_left = lefts[start : start + rest] if rest > 0 else []
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "actions": [a.model_dump(mode="json") for a in page_acts],
        "left": [{"why": why} | x.model_dump(mode="json") for why, x in page_left],
    }


CSV_FIELDS = [
    "group",
    "title",
    "artist",
    "album",
    "reason",
    "size_delta",
    "library_id",
    "item_id",
    # the Metadata rows: one per change, group "meta"
    "field",
    "old",
    "new",
    "direction",
    "conflict",
]
_NO_META = {"field": "", "old": "", "new": "", "direction": "", "conflict": ""}


def _rows(plan: SyncPlan) -> Iterator[dict[str, Any]]:
    for a in plan.actions:
        tr = a.item.track if a.item else None
        row = {
            "group": a.kind.value,
            "title": (tr.title if tr and tr.title else a.title) or a.ref.item_id,
            "artist": (tr.artist or tr.author or "") if tr else (a.subtitle or ""),
            "album": (tr.album or "") if tr else "",
            "reason": a.reason,
            "size_delta": a.size_delta,
            "library_id": a.ref.library_id,
            "item_id": a.ref.item_id,
        }
        yield row | _NO_META
        for c in a.changes:
            yield row | {
                "group": META,
                "size_delta": 0,
                "field": c.field,
                "old": c.old or "",
                "new": c.new or "",
                "direction": c.direction,
                "conflict": c.conflict,
            }
    for why, x in left_items(plan):
        lib, _, item = x.key.partition("/")
        yield {
            "group": "left",
            "title": x.title or x.key,
            "artist": "",
            "album": "",
            "reason": "device limit" if why == "lim" else "space",
            "size_delta": x.size,
            "library_id": lib,
            "item_id": item,
        } | _NO_META


def items_csv(plan: SyncPlan) -> str:
    """The whole plan as CSV (one row per item, every group) for the page's download."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    w.writerows(_rows(plan))
    return buf.getvalue()


def items_json(plan: SyncPlan) -> list[dict[str, Any]]:
    return list(_rows(plan))
