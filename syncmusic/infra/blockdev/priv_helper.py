# SPDX-License-Identifier: GPL-3.0-or-later
"""`syncmusic-priv`: the root helper that lets a normal desktop user mount, format, eject and time-sync devices
without broad sudo.

`packaging/install-systemd.sh --user NAME` allows NAME to run exactly this program as root without a password (a sudoers
drop-in); the app then runs `sudo -n syncmusic-priv <command> <args>` instead of `sudo -n <command>` when plain
sudo is not available (`mount.privileged`). The helper is root-owned (installed with the app's venv under the
prefix) and decides on its own what it runs, so a user who can call it can still only:

- run one of the commands below, with the options SyncMusic itself uses;
- name block devices that pass the blockdev guard (USB, removable or an iPod, never a system disk) for the
  command's operation - re-checked here against a fresh scan, whatever the app checked before;
- touch mountpoints only directly below the mount root (`/run/syncmusic`, or `mount_root` in the root-owned
  /etc/syncmusic/priv.toml), mounted with nosuid/nodev/noexec and owned by the calling user;
- unmount only those mountpoints and mountpoints of guard-accepted devices.

Anything else is refused with exit code 77 and a message on stderr; nothing is run.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

from syncmusic.infra.blockdev.guard import Guard, GuardError
from syncmusic.infra.blockdev.identify import scan_block_devices

CONFIG = Path("/etc/syncmusic/priv.toml")
DEFAULT_MOUNT_ROOT = Path("/run/syncmusic")
REFUSED = 77
FS_TYPES = frozenset({"vfat", "exfat", "ext4", "ntfs3"})
# mount options SyncMusic uses (mount.mount_options); uid=/gid= must be the caller's own
PLAIN_OPTIONS = frozenset(
    {"utf8", "flush", "umask=022", "shortname=mixed", "iocharset=utf8", "ro", "rw"}
)
REQUIRED_OPTIONS = frozenset({"nosuid", "nodev", "noexec"})
# label / cluster options of the mkfs tools (format.mkfs_command)
MKFS = {
    "mkfs.vfat": {
        "-F": r"32",
        "-I": None,
        "-n": r".{0,11}",
        "-s": r"\d+",
        "-S": r"\d+",
        "-h": r"\d+",
    },
    "mkfs.exfat": {"-L": r".{0,15}"},
    "mkfs.ext4": {"-F": None, "-q": None, "-L": r".{0,16}", "-m": r"0"},
    "mkfs.ntfs": {"-f": None, "-Q": None, "-L": r".{0,32}"},
}
# file system label tools (format.label_command: `<tool> <partition> <label>`), with the label's max length
LABEL_TOOLS = {"fatlabel": 11, "exfatlabel": 15, "e2label": 16, "ntfslabel": 32}


class Refused(Exception):
    pass


def _caller() -> tuple[int, int]:
    """The real user behind sudo (SUDO_UID/SUDO_GID, set by sudo itself)."""
    try:
        return int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    except (KeyError, ValueError):
        if os.getuid() != 0:
            return os.getuid(), os.getgid()
        raise Refused("run through sudo (SUDO_UID is not set)") from None


def mount_root(config: Path | None = None) -> Path:
    config = CONFIG if config is None else config
    if config.is_file():
        data = tomllib.loads(config.read_text())
        if "mount_root" in data:
            return Path(str(data["mount_root"]))
    return DEFAULT_MOUNT_ROOT


class Helper:
    def __init__(self, guard: Guard, root: Path, uid: int, gid: int) -> None:
        self.guard, self.root, self.uid, self.gid = guard, root, uid, gid

    # --- argument checks ------------------------------------------------------------------------

    def device(self, path: str, operation: str) -> str:
        if not path.startswith("/dev/"):
            raise Refused(f"{path!r} is not a block device path")
        try:
            self.guard.check(path, operation)
        except GuardError as e:
            raise Refused(str(e)) from None
        return path

    def mountpoint(self, path: str) -> str:
        """A directory directly below the mount root (`<root>/<device id>` or `<root>/.probe-<name>`)."""
        p = Path(path)
        if (
            p.parent != self.root
            or p.name in ("", ".", "..")
            or "/" in p.name
            or not p.is_absolute()
            or p.is_symlink()  # chown/mount must not follow a link out of the root
        ):
            raise Refused(f"{path!r} is not directly below {self.root}")
        return path

    def umount_target(self, path: str) -> str:
        """Ours (below the root) or a mountpoint of a guard-accepted device (a desktop automount)."""
        try:
            return self.mountpoint(path)
        except Refused:
            pass
        for disk in scan_block_devices(False):
            for node in (disk, *disk.children):
                if path in node.mountpoints:
                    self.device(node.path, "mount")
                    return path
        raise Refused(f"{path!r} is not a mountpoint of SyncMusic or of an accepted device")

    # --- commands ---------------------------------------------------------------------------------

    def check(self, argv: list[str]) -> list[str]:
        """The command to run for `argv` (as given by the app), or Refused."""
        if not argv:
            raise Refused("no command")
        name, args = argv[0], argv[1:]
        checks: dict[str, Callable[[list[str]], None]] = {
            "mount": self._mount,
            "umount": self._umount,
            "mkdir": self._mkdir,
            "rmdir": lambda a: self._exact(a, 1, lambda: self.mountpoint(a[0])),
            "chown": self._chown,
            "wipefs": lambda a: self._flags(a, ["-a", "-q"], "partition"),
            "sfdisk": lambda a: self._flags(a, ["--quiet", "--wipe", "always"], "partition"),
            "sg_raw": self._sg_raw,
            "sg_inq": self._sg_inq,
            # file system check and repair of a damaged data partition
            "fsck.vfat": lambda a: self._flags(a, ["-a"], "repair"),
            "fsck.exfat": lambda a: self._flags(a, ["-y"], "repair"),
            "dmesg": lambda a: self._exact(a, 0, lambda: None),
            "eject": lambda a: self._exact(a, 1, lambda: self.device(a[0], "eject")),
            "udisksctl": lambda a: self._flags(
                a, ["power-off", "--no-user-interaction", "-b"], "eject"
            ),
        }
        if name in MKFS:
            self._mkfs(name, args)
        elif name in LABEL_TOOLS:
            self._label(name, args)
        elif name in checks:
            checks[name](args)
        else:
            raise Refused(f"command {name!r} is not allowed")
        exe = _which(name)
        if exe is None:
            raise Refused(f"{name} is not installed")
        return [exe, *args]

    @staticmethod
    def _exact(args: list[str], n: int, then: Callable[[], object]) -> None:
        if len(args) != n:
            raise Refused(f"expected {n} arguments, got {len(args)}")
        then()

    def _flags(self, args: list[str], flags: list[str], operation: str) -> None:
        """Exactly `flags` then one device."""
        if args[:-1] != flags or len(args) != len(flags) + 1:
            raise Refused(f"expected {' '.join(flags)} <device>")
        self.device(args[-1], operation)

    def _mount(self, args: list[str]) -> None:
        if len(args) != 6 or args[0] != "-t" or args[2] != "-o":
            raise Refused("expected mount -t TYPE -o OPTIONS DEVICE TARGET")
        if args[1] not in FS_TYPES:
            raise Refused(f"file system type {args[1]!r} is not allowed")
        opts = args[3].split(",")
        for o in opts:
            if o in PLAIN_OPTIONS or o in REQUIRED_OPTIONS:
                continue
            if o == f"uid={self.uid}" or o == f"gid={self.gid}":
                continue
            raise Refused(f"mount option {o!r} is not allowed")
        if not set(opts) >= REQUIRED_OPTIONS:
            raise Refused("mount needs nosuid,nodev,noexec")
        self.device(args[4], "mount")
        self.mountpoint(args[5])

    def _umount(self, args: list[str]) -> None:
        if args[:1] == ["-l"]:
            self._exact(args[1:], 1, lambda: self.mountpoint(args[1]))  # stale mounts: ours only
        else:
            self._exact(args, 1, lambda: self.umount_target(args[0]))

    def _mkdir(self, args: list[str]) -> None:
        if args[:1] != ["-p"] or len(args) != 2:
            raise Refused("expected mkdir -p <mountpoint>")
        self.mountpoint(args[1])

    def _chown(self, args: list[str]) -> None:
        if len(args) != 2 or args[0] != f"{self.uid}:{self.gid}":
            raise Refused("chown only to the calling user")
        self.mountpoint(args[1])

    def _mkfs(self, name: str, args: list[str]) -> None:
        if not args:
            raise Refused("no device")
        allowed, i = MKFS[name], 0
        opts = args[:-1]
        while i < len(opts):
            flag = opts[i]
            if flag not in allowed:
                raise Refused(f"{name} option {flag!r} is not allowed")
            pattern = allowed[flag]
            if pattern is not None:
                if i + 1 >= len(opts) or not re.fullmatch(pattern, opts[i + 1]):
                    raise Refused(f"{name} {flag} needs a valid value")
                i += 1
            i += 1
        self.device(args[-1], "mkfs")

    def _label(self, name: str, args: list[str]) -> None:
        if len(args) != 2:
            raise Refused(f"expected {name} <partition> <label>")
        label = args[1]
        if (
            not label
            or label.startswith("-")
            or len(label.encode()) > LABEL_TOOLS[name]
            or not label.isprintable()
        ):
            raise Refused(f"{name}: not a valid label")
        self.device(args[0], "label")

    def _sg_raw(self, args: list[str]) -> None:
        # sg_raw -s N <disk> <CDB bytes...> (scsi.set_time: the iPod clock)
        if len(args) < 4 or args[0] != "-s" or not args[1].isdigit():
            raise Refused("expected sg_raw -s N <disk> <cdb>")
        self.device(args[2], "time_sync")
        if not all(re.fullmatch(r"[0-9a-fA-F]{2}", b) for b in args[3:]):
            raise Refused("the CDB must be hex bytes")

    def _sg_inq(self, args: list[str]) -> None:
        if (
            len(args) != 3
            or not re.fullmatch(r"--page=0x[0-9a-fA-F]{2}", args[0])
            or args[1] != "--raw"
        ):
            raise Refused("expected sg_inq --page=0xNN --raw <disk>")
        self.device(args[2], "read")


def _which(name: str) -> str | None:
    return shutil.which(name, path="/usr/sbin:/usr/bin:/sbin:/bin")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if os.geteuid() != 0:
            raise Refused("must run as root (through sudo)")
        uid, gid = _caller()
        helper = Helper(Guard(None), mount_root(), uid, gid)
        cmd = helper.check(argv)
    except Refused as e:
        print(f"syncmusic-priv: refused: {e}", file=sys.stderr)
        return REFUSED
    os.execv(cmd[0], cmd)
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main())
