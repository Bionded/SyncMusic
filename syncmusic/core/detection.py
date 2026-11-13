# SPDX-License-Identifier: GPL-3.0-or-later
"""Device detection (§3, §6): which connected disk is which registered device.

The watcher reports disks; the `Detector` matches each one to a registered `DeviceConfig` - by USB serial,
then by the on-device manifest's device id, then by the data partition's filesystem UUID - and publishes
`device.connected` (registered) or `device.unknown` (with every device class's probe result, best first) on
the EventBus; `device.removed` when it goes away. The manifest id is read from a partition that is already
mounted (desktop automount) or, when the USB serial did not match, through `manifest_probe` - the sync service
mounts the data partition read-only for that (guarded, unmounted right after) and,
while it is mounted, also asks every device class's probe on the mounted tree: an unknown iPod with `/.rockbox`
is then offered as Rockbox, not as a stock iPod.
What happens next (`on_connect` auto/ask/manual) is up to the sync service.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from syncmusic.base.device import ProbeResult
from syncmusic.core import manifest as manifest_io
from syncmusic.core import registry
from syncmusic.core.models import DeviceConfig
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from syncmusic.infra.blockdev.mtp import is_mtp
from syncmusic.infra.events import EventBus

log = logging.getLogger(__name__)


@dataclass
class ConnectedDisk:
    disk: BlockDeviceInfo
    device_id: str | None = None  # registered device, None = unknown
    matched_by: str | None = None  # "usb_serial" | "manifest" | "fs_uuid"
    probes: list[ProbeResult] = field(default_factory=list)
    data_partition: str | None = None  # /dev path to mount
    mountpoint: Path | None = None  # existing mount of the data partition (automount), if any
    firmware_switch: str | None = (
        None  # a sibling type that now claims the disk better (player reflashed)
    )


def _nodes(disk: BlockDeviceInfo) -> list[BlockDeviceInfo]:
    return [disk, *disk.children]


def existing_mount(disk: BlockDeviceInfo) -> tuple[BlockDeviceInfo, Path] | None:
    for n in _nodes(disk):
        if n.mountpoints:
            return n, Path(n.mountpoints[0])
    return None


@dataclass
class MountedProbe:
    """What a read-only probe mount of an unknown disk found: the manifest id and the probes on the mount."""

    manifest_id: str | None = None
    probes: list[ProbeResult] = field(default_factory=list)


ManifestProbe = Callable[[BlockDeviceInfo], Awaitable[MountedProbe | None]]


def manifest_id_at(root: Path) -> str | None:
    """Device id from the manifest under a mounted device root (read only); None if absent/unreadable."""
    try:
        header = manifest_io.read_header(
            root
        )  # the header only: a big manifest is never parsed here
    except (manifest_io.ManifestError, OSError) as e:
        log.info("%s: manifest unreadable (%s)", root, e)
        return None
    if header is None:
        return None
    return header.device_uid or header.device_id


def manifest_device_id(disk: BlockDeviceInfo) -> str | None:
    """Device id from the on-device manifest, only if a partition is already mounted (read-only access)."""
    found = existing_mount(disk)
    if found is None:
        return None
    return manifest_id_at(found[1])


def match_registered(
    disk: BlockDeviceInfo, configs: list[DeviceConfig], manifest_id: str | None = None
) -> tuple[DeviceConfig, str] | None:
    """(config, matched_by) for the first match in the order usb_serial → manifest id → fs uuid."""
    if disk.serial:
        for c in configs:
            if disk.serial in c.identity.serials():
                return c, "usb_serial"
    if manifest_id:
        for c in configs:
            if manifest_id in (c.id, c.identity.manifest_device_id):
                return c, "manifest"
    uuids = {n.uuid for n in _nodes(disk) if n.uuid}
    for c in configs:
        if c.identity.fs_uuid and c.identity.fs_uuid in uuids:
            return c, "fs_uuid"
    return None


def probe_disk(disk: BlockDeviceInfo, mount: Path | None = None) -> list[ProbeResult]:
    """Ask every registered device class; results ordered by confidence (best first)."""
    results: list[ProbeResult] = []
    for cls in registry.device_classes():
        if getattr(cls, "usb_mtp", False) != is_mtp(
            disk
        ):  # an MTP player only to the MTP classes, disks only to the others
            continue
        try:
            r = cls.probe(disk, mount)
        except Exception:  # noqa: BLE001 - a buggy probe must not hide the disk
            log.exception("%s.probe failed on %s", cls.__name__, disk.path)
            continue
        if r is not None:
            results.append(r)
    return sorted(results, key=lambda r: -r.confidence)


def firmware_switch(
    type_name: str, own: ProbeResult | None, disk: BlockDeviceInfo, mount: Path | None
) -> str | None:
    """The sibling type (`firmware_siblings`, e.g. the same player with another firmware) whose probe claims
    the disk with a higher confidence than the registered type's own probe; None when the type still fits."""
    try:
        siblings = registry.device_class(type_name).firmware_siblings
    except Exception:  # noqa: BLE001 - unknown type
        return None
    best, name = own.confidence if own else 0, None
    for sibling in siblings:
        try:
            r = registry.device_class(sibling).probe(disk, mount)
        except Exception:  # noqa: BLE001 - a buggy probe must not break matching
            log.debug("probe of %s as %s failed", disk.path, sibling, exc_info=True)
            continue
        if r is not None and r.confidence > best:
            best, name = r.confidence, sibling
    return name


