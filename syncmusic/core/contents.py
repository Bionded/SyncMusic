# SPDX-License-Identifier: GPL-3.0-or-later
"""Player contents: what is on a device (manifest joined with library metadata) and removing selected
items from it.

Removing an item changes the device's rules so the next sync does not bring it back - a manually added item leaves
the Manual selection (also its album / artist when every one of their tracks goes), an item a rule or template
still selects gets a device exclusion - and then runs a plan that only deletes those items.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from syncmusic.core.manifest import is_external
from syncmusic.core.models import (
    ActionKind,
    DeviceContents,
    DeviceItem,
    ItemRef,
    LibrarySnapshot,
    Manifest,
    ManifestEntry,
    MediaKind,
    PlanAction,
    RuleSet,
    SyncPlan,
    Track,
)
from syncmusic.core.rules.templates import (
    MANUAL_RULE_ID,
    IdField,
    manual_ids,
    remove_from_manual_selection,
)

FOREIGN_PREFIX = (
    "device:"  # row key of an item SyncMusic did not put on the device: "device:<device_ref>"
)


def is_orphan(
    entry: ManifestEntry, snapshots: Mapping[str, LibrarySnapshot], attached: Collection[str]
) -> bool:
    """Gone from its library (read, and no longer has it) or its library was removed or disabled
    (`attached` = the enabled libraries). An item of an enabled library no rule selects is not an orphan, nor is
    an item of another installation's library (it manages those)."""
    if is_external(entry.ref.library_id):
        return False
    if entry.ref.library_id not in attached:
        return True
    snap = snapshots.get(entry.ref.library_id)
    return snap is not None and snap.track(entry.ref.item_id) is None


def orphans(
    manifest: Manifest, snapshots: Mapping[str, LibrarySnapshot], attached: Collection[str]
) -> list[dict[str, Any]]:
    """The orphan rows (key, title, path, size) from the manifest alone, sorted by path."""
    rows = [
        {"key": key, "title": e.path.rsplit("/", 1)[-1], "path": e.path, "size": e.size}
        for key, e in manifest.items.items()
        if is_orphan(e, snapshots, attached)
    ]
    rows.sort(key=lambda r: str(r["path"]).casefold())
    return rows


def contents(
    found: DeviceContents,
    manifest: Manifest | None,
    snapshots: Mapping[str, LibrarySnapshot],
    attached: Collection[str],
) -> list[dict[str, Any]]:
    """Everything on the device: one row per item the device reports, joined with the manifest.

    Names and tags come from the device itself (iPod: iTunesDB, files: their tags), the library's values
    only fill gaps. `foreign` = not put there by SyncMusic (key `device:<device_ref>`); `orphan` = a managed item
    whose library was read and no longer has it, or whose library is not attached (any more) - an
    attached library that could not be read leaves it unknown. A managed item whose file is gone from the device is
    still listed (`missing`). `playlists` = {name: position} of the device playlists the item is in. `position_s` /
    `library_position_s`: where a podcast/audiobook stands on the device (as of the last sync) and in the library."""
    by_ref = manifest.by_device_ref() if manifest else {}
    positions: dict[str, dict[str, int]] = {}
    for name, refs in found.playlist_items.items():
        for i, ref in enumerate(refs):
            positions.setdefault(ref, {}).setdefault(name, i)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def row(item: DeviceItem | None, entry: ManifestEntry | None) -> dict[str, Any]:
        t = None
        if entry is not None:
            snap = snapshots.get(entry.ref.library_id)
            t = snap.track(entry.ref.item_id) if snap else None
        path = item.path if item else entry.path if entry else ""

        def pick(device_value: Any, attr: str) -> Any:
            if device_value not in (None, ""):
                return device_value
            return getattr(t, attr, None) if t is not None else None

        return {
            "key": entry.ref.key() if entry else FOREIGN_PREFIX + (item.device_ref if item else ""),
            "library_id": entry.ref.library_id if entry else None,
            "title": pick(item.title if item else None, "title") or path.rsplit("/", 1)[-1],
            "artist": pick(item.artist if item else None, "artist"),
            "album_artist": pick(item.album_artist if item else None, "album_artist"),
            "album": pick(item.album if item else None, "album"),
            "genre": (item.genre if item else None) or (t.genres[0] if t and t.genres else None),
            "year": pick(item.year if item else None, "year"),
            "track_number": pick(item.track_number if item else None, "track_no"),
            "disc_number": pick(item.disc_number if item else None, "disc_no"),
            "kind": (entry.kind if entry else item.kind if item else MediaKind.MUSIC).value,
            "size": item.size if item else entry.size if entry else 0,
            "duration_s": (item.duration_s if item else None)
            or (entry.duration_s if entry else None),
            "path": path,
            # when SyncMusic wrote it (the grid's "recently added" sort); None: not from SyncMusic
            "synced_at": entry.synced_at.isoformat() if entry is not None else None,
            "manual": entry is not None and MANUAL_RULE_ID in entry.rules,
            "orphan": entry is not None and is_orphan(entry, snapshots, attached),
            # put there by another installation from a library this one does not have: read-only here
            "other_instance": entry is not None and is_external(entry.ref.library_id),
            "foreign": entry is None,
            "missing": item is None,
            # a video: its show / season / episode from the library, for grouping by show
            "show": t.series if t is not None and t.kind is MediaKind.VIDEO else None,
            "season": t.extra.get("season")
            if t is not None and t.kind is MediaKind.VIDEO
            else None,
            "episode": t.extra.get("episode")
            if t is not None and t.kind is MediaKind.VIDEO
            else None,
            "playlists": positions.get(item.device_ref, {}) if item else {},
            # podcasts/audiobooks: the bookmark as of the last sync and the library's position now
            "position_s": entry.last_position_s
            if entry is not None and entry.kind is not MediaKind.MUSIC
            else None,
            "library_position_s": t.position_s
            if t is not None and entry is not None and entry.kind is not MediaKind.MUSIC
            else None,
        }

    for item in found.items:
        entry = by_ref.get(item.device_ref)
        if entry is not None:
            seen.add(entry.ref.key())
        rows.append(row(item, entry))
    for key, entry in manifest.items.items() if manifest else []:
        if key not in seen:
            rows.append(row(None, entry))
    rows.sort(
        key=lambda r: (
            *(str(r[k] or "").casefold() for k in ("kind", "album_artist", "artist", "album")),
            r["disc_number"] or 0,
            r["track_number"] or 0,
            str(r["title"]).casefold(),
        )
    )
    return rows


