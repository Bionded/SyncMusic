# SPDX-License-Identifier: GPL-3.0-or-later
"""Guarded mount / unmount / eject of device partitions (§7).

Every operation goes through `Guard.check` first. Devices are mounted under `<mount_root>/<device_id>`; a
partition already mounted elsewhere (desktop automount) is reused and left mounted. File systems are mounted
so the app user owns the files: `uid`/`gid` options for vfat/exfat/ntfs3, `chown` of the root for ext4.

Privileged commands run directly when the process is root (the production service), via `sudo -n` when the user
has passwordless sudo (development and tests), and otherwise through the root helper `syncmusic-priv`(`sudo -n syncmusic-priv <command>`, allowed by `packaging/install-systemd.sh --user NAME`), which runs only
SyncMusic's own commands on guard-accepted devices.

Unplug without eject: the kernel keeps the mount of a vanished disk. `drop_mount` lazily detaches our own
mountpoint when the disk is removed, and `mount` first detaches stale mounts (source node gone or now another
device) left on its target, so mounts never stack. Detaching our own mountpoint is not a disk operation (the
node may not exist any more), so it bypasses the guard; it only ever touches `<mount_root>/<device_id>`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from syncmusic.infra.blockdev.guard import Guard, GuardError
from syncmusic.infra.blockdev.identify import BlockDeviceInfo, scan_block_devices

log = logging.getLogger(__name__)

# fstype as reported by lsblk -> mount -t type
MOUNT_TYPES = {"vfat": "vfat", "exfat": "exfat", "ext4": "ext4", "ntfs": "ntfs3", "ntfs3": "ntfs3"}
CMD_TIMEOUT_S = 120.0

_libc = ctypes.CDLL(None, use_errno=True)


def syncfs(path: Path | str) -> None:
    """Flush only the file system holding `path` (syncfs(2)), never sync(2): a stuck volume (a hung USB disk,
    an ntfs3 writeback deadlock) must not freeze every other flush on the host. Blocking - call it
    in a thread."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if _libc.syncfs(fd) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), str(path))
    finally:
        os.close(fd)


async def _flush(path: Path | str) -> None:
    """Best-effort syncfs before umount/eject (umount flushes anyway; a vanished mountpoint is no error)."""
    t0 = time.monotonic()
    try:
        await asyncio.to_thread(syncfs, path)
    except OSError as e:
        log.warning("could not flush %s: %s", path, e)
        return
    log.info("flushed %s in %.1f s", path, time.monotonic() - t0)


class BlockdevError(RuntimeError):
    """A mount/format command failed."""


class UnmountableError(BlockdevError):
    """The file system does not mount: blank, unknown, corrupt or a dirty NTFS volume."""


def _kernel_messages(dev_path: str, lines: int = 30) -> list[str]:
    """Recent kernel log lines about `dev_path` (best effort: dmesg may need root)."""
    name = Path(dev_path).name
    try:
        out = subprocess.run(
            privileged(["dmesg"]), capture_output=True, text=True, timeout=5, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in out.splitlines()[-lines:] if f"({name})" in ln or f" {name}:" in ln]


def unmountable_message(dev_path: str, fstype: str, error: str, kernel: list[str]) -> str:
    """A human message for a failed mount: what happened and what the user can do."""
    if fstype == "ntfs" and any("dirty" in ln for ln in kernel):
        return (
            f"{dev_path}: the NTFS volume is marked dirty (it was not ejected safely). "
            "Repair it in Windows (chkdsk /f) or format it."
        )
    detail = kernel[-1].split("]", 1)[-1].strip() if kernel else error
    return f"{dev_path}: the {fstype} file system does not mount ({detail}). Repair or format it."


def mount_source(path: Path, mountinfo: str = "/proc/self/mountinfo") -> str | None:
    """The source (`/dev/sdX1`) of the file system mounted exactly at `path`, from the kernel's mount table
    (the last mount there wins, like the kernel); None when `path` is not a mount point."""
    target = str(path)
    found = None
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                fields, _, rest = line.partition(" - ")
                parts, tail = fields.split(), rest.split()
                if len(parts) >= 5 and len(tail) >= 2 and _unescape(parts[4]) == target:
                    found = _unescape(tail[1])
    except OSError:
        return None
    return found


