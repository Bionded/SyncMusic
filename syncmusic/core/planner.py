# SPDX-License-Identifier: GPL-3.0-or-later
"""Planner: rules + snapshots + device manifest + capabilities + processor chain → SyncPlan.

Pure (no I/O). For every selected item the chain's `plan()` predicts the device file; the result is diffed
against the manifest:
- `add`: selected, not on the device (also: in the manifest, but the device reports the file `missing`, e.g. the
  user deleted it on the player - the manifest is the source of truth only for files that still exist);
- `replace`: on the device, but the source changed (`source_state`) or the processing did (`chain_hash`), or
  the metadata changed and the device reads tags from the file (a tag-writing processor is in the chain). A source
  that was only touched - same size and cover, only its timestamp moved, metadata unchanged (`touched_only`) - is
  kept instead, with `restamp`: the applier stores the new `source_state` in the manifest;
- `update_meta`: only metadata changed and the device keeps metadata in its own DB (iPod);
- `remove`: on the device (manifest = managed by us) and no longer selected - `mirror` mode only;
- `keep`: everything else (also carries the planned item so the applier can refresh the manifest entry). An item
  its library no longer has, or whose library was removed, disabled or cannot be read, is an
  **orphan**: kept in both modes (`orphan=True`); Device settings / Player contents remove it.
  An item of a readable library that no rule selects any more (e.g. its template was unlinked) is just
  unselected (`remove` in mirror mode). An item deleted from its (still readable) library is removed instead (`deleted=True`) when the
  device's `remove_deleted` is on - unless that library returned nothing, or more than `MASS_VANISH_PCT` % of its
  items on the device vanished at once (a rescan glitch): those stay orphans, with a warning.
Unmanaged files never appear in a plan. Selected items the device cannot take (media kind, format after the
chain) are skipped with a warning.

Confirmation: a first sync (no manifest) always needs it; in auto mode a plan removing more than
`delete_threshold_pct` % of the managed items needs it too. An attached library that returned no items at all (an unmounted share, a rescan glitch) removes nothing: its items are orphans, with a warning. Space is computed
separately (`core/space.py`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core import books
from syncmusic.core.manifest import is_external
from syncmusic.core.models import (
    ActionKind,
    DeviceConfig,
    DeviceFeedback,
    DevicePlaylist,
    FillKey,
    LeftOut,
    LibrarySnapshot,
    LimitReport,
    Manifest,
    ManifestEntry,
    MediaKind,
    MetaChange,
    PlanAction,
    PlanItem,
    PlaylistChange,
    ReverseSyncSettings,
    RuleSet,
    RuleTemplate,
    SyncPlan,
    Track,
    compact_tracks,
    playlist_hash,
)
from syncmusic.core.prepare import CAPS_HASH_VERSION, Chain, initial_plan_item, inputs_diff
from syncmusic.core.rules import evaluate_ruleset
from syncmusic.core.rules.evaluator import resolve_rules

log = logging.getLogger(__name__)

# the containers a stock iPod plays video from (the `video` processor writes m4v)
IPOD_VIDEO_FORMATS = ("mp4", "m4v", "mov")

# Track fields that end up in the device's metadata (tags or device DB). Play counts are reverse-synced
# separately and are not part of it.
META_FIELDS = (
    "kind",
    "title",
    "artist",
    "album_artist",
    "album",
    "track_no",
    "disc_no",
    "year",
    "genres",
    "compilation",
    "comment",
    "series",
    "author",
    "chapter_index",
    "published",
    "rating",
)


# built once: per-item hot path
_META_JSON = json.JSONEncoder(sort_keys=True, default=str, ensure_ascii=False)


def meta_hash(t: Track) -> str:
    data: dict[str, Any] = {f: getattr(t, f) for f in META_FIELDS}
    if t.published is not None and t.kind in (MediaKind.PODCAST, MediaKind.AUDIOBOOK):
        # the release date reaches the device since only these items' hashes change, so episodes
        # already on a device get it with their next update (a metadata write) and nothing else is rewritten
        data["released"] = 1
    return hashlib.sha256(_META_JSON.encode(data).encode()).hexdigest()[:16]


def meta_snapshot(t: Track) -> dict[str, str]:
    """The metadata written to the device as text, unset fields left out: the manifest keeps it
    so the next plan can show what changes."""
    out: dict[str, str] = {}
    for f in META_FIELDS:
        v = getattr(t, f)
        if v is None or v == "" or v == [] or v is False:
            continue
        out[f] = ", ".join(map(str, v)) if isinstance(v, list) else str(getattr(v, "value", v))
    return out


NOT_STARTED_S = 5.0  # a resume position below this is "not started", like none


def position_changed(new: float | None, old: float | None) -> bool:
    """Whether a spoken-word resume position really moved: none, 0 and a few seconds are all "not started"
    (an unplayed episode is no plan change), otherwise a move of a second or more."""
    a = 0.0 if new is None or new < NOT_STARTED_S else new
    b = 0.0 if old is None or old < NOT_STARTED_S else old
    return abs(a - b) >= 1


def meta_changes(
    entry: ManifestEntry, track: Track, caps: DeviceCapabilities, tags: bool = True
) -> list[MetaChange]:
    """What a sync changes on the device besides files: each changed tag (old → new; one
    "metadata" change when the entry predates the snapshot), the rating and a spoken-word resume position that
    the forward sync writes (the same tests as the applier; a bookmark moved on the device wins, see 548.2)."""
    out: list[MetaChange] = []
    new = meta_snapshot(track)
    if tags and entry.meta_hash != meta_hash(track):
        if not entry.meta:
            out.append(MetaChange(field="metadata"))
        else:
            for f in META_FIELDS:
                if f != "rating" and entry.meta.get(f) != new.get(f):
                    out.append(MetaChange(field=f, old=entry.meta.get(f), new=new.get(f)))
    if (
        caps.supports_ratings_write
        and track.rating is not None
        and track.rating != entry.last_rating
    ):
        old = None if entry.last_rating is None else str(entry.last_rating)
        out.append(MetaChange(field="rating", old=old, new=str(track.rating)))
    pos = track.position_s
    if (
        entry.kind is not MediaKind.MUSIC
        and pos is not None
        and position_changed(pos, entry.last_position_s)
    ):
        old = None if entry.last_position_s is None else f"{entry.last_position_s:g}"
        out.append(MetaChange(field="position", old=old, new=f"{pos:g}"))
    return out


def reverse_changes(
    plan: SyncPlan, fb: DeviceFeedback, settings: ReverseSyncSettings, rating_priority: str
) -> None:
    """The listening data a sync sends to the libraries, from a dry read of the device's feedback
    (nothing consumed): plays (count old → new, a spoken-word item played), ratings and positions, each on its
    action. A bookmark moved on the device replaces the forward position change (the applier leaves it alone); both sides moved → a conflict that says which side wins."""
    plays = Counter(p.ref.key() for p in fb.plays) if settings.plays else Counter()
    for a in plan.actions:
        e, tr = a.entry, a.item.track if a.item else None
        if e is None or tr is None or a.kind is ActionKind.REMOVE or e.orphan:
            continue
        key = a.ref.key()
        back: list[MetaChange] = []
        if n := plays.get(key):
            back.append(
                MetaChange(
                    field="play_count",
                    old=str(tr.play_count),
                    new=str(tr.play_count + n),
                    direction="to_library",
                )
            )
        if settings.ratings and key in fb.ratings and fb.ratings[key] != tr.rating:
            r = fb.ratings[key]
            moved = tr.rating != e.last_rating
            lib_wins = rating_priority == "library"
            back.append(
                MetaChange(
                    field="rating",
                    old=None if tr.rating is None else str(tr.rating),
                    new=None if r is None else str(r),
                    direction="to_library",
                    conflict=(
                        "both changed: the library's rating wins (rating priority: library)"
                        if lib_wins
                        else "both changed: the device's rating wins (rating priority: device)"
                    )
                    if moved
                    else "",
                )
            )
            if not lib_wins:
                a.changes = [c for c in a.changes if c.field != "rating"]
        pos = fb.positions.get(key) if settings.positions else None
        if pos is not None:
            if pos.finished and not tr.finished:
                back.append(
                    MetaChange(field="played", old="unplayed", new="played", direction="to_library")
                )
            if position_changed(pos.position_s, tr.position_s):
                moved = (
                    tr.position_s is not None
                    and e.last_position_s is not None
                    and position_changed(tr.position_s, e.last_position_s)
                )
                back.append(
                    MetaChange(
                        field="position",
                        old=None if tr.position_s is None else f"{tr.position_s:g}",
                        new=f"{pos.position_s:g}",
                        direction="to_library",
                        conflict="both moved: the newer position wins (the device's when a time is unknown)"
                        if moved
                        else "",
                    )
                )
            a.changes = [c for c in a.changes if c.field != "position"]
        a.changes = a.changes + back


def with_disc_totals(tracks: dict[str, Track]) -> dict[str, Track]:
    """Music without a disc-total tag gets its album's highest disc number in the library:
    a release whose tracks are all disc 1 is single-disc. Tracks with the tag, and other kinds, stay as they are."""
    top: dict[tuple[str, str], int] = {}

    def album(t: Track) -> tuple[str, str] | None:
        if t.kind is not MediaKind.MUSIC or not (t.album_id or t.album):
            return None
        return t.ref.library_id, t.album_id or f"{t.album_artist or t.artist or ''}|{t.album}"

    for t in tracks.values():
        a = album(t)
        if a is not None and t.disc_no:
            top[a] = max(top.get(a, 0), t.disc_no)
    out = dict(tracks)
    for k, t in tracks.items():
        a = album(t)
        if t.disc_total is None and a is not None and a in top:
            out[k] = c = t.model_copy(update={"disc_total": top[a]})
            compact_tracks(
                (c,)
            )  # a pending plan keeps the copies: share one fields set, as snapshot tracks do
    return out


def supported_kinds(caps: DeviceCapabilities) -> set[MediaKind]:
    return {k for k in MediaKind if caps.supports_kind(k)}


def _writes_tags(chain: Chain) -> bool:
    return any(p.stage == "tags" for p in chain.processors)


@dataclass
class _Candidate:
    """A selected item the device can take, before the device limit."""

    key: str
    track: Track
    rules: list[str]
    item: PlanItem
    chain_hash: str
    processing: dict[str, str]

    @property
    def size(self) -> int:
        return self.item.file.size + self.item.sidecar_bytes


FILL_LABELS: dict[str, str] = {
    "priority": "rule priority",
    "rating": "rating",
    "recently_played": "recently played",
    "play_count": "play count",
    "recently_added": "recently added",
    "random": "random",
}


def _newest_first(d: datetime | None) -> float:
    return -d.timestamp() if d else 0.0  # never played / no date: after every dated item


def _fill_key(key: FillKey, seed: str) -> Callable[[_Candidate], Any]:
    """Sort key for one fill order; ascending sort = filled first."""
    if key == "rating":
        return lambda c: (-(c.track.rating or 0), not c.track.starred)
    if key == "recently_played":
        return lambda c: _newest_first(c.track.last_played)
    if key == "play_count":
        return lambda c: -c.track.play_count
    if key == "recently_added":
        return lambda c: _newest_first(c.track.date_added)
    if key == "random":  # stable per device: the same items stay on it across syncs
        return lambda c: hashlib.sha1(f"{seed}|{c.key}".encode()).hexdigest()
    return lambda c: 0  # priority: the selection order decides alone


def apply_device_limit(
    candidates: list[_Candidate],
    rs: RuleSet,
    entries: Mapping[str, ManifestEntry],
    *,
    seed: str,
    missing: Collection[str] = (),
) -> tuple[list[_Candidate], LimitReport]:
    """Keep candidates within `rs.limit_bytes` / `rs.limit_tracks`, filled in `rs.fill_order` (ties:
    the selection order = rule priority). Strict order: the first item that does not fit ends the fill, so a
    lower-priority small item never takes the place of a higher-priority one. In additive mode the items already on
    the device go first: they would stay anyway. Returns the kept candidates (selection order) and the report."""
    keys = [_fill_key(k, seed) for k in rs.fill_order]
    additive = rs.mode == "additive"

    def order(ic: tuple[int, _Candidate]) -> tuple[Any, ...]:
        i, c = ic
        first = 0 if additive and c.key in entries and c.key not in missing else 1
        return (first, *(k(c) for k in keys), i)

    kept: set[str] = set()
    total = 0
    full = False
    for _, c in sorted(enumerate(candidates), key=order):
        if not full and (
            (rs.limit_tracks is not None and len(kept) >= rs.limit_tracks)
            or (rs.limit_bytes is not None and total + c.size > rs.limit_bytes)
        ):
            full = True
        if full:
            continue
        kept.add(c.key)
        total += c.size
    left = [c for c in candidates if c.key not in kept]
    report = LimitReport(
        limit_bytes=rs.limit_bytes,
        limit_tracks=rs.limit_tracks,
        fill_order=list(rs.fill_order) or ["priority"],
        kept_items=len(kept),
        kept_bytes=total,
        left_out=[LeftOut(key=c.key, title=_label(c.track), size=c.size) for c in left],
        left_out_bytes=sum(c.size for c in left),
    )
    return [c for c in candidates if c.key in kept], report


def _label(t: Track) -> str:
    who = t.artist or t.album_artist or t.author
    return f"{who} – {t.title}" if who else t.title


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1000:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def limit_warning(r: LimitReport, player_tracks: int | None = None) -> str:
    """`player_tracks`: the most tracks the player handles (device profile), named when it is the limit."""
    parts = []
    if r.limit_tracks is not None:
        parts.append(f"{r.limit_tracks} track{'s' if r.limit_tracks != 1 else ''}")
        if r.limit_tracks == player_tracks:
            parts[-1] += " - the most this player handles"
    if r.limit_bytes is not None:
        parts.append(human_size(r.limit_bytes))
    order = ", ".join(FILL_LABELS.get(k, k) for k in r.fill_order)
    n = len(r.left_out)
    return (
        f"{n} selected item{'s' if n != 1 else ''} ({human_size(r.left_out_bytes)}) left out by the device limit "
        f"({' / '.join(parts)}; filled by {order})"
    )


MASS_VANISH_PCT = (
    20  # more of a library's items on the device gone at once: a glitch, not deletions
)
MASS_VANISH_MIN = (
    5  # ...and more than this many (one deleted song of a small selection is not a glitch)
)


def plan_sync(
    device: DeviceConfig,
    snapshots: Mapping[str, LibrarySnapshot],
    manifest: Manifest | None,
    caps: DeviceCapabilities,
    chain: Chain,
    *,
    templates: Mapping[str, RuleTemplate] | None = None,
    auto: bool = False,
    space_steps: bool = False,
    missing: Collection[str] = (),
    now: datetime | None = None,
    library_names: Mapping[str, str] | None = None,
    account_names: Mapping[str, str] | None = None,
    attached: Collection[str] | None = None,
) -> SyncPlan:
    """The plan for one device. `snapshots` = the libraries read for it (by library id): the attached ones
    (those its templates draw from; `attached`, default: all snapshots) plus any other enabled library it holds
    managed items of - an item of such a library that no rule selects any more is unselected (removed in
    mirror mode), not an orphan.
    `space_steps` enables the transcode `when: space_needed` rules (set by `core/space.py`).
    `missing` = manifest keys whose file is gone from the device (`Device.missing`). `library_names`: by id, for
    the removal reasons. `account_names`: library id → the account the device reads it as (warnings).

    Steps: select (rules) → candidates (chain + format check) → device limit → add/replace/keep the selected →
    remove/keep what is on the device but not selected → playlists → warnings and confirmations."""
    attached = [
        lid
        for lid in (attached if attached is not None else device.library_ids or list(snapshots))
        if lid in snapshots
    ]
    selection = evaluate_ruleset(
        device.rule_set,
        snapshots,
        templates,
        attached=attached,
        allowed_kinds=supported_kinds(caps),
        now=now,
        accounts=account_names,
    )
    tracks = with_disc_totals(
        {t.ref.key(): t for snap in snapshots.values() for t in snap.tracks.values()}
    )
    selected = selection.items
    if device.sync.combine_audiobooks:  # a multi-file audiobook as one book with chapters
        by_part: dict[str, Track] = {}
        for lid in attached:
            for book in snapshots[lid].books.values():
                tracks[book.ref.key()] = book
                by_part.update((p.ref.key(), book) for p in books.parts_of(book))
        selected = books.merge_selection(selected, by_part)
    # every item says why: the rules that select it, the exclude that removes it
    rules, excludes, _ = resolve_rules(device.rule_set, templates or {})
    names = {r.id: r.name or r.id for r in [*rules, *excludes]}

    def by(rule_ids: Collection[str]) -> str:
        return ", ".join(f"“{names.get(r, r)}”" for r in rule_ids)

    entries = manifest.items if manifest else {}
    warnings = list(selection.warnings)

    candidates, unsupported = _candidates(selected, tracks, chain, caps, space_steps)
    limit: LimitReport | None = None
    # the device limit, at most the tracks the player handles (device profile `max_tracks`)
    limit_tracks = min(
        (n for n in (device.rule_set.limit_tracks, caps.max_tracks) if n is not None), default=None
    )
    if device.rule_set.limit_bytes or limit_tracks is not None:
        rs = device.rule_set.model_copy(update={"limit_tracks": limit_tracks})
        candidates, limit = apply_device_limit(
            candidates, rs, entries, seed=device.id, missing=missing
        )
        if limit.left_out:
            warnings.append(limit_warning(limit, caps.max_tracks))
    left_out = {x.key for x in limit.left_out} if limit else set()

    tag_device = not caps.needs_device_db and _writes_tags(chain)
    actions: list[PlanAction] = []
    reprocessed = 0  # replaced only because the processing changed
    reprocessed_parts: set[str] = set()  # which processing parts changed
    inputs: dict[str, dict[str, Any]] = {}  # the parts' readable inputs by hash
    stored = manifest.header.processing_inputs if manifest else {}
    what_changed: dict[str, None] = {}  # "part: old → new" texts, in order, once each
    for c in candidates:
        if any(h not in inputs for h in c.processing.values()):
            inputs.update(chain.processing_inputs(c.item, caps))
        entry = entries.get(c.key)
        action, changed = _selected_action(c, entry, missing, chain, caps, tag_device, by)
        actions.append(action)
        if changed is not None:
            reprocessed += 1
            reprocessed_parts.update(changed)
            if entry is not None:
                texts = _part_diffs(changed, entry.processing, c.processing, stored, inputs)
                what_changed.update(dict.fromkeys(texts))
                known = [
                    t for t in texts if not t.endswith((UNKNOWN_INPUTS, ": added", ": removed"))
                ]
                if known:  # the replace reason's tooltip says what changed
                    action.reason += " - " + "; ".join(known)
    if what_changed:
        log.info("%s: processing inputs changed: %s", device.id, " | ".join(what_changed))
    on_device = {c.key for c in candidates}  # keys that will be on the device after the plan

    if unsupported:
        shown = ", ".join(unsupported[:5]) + (" ..." if len(unsupported) > 5 else "")
        warnings.append(
            f"{len(unsupported)} selected items skipped: format not playable on the device: {shown}"
        )

    def gone(key: str, entry: ManifestEntry) -> bool:
        # gone from its library, or its library was not read (removed, disabled, unreadable)
        lid = entry.ref.library_id
        return key not in on_device and (
            lid not in snapshots or not snapshots[lid].has(entry.ref.item_id)
        )

    # deleted from an attached library: removed, unless the library looks broken
    per_lib = Counter(e.ref.library_id for e in entries.values())
    vanished = Counter(
        e.ref.library_id for k, e in entries.items() if k not in missing and gone(k, e)
    )
    lib_names = library_names or {}
    deletable = {
        lid
        for lid in snapshots
        if device.remove_deleted
        and snapshots[lid].tracks
        and not (
            vanished[lid] > MASS_VANISH_MIN and vanished[lid] * 100 > MASS_VANISH_PCT * per_lib[lid]
        )
    }
    # rule id -> the name of the template it belongs to, linked or not: a removal names the template that had put
    # the item there, not its rule id
    template_of = {
        r.id: tpl.name for tpl in (templates or {}).values() for r in [*tpl.rules, *tpl.excludes]
    }

    def unselected(key: str, entry: ManifestEntry) -> str:
        if key in left_out:
            return "over the device limit"
        return _unselected(key, entry.rules, selection.excluded, by, template_of)

    removals = 0
    orphaned_from: Counter[str] = Counter()  # library id -> orphans
    for key, entry in entries.items():
        if key in on_device:
            continue
        if is_external(
            entry.ref.library_id
        ):  # another instance's library: left alone, its space counted
            actions.append(
                PlanAction(
                    kind=ActionKind.KEEP,
                    ref=entry.ref,
                    title=entry.path,
                    entry=entry,
                    meta_hash=entry.meta_hash,
                    reason="kept: synced by another installation",
                )
            )
            continue
        action = _unselected_action(
            key,
            entry,
            source_gone=gone(key, entry),
            missing=key in missing,
            deletable=entry.ref.library_id in deletable,
            mirror=device.rule_set.mode == "mirror",
            known=tracks.get(key),
            lib_name=lib_names.get(entry.ref.library_id, entry.ref.library_id),
            unselected=unselected,
        )
        actions.append(action)
        if action.kind is ActionKind.REMOVE:
            removals += 1
        elif action.orphan:
            orphaned_from[entry.ref.library_id] += 1

    playlists: list[DevicePlaylist] = []
    if caps.supports_playlists:
        playlists = [
            p.model_copy(update={"refs": [r for r in p.refs if r.key() in on_device]})
            for p in selection.playlists  # keeps the library source + smart flag
        ]
    elif selection.playlists:
        warnings.append(
            f"{len(selection.playlists)} playlists skipped: the device has no playlists"
        )

    # what happens to each device playlist, compared with what the last sync wrote
    # device-made playlists already saved to a library and selected by no rule leave the device too
    written = set(manifest.playlists) | set(manifest.playlist_created) if manifest else set()
    hashes = manifest.playlist_hashes if manifest else {}
    changes = [
        PlaylistChange(
            name=p.name,
            change="create"
            if p.name not in written
            else "update"
            if hashes.get(p.name, playlist_hash(p.refs)) != playlist_hash(p.refs)
            else "same",
            songs=len(p.refs),
            rule=p.source is None,
            smart=p.smart,
        )
        for p in playlists
    ]
    planned = {p.name for p in playlists}
    if caps.supports_playlists:
        changes += [PlaylistChange(name=n, change="remove") for n in sorted(written - planned)]

    plan = SyncPlan(
        id=uuid.uuid4().hex,
        device_id=device.id,
        actions=actions,
        playlists=playlists,
        playlist_changes=changes,
        first_sync=manifest is None,
        warnings=warnings,
        limit=limit,
        processing_inputs=inputs,
    )
    if manifest is None:
        plan.needs_confirmation = True
        plan.confirmation_reasons.append(
            "first sync: choose to keep unknown files, adopt matched files or wipe the device"
        )
    _orphan_warnings(plan, device, snapshots, orphaned_from, per_lib, lib_names, auto)
    plan.removals_over_threshold = over_delete_threshold(
        removals, len(entries), device.delete_threshold_pct
    )
    if auto and plan.removals_over_threshold:
        plan.needs_confirmation = True
        plan.confirmation_reasons.append(
            f"removes {removals} of {len(entries)} items (more than {device.delete_threshold_pct} %)"
        )
    # not library changes: a sync stops and asks, a manual plan too
    if over_delete_threshold(reprocessed, len(entries), device.delete_threshold_pct):
        plan.needs_confirmation = True
        # neutral wording: a device settings edit and a SyncMusic update give the same hash change
        what = f" ({', '.join(sorted(reprocessed_parts))})" if reprocessed_parts else ""
        detail = f" - {' | '.join(what_changed)}" if what_changed else ""
        plan.confirmation_reasons.append(
            f"the processing changed for {reprocessed} of {len(entries)} items{what}: "
            f"they would be processed and copied again{detail}"
        )
    return plan


UNKNOWN_INPUTS = "previous values unknown (stored from now on)"


def _part_diffs(
    changed: Collection[str],
    old: Mapping[str, str],
    new: Mapping[str, str],
    stored: Mapping[str, dict[str, Any]],
    inputs: Mapping[str, dict[str, Any]],
) -> list[str]:
    """`device: max_sample_rate 96000 → 48000` per changed processing part, from the inputs the
    manifest stored for the old hash and the current ones."""
    out = []
    for n in sorted(changed):
        if n not in old:
            out.append(f"{n}: added")
        elif n not in new:
            out.append(f"{n}: removed")
        else:
            prev = stored.get(old[n])
            diff = inputs_diff(prev, inputs.get(new[n])) if prev is not None else UNKNOWN_INPUTS
            out.append(f"{n}: {diff or 'same values, new SyncMusic version'}")
    return out


def _candidates(
    selected: Mapping[str, Collection[str]],
    tracks: Mapping[str, Track],
    chain: Chain,
    caps: DeviceCapabilities,
    space_steps: bool,
) -> tuple[list[_Candidate], list[str]]:
    """The selected items run through the chain's planning; those the device cannot play go to the second list."""
    candidates: list[_Candidate] = []
    unsupported: list[str] = []
    for key, rule_ids in selected.items():
        track = tracks.get(key)
        if (
            track is None
        ):  # selected from a library that is not attached (cannot happen via `attached`)
            continue
        base = initial_plan_item(track, list(rule_ids), space_steps)
        item = chain.plan(base, caps)
        # videos: the `video` processor or the device decides the format, not the audio codec list
        if (
            caps.formats
            and track.kind is not MediaKind.VIDEO
            and item.file.format not in caps.formats
        ):
            unsupported.append(f"{track.title} ({item.file.format or 'unknown format'})")
            continue
        # a stock iPod plays only MP4 video: without Convert video in the chain an MKV/AVI fails at put
        if (
            caps.video_profile == "ipod"
            and track.kind is MediaKind.VIDEO
            and item.file.format not in IPOD_VIDEO_FORMATS
        ):
            unsupported.append(f"{track.title} ({item.file.format or 'unknown format'})")
            continue
        chash, processing = chain.hashes(base, caps)
        candidates.append(_Candidate(key, track, list(rule_ids), item, chash, processing))
    return candidates, unsupported


