# SPDX-License-Identifier: GPL-3.0-or-later
"""Block device watcher: udev events → "disk added / removed" callbacks.

A full scan at start reports disks that are already plugged in. udev `block` events are debounced per disk
(~2 s: a disk and its partitions appear in a burst, and a format produces several change events); after the
quiet period the disk is rescanned and reported as added (new, or its partitions / file systems changed) or
removed. Only USB disks are reported (plus loop devices when allowed, for tests with images), and MTP players(udev `usb_device` events, infra/blockdev/mtp.py) as pseudo disks of type "mtp".

`handle_uevent(action, disk_name)` is the entry point for events; the pyudev monitor feeds it from the event
loop (`start(use_udev=True)`), tests call it directly.

Neither a failing scan (no `lsblk`, a container without /sys) nor a udev monitor that cannot start (no pyudev, no
netlink access) stops the application: the problem is logged and kept in `status` for the device diagnostics(`GET /api/v1/devices/diagnostics`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from syncmusic.infra.blockdev.identify import BlockDeviceInfo, scan_block_devices
from syncmusic.infra.blockdev.mtp import MTP_TYPE, NAME_PREFIX, scan_mtp_devices

log = logging.getLogger(__name__)

DiskCallback = Callable[[BlockDeviceInfo], Awaitable[None]]
NameCallback = Callable[[str], Awaitable[None]]


def scan_usb_devices() -> list[BlockDeviceInfo]:
    """The disks (lsblk) and the MTP players (udev) that are plugged in."""
    return [*scan_block_devices(), *scan_mtp_devices()]


def _fingerprint(d: BlockDeviceInfo) -> tuple[Any, ...]:
    return (
        d.serial,
        d.size,
        d.fstype,
        d.uuid,
        tuple((c.name, c.fstype, c.uuid, c.label, c.size) for c in d.children),
    )


class BlockWatcher:
    def __init__(
        self,
        on_added: DiskCallback,
        on_removed: NameCallback,
        *,
        scan: Callable[[], list[BlockDeviceInfo]] = scan_usb_devices,
        debounce_s: float = 2.0,
        include_loop: bool = False,
    ) -> None:
        self.on_added = on_added
        self.on_removed = on_removed
        self.scan = scan
        self.debounce_s = debounce_s
        self.include_loop = include_loop
        self.known: dict[str, tuple[Any, ...]] = {}  # disk name -> fingerprint
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._monitor: Any = None
        self._lock = asyncio.Lock()
        # diagnostics: udev = "running" / "off" / "unavailable: <why>"; scan_error = last scan failure
        self.status: dict[str, Any] = {"udev": "off", "scan_error": None, "last_event": None}

    def wanted(self, d: BlockDeviceInfo) -> bool:
        if d.type == MTP_TYPE:  # an MTP player
            return True
        if d.type == "loop":  # emulated test devices report usb too: only when loop devices are on
            return self.include_loop and d.fstype != "swap" and bool(d.children or d.fstype)
        return d.tran == "usb" and d.is_disk

    async def _scan(self) -> dict[str, BlockDeviceInfo] | None:
        """Wanted disks by name; None when the scan itself failed (nothing is reported removed then)."""
        try:
            disks = await asyncio.to_thread(self.scan)
        except Exception as e:  # noqa: BLE001 - lsblk missing / failing must not stop the app
            if self.status["scan_error"] != str(e):
                log.error("block device scan failed, no devices can be detected: %s", e)
            self.status["scan_error"] = str(e)
            return None
        self.status["scan_error"] = None
        return {d.name: d for d in disks if self.wanted(d)}

    async def start(self, *, use_udev: bool = True) -> None:
        """Startup scan (reports every present disk), then follow udev events."""
        await self.rescan()
        if use_udev:
            self._start_udev()

    async def rescan(self, only: str | None = None) -> None:
        async with self._lock:
            current = await self._scan()
            if current is None:
                return
            names = [only] if only else sorted(set(current) | set(self.known))
            for name in names:
                d = current.get(name)
                if d is None:
                    if self.known.pop(name, None) is not None:
                        await self._call(self.on_removed(name), name)
                    continue
                fp = _fingerprint(d)
                if self.known.get(name) != fp:
                    self.known[name] = fp
                    await self._call(self.on_added(d), name)

    async def _call(self, coro: Awaitable[None], name: str) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001 - one bad disk must not stop the watcher
            log.exception("device callback for %s failed", name)

    def handle_uevent(self, action: str, disk_name: str) -> None:
        """Schedule a debounced rescan of `disk_name` (action: add / change / remove)."""
        old = self._timers.pop(disk_name, None)
        if old is not None:
            old.cancel()
        self._timers[disk_name] = asyncio.get_running_loop().create_task(self._debounced(disk_name))
        self.status["last_event"] = f"{action} {disk_name}"
        log.debug("uevent %s %s", action, disk_name)

    async def _debounced(self, name: str) -> None:
        await asyncio.sleep(self.debounce_s)
        self._timers.pop(name, None)
        await self.rescan(only=name)

    async def settle(self) -> None:
        """Wait until every pending debounce has run (tests)."""
        while self._timers:
            await asyncio.gather(*list(self._timers.values()), return_exceptions=True)

    def _start_udev(self) -> None:
        try:
            import pyudev
        except ImportError:
            log.warning("pyudev not available: no hot-plug detection (startup scan only)")
            self.status["udev"] = "unavailable: pyudev is not installed"
            return
        try:
            context = pyudev.Context()
            monitor = pyudev.Monitor.from_netlink(context)
            monitor.filter_by("block")
            monitor.filter_by("usb", "usb_device")  # MTP players
            monitor.start()
        except Exception as e:  # noqa: BLE001 - e.g. no netlink in a container: startup scan only
            log.warning("udev monitor could not start, no hot-plug detection: %s", e)
            self.status["udev"] = f"unavailable: {e}"
            return
        self._monitor = monitor
        self.status["udev"] = "running"
        loop = asyncio.get_running_loop()

        def readable() -> None:
            while True:
                dev = monitor.poll(timeout=0)
                if dev is None:
                    return
                if dev.subsystem == "usb":  # rescanned: an MTP player appears or is gone
                    self.handle_uevent(str(dev.action), NAME_PREFIX + str(dev.sys_name))
                    continue
                disk = dev if dev.device_type == "disk" else dev.find_parent("block", "disk")
                if disk is not None:
                    self.handle_uevent(str(dev.action), str(disk.sys_name))

        loop.add_reader(monitor.fileno(), readable)

    async def stop(self) -> None:
        if self._monitor is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self._monitor.fileno())
            self._monitor = None
        for t in self._timers.values():
            t.cancel()
        await asyncio.gather(*self._timers.values(), return_exceptions=True)
        self._timers.clear()
