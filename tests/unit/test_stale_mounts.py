"""Stale mounts after an unplug without eject: fake mount table and command runner."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from syncmusic.infra.blockdev import mount as mnt


class FakeKernel:
    """Mount table as a stack; `umount -l <path>` pops the top mount at that path."""

    def __init__(self, entries: list[mnt.MountEntry]) -> None:
        self.entries = list(entries)
        self.cmds: list[list[str]] = []

    def table(self) -> list[mnt.MountEntry]:
        return list(self.entries)

    async def run(self, cmd, **kw):
        self.cmds.append(cmd)
        if cmd[:2] == ["umount", "-l"]:
            top = [i for i, e in enumerate(self.entries) if e.path == cmd[2]]
            self.entries.pop(top[-1])
        return ""


@pytest.fixture
def live_dev(tmp_path):
    """A path standing in for a live /dev node: (path, its real major:minor)."""
    st = os.stat("/dev/null")
    return "/dev/null", f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"


def _kernel(monkeypatch, entries):
    k = FakeKernel(entries)
    monkeypatch.setattr(mnt, "mount_table", k.table)
    monkeypatch.setattr(mnt, "run_cmd", k.run)
    return k


def test_is_stale(live_dev):
    dev, majmin = live_dev
    assert not mnt.is_stale(mnt.MountEntry(majmin, "/x", dev))
    assert mnt.is_stale(mnt.MountEntry("8:1", "/x", "/dev/sd-gone-1"))  # node vanished
    assert mnt.is_stale(mnt.MountEntry("999:999", "/x", dev))  # node name reused by another device
    assert not mnt.is_stale(mnt.MountEntry("0:42", "/x", "tmpfs"))


def test_mount_table_parses_proc():
    table = mnt.mount_table()
    assert any(e.path == "/" for e in table)


async def test_drop_mount_detaches_stacked_mounts_of_removed_disk(monkeypatch, tmp_path):
    target = str(tmp_path / "mnt" / "flash")
    k = _kernel(
        monkeypatch,
        [
            mnt.MountEntry("8:1", target, "/dev/sd-gone-1"),  # an older stale one
            mnt.MountEntry("8:49", target, "/dev/sd-gone-2"),  # ours
            mnt.MountEntry("8:65", "/elsewhere", "/dev/sd-gone-3"),
        ],
    )
    await mnt.drop_mount(mnt.Mounted(Path(target), "/dev/sd-gone-2", "vfat", owned=True))
    assert [c for c in k.cmds if c[0] == "umount"] == [["umount", "-l", target]] * 2
    assert k.cmds[-1] == ["rmdir", target]
    assert [e.path for e in k.entries] == ["/elsewhere"]


async def test_drop_mount_detaches_ours_even_if_node_still_exists(monkeypatch, tmp_path, live_dev):
    dev, majmin = live_dev  # udev remove can arrive before the node disappears
    target = str(tmp_path / "flash")
    k = _kernel(monkeypatch, [mnt.MountEntry(majmin, target, dev)])
    await mnt.drop_mount(mnt.Mounted(Path(target), dev, "vfat", owned=True))
    assert ["umount", "-l", target] in k.cmds and not k.entries


async def test_drop_mount_leaves_reused_mounts_alone(monkeypatch, tmp_path):
    k = _kernel(monkeypatch, [mnt.MountEntry("8:1", "/media/x", "/dev/sd-gone-1")])
    await mnt.drop_mount(mnt.Mounted(Path("/media/x"), "/dev/sd-gone-1", "vfat", owned=False))
    assert k.cmds == [] and len(k.entries) == 1


async def test_detach_before_mount_only_removes_stale(monkeypatch, tmp_path, live_dev):
    dev, majmin = live_dev
    target = tmp_path / "flash"
    k = _kernel(monkeypatch, [mnt.MountEntry("8:1", str(target), "/dev/sd-gone-1")])
    assert await mnt._detach(target, lambda e: not mnt.is_stale(e)) == 1 and not k.entries
    k = _kernel(monkeypatch, [mnt.MountEntry(majmin, str(target), dev)])
    assert await mnt._detach(target, lambda e: not mnt.is_stale(e)) == 0 and k.entries


async def test_mount_detaches_stale_mount_on_target_first(monkeypatch, tmp_path):
    from syncmusic.infra.blockdev.identify import BlockDeviceInfo

    target = tmp_path / "root" / "flash"
    k = _kernel(monkeypatch, [mnt.MountEntry("8:1", str(target), "/dev/sd-gone-1")])

    class Guard:
        def check(self, *a, **kw):
            return None

    node = BlockDeviceInfo(name="sdd1", path="/dev/sdd1", fstype="vfat")
    monkeypatch.setattr(mnt, "_node", lambda p, disks: node)
    monkeypatch.setattr(mnt, "scan_block_devices", lambda *_: [])
    m = await mnt.mount(Guard(), "/dev/sdd1", "flash", tmp_path / "root")
    umount = k.cmds.index(["umount", "-l", str(target)])
    mount = next(i for i, c in enumerate(k.cmds) if c[0] == "mount")
    assert umount < mount and m.owned and m.path == target


async def test_mount_refuses_target_held_by_a_live_device(monkeypatch, tmp_path, live_dev):
    from syncmusic.infra.blockdev.identify import BlockDeviceInfo

    dev, majmin = live_dev
    target = tmp_path / "root" / "flash"
    k = _kernel(monkeypatch, [mnt.MountEntry(majmin, str(target), dev)])

    class Guard:
        def check(self, *a, **kw):
            return None

    monkeypatch.setattr(
        mnt, "_node", lambda p, disks: BlockDeviceInfo(name="sdd1", path="/dev/sdd1", fstype="vfat")
    )
    monkeypatch.setattr(mnt, "scan_block_devices", lambda *_: [])
    with pytest.raises(mnt.BlockdevError, match="already a mountpoint"):
        await mnt.mount(Guard(), "/dev/sdd1", "flash", tmp_path / "root")
    assert not any(c[0] == "mount" for c in k.cmds)


async def test_sync_service_drops_mount_when_disk_removed(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from syncmusic.core.sync import SyncService

    dropped = []

    async def drop(m):
        dropped.append(m)

    monkeypatch.setattr(mnt, "drop_mount", drop)
    removed = []

    async def disk_removed(name):
        removed.append(name)

    ours = mnt.Mounted(tmp_path / "flash", "/dev/sdd1", "vfat", owned=True)
    svc = SimpleNamespace(
        detector=SimpleNamespace(
            connected={"sdd": SimpleNamespace(device_id="flash")}, disk_removed=disk_removed
        ),
        _mounts={"flash": ours},
        _errors={"flash": "dirty"},
        _damaged={},
        _ejected={"flash"},
        _waiting={"flash": {"reason": "on_connect: ask", "plan_id": None}},
        _clock_set={"flash": 1.0},
        _clock_tasks={},
        drop_plans=lambda device_id: None,
        _mtp_open={},
    )
    svc._close_mtp = lambda device_id: SyncService._close_mtp(svc, device_id)  # type: ignore[attr-defined]
    await SyncService.on_disk_removed(svc, "sdd")
    assert removed == ["sdd"] and dropped == [ours] and svc._mounts == {}
    assert svc._waiting == {} and svc._ejected == set()
    assert svc._clock_set == {}  # a reconnected device gets its clock set again


def test_mount_table_keeps_non_ascii_paths(monkeypatch):
    """The kernel writes mount points as raw UTF-8 with only space/tab/newline/backslash octal-escaped: a Cyrillic
    device id must come back unchanged, or is_live() reports the mount lost mid-sync."""
    line = (
        "199 61 8:18 / /mnt/домашній-mini\\040два rw,relatime shared:339 - vfat /dev/sdb2 rw,utf8\n"
    )

    class Mountinfo:
        def read_text(self) -> str:
            return line

    monkeypatch.setattr(mnt, "Path", lambda _: Mountinfo())
    [entry] = mnt.mount_table()
    assert entry == mnt.MountEntry("8:18", "/mnt/домашній-mini два", "/dev/sdb2")


def test_is_live_with_non_ascii_and_escaped_mount_point(monkeypatch, tmp_path):
    """is_live() finds a Cyrillic mount point containing a space (\\040). A regular file stands in for
    the device node: its st_rdev is 0:0."""
    node = tmp_path / "sdb2"
    node.touch()
    line = f"199 61 0:0 / /mnt/домашній\\040mini rw - vfat {node} rw\n"

    class Mountinfo:
        def read_text(self) -> str:
            return line

    monkeypatch.setattr(mnt, "Path", lambda _: Mountinfo())
    m = mnt.Mounted(path=Path("/mnt/домашній mini"), dev_path=str(node), fstype="vfat", owned=True)
    assert mnt.is_live(m)


def test_unescape_mount_field_only_octal():
    assert mnt.unescape_mount_field("/a\\040b\\134c\\011d") == "/a b\\c\td"
    assert mnt.unescape_mount_field("/mnt/日本語-プレーヤー") == "/mnt/日本語-プレーヤー"
