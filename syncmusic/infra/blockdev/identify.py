# SPDX-License-Identifier: GPL-3.0-or-later
"""Block device inventory: `lsblk --json` + udev properties → BlockDeviceInfo trees."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

LSBLK_COLUMNS = (
    "NAME,PATH,SIZE,TRAN,RM,HOTPLUG,MODEL,VENDOR,SERIAL,FSTYPE,LABEL,UUID,PARTTYPE,"
    "MOUNTPOINTS,TYPE,PKNAME"
)


class BlockDeviceInfo(BaseModel):
    """One disk or partition as reported by lsblk (disk entries carry their partitions)."""

    name: str
    path: str
    size: int = 0
    type: str = "disk"  # disk | part | loop | rom ...
    tran: str | None = None
    rm: bool = False
    hotplug: bool = False
    model: str | None = None
    vendor: str | None = None
    serial: str | None = None
    fstype: str | None = None
    label: str | None = None
    uuid: str | None = None
    parttype: str | None = None
    mountpoints: list[str] = []
    pkname: str | None = None
    usb_vendor_id: str | None = None
    usb_product_id: str | None = None
    children: list[BlockDeviceInfo] = Field(default_factory=list)

    @property
    def is_disk(self) -> bool:
        return self.type in ("disk", "loop")

    def partitions(self) -> list[BlockDeviceInfo]:
        """The partitions by partition number: right after a re-read lsblk may list p2 before p1."""
        parts = [c for c in self.children if c.type == "part"]
        return sorted(parts, key=lambda p: _partition_number(p.name))

    def all_mountpoints(self) -> list[str]:
        out = list(self.mountpoints)
        for c in self.children:
            out.extend(c.all_mountpoints())
        return out


def _partition_number(name: str) -> int:
    """The trailing number of a partition name: sdb10 → 10, loop0p2 → 2, mmcblk0p1 → 1."""
    digits = name[len(name.rstrip("0123456789")) :]
    return int(digits) if digits else 0


def _parse_node(node: dict[str, object], parent: str | None = None) -> BlockDeviceInfo:
    def s(key: str) -> str | None:
        v = node.get(key)
        return str(v).strip() if v not in (None, "") else None

    def b(key: str) -> bool:
        v = node.get(key)
        return v in (True, 1, "1", "true")

    mps = node.get("mountpoints") or []
    info = BlockDeviceInfo(
        name=str(node["name"]),
        path=str(node.get("path") or f"/dev/{node['name']}"),
        size=int(node.get("size") or 0),  # type: ignore[call-overload]
        type=s("type") or "disk",
        tran=s("tran"),
        rm=b("rm"),
        hotplug=b("hotplug"),
        model=s("model"),
        vendor=s("vendor"),
        serial=s("serial"),
        fstype=s("fstype"),
        label=s("label"),
        uuid=s("uuid"),
        parttype=s("parttype"),
        mountpoints=[str(m) for m in mps if m] if isinstance(mps, list) else [],
        pkname=s("pkname") or parent,
    )
    kids = node.get("children") or []
    if isinstance(kids, list):
        for child in kids:
            c = _parse_node(child, info.name)
            # lsblk reports TRAN/RM only reliably on the disk; inherit for partitions.
            c.tran = c.tran or info.tran
            info.children.append(c)
    return info


def parse_lsblk(data: str | dict[str, object]) -> list[BlockDeviceInfo]:
    doc = json.loads(data) if isinstance(data, str) else data
    nodes = doc.get("blockdevices") or []
    assert isinstance(nodes, list)
    return [_parse_node(n) for n in nodes]


def udev_properties(dev_path: str) -> dict[str, str]:
    try:
        out = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={dev_path}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    props: dict[str, str] = {}
    for line in out.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            props[k] = v
    return props


def scan_block_devices(with_udev: bool = True) -> list[BlockDeviceInfo]:
    """All top-level disks (with partitions) currently present."""
    out = subprocess.run(
        ["lsblk", "--json", "-b", "-o", LSBLK_COLUMNS],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    ).stdout
    disks = parse_lsblk(out)
    if with_udev:
        for d in disks:
            if d.tran == "usb":
                props = udev_properties(d.path)
                d.usb_vendor_id = props.get("ID_VENDOR_ID") or props.get("ID_USB_VENDOR_ID")
                d.usb_product_id = props.get("ID_MODEL_ID") or props.get("ID_USB_MODEL_ID")
                d.serial = props.get("ID_SERIAL_SHORT") or d.serial
    for d in disks:
        if d.type == "loop":
            _emulated_identity(d)
    return disks


def _emulated_identity(disk: BlockDeviceInfo) -> None:
    """Emulated test devices: a loop image with a `<image>.device.json` sidecar gets the USB identity
    written there (`serial`, `usb_vendor_id`, `usb_product_id`, `vendor`, `model`). The guard still refuses loop
    devices unless loop devices are allowed and the allowlist names this serial (tests/emulated.toml)."""
    try:
        backing = Path(f"/sys/block/{disk.name}/loop/backing_file").read_text().strip()
        doc = json.loads(Path(backing + ".device.json").read_text())
    except (OSError, ValueError):
        return
    if not isinstance(doc, dict):
        return
    disk.serial = doc.get("serial") or disk.serial
    disk.usb_vendor_id = doc.get("usb_vendor_id") or disk.usb_vendor_id
    disk.usb_product_id = doc.get("usb_product_id") or disk.usb_product_id
    disk.vendor = doc.get("vendor") or disk.vendor
    disk.model = doc.get("model") or disk.model
    disk.rm = True
    disk.tran = "usb"  # the device classes probe USB disks only; the watcher still skips loops unless enabled
    for c in disk.children:
        c.tran = "usb"


def find_disk_of(
    path: str, disks: list[BlockDeviceInfo]
) -> tuple[BlockDeviceInfo, BlockDeviceInfo]:
    """Return (disk, node) for a device path (disk or partition). Raises KeyError if unknown."""
    real = str(Path(path).resolve()) if path.startswith("/dev/") else path
    for d in disks:
        if real in (d.path, f"/dev/{d.name}"):
            return d, d
        for c in d.children:
            if real in (c.path, f"/dev/{c.name}"):
                return d, c
    raise KeyError(path)