def _unescape(field: str) -> str:
    """mountinfo octal escapes (`\\040` = space)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


HELPER_NAME = "syncmusic-priv"
_route: list[str] | None = (
    None  # the prefix privileged commands get, decided once (`privilege_route`)
)


def _sudo_ok(*args: str) -> bool:
    try:
        return (
            subprocess.run(
                ["sudo", "-n", *args], capture_output=True, timeout=10, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def helper_path(name: str = HELPER_NAME) -> Path:
    """The root helper next to this interpreter (the app's venv: `<prefix>/.venv/bin/syncmusic-priv`)."""
    return Path(sys.executable).parent / name


def privilege_route() -> list[str]:
    """[] as root, `sudo -n` with passwordless sudo, `sudo -n <helper>` when only the helper is allowed, else
    `sudo -n` (it fails with sudo's own message: devices are listed but cannot be used)."""
    global _route
    if _route is None:
        helper = helper_path()
        if os.geteuid() == 0:
            _route = []
        elif _sudo_ok("true"):
            _route = ["sudo", "-n"]
        else:
            _route = ["sudo", "-n"]  # without an allowed helper it fails with sudo's own message
            if helper.is_file() and _sudo_ok("-l", str(helper)):
                _route = ["sudo", "-n", str(helper)]
        log.info("privileged commands run via: %s", " ".join(_route) or "(root)")
    return _route


def privileged(cmd: list[str]) -> list[str]:
    return [*privilege_route(), *cmd]


async def run_cmd_raw(
    cmd: list[str],
    *,
    priv: bool = True,
    timeout_s: float = CMD_TIMEOUT_S,
    input: bytes | None = None,
) -> bytes:
    """Run a command (as root when `priv`), return stdout bytes; BlockdevError with stderr on failure."""
    argv = privileged(cmd) if priv else cmd
    log.debug("run: %s", " ".join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise BlockdevError(f"{cmd[0]} timed out after {timeout_s:.0f} s") from None
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip() or out.decode(errors="replace").strip()
        raise BlockdevError(f"{' '.join(cmd)} failed ({proc.returncode}): {msg}")
    return out


async def run_cmd(
    cmd: list[str],
    *,
    priv: bool = True,
    timeout_s: float = CMD_TIMEOUT_S,
    input: bytes | None = None,
) -> str:
    """Run a command (as root when `priv`), return stdout; BlockdevError with stderr on failure."""
    out = await run_cmd_raw(cmd, priv=priv, timeout_s=timeout_s, input=input)
    return out.decode(errors="replace")


def _node(dev_path: str, disks: list[BlockDeviceInfo]) -> BlockDeviceInfo:
    real = str(Path(dev_path).resolve())
    for d in disks:
        for n in (d, *d.children):
            if real in (n.path, f"/dev/{n.name}"):
                return n
    raise BlockdevError(f"{dev_path}: no such block device")


@dataclass(frozen=True)
class MountEntry:
    dev: str  # major:minor of the mounted filesystem
    path: str
    source: str


_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def unescape_mount_field(field: str) -> str:
    """Undo the kernel's octal escapes in /proc mount tables (\\040 space, \\011, \\012, \\134); UTF-8 stays as is."""
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m[1], 8)), field)


def mount_table() -> list[MountEntry]:
    """/proc/self/mountinfo, in mount order (the last entry for a path is the visible one)."""
    out: list[MountEntry] = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        parts = line.split()
        if "-" not in parts:
            continue
        sep = parts.index("-")
        out.append(
            MountEntry(
                parts[2], unescape_mount_field(parts[4]), unescape_mount_field(parts[sep + 2])
            )
        )
    return out


def is_stale(e: MountEntry) -> bool:
    """A block-device mount whose source node is gone or now belongs to another device."""
    if not e.source.startswith("/dev/"):
        return False
    try:
        rdev = os.stat(e.source).st_rdev
    except OSError:
        return True
    return f"{os.major(rdev)}:{os.minor(rdev)}" != e.dev


def is_live(m: Mounted) -> bool:
    """The visible mount at `m.path` is still `m.dev_path` (not unmounted behind our back)."""
    entries = [e for e in mount_table() if e.path == str(m.path)]
    if not entries:
        return False
    try:
        rdev = os.stat(m.dev_path).st_rdev
    except OSError:
        return False
    return entries[-1].dev == f"{os.major(rdev)}:{os.minor(rdev)}" and not is_stale(entries[-1])


