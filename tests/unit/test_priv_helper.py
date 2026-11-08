"""Root helper for normal desktop users: it runs only SyncMusic's own commands, on devices the blockdev
guard accepts (checked again here), with mountpoints directly below the mount root and the caller's uid/gid; the app
uses it only when it is neither root nor allowed plain sudo."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev import priv_helper as ph
from syncmusic.infra.blockdev.format import label_command, mkfs_command
from syncmusic.infra.blockdev.guard import GuardError
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from syncmusic.infra.blockdev.scsi import TIME_CDB

ROOT = Path("/run/syncmusic-test")  # not the real root: an installed service owns that one (0700)


class FakeGuard:
    """Accepts the USB stick /dev/sdz (and its partitions); refuses everything else, like the real guard does for
    the NVMe system disk."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def check(self, target, operation, registered=None, disks=None):
        self.calls.append((target, operation))
        if not target.startswith("/dev/sdz"):
            raise GuardError(f"refusing {operation} on {target}: NVMe/MMC system disk")
        return None, None


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.setattr(ph, "_which", lambda name: f"/usr/bin/{name}")
    return ph.Helper(FakeGuard(), ROOT, 1001, 1001)


def ok(helper, argv):
    return helper.check(argv)[1:] == argv[1:]


def refused(helper, argv):
    with pytest.raises(ph.Refused):
        helper.check(argv)
    return True


def test_the_apps_own_commands_pass(helper):
    opts = ",".join(mnt.mount_options("vfat", 1001, 1001))
    assert ok(
        helper, ["mount", "-t", "vfat", "-o", opts, "/dev/sdz1", "/run/syncmusic-test/zorvex"]
    )
    assert ok(
        helper,
        [
            "mount",
            "-t",
            "ext4",
            "-o",
            ",".join(mnt.mount_options("ext4", 1001, 1001, True)),
            "/dev/sdz1",
            "/run/syncmusic-test/.probe-sdz1",
        ],
    )
    assert ok(helper, ["mkdir", "-p", "/run/syncmusic-test/zorvex"])
    assert ok(helper, ["rmdir", "/run/syncmusic-test/zorvex"])
    assert ok(helper, ["chown", "1001:1001", "/run/syncmusic-test/zorvex"])
    assert ok(helper, ["umount", "/run/syncmusic-test/zorvex"])
    assert ok(helper, ["umount", "-l", "/run/syncmusic-test/zorvex"])
    assert ok(helper, ["wipefs", "-a", "-q", "/dev/sdz"])
    assert ok(helper, ["sfdisk", "--quiet", "--wipe", "always", "/dev/sdz"])
    for fs in ("vfat", "exfat", "ext4", "ntfs"):
        assert ok(helper, mkfs_command("/dev/sdz1", fs, "Мій stick"))
    from syncmusic.infra.blockdev.format import Geometry

    assert ok(
        helper,
        mkfs_command("/dev/sdz2", "vfat", "IPOD", Geometry(logical_sector=4096, start_512=2048)),
    )
    for fs in ("vfat", "exfat", "ext4", "ntfs"):  # Rename
        assert ok(helper, label_command("/dev/sdz1", fs, "Мій новий stick"))
    assert ("/dev/sdz1", "label") in helper.guard.calls
    assert ok(helper, ["sg_raw", "-s", "12", "/dev/sdz", *TIME_CDB])
    assert ok(helper, ["sg_inq", "--page=0xc0", "--raw", "/dev/sdz"])
    assert ok(helper, ["udisksctl", "power-off", "--no-user-interaction", "-b", "/dev/sdz"])
    assert ok(helper, ["eject", "/dev/sdz"])
    assert ok(helper, ["dmesg"])
    assert ok(helper, ["fsck.vfat", "-a", "/dev/sdz2"])  # repair
    assert ok(helper, ["fsck.exfat", "-y", "/dev/sdz1"])
    assert ("/dev/sdz2", "repair") in helper.guard.calls
    assert ("/dev/sdz1", "mount") in helper.guard.calls and (
        "/dev/sdz",
        "time_sync",
    ) in helper.guard.calls


