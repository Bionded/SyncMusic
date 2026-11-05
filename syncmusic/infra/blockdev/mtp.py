# SPDX-License-Identifier: GPL-3.0-or-later
"""MTP players on USB: udev `usb_device` entries with `ID_MTP_DEVICE=1` → BlockDeviceInfo.

An Android phone or player is no block device, but it is detected like one, so the dashboard and the Register page
list it among the found devices and a registered one is recognised when it is plugged in (by its USB serial). The
entry is a pseudo disk: `type = "mtp"`, `name = "mtp-<sysfs name>"` (e.g. `mtp-1-1`, the USB port), `path` its
sysfs folder, no partitions - nothing mounts, probes or formats it. Only udev is read: no MTP session is opened to
an unknown device: never open one to a phone).

The USB serial udev reports (`ID_SERIAL_SHORT`) is not always the one libmtp reports (MTP DeviceInfo): a device's
identity holds both (`usb_serial` + `usb_serial_aliases`); `usb_addresses` maps bus addresses to udev serials so
the libmtp backend opens only the wanted device.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from syncmusic.infra.blockdev.identify import BlockDeviceInfo

log = logging.getLogger(__name__)

MTP_TYPE = "mtp"
NAME_PREFIX = "mtp-"


def _usb_devices() -> list[tuple[str, Mapping[str, str]]]:
    """(sysfs name, udev properties) of every USB device that udev marks as MTP."""
    import pyudev

    context = pyudev.Context()
    out = []
    for dev in context.list_devices(subsystem="usb", DEVTYPE="usb_device"):
        props = dict(dev.properties)
        if props.get("ID_MTP_DEVICE") == "1":
            out.append((str(dev.sys_name), props))
    return out


def mtp_info(sys_name: str, props: Mapping[str, str]) -> BlockDeviceInfo:
    def text(key: str) -> str | None:
        v = props.get(key)
        return v.replace("_", " ").strip() or None if v else None

    return BlockDeviceInfo(
        name=NAME_PREFIX + sys_name,
        path=f"/sys/bus/usb/devices/{sys_name}",
        type=MTP_TYPE,
        tran="usb",
        rm=True,
        hotplug=True,
        model=text("ID_MODEL_FROM_DATABASE") or text("ID_MODEL"),
        vendor=text("ID_VENDOR_FROM_DATABASE") or text("ID_VENDOR"),
        serial=props.get("ID_SERIAL_SHORT") or None,
        usb_vendor_id=props.get("ID_VENDOR_ID") or None,
        usb_product_id=props.get("ID_MODEL_ID") or None,
    )


def scan_mtp_devices(
    devices: Iterable[tuple[str, Mapping[str, str]]] | None = None,
) -> list[BlockDeviceInfo]:
    """The MTP players on USB now; [] when udev cannot be read (no pyudev, a container without /sys)."""
    if devices is None:
        try:
            devices = _usb_devices()
        except Exception as e:  # noqa: BLE001 - MTP detection is optional: disks are still found
            log.debug("MTP scan unavailable: %s", e)
            return []
    return [mtp_info(name, props) for name, props in devices]


def usb_addresses(
    devices: Iterable[tuple[str, Mapping[str, str]]] | None = None,
) -> dict[tuple[int, int], str] | None:
    """(bus number, device number) → udev USB serial of every MTP device; None when udev cannot be read."""
    try:
        found = list(devices) if devices is not None else _usb_devices()
    except Exception:  # noqa: BLE001 - no udev: the caller falls back to asking every device
        return None
    out = {}
    for _, props in found:
        try:
            out[int(props["BUSNUM"]), int(props["DEVNUM"])] = props.get("ID_SERIAL_SHORT", "")
        except (KeyError, ValueError):
            continue
    return out


def is_mtp(disk: Any) -> bool:
    return getattr(disk, "type", None) == MTP_TYPE