async def _detach(target: Path, keep: Callable[[MountEntry], bool]) -> int:
    """`umount -l` the mounts at `target` (top first) until one is to be kept; returns how many."""
    done = 0
    for _ in range(16):  # never loop forever on a mount that does not go away
        entries = [e for e in await asyncio.to_thread(mount_table) if e.path == str(target)]
        if not entries or keep(entries[-1]):
            break
        log.warning("detaching stale mount of %s at %s", entries[-1].source, target)
        await run_cmd(["umount", "-l", str(target)])
        done += 1
    return done


async def drop_mount(m: Mounted) -> None:
    """The disk behind our mount was removed: lazily detach the mountpoint and remove it."""
    if not m.owned:
        return  # someone else's mount (desktop automount): theirs to clean up
    await _detach(m.path, lambda e: e.source != m.dev_path and not is_stale(e))
    try:
        await run_cmd(["rmdir", str(m.path)])
    except BlockdevError as e:
        log.info("could not remove mountpoint %s: %s", m.path, e)


def mount_options(fstype: str, uid: int, gid: int, read_only: bool = False) -> list[str]:
    """No vfat `flush`: on every close of a written file it writes back and then sleeps 100 ms
    (`fat_file_release`), which made a full iPod sync ~3x slower (the artwork files, the inflight list and every
    track are closed per item), and it made a CF-modded iPod mini hang. Durability comes from the
    fsyncs, the checkpoint commits and the flush before the read-back."""
    opts: list[str] = []
    if fstype == "vfat":
        opts += [f"uid={uid}", f"gid={gid}", "utf8"]
        opts += ["umask=022", "shortname=mixed"]
    elif fstype in ("exfat", "ntfs", "ntfs3"):
        opts += [f"uid={uid}", f"gid={gid}", "umask=022", "iocharset=utf8"]
    opts.append("ro" if read_only else "rw")
    opts += ["nosuid", "nodev", "noexec"]
    return opts


@dataclass(frozen=True)
class Mounted:
    path: Path
    dev_path: str
    fstype: str | None
    owned: bool  # we mounted it (unmount when done); False = reused an existing mountpoint


async def _settle_udev() -> None:
    try:
        await run_cmd(["udevadm", "settle", "--timeout=10"], priv=False, timeout_s=15)
    except BlockdevError as e:
        log.debug("udevadm settle: %s", e)


async def mount(
    guard: Guard,
    dev_path: str,
    device_id: str,
    mount_root: Path,
    *,
    read_only: bool = False,
    registered: Iterable[str] | None = None,
) -> Mounted:
    """Mount `dev_path` (a partition or a whole-disk filesystem) for `device_id`, or reuse its mountpoint."""
    try:
        guard.check(dev_path, "mount", registered)
    except GuardError as e:
        if "unknown block device" not in str(e):
            raise
        # udev re-probing the disk (after an unmount, a SCSI command) briefly drops its partitions from lsblk
        #: wait for udev once; the guard checks again in full
        log.info("%s not listed yet, waiting for udev", dev_path)
        await _settle_udev()
        guard.check(dev_path, "mount", registered)
    node = _node(dev_path, await asyncio.to_thread(scan_block_devices, False))
    if node.mountpoints:
        log.info("%s already mounted at %s: reusing it", dev_path, node.mountpoints[0])
        return Mounted(Path(node.mountpoints[0]), dev_path, node.fstype, owned=False)
    if not node.fstype:
        # right after a format, a partition table re-read or a SCSI command, udev may still be re-probing the
        # partition and lsblk reports no file system yet: wait for udev once and look again
        log.info("%s: no file system reported yet, waiting for udev", dev_path)
        await _settle_udev()
        node = _node(dev_path, await asyncio.to_thread(scan_block_devices, False))
    if not node.fstype or node.fstype not in MOUNT_TYPES:
        raise UnmountableError(
            f"{dev_path}: unsupported or no file system ({node.fstype!r}). Format it."
        )
    target = mount_root / device_id
    await _detach(target, lambda e: not is_stale(e))
    if any(e.path == str(target) for e in await asyncio.to_thread(mount_table)):
        raise BlockdevError(f"{target} is already a mountpoint of another device")
    await run_cmd(["mkdir", "-p", str(target)])
    uid, gid = os.getuid(), os.getgid()
    opts = mount_options(node.fstype, uid, gid, read_only)
    try:
        await run_cmd(
            ["mount", "-t", MOUNT_TYPES[node.fstype], "-o", ",".join(opts), dev_path, str(target)]
        )
    except BlockdevError as e:
        if "timed out" in str(e):
            raise
        kernel = await asyncio.to_thread(_kernel_messages, dev_path)
        log.warning("mount %s failed: %s; kernel: %s", dev_path, e, kernel[-3:])
        with contextlib.suppress(BlockdevError):
            await run_cmd(["rmdir", str(target)])
        raise UnmountableError(unmountable_message(dev_path, node.fstype, str(e), kernel)) from e
    if node.fstype == "ext4" and not read_only and os.geteuid() != 0:
        await run_cmd(["chown", f"{uid}:{gid}", str(target)])
    log.info("mounted %s (%s) at %s", dev_path, node.fstype, target)
    return Mounted(target, dev_path, node.fstype, owned=True)