FOREIGN_LIBRARY = (
    "device:"  # ItemRef.library_id of a foreign item in a removal plan (no library has a colon)
)


def foreign_removals(
    found: DeviceContents, keys: Collection[str]
) -> tuple[list[PlanAction], list[str]]:
    """REMOVE actions for the foreign rows `keys` (`device:<device_ref>`) and the keys not on the device.
    The applier deletes the file / database entry of such an action like any other; the manifest never had it."""
    by_ref = {i.device_ref: i for i in found.items}
    actions: list[PlanAction] = []
    unknown: list[str] = []
    for key in keys:
        item = by_ref.get(key.removeprefix(FOREIGN_PREFIX))
        if item is None:
            unknown.append(key)
            continue
        ref = ItemRef(library_id=FOREIGN_LIBRARY, item_id=item.device_ref)
        entry = ManifestEntry(
            ref=ref, device_ref=item.device_ref, path=item.path, size=item.size, kind=item.kind
        )
        actions.append(
            PlanAction(
                kind=ActionKind.REMOVE,
                ref=ref,
                title=item.title or item.path,
                entry=entry,
                size_delta=-item.size,
                reason="removed in Player contents",
            )
        )
    return actions, unknown


def without_manual(
    ruleset: RuleSet,
    removed: Collection[str],
    tracks: Mapping[str, Track],
    on_device: Collection[str],
) -> RuleSet:
    """Drop the removed tracks from the Manual selection; an album / artist of the selection goes too when none of
    its tracks on the device stays (`tracks`: key → library track, for the items on the device)."""
    gone = set(removed)
    rs = remove_from_manual_selection(ruleset, "track_id", [k.split("/", 1)[1] for k in gone])
    fields: tuple[tuple[IdField, str], ...] = (("album_id", "album_id"), ("artist_id", "artist_id"))
    for field, attr in fields:
        drop = []
        for value in manual_ids(rs, field):
            mine = [k for k in on_device if k in tracks and getattr(tracks[k], attr) == value]
            if mine and all(k in gone for k in mine):
                drop.append(value)
        if drop:
            rs = remove_from_manual_selection(rs, field, drop)
    return rs


def only_removals(plan: SyncPlan, keys: Collection[str]) -> SyncPlan:
    """The plan reduced to deleting `keys`: every other item on the device is kept as it is, nothing is added or
    replaced (a removal never waits for unrelated changes)."""
    actions = []
    for a in plan.actions:
        if a.entry is None:
            continue  # an add
        if a.ref.key() in keys:
            actions.append(
                PlanAction(
                    kind=ActionKind.REMOVE,
                    ref=a.ref,
                    title=a.title,
                    subtitle=a.subtitle,
                    entry=a.entry,
                    size_delta=-a.entry.size,
                    reason="removed in Player contents",
                )
            )
        else:
            actions.append(
                PlanAction(
                    kind=ActionKind.KEEP,
                    ref=a.ref,
                    title=a.title,
                    entry=a.entry,
                    rules=a.entry.rules,
                    meta_hash=a.entry.meta_hash,
                    orphan=a.orphan,
                )
            )
    return plan.model_copy(
        update={
            "actions": actions,
            "needs_confirmation": False,
            "confirmation_reasons": [],
            "does_not_fit": False,
            "db": None,
            "warnings": [],
        },
        deep=True,
    )