def data_partition(disk: BlockDeviceInfo, probe: ProbeResult | None) -> str | None:
    if probe is not None and probe.data_partition:
        return probe.data_partition
    for n in [*disk.partitions(), disk]:
        if n.fstype and n.fstype != "swap":
            return n.path
    return None


class Detector:
    def __init__(
        self,
        bus: EventBus,
        configs: Callable[[], list[DeviceConfig]],
        manifest_probe: ManifestProbe | None = None,
    ) -> None:
        self.bus = bus
        self.configs = configs
        self.manifest_probe = manifest_probe
        self.connected: dict[str, ConnectedDisk] = {}  # by disk name

    async def _find_match(
        self, disk: BlockDeviceInfo
    ) -> tuple[tuple[DeviceConfig, str] | None, list[ProbeResult]]:
        """(match_registered with the manifest id, probes from a probe mount). An unmounted disk is
        probe-mounted only when the USB serial did not already match (the cheap, unambiguous case)."""
        configs = self.configs()
        if is_mtp(disk):  # an MTP player: matched by its USB serial only, never probe-mounted
            return match_registered(disk, configs), []
        manifest_id = await asyncio.to_thread(manifest_device_id, disk)  # file I/O off the loop
        mounted_probes: list[ProbeResult] = []
        if manifest_id is None and existing_mount(disk) is None and self.manifest_probe is not None:
            match = match_registered(disk, configs)
            if match is not None and match[1] == "usb_serial":
                return match, []
            try:
                found = await self.manifest_probe(disk)
            except Exception:  # noqa: BLE001 - a failed probe must not hide the disk
                log.info("manifest probe of %s failed", disk.path, exc_info=True)
                found = None
            if found is not None:
                manifest_id, mounted_probes = found.manifest_id, found.probes
        return match_registered(disk, configs, manifest_id), mounted_probes

    def by_device(self, device_id: str) -> ConnectedDisk | None:
        return next((c for c in self.connected.values() if c.device_id == device_id), None)

    async def disk_added(self, disk: BlockDeviceInfo) -> ConnectedDisk:
        found = existing_mount(disk)
        mount = found[1] if found else None
        cd = ConnectedDisk(disk=disk, mountpoint=mount)
        self._match(cd, *await self._find_match(disk))
        previous = self.connected.get(disk.name)
        self.connected[disk.name] = cd
        if previous is not None and previous.device_id == cd.device_id:
            self.bus.publish("device.changed", disk=disk.path, device_id=cd.device_id)
        else:
            self._announce(cd)
        return cd

    async def rematch(self) -> None:
        """Match every connected disk again after configs changed (register, update, delete):
        a deleted device's disk becomes unknown (with probes), a newly registered one is matched.
        Disks whose match is unchanged are left alone."""
        for cd in list(self.connected.values()):
            match, mounted_probes = await self._find_match(cd.disk)
            if (match[0].id if match else None) != cd.device_id:
                self._match(cd, match, mounted_probes)
                self._announce(cd)

    def _match(
        self,
        cd: ConnectedDisk,
        match: tuple[DeviceConfig, str] | None,
        mounted_probes: list[ProbeResult] | None = None,
    ) -> None:
        """Fill device_id / matched_by / probes / data_partition of `cd` from `match` (see _find_match).
        An unknown disk gets the probes of a probe mount when they found anything (they see the content:
        `/.rockbox`, `iPod_Control`), else the probes on the bare disk."""
        disk, mount = cd.disk, cd.mountpoint
        if match is not None:
            cfg, how = match
            cls_probe = None
            try:
                cls_probe = registry.device_class(cfg.type).probe(disk, mount)
            except Exception:  # noqa: BLE001 - unknown type / probe error: fall back to partitions
                log.debug("probe of %s as %s failed", disk.path, cfg.type, exc_info=True)
            cd.device_id, cd.matched_by = cfg.id, how
            cd.firmware_switch = firmware_switch(cfg.type, cls_probe, disk, mount)
            cd.probes = [cls_probe] if cls_probe else []
            cd.data_partition = data_partition(disk, cls_probe)
        else:
            cd.device_id, cd.matched_by, cd.firmware_switch = None, None, None
            cd.probes = mounted_probes or probe_disk(disk, mount)
            cd.data_partition = data_partition(disk, cd.probes[0] if cd.probes else None)

    def _announce(self, cd: ConnectedDisk) -> None:
        disk = cd.disk
        if cd.device_id is not None:
            self.bus.publish(
                "device.connected", device_id=cd.device_id, disk=disk.path, matched_by=cd.matched_by
            )
        else:
            self.bus.publish(
                "device.unknown",
                disk=disk.path,
                model=disk.model,
                vendor=disk.vendor,
                probes=[p.model_dump(mode="json") for p in cd.probes],
            )

    async def disk_removed(self, name: str) -> None:
        cd = self.connected.pop(name, None)
        if cd is not None:
            self.bus.publish("device.removed", disk=cd.disk.path, device_id=cd.device_id)
