"""Hardware tests: no test may leave a partition of an allowlisted device mounted; a running SyncMusic
is stopped while they run.

After every test (also a failed one) the allowlisted disks (USB serials from tests/hardware.toml) are checked; a
leftover mount is unmounted, so the next test starts clean, and the test errors in teardown naming it. A mount
that already existed before the test is unmounted first and not blamed on it.
"""

from __future__ import annotations

import os
import subprocess
import tomllib

import pytest

from syncmusic.infra.blockdev.identify import scan_block_devices
from tests.conftest import HARDWARE_TOML


def _allowlist() -> dict:
    return tomllib.loads(HARDWARE_TOML.read_text()) if HARDWARE_TOML.is_file() else {}


# The modules look up the sections of a players at import; a checkout whose allowlist has none of them
# (a fresh clone: empty tests/hardware.toml) collects no hardware test at all.
if not any(isinstance(e, dict) for e in _allowlist().values()):
    collect_ignore_glob = ["test_*.py"]


def _allowlisted_serials() -> set[str]:
    entries = _allowlist().values()
    return {e["usb_serial"] for e in entries if isinstance(e, dict) and e.get("usb_serial")}


def kind_of(entry: dict) -> str:
    """The kind of player an allowlist section is, from its model: "ipod" (a stock iPod Classic), "rockbox" (an
    iPod mini with Rockbox), "hiby" (a HiBy R1) or "flash" (no model: a USB drive the tests may reformat)."""
    model = str(entry.get("model", ""))
    if model.startswith("classic"):
        return "ipod"
    if model.startswith("mini"):
        return "rockbox"
    if model.startswith("hiby"):
        return "hiby"
    return "flash"


def sections(kind: str) -> list[str]:
    """The hardware.toml sections of one kind of player (each device's tests run once per section)."""
    return [n for n, e in _allowlist().items() if isinstance(e, dict) and kind_of(e) == kind]


CLASSIC_SECTIONS = sections("ipod")
"""hardware.toml sections of the stock-firmware iPod Classics."""


def hw_value(section: str, key: str) -> str:
    """`key` of an allowlist section; "" when the section is not there (that device's tests skip)."""
    return str(_allowlist().get(section, {}).get(key, ""))


def classic_serial(section: str) -> str:
    return hw_value(section, "usb_serial")


def serials_of(section: str) -> set[str]:
    """Every USB serial a section's device shows (`usb_serial` and `usb_serial_aliases`)."""
    entry = _allowlist().get(section, {})
    return {s for s in [entry.get("usb_serial"), *entry.get("usb_serial_aliases", [])] if s}


def connected(section: str):
    """The section's device as the scan sees it now, or None (not connected)."""
    serials = serials_of(section)
    fs_uuid = (
        hw_value(section, "fs_uuid") if kind_of(_allowlist().get(section, {})) == "hiby" else ""
    )
    for disk in scan_block_devices():
        if disk.serial in serials and (
            not fs_uuid or fs_uuid in {disk.uuid, *(c.uuid for c in disk.children)}
        ):
            return disk
    return None


def _disk_fixture(kind: str, what: str):
    @pytest.fixture(
        params=sections(kind)
        or [pytest.param(None, marks=pytest.mark.skip(reason=f"no {what} in tests/hardware.toml"))]
    )
    def fixture(request):
        disk = connected(request.param)
        if disk is None:
            pytest.skip(f"allowlisted {what} [{request.param}] not connected")
        return disk

    fixture.__doc__ = (
        f"The connected allowlisted {what} of one hardware.toml section (once per section)."
    )
    return fixture


# a flash drive or an iPod Classic: the scenarios that run on either (Player contents, Rename), once per device
FLASH_OR_CLASSIC = [*sections("flash"), *sections("ipod")]


@pytest.fixture(
    params=FLASH_OR_CLASSIC
    or [
        pytest.param(
            None,
            marks=pytest.mark.skip(reason="no flash drive or iPod Classic in tests/hardware.toml"),
        )
    ]
)
def flash_or_classic_disk(request):
    """The connected flash drive or iPod Classic of one hardware.toml section (once per section)."""
    disk = connected(request.param)
    if disk is None:
        pytest.skip(f"allowlisted [{request.param}] not connected")
    return disk


classic_disk = _disk_fixture("ipod", "iPod Classic")
mini_disk = _disk_fixture("rockbox", "iPod mini with Rockbox")
flash_disk = _disk_fixture("flash", "flash drive")
hiby_disk = _disk_fixture("hiby", "HiBy R1")


def allowlisted_mounts() -> list[tuple[str, str]]:
    """(device path, mountpoint) of every mounted filesystem on an allowlisted USB disk."""
    serials = _allowlisted_serials()
    out: list[tuple[str, str]] = []
    for disk in scan_block_devices(with_udev=False):
        if disk.tran == "usb" and disk.serial in serials:
            for node in (disk, *disk.children):
                out += [(node.path, mp) for mp in node.mountpoints]
    return out


async def quiesce(app, device_id: str, timeout: float = 120.0) -> None:
    """Before a test's last unmount + fsck: no automatic update sync may mount the device again.
    fsck.fat reads the Classic's 2 × 121 MB FATs for ~17 s; an update sync on connect / after a library reload
    that mounts meanwhile rewrites iTunesDB and the manifest under it, so fsck pairs its old FAT with the new
    directory entries and reports their clusters free (with the dirty bit). Turns the device's update syncs off
    and waits for a running one and the device's jobs to end."""
    import asyncio
    import time

    cfg = await app.sync.config(device_id)
    if cfg.update_sync:
        await app.sync.update_device(cfg.model_copy(update={"update_sync": False}))
    end = time.monotonic() + timeout
    while app.sync._update_tasks or app.jobs.device_busy(device_id):
        assert time.monotonic() < end, "an update sync or job of the device did not end"
        await asyncio.sleep(0.5)


def _unmount(mounts: list[tuple[str, str]]) -> None:
    subprocess.run(["sync"], check=False)
    for _, mp in mounts:
        subprocess.run(["sudo", "-n", "umount", mp], check=False, capture_output=True)


@pytest.fixture(autouse=True)
def no_leftover_mounts():
    _unmount(allowlisted_mounts())
    yield
    left = allowlisted_mounts()
    if left:
        _unmount(left)
        still = allowlisted_mounts()
        pytest.fail(f"the test left allowlisted devices mounted: {left} (still mounted: {still})")


@pytest.fixture(scope="session", autouse=True)
def hardware_compose_stack(compose_stack):
    """Most hardware tests sync from the 'nd' Navidrome library of the compose stack (on the generated test
    media); without it they fail with "Subsonic server not reachable at http://127.0.0.1:4533". Started once per
    run."""
    return compose_stack


# an installed SyncMusic service that would claim the devices too
LIVE_UNIT = os.environ.get("SM_HARDWARE_STOP_UNIT", "syncmusic")


@pytest.fixture(scope="session", autouse=True)
def live_instance_stopped():
    """Only one process may own the devices: a running SyncMusic service is stopped for the hardware
    run and started again afterwards."""
    active = (
        subprocess.run(["systemctl", "is-active", "--quiet", LIVE_UNIT], check=False).returncode
        == 0
    )
    if active:
        subprocess.run(["sudo", "-n", "systemctl", "stop", LIVE_UNIT], check=True)
    yield
    if active:
        subprocess.run(["sudo", "-n", "systemctl", "start", LIVE_UNIT], check=True)
