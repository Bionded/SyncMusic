# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure adjustments of a sync plan after it was made: ratings read by the reverse sync (step 0), deferred
device rewrites, and the update-sync subset of a plan. Split out of core/sync.py."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from typing import Any

from syncmusic.base.feedback import stores_ratings
from syncmusic.base.library import Library
from syncmusic.core.models import (
    ActionKind,
    DeviceConfig,
    PlanAction,
    ReverseReport,
    SyncPlan,
    SyncReport,
)
from syncmusic.core.planner import meta_hash


def ratings_after_reverse(
    plan: SyncPlan,
    reverse: ReverseReport,
    cfg: DeviceConfig,
    writes_ratings: bool,
    libraries: Mapping[str, Library],
) -> SyncPlan:
    """The plan was made from library values read before step 0 (docs/08 Ratings).

    Device priority (default): a rating changed on the device was pushed to the library (or is pending), so the
    forward sync writes and remembers that value, never the stale library one - also when the library changed
    too. The planned meta hash follows only for a library that stores ratings (the next plan then sees no
    change). Library priority: the device's rating was not pushed; a device that stores ratings gets the
    library's value written back (KEEP → UPDATE_META)."""
    ratings = reverse.device_ratings
    if not ratings:
        return plan
    plan = plan.model_copy(deep=True)
    for a in plan.actions:
        key = a.ref.key()
        if a.item is None or key not in ratings:
            continue
        lib_rates = stores_ratings(libraries.get(a.ref.library_id))
        if cfg.sync.rating_priority == "device":
            track = a.item.track.model_copy(update={"rating": ratings[key]})
            a.item = a.item.model_copy(update={"track": track})
            if lib_rates:
                a.meta_hash = meta_hash(track)
        elif (
            lib_rates
            and writes_ratings
            and a.kind is ActionKind.KEEP
            and ratings[key] != a.item.track.rating
        ):
            a.kind = ActionKind.UPDATE_META
            a.reason = "rating changed on the device; the library's rating has priority"
    return plan


def defer_device_rewrites(plan: SyncPlan, reverse: ReverseReport, writes_ratings: bool) -> SyncPlan:
    """Step 0 failed on a device whose ratings the forward path writes (iPod): an UPDATE_META or REPLACE
    would put the stale library rating over the device's unread one and lose it for good. They become KEEPs that do
    not advance the manifest's hashes, so the next sync (with a working step 0) plans them again."""
    if not (reverse.failed and writes_ratings):
        return plan
    plan = plan.model_copy(deep=True)
    deferred = 0
    for a in plan.actions:
        if a.kind in (ActionKind.UPDATE_META, ActionKind.REPLACE):
            a.kind, a.meta_hash, a.size_delta = ActionKind.KEEP, "", 0
            a.reason = "deferred: the reverse sync failed, the device's ratings were not read"
            deferred += 1
    if deferred:
        reverse.errors.append(
            f"{deferred} updates of tracks on the device deferred until the reverse sync works"
        )
    return plan


def metadata_rewrite(a: PlanAction) -> bool:
    """A REPLACE whose source and processing are unchanged: only the tags in the file differ (a file device with a
    tag step, planner "metadata changed (tags in the file)"), so the same file is rewritten in place."""
    return (
        a.kind is ActionKind.REPLACE
        and a.entry is not None
        and a.item is not None
        and a.entry.source_state == a.item.track.source_state
        and a.entry.chain_hash == a.chain_hash
    )


def update_only(plan: SyncPlan, removals: bool = False, consumed: Collection[str] = ()) -> SyncPlan:
    """The plan of an update sync: what changes on the device without new files - metadata,
    ratings, positions, playlists. ADDs are left for the next full sync; a REPLACE (a new file) becomes a KEEP that
    does not advance the manifest's hashes, so the full sync still plans it - except a tags-only rewrite on a file
    device (374.3: the metadata lives in the file there); REMOVEs only with `removals`. The manifest keeps every
    entry the plan no longer names. `consumed`: keys removed even without `removals` - played, finished or rated on
    this device and no longer selected because of it."""
    plan = plan.model_copy(deep=True)
    actions: list[PlanAction] = []
    for a in plan.actions:
        if a.kind is ActionKind.ADD:
            continue
        if a.kind is ActionKind.REMOVE and not removals and a.ref.key() not in consumed:
            continue
        if a.kind is ActionKind.REPLACE and not metadata_rewrite(a):
            a.kind, a.meta_hash, a.size_delta = ActionKind.KEEP, "", 0
            a.reason = "the new file waits for a full sync"
        actions.append(a)
    plan.actions = actions
    plan.does_not_fit = False
    return plan


def removed_played(n: int, message: str) -> str:
    """The report line of an update that removed what the device played."""
    line = f"removed {n} played item{'' if n == 1 else 's'}"
    return f"{message}; {line}" if message else line


def update_writes_items(plan: SyncPlan) -> bool:
    """An update plan that changes items (metadata in the device DB or in the file, removals, a touched source
    whose new state goes into the manifest): always applied."""
    return any(a.kind is not ActionKind.KEEP or a.restamp for a in plan.actions)


def update_signature(plan: SyncPlan) -> str:
    """The device-level state an update sync writes: playlists and positions; equal signatures and no
    item changes = a periodic check has nothing to do."""
    parts: list[Any] = [[p.name, [r.key() for r in p.refs]] for p in plan.playlists]
    parts += sorted(
        (a.ref.key(), a.item.track.position_s)
        for a in plan.actions
        if a.item is not None and a.item.track.position_s is not None
    )
    return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()


def update_summary(report: SyncReport) -> dict[str, int]:
    """What an update changed per kind, only kinds with a change: tags (metadata in the device database
    or a tags-only file rewrite), ratings, positions, playlists, removed."""
    c = report.counts
    out = {
        "tags": c.get("update_meta", 0) + c.get("replace", 0),
        "ratings": c.get("ratings", 0),
        "positions": c.get("positions", 0),
        "playlists": c.get("playlists", 0),
        "removed": c.get("remove", 0),
    }
    return {k: v for k, v in out.items() if v}