def _source_parts(state: str) -> tuple[str, str, str] | None:
    """`size:stamp|rest` → (size, stamp, rest) when the stamp is a timestamp (digits: mtime in ns/ms, or an ISO
    time); None for every other format (e.g. size|etag, size:duration:bitrate)."""
    head, _, rest = state.partition("|")
    size, sep, stamp = head.partition(":")
    if not sep or not size.isdigit():
        return None
    if not (stamp.replace(".", "", 1).isdigit() or ("T" in stamp and stamp[:4].isdigit())):
        return None
    return size, stamp, rest


def touched_only(old: str, new: str) -> bool:
    """Two source states that differ only in their timestamp: the size and the rest (cover image hash, ...) are
    the same - a tagger or a rescan touched the file without changing it."""
    a, b = _source_parts(old), _source_parts(new)
    return a is not None and b is not None and a[0] == b[0] and a[2] == b[2] and a[1] != b[1]


def _selected_action(
    c: _Candidate,
    entry: ManifestEntry | None,
    missing: Collection[str],
    chain: Chain,
    caps: DeviceCapabilities,
    tag_device: bool,
    by: Callable[[Collection[str]], str],
) -> tuple[PlanAction, list[str] | None]:
    """Add, replace, update or keep one selected item. The second value is set when it is replaced only because
    the processing changed: the processing parts that changed."""
    key, track, rule_ids, item, chash = c.key, c.track, c.rules, c.item, c.chain_hash
    mhash = meta_hash(track)
    if entry is None or key in missing:
        add = PlanAction(
            kind=ActionKind.ADD,
            ref=track.ref,
            title=track.title,
            item=item,
            size_delta=c.size,
            rules=list(rule_ids),
            reason=("missing on the device · " if entry is not None else "")
            + f"selected by {by(rule_ids)}"
            + _book_note(track),
            chain_hash=chash,
            processing=c.processing,
            meta_hash=mhash,
        )
        return add, None
    tags_only = False
    reprocessed: list[str] | None = None
    # written with an older caps hash scheme and only the "device" part differs: the file is what it was
    rehash = (
        entry.chain_hash != chash
        and entry.caps_v < CAPS_HASH_VERSION
        and bool(entry.processing)
        and entry.processing.keys() == c.processing.keys()
        and all(entry.processing[n] == v for n, v in c.processing.items() if n != "device")
    )
    restamp = (
        entry.source_state != track.source_state
        and entry.meta_hash == mhash
        and touched_only(entry.source_state, track.source_state)
    )
    if entry.source_state != track.source_state and not restamp:
        kind, reason = ActionKind.REPLACE, "source changed"
    elif entry.chain_hash != chash and not rehash:
        kind, reason = ActionKind.REPLACE, "processing changed"
        reprocessed = []
        if entry.processing:  # which part changed, and does it touch the audio
            reprocessed = sorted(
                n
                for n in entry.processing.keys() | c.processing.keys()
                if entry.processing.get(n) != c.processing.get(n)
            )
            tags_only = bool(reprocessed) and not any(chain.audio_stage(n) for n in reprocessed)
            reason += f": {', '.join(reprocessed)}" + (" (tags only)" if tags_only else "")
    elif entry.layout and entry.layout != (
        caps.layout_for(track.kind, track.disc_total) or entry.layout
    ):
        # the device's path layout changed: re-written at the new path, the old file removed
        kind, reason = ActionKind.REPLACE, "path layout changed"
    elif entry.meta_hash != mhash and tag_device:
        kind, reason = ActionKind.REPLACE, "metadata changed (tags in the file)"
    elif entry.meta_hash != mhash and caps.needs_device_db:
        kind, reason = ActionKind.UPDATE_META, "metadata changed"
    else:
        # the plan review says why it stays: the rules that still select it
        kind, reason = ActionKind.KEEP, f"still selected by {by(rule_ids)}"
    tags = kind is ActionKind.UPDATE_META or reason.startswith("metadata changed")
    action = PlanAction(
        kind=kind,
        ref=track.ref,
        title=track.title,
        item=item,
        entry=entry,
        size_delta=c.size - entry.size if kind is ActionKind.REPLACE else 0,
        rules=list(rule_ids),
        reason=reason,
        chain_hash=chash,
        processing=c.processing,
        tags_only=tags_only,
        rehash=rehash and kind in (ActionKind.KEEP, ActionKind.UPDATE_META),
        restamp=restamp and kind in (ActionKind.KEEP, ActionKind.UPDATE_META),
        meta_hash=mhash,
        changes=meta_changes(entry, track, caps, tags)
        if kind is not ActionKind.REPLACE or tags
        else [],
    )
    return action, reprocessed