def test_everything_else_is_refused(helper):
    vfat = ",".join(mnt.mount_options("vfat", 1001, 1001))
    for argv in (
        ["bash", "-c", "id"],
        [
            "mount",
            "-t",
            "vfat",
            "-o",
            vfat,
            "/dev/nvme0n1p2",
            "/run/syncmusic-test/x",
        ],  # the system disk
        ["mount", "-t", "vfat", "-o", vfat, "/dev/sdz1", "/etc"],  # outside the mount root
        [
            "mount",
            "-t",
            "vfat",
            "-o",
            vfat,
            "/dev/sdz1",
            "/run/syncmusic-test/a/b",
        ],  # not directly below
        ["mount", "-t", "vfat", "-o", vfat, "/dev/sdz1", "/run/syncmusic-test/.."],
        [
            "mount",
            "-t",
            "vfat",
            "-o",
            vfat.replace("uid=1001", "uid=0"),
            "/dev/sdz1",
            "/run/syncmusic-test/x",
        ],
        [
            "mount",
            "-t",
            "vfat",
            "-o",
            vfat.replace(",noexec", ""),
            "/dev/sdz1",
            "/run/syncmusic-test/x",
        ],
        ["mount", "-t", "vfat", "-o", vfat + ",suid", "/dev/sdz1", "/run/syncmusic-test/x"],
        ["mount", "-t", "proc", "-o", vfat, "/dev/sdz1", "/run/syncmusic-test/x"],
        ["mount", "--bind", "/", "/run/syncmusic-test/x"],
        ["mkdir", "-p", "/etc/cron.d"],
        ["rmdir", "/home/user"],
        ["chown", "0:0", "/run/syncmusic-test/x"],
        ["chown", "1001:1001", "/etc/shadow"],
        ["umount", "/"],
        ["umount", "-l", "/home"],
        ["wipefs", "-a", "-q", "/dev/nvme0n1"],
        ["wipefs", "-a", "/dev/sdz", "/dev/nvme0n1"],
        ["sfdisk", "--delete", "/dev/sdz"],
        ["mkfs.ext4", "-F", "/dev/nvme0n1p2"],
        ["mkfs.ext4", "-F", "-E", "root_owner=0:0", "/dev/sdz1"],
        ["mkfs.vfat", "-F", "32", "-n", "TOO LONG LABEL", "/dev/sdz1"],
        ["sg_raw", "-s", "12", "/dev/nvme0n1", *TIME_CDB],
        ["sg_raw", "-s", "12", "/dev/sdz", "3B", "-o", "/etc/passwd"],
        ["dmesg", "-C"],
        ["eject", "/dev/nvme0n1"],
        ["udisksctl", "mount", "-b", "/dev/sdz1"],
        ["fatlabel", "/dev/nvme0n1p2", "X"],  # the system disk
        ["e2label", "/dev/sdz1"],  # no label
        ["fatlabel", "/dev/sdz1", "TOO LONG LABEL"],
        ["ntfslabel", "/dev/sdz1", "--new-serial"],  # an option, not a label
        ["exfatlabel", "/dev/sdz1", "a", "b"],
        ["fsck.vfat", "-a", "/dev/nvme0n1p2"],  # the system disk
        ["fsck.vfat", "-a", "-r", "/dev/sdz1"],  # interactive
        ["fsck.ext4", "-y", "/dev/sdz1"],
        [],
    ):
        assert refused(helper, argv), argv


