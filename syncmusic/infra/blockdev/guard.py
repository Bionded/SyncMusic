# SPDX-License-Identifier: GPL-3.0-or-later
"""Mandatory safety check before any mount / write / format / partition operation.

A target block device (disk or partition) is accepted only if:
- its disk is attached over USB (`TRAN=usb`) - or is a loop device when loop devices are explicitly
  allowed and the allowlist names it (emulated test devices, tests/emulated.toml);
- the disk is not NVMe / mmcblk, carries no system mountpoint (`/`, `/boot`, `/boot/firmware`, ...) and
  no swap area;
- the disk is removable (`RM=1`) or a known iPod (USB vendor 05ac) - or a fixed USB disk (`RM=0`: USB SSDs, most
  USB hard-disk enclosures) that the caller vouches for: either `registered` identities are given (and the disk must
  match them, below), or `fixed_usb` is set for a non-destructive operation (the privileged helper, which never
  sees the registrations: it may mount/read/eject such a disk but never format, partition or relabel it);
- if an allowlist (tests/hardware.toml) is configured: the disk's USB serial or the partition's
  filesystem UUID is listed in it;
- if `registered` identities are given (production): the disk serial / fs UUID is one of them;
- on an iPod (Apple vendor 05ac) destructive operations (raw write/format/mkfs/partition, and `label`: writing a file system's label) are allowed only on the data partition: never on the whole disk (partition table) or the firmware partition;
- `time_sync` (a vendor SCSI command setting an iPod's clock) only on an iPod's whole disk.

`operation` is a closed set (`OPERATIONS`); unknown names are refused (fail closed). `write` means a raw write
to the block device node; file writes go through a mounted file system and are covered by `mount`.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from syncmusic.infra.blockdev.identify import BlockDeviceInfo, find_disk_of, scan_block_devices

log = logging.getLogger(__name__)

APPLE_VENDOR_ID = "05ac"
SYSTEM_MOUNTPOINTS = (
    "/",
    "/boot",
    "/boot/firmware",
    "/boot/efi",
    "/usr",
    "/var",
    "/home",
    "[SWAP]",
)
# Operations that destroy or rewrite a partition table or file system metadata (`write` = raw write to the device
# node; `label` = a file system label tool; `repair` = fsck rewriting a damaged file system).
DESTRUCTIVE_OPERATIONS = frozenset({"write", "format", "mkfs", "partition", "label", "repair"})
# Every operation the guard knows; anything else is refused.
OPERATIONS = DESTRUCTIVE_OPERATIONS | {"read", "mount", "eject", "time_sync"}
# MBR partition types of an iPod data partition (FAT16/FAT32, HFS+). The firmware partition is type 0x0.
IPOD_DATA_PARTTYPES = frozenset({"0x6", "0xb", "0xc", "0xe", "0xaf"})
IPOD_DATA_FSTYPES = frozenset({"vfat", "hfsplus"})


class GuardError(PermissionError):
    """The guard refused the operation."""


# USB serials every unit of a model shares: never enough on their own. An allowlist entry with such a serial matches
# a disk only when its file system UUID matches too.
GENERIC_SERIALS = frozenset({"HiBy_R1"})


@dataclass(frozen=True)
class Allowlist:
    serials: frozenset[str] = frozenset()
    fs_uuids: frozenset[str] = frozenset()
    names: dict[str, str] = field(default_factory=dict)  # serial -> entry name
    # fs UUID -> the generic serial its entry names: the UUID matches only on a disk with that serial
    uuid_serials: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Allowlist:
        """A missing file is an empty allowlist (every disk refused), never "no allowlist"."""
        try:
            with open(path, "rb") as f:
                doc = tomllib.load(f)
        except FileNotFoundError:
            log.warning("device allowlist %s not found: every disk is refused", path)
            doc = {}
        serials: set[str] = set()
        uuids: set[str] = set()
        names: dict[str, str] = {}
        uuid_serials: dict[str, str] = {}
        for name, entry in doc.items():
            if not isinstance(entry, dict):
                continue
            uuid = str(entry["fs_uuid"]) if entry.get("fs_uuid") else None
            # `usb_serial_aliases`: the same device under another serial
            for serial in [entry.get("usb_serial"), *entry.get("usb_serial_aliases", [])]:
                if not serial:
                    continue
                if str(serial) in GENERIC_SERIALS:
                    # shared by every unit of the model: only together with the entry's fs UUID
                    if uuid:
                        uuid_serials[uuid] = str(serial)
                    else:
                        log.warning(
                            "allowlist entry [%s]: generic serial %s without fs_uuid is ignored",
                            name,
                            serial,
                        )
                    continue
                serials.add(str(serial))
                names[str(serial)] = name
            if uuid:
                uuids.add(uuid)
        return cls(frozenset(serials), frozenset(uuids), names, uuid_serials)

    def allows(self, disk: BlockDeviceInfo, node: BlockDeviceInfo) -> bool:
        if disk.serial and disk.serial in self.serials:
            return True
        if not (node.uuid and node.uuid in self.fs_uuids):
            return False
        required = self.uuid_serials.get(node.uuid)
        return required is None or disk.serial == required


def check_target(
    target: str,
    disks: list[BlockDeviceInfo],
    *,
    allowlist: Allowlist | None = None,
    allow_loop: bool = False,
    registered: Iterable[str] | None = None,
    operation: str = "write",
    fixed_usb: bool = False,
) -> tuple[BlockDeviceInfo, BlockDeviceInfo]:
    """Validate `target` (a /dev path) for `operation`. Returns (disk, node) or raises GuardError."""
    if operation not in OPERATIONS:
        raise GuardError(
            f"refusing {operation!r} on {target}: unknown operation (known: {sorted(OPERATIONS)})"
        )
    try:
        disk, node = find_disk_of(target, disks)
    except KeyError:
        raise GuardError(f"refusing {operation} on {target}: unknown block device") from None

    def refuse(reason: str) -> GuardError:
        return GuardError(f"refusing {operation} on {target} ({disk.model or disk.name}): {reason}")

    if disk.name.startswith(("nvme", "mmcblk")):
        raise refuse("NVMe/MMC system disk")
    for mp in disk.all_mountpoints():
        if mp in SYSTEM_MOUNTPOINTS:
            raise refuse(f"disk holds system mountpoint {mp}")
    if any(n.fstype == "swap" for n in [disk, *disk.children]):
        raise refuse("disk holds a swap area")
    if operation == "time_sync" and (node is not disk or disk.usb_vendor_id != APPLE_VENDOR_ID):
        raise refuse("time sync is sent to an iPod's whole disk only")
    is_loop = disk.type == "loop" or disk.name.startswith("loop")
    if is_loop:
        # emulated test devices: only with loop devices allowed and named by the allowlist
        if not allow_loop:
            raise refuse("loop devices not allowed")
        if allowlist is None or not allowlist.allows(disk, node):
            raise refuse("loop device not in the emulated-device allowlist")
    else:
        if disk.tran != "usb":
            raise refuse(f"transport is {disk.tran!r}, not usb")
        if not disk.rm and disk.usb_vendor_id != APPLE_VENDOR_ID:
            vouched = registered is not None or (
                fixed_usb and operation not in DESTRUCTIVE_OPERATIONS
            )
            if not vouched:
                raise refuse("not removable and not an iPod")
        if allowlist is not None and not allowlist.allows(disk, node):
            raise refuse("not in the hardware allowlist")
    if registered is not None:
        ids = set(registered)
        if not ((disk.serial and disk.serial in ids) or (node.uuid and node.uuid in ids)):
            raise refuse("not a registered or user-selected device")
    if disk.usb_vendor_id == APPLE_VENDOR_ID and operation in DESTRUCTIVE_OPERATIONS:
        _check_ipod_destructive(node, refuse)
    return disk, node


def _check_ipod_destructive(node: BlockDeviceInfo, refuse: Callable[[str], GuardError]) -> None:
    """Only the iPod data partition may be formatted; partition table and firmware stay untouched."""
    if node.type != "part":
        raise refuse("whole iPod disk: the partition table must never be rewritten")
    if node.parttype == "0x0":
        raise refuse("iPod firmware partition")
    if node.fstype not in IPOD_DATA_FSTYPES and node.parttype not in IPOD_DATA_PARTTYPES:
        raise refuse(
            f"not an iPod data partition (fstype {node.fstype!r}, parttype {node.parttype!r})"
        )


class Guard:
    """Configured guard used by mount/format code. Rescans block devices on every check."""

    def __init__(self, allowlist_path: Path | None = None, allow_loop: bool = False) -> None:
        self.allowlist = Allowlist.load(allowlist_path) if allowlist_path else None
        self.allow_loop = allow_loop

    def check(
        self,
        target: str,
        operation: str,
        registered: Iterable[str] | None = None,
        disks: list[BlockDeviceInfo] | None = None,
        *,
        fixed_usb: bool = False,
    ) -> tuple[BlockDeviceInfo, BlockDeviceInfo]:
        return check_target(
            target,
            disks if disks is not None else scan_block_devices(),
            allowlist=self.allowlist,
            allow_loop=self.allow_loop,
            registered=registered,
            operation=operation,
            fixed_usb=fixed_usb,
        )
