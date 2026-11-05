# SPDX-License-Identifier: GPL-3.0-or-later
"""Validation of `DeviceConfig.mount_path`: a directory used as a device.

Syncing into a directory deletes files there (mirror removes, first sync "wipe"), so a mount_path must never be
`/`, the home directory or one of its ancestors, a system tree, the app's own data/cache/mount/source dirs, a
library root (or anything containing one) or another device's directory. On top of that it must live on a
removable disk the block-device guard accepts (a USB drive mounted outside SyncMusic) unless plain
directories are explicitly allowed (`BootstrapConfig.allow_directory_devices`, tests only).

A mount_path may also never lie on an iPod or Rockbox file system  (a wipe or mirror would delete iPod_Control / .rockbox, a whole device library) nor contain another file system's mount point.
Those devices are registered by disk as iPod / Rockbox devices - or by directory with a device type that owns
such a tree (`Device.owns_device_tree`): then the directory must be the tree's root (the marker folder
directly in it, not in a parent) and may be on an Apple disk.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.guard import APPLE_VENDOR_ID, Guard, GuardError
from syncmusic.infra.blockdev.identify import find_disk_of, scan_block_devices

# A mount_path may not be inside these trees.
SYSTEM_TREES = tuple(
    Path(p)
    for p in (
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/lib",
        "/lib64",
        "/opt",
        "/proc",
        "/root",
        "/sbin",
        "/snap",
        "/srv",
        "/sys",
        "/usr",
        "/var",
    )
)


# Top-level folders that mark an iPod / Rockbox file system: never deleted by a directory device.
DEVICE_MARKERS = ("iPod_Control", ".rockbox")


def overlaps(a: Path, b: Path) -> bool:
    """Same directory, or one contains the other (both resolved)."""
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def protected_reason(path: Path, protected: Iterable[tuple[str, Path]]) -> str | None:
    """Why `path` (resolved) must not be a device directory, or None.

    `protected` are (description, resolved dir) pairs that may neither contain nor lie inside the path."""
    home = Path.home().resolve()
    if path == home or home.is_relative_to(path):
        return f"{path} is the root, the home directory or one of its parents"
    for tree in SYSTEM_TREES:
        if path.is_relative_to(tree):
            return f"{path} is a system directory ({tree})"
    if path.parent == Path("/") and not os.path.ismount(path):
        return f"{path} is a top-level directory"
    for what, p in protected:
        if overlaps(path, p):
            return f"{path} overlaps {what} ({p})"
    return None


def _mount_entry(path: Path) -> mnt.MountEntry | None:
    """Mount table entry of the file system holding `path` (resolved)."""
    best: mnt.MountEntry | None = None
    # in mount order: a later entry for the same path hides earlier ones
    for e in mnt.mount_table():
        mp = Path(e.path)
        if path.is_relative_to(mp) and (
            best is None or len(mp.parts) >= len(Path(best.path).parts)
        ):
            best = e
    return best


def mount_source(path: Path) -> str | None:
    """Block device the filesystem holding `path` is mounted from (None: not a /dev source)."""
    best = _mount_entry(path)
    if best is None or not best.source.startswith("/dev/"):
        return None
    return best.source


def device_tree_reason(path: Path, owns_tree: bool = False) -> str | None:
    """Why `path` (resolved) must not be a device directory because of what is on its disk, or None:
    it lies on an iPod / Rockbox file system (a marker folder in it or in a parent up to the file system
    root, or an Apple disk) or contains another file system's mount point. With `owns_tree` (an iPod / Rockbox device type) a marker directly in `path` and an Apple disk are fine; a marker in a
    parent is not (the device would build a second tree inside the first). Blocking: reads the mount
    table, scans block devices."""
    entry = _mount_entry(path)
    root = Path(entry.path) if entry else Path("/")
    hint = "register it as an iPod / Rockbox device instead (by disk, or this directory with that type)"
    for d in (path, *path.parents):
        for marker in DEVICE_MARKERS:
            if not (d / marker).exists() or (owns_tree and d == path):
                continue
            if owns_tree:
                return f"{path} is inside the iPod/Rockbox tree at {d}: use {d} itself"
            return f"{path} is on an iPod/Rockbox file system ({d / marker} exists): {hint}"
        if d == root:
            break
    for e in mnt.mount_table():
        mp = Path(e.path)
        if mp != path and mp.is_relative_to(path):
            return f"{path} contains the mount point {mp}"
    source = mount_source(path)
    if source is not None and not owns_tree:
        try:
            disk, _ = find_disk_of(source, scan_block_devices())
        except KeyError:
            return None
        if disk.usb_vendor_id == APPLE_VENDOR_ID:
            return f"{path} is on an iPod ({source}): {hint}"
    return None


def on_removable_disk(path: Path, guard: Guard | None) -> bool:
    """`path` lies on a disk the guard would let us mount (USB, removable or iPod, allowlisted under test).
    Blocking: scans block devices."""
    source = mount_source(path)
    if source is None or guard is None:
        return False
    try:
        guard.check(source, "mount")
    except GuardError:
        return False
    return True