def test_umount_of_an_automount_of_an_accepted_device(helper, monkeypatch):
    stick = BlockDeviceInfo(
        name="sdz",
        path="/dev/sdz",
        children=[BlockDeviceInfo(name="sdz1", path="/dev/sdz1", mountpoints=["/media/u/STICK"])],
    )
    nvme = BlockDeviceInfo(name="nvme0n1", path="/dev/nvme0n1", mountpoints=["/boot/firmware"])
    monkeypatch.setattr(ph, "scan_block_devices", lambda *a: [stick, nvme])
    assert ok(helper, ["umount", "/media/u/STICK"])
    assert refused(helper, ["umount", "/boot/firmware"])
    # lazy detach of a busy automount (an explicit second step of "Unmount everywhere"): accepted devices only
    assert ok(helper, ["umount", "-l", "/media/u/STICK"])
    assert refused(helper, ["umount", "-l", "/boot/firmware"])


def test_main_refuses_without_root_and_runs_nothing(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(ph.os, "execv", lambda *a: ran.append(a))
    monkeypatch.setattr(ph.os, "geteuid", lambda: 1001)
    assert ph.main(["dmesg"]) == ph.REFUSED and ran == []
    monkeypatch.setattr(ph.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", "1001")
    monkeypatch.setenv("SUDO_GID", "1001")
    monkeypatch.setattr(ph, "Guard", lambda *a, **k: FakeGuard())
    monkeypatch.setattr(ph, "_which", lambda name: f"/usr/bin/{name}")
    assert ph.main(["rm", "-rf", "/"]) == ph.REFUSED and ran == []
    assert "refused" in capsys.readouterr().err
    ph.main(["dmesg"])
    assert ran == [("/usr/bin/dmesg", ["/usr/bin/dmesg"])]


def test_mount_root_from_the_root_owned_config(tmp_path):
    assert ph.mount_root(tmp_path / "missing.toml") == Path("/run/syncmusic")  # the default
    cfg = tmp_path / "priv.toml"
    cfg.write_text('mount_root = "/media/syncmusic"\n')
    assert ph.mount_root(cfg) == Path("/media/syncmusic")


@pytest.mark.parametrize(
    ("euid", "sudo", "allowed", "route"),
    [
        (0, False, {"syncmusic-priv"}, []),
        (1001, True, {"syncmusic-priv"}, ["sudo", "-n"]),
        (1001, False, {"syncmusic-priv"}, ["sudo", "-n", "syncmusic-priv"]),
        (1001, False, set(), ["sudo", "-n"]),
    ],
)
def test_privilege_route(monkeypatch, tmp_path, euid, sudo, allowed, route):
    (tmp_path / "syncmusic-priv").write_text("#!/bin/sh\n")
    monkeypatch.setattr(mnt, "_route", None)
    monkeypatch.setattr(mnt.os, "geteuid", lambda: euid)
    monkeypatch.setattr(mnt, "helper_path", lambda name=mnt.HELPER_NAME: tmp_path / name)

    def sudo_ok(*a):
        return sudo if a == ("true",) else Path(a[1]).name in allowed

    monkeypatch.setattr(mnt, "_sudo_ok", sudo_ok)
    expect = [str(tmp_path / x) if x.endswith("-priv") else x for x in route]
    assert mnt.privileged(["dmesg"]) == [*expect, "dmesg"]
    monkeypatch.setattr(mnt, "_route", None)


def test_whole_disk_fat_format_and_no_symlinked_mountpoint(monkeypatch, tmp_path):
    """Security review: the app's `mkfs.vfat -I` (whole-disk HiBy) passes; a mountpoint that is a link
    out of the mount root is refused (chown/mount would follow it)."""
    monkeypatch.setattr(ph, "_which", lambda name: f"/usr/bin/{name}")
    h = ph.Helper(FakeGuard(), tmp_path, 1001, 1001)
    assert ok(h, mkfs_command("/dev/sdz", "vfat", "HIBY", whole_disk=True))
    (tmp_path / "evil").symlink_to("/etc")
    assert refused(h, ["chown", "1001:1001", str(tmp_path / "evil")])
    assert ok(h, ["chown", "1001:1001", str(tmp_path / "stick")])
