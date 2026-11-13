# SPDX-License-Identifier: GPL-3.0-or-later
"""Reverse playlist sync: edits of library playlists made on the device go back.

For every playlist the last sync wrote (`Manifest.playlists`) whose library playlist is known
(`Manifest.playlist_sources`) and not smart: the device's copy (read by `Device.read_playlists`, entries mapped to
items through the manifest) is compared with what was written (`Manifest.playlist_refs`); if it changed, it is merged
with the library's current list (`core/playlist_merge`) and saved there (`FeedbackSink.save_playlist`). The merged list
becomes the new base and replaces the plan's list of the forward sync that follows (`playlists_after_reverse`).

Playlists created on the device are created in the library (566.4); a conflict (both sides reordered, or a device
playlist named like a library one) follows `SyncSettings.playlist_conflicts` (566.3) or the plan review's choice per
playlist (566.5); "ask" without a choice leaves both sides as they are this run (a warning says so).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from typing import Literal

from syncmusic.base.device import Device
from syncmusic.base.feedback import FeedbackRejected, FeedbackSink
from syncmusic.base.library import Library
from syncmusic.core.models import ItemRef, Manifest, ReverseReport, SyncPlan
from syncmusic.core.playlist_merge import merge

Policy = Literal["ask", "merge", "replace", "ignore", "rename"]

log = logging.getLogger(__name__)


def device_lists(
    manifest: Manifest, raw: Mapping[str, list[str]], case_sensitive: bool
) -> tuple[dict[str, list[str]], int]:
    """Device playlists (name -> device-relative paths) as item keys via the manifest; plus the number of entries
    that are no managed item (skipped)."""
    by_path = {
        (e.path if case_sensitive else e.path.casefold()): k for k, e in manifest.items.items()
    }
    out: dict[str, list[str]] = {}
    unknown = 0
    for name, paths in raw.items():
        keys = []
        for p in paths:
            key = by_path.get(p if case_sensitive else p.casefold())
            if key is None:
                unknown += 1
            else:
                keys.append(key)
        # entries that all match nothing: unreadable, never an emptied playlist
        if keys or not paths:
            out[name] = keys
    return out, unknown


async def push_playlists(
    device: Device,
    manifest: Manifest,
    libraries: Mapping[str, Library | None],
    report: ReverseReport,
    policy: Policy = "ask",
    device_name: str = "",
    choices: Mapping[str, Policy] | None = None,
    dry: bool = False,
) -> bool:
    """Push the device's edits of managed playlists and create the ones made on it; `policy` decides conflicts
    (`SyncSettings.playlist_conflicts`), `choices` overrides it per playlist name (the plan review's choice). Updates `manifest.playlist_refs` and the report. Returns whether the manifest changed.
    `dry`: nothing is saved - only `report.playlist_conflicts` is filled (the plan lists them, pass a manifest copy)."""
    raw = await device.read_playlists(manifest)
    if raw is None:
        return False
    lists, unknown = device_lists(
        manifest, raw, device.effective_capabilities().path_rules.case_sensitive
    )
    suffix = f" ({device_name or device.id})"
    choices = choices or {}
    changed = await _create(
        device, manifest, lists, libraries, report, policy, suffix, choices, dry
    )
    for name in dict.fromkeys([*manifest.playlists, *manifest.playlist_created]):
        policy_of = choices.get(name, policy)
        if await _push_edit(
            device, manifest, name, lists, libraries, report, policy_of, suffix, dry
        ):
            changed = True
    if unknown and report.playlists_pushed:
        report.warnings.append(
            f"{unknown} playlist entries on the device are no synced song: left out"
        )
    return changed


async def _push_edit(
    device: Device,
    manifest: Manifest,
    name: str,
    lists: Mapping[str, list[str]],
    libraries: Mapping[str, Library | None],
    report: ReverseReport,
    policy_of: Policy,
    suffix: str,
    dry: bool,
) -> bool:
    """Push the device's edit of one managed or created playlist to its library playlist (a three-way merge with
    the last synced songs; `policy_of` decides when both sides reordered it). Returns whether the manifest
    changed."""
    base = manifest.playlist_refs.get(name)
    on_device = lists.get(name)
    if base is None or on_device is None or on_device == base:
        return False  # written (no base), deleted on the device, or unchanged
    source = manifest.playlist_sources.get(name) or manifest.playlist_created.get(name)
    if name in manifest.playlist_smart:
        report.warnings.append(
            f"playlist {name!r} was edited on the device, but it is a smart playlist: the sync overwrites it"
        )
        return False
    if source is None:
        report.warnings.append(
            f"playlist {name!r} was edited on the device, but it comes from a rule, not a library playlist: "
            "the sync overwrites it"
        )
        return False
    ref = ItemRef.parse(source)
    lib = libraries.get(ref.library_id)
    if not isinstance(lib, FeedbackSink):
        report.warnings.append(
            f"playlist {name!r}: its library is not available, kept for the next sync"
        )
        return False
    current = await lib.get_playlist(ref.item_id)
    if current is None:
        report.warnings.append(f"playlist {name!r}: the library cannot tell its current songs")
        return False
    merged = merge(base, on_device, [r.key() for r in current])
    target_name, target_id = name, ref.item_id
    if merged is None:  # both sides reordered: the policy decides
        why = f"playlist {name!r} was reordered on the device and in the library"
        if dry or policy_of in ("ask", "ignore"):
            report.warnings.append(f"{why}: the library's order is kept")
            if policy_of == "ask":
                report.playlist_conflicts.append(name)
            return False
        if policy_of == "rename":
            target_name, target_id = name + suffix, None
            if target_name in manifest.playlist_created:
                return False  # saved under that name before: its own entry carries the device's later edits
            merged = on_device
        else:
            merged = merge(base, on_device, [r.key() for r in current], prefer="device")
            if policy_of == "replace" or merged is None:
                merged = on_device
    if dry:
        return False
    refs = [ItemRef.parse(k) for k in merged]
    foreign = [r for r in refs if r.library_id != ref.library_id]
    if foreign:
        report.warnings.append(
            f"playlist {name!r}: {len(foreign)} songs of other libraries left out of the library's copy"
        )
        refs = [r for r in refs if r.library_id == ref.library_id]
    try:
        saved = await lib.save_playlist(target_name, refs, target_id)
    except FeedbackRejected as e:
        report.warnings.append(f"playlist {name!r} not saved: {e}")
        return False
    except Exception as e:  # noqa: BLE001 - a library down keeps the device's edit for the next sync
        report.errors.append(f"playlist {name!r} not saved: {e}")
        return False
    if target_id is None:  # renamed copy (rename policy): the managed one stays the library's
        log.info("%s: playlist %r saved as %r", device.id, name, target_name)
        report.playlists_created.append(target_name)
        manifest.playlist_created[target_name] = ItemRef(
            library_id=ref.library_id, item_id=saved
        ).key()
        manifest.playlist_refs[target_name] = [r.key() for r in refs]
        return True
    log.info("%s: playlist %r saved to %s (%d songs)", device.id, name, saved, len(refs))
    merged_keys = [r.key() for r in refs]
    report.playlists_pushed[name] = merged_keys
    # the new base is what the device will hold: library-only songs not on it are no device removal next time
    manifest.playlist_refs[name] = [k for k in merged_keys if k in manifest.items]
    return True


async def _create(
    device: Device,
    manifest: Manifest,
    lists: Mapping[str, list[str]],
    libraries: Mapping[str, Library | None],
    report: ReverseReport,
    policy: Policy,
    suffix: str,
    choices: Mapping[str, Policy],
    dry: bool,
) -> bool:
    """Playlists made on the device (no managed or created one of that name) go to the library most of their songs
    come from. A library playlist of the same name: the policy decides (566.3) - merge (union) or
    replace its songs, rename (saved as "<name> (<device>)"), ask / ignore (left alone)."""
    changed = False
    for name, keys in lists.items():
        if name in manifest.playlists or name in manifest.playlist_created:
            continue
        if not keys:
            report.warnings.append(f"device playlist {name!r} has no synced songs: not saved")
            continue
        refs = [ItemRef.parse(k) for k in keys]
        lib_id = Counter(r.library_id for r in refs).most_common(1)[0][0]
        lib = libraries.get(lib_id)
        if not isinstance(lib, FeedbackSink):
            continue  # a folder library (no reverse sync) or not reachable now: tried again next time
        try:
            existing = await lib.playlists_by_name()
            if existing is None:
                continue
            own = [r for r in refs if r.library_id == lib_id]
            saved_as = name
            policy_of = choices.get(name, policy)
            if name in existing:
                why = f"device playlist {name!r}: the library has a playlist of that name"
                if policy_of in ("ask", "ignore"):
                    report.warnings.append(f"{why}, not saved")
                    if policy_of == "ask":
                        report.playlist_conflicts.append(name)
                    continue
                if dry:
                    continue
                if policy_of == "rename":
                    if name + suffix in existing:
                        report.warnings.append(f"{why} and one named {name + suffix!r}, not saved")
                        continue
                    pid = await lib.save_playlist(name + suffix, own)
                    saved_as = name + suffix
                else:
                    pid = existing[name]
                    if policy_of == "merge":
                        current = await lib.get_playlist(pid) or []
                        union = merge([], [r.key() for r in own], [r.key() for r in current])
                        own = [ItemRef.parse(k) for k in union or []]
                    await lib.save_playlist(name, own, pid)
            elif dry:
                continue
            else:
                pid = await lib.save_playlist(name, own)
        except FeedbackRejected as e:
            report.warnings.append(f"device playlist {name!r} not saved: {e}")
            continue
        except Exception as e:  # noqa: BLE001 - tried again next time
            report.errors.append(f"device playlist {name!r} not saved: {e}")
            continue
        log.info(
            "%s: device playlist %r created in %s (%d songs)", device.id, name, lib_id, len(own)
        )
        manifest.playlist_created[name] = ItemRef(library_id=lib_id, item_id=pid).key()
        manifest.playlist_refs[name] = list(keys)
        report.playlists_created.append(saved_as)  # the library's name
        changed = True
    return changed


def playlists_after_reverse(plan: SyncPlan, reverse: ReverseReport) -> SyncPlan:
    """The forward sync writes the lists step 0 saved to the library, not the plan's older copies."""
    if not reverse.playlists_pushed:
        return plan
    pls = [
        p.model_copy(update={"refs": [ItemRef.parse(k) for k in reverse.playlists_pushed[p.name]]})
        if p.name in reverse.playlists_pushed
        else p
        for p in plan.playlists
    ]
    return plan.model_copy(update={"playlists": pls})