# fstype -> fsck command that repairs without asking; exit 0 = clean, 1 = errors were corrected
FSCK_COMMANDS = {"vfat": ["fsck.vfat", "-a"], "exfat": ["fsck.exfat", "-y"]}
FSCK_TIMEOUT_S = 1800.0


@dataclass
class RepairResult:
    fixed: bool  # the check found and corrected errors
    output: str  # the tool's last lines


async def repair(
    guard: Guard, dev_path: str, *, registered: Iterable[str] | None = None
) -> RepairResult:
    """Check and repair the (unmounted) file system on `dev_path`, a data partition only: the guard refuses the
    whole iPod disk, its firmware partition and anything not allowed for `repair`."""
    guard.check(dev_path, "repair", registered)
    node = _node(dev_path, await asyncio.to_thread(scan_block_devices, False))
    if node.mountpoints:
        raise BlockdevError(f"{dev_path} is mounted at {node.mountpoints[0]}: unmount it first")
    cmd = FSCK_COMMANDS.get(node.fstype or "")
    if cmd is None:
        raise BlockdevError(f"{dev_path}: no repair tool for the {node.fstype!r} file system")
    argv = privileged([*cmd, dev_path])
    log.info("repair: %s", " ".join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), FSCK_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise BlockdevError(f"{cmd[0]} timed out after {FSCK_TIMEOUT_S:.0f} s") from None
    text = "\n".join(out.decode(errors="replace").strip().splitlines()[-20:])
    code = proc.returncode
    log.info("repair %s: exit %s\n%s", dev_path, code, text)
    if code not in (0, 1):
        raise BlockdevError(
            f"{cmd[0]} {dev_path} could not repair the file system ({code}): {text}"
        )
    return RepairResult(fixed=code == 1, output=text)


UMOUNT_BUSY_TRIES = 5
UMOUNT_BUSY_WAIT_S = 0.5


async def _umount_retrying(path: Path) -> None:
    """umount, again a few times while it is busy: right after a mount or a format udev and udisks probe the new
    file system and hold it open for a moment (exit status 32, "target is busy"); a lasting holder still fails."""
    for attempt in range(UMOUNT_BUSY_TRIES):
        try:
            await run_cmd(["umount", str(path)])
            return
        except BlockdevError as e:
            if "busy" not in str(e) or attempt == UMOUNT_BUSY_TRIES - 1:
                raise
            log.debug("umount %s busy, again in %.1f s", path, UMOUNT_BUSY_WAIT_S)
            await asyncio.sleep(UMOUNT_BUSY_WAIT_S)


async def unmount(m: Mounted, *, force_reused: bool = False) -> None:
    """Unmount what we mounted (a reused mountpoint stays unless `force_reused`) and remove our mountpoint."""
    if not m.owned and not force_reused:
        return
    await _flush(m.path)
    await _umount_retrying(m.path)
    if m.owned:
        try:
            await run_cmd(["rmdir", str(m.path)])
        except BlockdevError as e:
            log.warning("could not remove mountpoint %s: %s", m.path, e)