def _unselected_action(
    key: str,
    entry: ManifestEntry,
    *,
    source_gone: bool,
    missing: bool,
    deletable: bool,
    mirror: bool,
    known: Track | None,
    lib_name: str,
    unselected: Callable[[str, ManifestEntry], str],
) -> PlanAction:
    """What happens to a managed item that is not selected (any more): removed when deleted from its library or
    in mirror mode, kept as an orphan when its library looks broken, otherwise kept (additive mode)."""
    if source_gone and not missing and deletable:
        return PlanAction(
            kind=ActionKind.REMOVE,
            ref=entry.ref,
            title=entry.path,
            entry=entry,
            size_delta=-entry.size,
            rules=list(entry.rules),
            reason=f"deleted from {lib_name}",
            deleted=True,
        )
    if source_gone and not missing:
        return PlanAction(
            kind=ActionKind.KEEP,
            ref=entry.ref,
            title=entry.path,
            entry=entry,
            rules=list(entry.rules),
            meta_hash=entry.meta_hash,
            reason="orphan: gone from its library",
            orphan=True,
        )
    if not (mirror or source_gone):  # additive: never removes; the entry stays managed
        return PlanAction(kind=ActionKind.KEEP, ref=entry.ref, title=entry.path, entry=entry)
    # source_gone here: the file is gone too
    title, sub = entry.path, None
    if known is not None and not source_gone:  # named like an add, not by its file
        title = known.title or entry.path
        sub = " - ".join(x for x in (known.artist or known.author, known.album) if x)
    return PlanAction(
        kind=ActionKind.REMOVE,
        ref=entry.ref,
        title=title,
        subtitle=sub,
        entry=entry,
        size_delta=0 if missing else -entry.size,
        rules=list(entry.rules),
        reason="gone from the device and its library" if source_gone else unselected(key, entry),
    )


