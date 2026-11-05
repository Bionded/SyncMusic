# SPDX-License-Identifier: GPL-3.0-or-later
"""Guarded formatting: whole flash drives, and single partitions (iPod data partition).

`format_disk` writes a new partition table with one partition and creates a file system on it - only for
non-iPod disks (the guard refuses partitioning an iPod's whole disk). `make_filesystem` formats one existing
partition (the iPod data partition; the partition table and firmware partition stay untouched). Both check
the guard for every step and unmount the disk first.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from syncmusic.infra.blockdev.guard import APPLE_VENDOR_ID, Guard, GuardError
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from syncmusic.infra.blockdev.mount import BlockdevError, run_cmd, unmount_all

log = logging.getLogger(__name__)

Filesystem = Literal["vfat", "exfat", "ext4", "ntfs"]
FILESYSTEMS: tuple[Filesystem, ...] = ("vfat", "exfat", "ext4", "ntfs")
# MBR partition type per file system
PART_TYPES = {"vfat": "c", "exfat": "7", "ntfs": "7", "ext4": "83"}
LABEL_MAX = {"vfat": 11, "exfat": 15, "ntfs": 32, "ext4": 16}
GPT_TYPES = {"vfat": "basic", "exfat": "basic", "ntfs": "basic", "ext4": "linux"}  # sfdisk aliases
MBR_MAX_BYTES = 2 * 1024**4  # larger disks get GPT
FS_LABELS = {"vfat": "FAT32", "exfat": "exFAT", "ntfs": "NTFS", "ext4": "ext4"}
# what creating and mounting each file system needs on this host: the mkfs tool and the kernel driver mount.py uses
MKFS_TOOLS = {"vfat": "mkfs.vfat", "exfat": "mkfs.exfat", "ext4": "mkfs.ext4", "ntfs": "mkfs.ntfs"}
KERNEL_FS = {"vfat": "vfat", "exfat": "exfat", "ext4": "ext4", "ntfs": "ntfs3"}
APT_PACKAGES = {"vfat": "dosfstools", "exfat": "exfatprogs", "ext4": "e2fsprogs", "ntfs": "ntfs-3g"}
# the tools that change the label of an existing (unmounted) file system (Rename), from the same packages
LABEL_TOOLS = {"vfat": "fatlabel", "exfat": "exfatlabel", "ext4": "e2label", "ntfs": "ntfslabel"}


def _kernel_has(
    fs: str, proc: Path = Path("/proc/filesystems"), modules: Path | None = None
) -> bool:
    """The kernel knows `fs` (built in / loaded) or has a module for it."""
    try:
        if any(line.split()[-1] == fs for line in proc.read_text().splitlines() if line.split()):
            return True
    except OSError:
        pass
    modules = modules or Path("/lib/modules") / os.uname().release
    return any(modules.glob(f"kernel/fs/**/{fs}.ko*"))


def _which(tool: str) -> str | None:
    """`tool` on PATH or in /usr/sbin, /sbin (where mkfs tools live; a normal user's PATH may lack them)."""
    return shutil.which(
        tool, path=os.pathsep.join([os.environ.get("PATH", ""), "/usr/sbin", "/sbin"])
    )


def filesystem_support(
    which: Callable[[str], str | None] = _which,
    kernel_has: Callable[[str], bool] = _kernel_has,
) -> dict[str, str | None]:
    """For every file system: None when this host can create and mount it, else why not."""
    out: dict[str, str | None] = {}
    for fs in FILESYSTEMS:
        tool = MKFS_TOOLS[fs]
        if not which(tool):
            out[fs] = f"{tool} is not installed (package {APT_PACKAGES[fs]})"
        elif not kernel_has(KERNEL_FS[fs]):
            out[fs] = f"the kernel has no {KERNEL_FS[fs]} driver"
        else:
            out[fs] = None
    return out


def clean_label(label: str, fs: Filesystem) -> str:
    """A label the mkfs tool accepts: FAT is upper-case ASCII without specials, others plain."""
    if fs == "vfat":
        text = re.sub(r"[^A-Z0-9 _-]", "", label.upper())
    else:
        text = "".join(c for c in label if c.isprintable() and c not in "/\\\"'")
    out = text.strip()
    while len(out.encode("utf-8")) > LABEL_MAX[fs]:
        out = out[:-1]
    return out.strip() or "SYNCDIR"


# FAT32 cluster size on disks with logical sectors > 512 bytes: Apple's own restore of the SSD-modded Classic
# (4096-byte sectors) uses 16 KiB; mkfs.fat would pick 256 KiB there, which the iPod cannot play from.
LARGE_SECTOR_CLUSTER = 16 * 1024


@dataclass(frozen=True)
class Geometry:
    logical_sector: int = 512  # bytes
    start_512: int = 0  # partition start in 512-byte units (sysfs), 0 = whole disk / unknown


def geometry(dev_path: str) -> Geometry:
    """Logical sector size and partition start from sysfs (defaults when unreadable)."""
    node = (Path("/sys/class/block") / Path(dev_path).name).resolve()
    try:
        part = (node / "partition").exists()
        start = int((node / "start").read_text()) if part else 0
        sector = int(((node.parent if part else node) / "queue" / "logical_block_size").read_text())
    except (OSError, ValueError):
        return Geometry()
    return Geometry(logical_sector=sector, start_512=start)


def mkfs_command(
    dev_path: str,
    fs: Filesystem,
    label: str,
    geo: Geometry | None = None,
    whole_disk: bool = False,
) -> list[str]:
    lab = clean_label(label, fs)
    if fs == "vfat":
        # -I: mkfs.fat refuses a whole disk (no partition table, the HiBy R1's layout) without it
        cmd = ["mkfs.vfat", "-F", "32", *(["-I"] if whole_disk else []), "-n", lab]
        g = geo or Geometry()
        if g.logical_sector > 512:
            # mkfs.fat 4.2 counts hidden sectors in 512-byte units and picks huge clusters on 4K-sector disks
            cmd += [
                "-S",
                str(g.logical_sector),
                "-s",
                str(max(1, LARGE_SECTOR_CLUSTER // g.logical_sector)),
            ]
            cmd += ["-h", str(g.start_512 * 512 // g.logical_sector)]
        return [*cmd, dev_path]
    if fs == "exfat":
        return ["mkfs.exfat", "-L", lab, dev_path]
    if fs == "ext4":
        return ["mkfs.ext4", "-F", "-q", "-L", lab, "-m", "0", dev_path]
    return ["mkfs.ntfs", "-f", "-Q", "-L", lab, dev_path]


def partition_path(disk: BlockDeviceInfo, number: int = 1) -> str:
    """/dev/sda + 1 → /dev/sda1; /dev/loop3 + 1 → /dev/loop3p1."""
    sep = "p" if disk.name[-1].isdigit() else ""
    return f"{disk.path}{sep}{number}"


async def _settle(disk_path: str, *, table_written: bool = False) -> None:
    """Let the kernel and udev catch up. `partprobe` only after a new partition table: libparted drops the MBR
    entries it does not recognise from the kernel's view - the type-0 firmware partition of an iPod would lose
    its device node until the next replug. A new file system or label changes no partition; udev re-reads it."""
    cmds = [["partprobe", disk_path]] if table_written else []
    for cmd in (*cmds, ["udevadm", "settle", "--timeout=10"]):
        try:
            await run_cmd(cmd)
        except BlockdevError as e:
            log.debug("%s: %s", cmd[0], e)


async def make_filesystem(
    guard: Guard,
    part_path: str,
    fs: Filesystem,
    label: str,
    registered: Iterable[str] | None = None,
    *,
    whole_disk: bool = False,
) -> None:
    """Create a file system on one existing partition (guard: `mkfs`). `whole_disk`: on the disk itself, without a
    partition table (old signatures wiped first, the HiBy R1)."""
    if fs not in FILESYSTEMS:
        raise ValueError(f"unsupported file system {fs!r} (one of {FILESYSTEMS})")
    disk, node = guard.check(part_path, "mkfs", registered)
    if whole_disk and node is not disk:
        raise GuardError(f"refusing whole-disk mkfs on {part_path}: not a whole disk")
    if not whole_disk and node is disk and disk.partitions():
        raise GuardError(f"refusing mkfs on {part_path}: the disk has partitions")
    await unmount_all(disk)
    guard.check(part_path, "mkfs", registered)  # re-check after unmount (fresh scan)
    if whole_disk:
        await run_cmd(["wipefs", "-a", "-q", node.path])
    cmd = mkfs_command(node.path, fs, label, geometry(node.path), whole_disk=whole_disk)
    await run_cmd(cmd, timeout_s=600)
    await _settle(disk.path, table_written=whole_disk)  # wipefs removed the old partition table
    log.info("created %s on %s", fs, node.path)


async def format_disk(
    guard: Guard,
    disk_path: str,
    fs: Filesystem,
    label: str,
    registered: Iterable[str] | None = None,
) -> str:
    """New partition table + one partition spanning the disk + file system. Returns the partition path.
    Flash drives only: iPods keep their partition table (use `make_filesystem` on the data partition)."""
    if fs not in FILESYSTEMS:
        raise ValueError(f"unsupported file system {fs!r} (one of {FILESYSTEMS})")
    disk, node = guard.check(disk_path, "partition", registered)
    if node is not disk:
        raise GuardError(f"refusing partition on {disk_path}: not a whole disk")
    if disk.usb_vendor_id == APPLE_VENDOR_ID:
        raise GuardError(f"refusing partition on {disk_path}: iPods keep their partition table")
    await unmount_all(disk)
    disk, _ = guard.check(disk_path, "partition", registered)
    await run_cmd(["wipefs", "-a", "-q", disk.path])
    label_type = "gpt" if disk.size > MBR_MAX_BYTES else "dos"
    ptype = PART_TYPES[fs] if label_type == "dos" else GPT_TYPES[fs]
    script = f"label: {label_type}\n,,{ptype}\n".encode()
    await run_cmd(["sfdisk", "--quiet", "--wipe", "always", disk.path], input=script)
    await _settle(disk.path, table_written=True)
    part = partition_path(disk)
    guard.check(part, "mkfs", registered)  # fresh scan: the new partition must pass the guard too
    await run_cmd(mkfs_command(part, fs, label, geometry(part)), timeout_s=600)
    await _settle(disk.path)
    log.info("formatted %s: %s partition %s (%s)", disk.path, label_type, part, fs)
    return part


def label_command(part_path: str, fs: Filesystem, label: str) -> list[str]:
    # a leading "-" would be read as an option by the tool
    return [LABEL_TOOLS[fs], part_path, clean_label(label, fs).lstrip("-") or "SYNCDIR"]


async def set_label(
    guard: Guard,
    part_path: str,
    fs: str,
    label: str,
    registered: Iterable[str] | None = None,
) -> str:
    """Change the label of the file system on `part_path` (guard: `label`; on an iPod only the data partition) -
    nothing else on the disk changes. Unmounts the disk first. Returns the label written (`clean_label`)."""
    if fs not in LABEL_TOOLS:
        raise ValueError(f"cannot label a {fs!r} file system (one of {FILESYSTEMS})")
    disk, node = guard.check(part_path, "label", registered)
    await unmount_all(disk)
    guard.check(part_path, "label", registered)  # re-check after unmount (fresh scan)
    cmd = label_command(node.path, fs, label)  # type: ignore[arg-type]
    await run_cmd(cmd)
    await _settle(disk.path)
    log.info("labelled %s (%s) %r", node.path, fs, cmd[-1])
    return cmd[-1]