async def probe_read[T](
    guard: Guard, dev_path: str, mount_root: Path, read: Callable[[Path], T]
) -> T:
    """Mount `dev_path` read-only under `<mount_root>/.probe-<name>`, run `read(mountpoint)` in a thread,
    unmount. For identifying unknown disks: the guard check in `mount()`
    applies (allowlisted / USB / removable / iPod only), nothing is ever written. An existing mount is reused
    and left alone."""
    m = await mount(guard, dev_path, f".probe-{Path(dev_path).name}", mount_root, read_only=True)
    try:
        return await asyncio.to_thread(read, m.path)
    finally:
        await unmount(m)


async def unmount_all(disk: BlockDeviceInfo) -> None:
    """Unmount every mounted filesystem of a disk (before partitioning / formatting / eject)."""
    for n in (disk, *disk.children):
        for mp in n.mountpoints:
            await run_cmd(["umount", mp])


async def eject(guard: Guard, disk_path: str, registered: Iterable[str] | None = None) -> None:
    """sync, unmount all partitions, power the disk off (an iPod leaves disk mode)."""
    disk, _ = guard.check(disk_path, "eject", registered)
    for n in (disk, *disk.children):
        for mp in n.mountpoints:
            await _flush(mp)
    await unmount_all(disk)
    if shutil.which("udisksctl"):
        try:
            await run_cmd(["udisksctl", "power-off", "--no-user-interaction", "-b", disk.path])
            return
        except BlockdevError as e:
            log.info("udisksctl power-off failed (%s), trying eject", e)
    if shutil.which("eject"):
        try:
            await run_cmd(["eject", disk.path])
        except BlockdevError as e:
            log.warning("eject %s failed: %s", disk.path, e)


@dataclass(frozen=True)
class PartitionMount:
    partition: str  # /dev/sdX2
    path: str  # where it is mounted
    ours: bool  # below the app's mount directory


def disk_mounts(disk: BlockDeviceInfo, mount_root: Path | None = None) -> list[PartitionMount]:
    """Every mountpoint of every partition of `disk` (lsblk lists a partition mounted twice with both paths)."""
    root = str(mount_root.resolve()) if mount_root is not None else None
    return [
        PartitionMount(n.path, mp, root is not None and str(Path(mp).parent) == root)
        for n in (disk, *disk.children)
        for mp in n.mountpoints
        if mp and not mp.startswith("[")  # "[SWAP]"
    ]


class BusyMountError(BlockdevError):
    """A mountpoint cannot be unmounted because programs still use it."""

    def __init__(self, path: str, holders: list[str]) -> None:
        who = ", ".join(holders) if holders else "an unknown program"
        super().__init__(f"{path} is in use by {who}")
        self.path = path
        self.holders = holders


def mount_holders(path: str, proc: Path = Path("/proc")) -> list[str]:
    """`name (pid)` of the processes with a cwd, root or open file below `path` (best effort: processes of other
    users are only visible when the app runs as root)."""
    prefix = path.rstrip("/") + "/"
    found: list[str] = []
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        links = [pid_dir / "cwd", pid_dir / "root"]
        with contextlib.suppress(OSError):
            links += list((pid_dir / "fd").iterdir())
        for link in links:
            try:
                target = os.readlink(link)
            except OSError:
                continue
            if target == path or target.startswith(prefix):
                try:
                    name = (pid_dir / "comm").read_text().strip()
                except OSError:
                    name = "?"
                found.append(f"{name} ({pid_dir.name})")
                break
    return found


async def unmount_everywhere(
    guard: Guard,
    disk_path: str,
    registered: Iterable[str] | None = None,
    *,
    lazy: bool = False,
    mount_root: Path | None = None,
) -> list[PartitionMount]:
    """Flush and unmount every mount of every partition of the disk, ours and other programs' (a host
    automounter). A busy mount raises BusyMountError naming the holders; `lazy` detaches busy mounts instead
    (`umount -l`, the file system goes away once the last holder closes it). Returns what was unmounted."""
    disk, _ = guard.check(disk_path, "eject", registered)
    mounts = disk_mounts(disk, mount_root)
    for m in mounts:
        with contextlib.suppress(OSError, BlockdevError):
            await _flush(m.path)
    for m in mounts:
        try:
            await _umount_retrying(Path(m.path))
        except BlockdevError as e:
            if "busy" not in str(e):
                raise
            if not lazy:
                raise BusyMountError(m.path, await asyncio.to_thread(mount_holders, m.path)) from e
            log.warning("detaching busy mount %s lazily", m.path)
            await run_cmd(["umount", "-l", m.path])
    return mounts