def _orphan_warnings(
    plan: SyncPlan,
    device: DeviceConfig,
    snapshots: Mapping[str, LibrarySnapshot],
    orphaned_from: Counter[str],
    per_lib: Counter[str],
    lib_names: Mapping[str, str],
    auto: bool,
) -> None:
    """Warn about orphans (items kept although gone from their library); an automatic sync asks first after a
    mass vanish."""
    orphans = sum(orphaned_from.values())
    if orphans:
        plan.warnings.append(
            "1 item on the device is gone from its library (orphan): it stays on the device; "
            "remove it in Device settings"
            if orphans == 1
            else f"{orphans} items on the device are gone from their library (orphans): they stay on the "
            "device; remove them in Device settings"
        )
    for lid in snapshots:
        if orphaned_from[lid] and (notice := snapshots[lid].notice):
            plan.warnings.append(notice)
        # an empty library (unmounted share, rescan glitch) removes nothing: its items are orphans
        if not snapshots[lid].tracks and orphaned_from[lid]:
            plan.warnings.append(f"library {lid!r} returned no items (is it mounted / reachable?)")
        elif device.remove_deleted and orphaned_from[lid]:  # a mass vanish
            plan.warnings.append(
                f"{orphaned_from[lid]} of {per_lib[lid]} items from {lib_names.get(lid, lid)} vanished at once "
                f"(more than {MASS_VANISH_PCT} %): kept on the device in case it is a library glitch"
            )
            if auto:
                plan.needs_confirmation = True
                plan.confirmation_reasons.append(
                    f"{orphaned_from[lid]} items vanished from {lib_names.get(lid, lid)} at once: "
                    "they stay on the device; remove them in Device settings if they were deleted"
                )


def _book_note(track: Track) -> str:
    n = len(track.extra.get(books.BOOK_PARTS) or [])
    if not n:
        return ""
    part = track.extra.get(books.BOOK_PART)
    into = f"part {part[0]} of {part[1]}" if part else "1 book"
    files, chapters = ("1 file", "1 chapter") if n == 1 else (f"{n} files", f"{n} chapters")
    return f" · {files} → {into} ({chapters})"


def over_delete_threshold(removals: int, on_device: int, pct: int) -> bool:
    """An automatic sync would remove more than `pct` % of the device's managed items."""
    return on_device > 0 and removals * 100 > pct * on_device


def _unselected(
    key: str,
    was: Collection[str],
    excluded: Mapping[str, str],
    by: Callable[[Collection[str]], str],
    template_of: Mapping[str, str],
) -> str:
    """Why an item on the device is no longer selected: an exclude rule, or no rule matches it now -
    named by the templates its rules came from when they all did."""
    if key in excluded:
        return f"excluded by {by([excluded[key]])}"
    if was and all(r in template_of for r in was):
        shown = ", ".join(f"“{n}”" for n in dict.fromkeys(template_of[r] for r in was))
        return f"no template selects it any more (was in {shown})"
    return f"no rule selects it any more (was {by(was)})" if was else "no rule selects it any more"
