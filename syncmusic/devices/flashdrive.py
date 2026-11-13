# SPDX-License-Identifier: GPL-3.0-or-later
"""USB flash drive: a plain file tree for whatever player the drive is plugged into.

One-way only (no feedback). The target player is unknown, so the playable formats come from the settings
(the user narrows them); path rules come from the mounted filesystem (FAT32 4 GiB limit, forbidden chars,
UTF-16 name length; exFAT; NTFS; ext4 case-sensitive). Probe: any USB disk not claimed by another class
(lowest confidence) except Apple devices (USB vendor 05ac: iPods, stock or Rockbox), or a mounted tree that
already carries our `/.syncmusic/` state dir.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from syncmusic.base.capabilities import DeviceCapabilities, PathRules
from syncmusic.base.device import Device, ProbeResult
from syncmusic.core.manifest import STATE_DIR
from syncmusic.core.models import DeviceIdentity
from syncmusic.devices._filedevice import FileDeviceMixin
from syncmusic.devices.paths import PathLayout, clean_folder
from syncmusic.infra.blockdev.format import FILESYSTEMS, format_disk
from syncmusic.infra.blockdev.identify import BlockDeviceInfo, find_disk_of, scan_block_devices
from syncmusic.infra.blockdev.mount import mount

# markers of trees that belong to another device class
FOREIGN_MARKERS = (".rockbox", "iPod_Control")
APPLE_USB_VENDOR = (
    "05ac"  # every iPod in disk mode (also CF/SSD-modded ones: the bridge is Apple's)
)


class FlashDriveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # playable formats; empty = no limit (a backup disk, or a player that plays what the library has)
    formats: set[str] = set()
    # what the disk is for: a player or car stick, or a backup that keeps the library files as they are
    # (the Register flow sets no processors, additive mode and a base folder for a backup)
    purpose: Literal["player", "backup"] = "player"
    layout: PathLayout = PathLayout()
    # the folder on the stick the path templates start in; "" = the stick's root. A change moves
    # the files on the next sync
    base_folder: str = ""
    filesystem: str | None = None  # override the detected filesystem (path rules)
    ascii_only: bool = False  # transliterate names for players without Unicode fonts
    # car stereos and old players: ASCII names and short paths; a change renames on the next sync
    car_mode: bool = False
    max_path_len: int = Field(128, ge=32, le=240)  # characters of the whole path, in car mode
    max_name_len: int = Field(
        64, ge=16, le=255
    )  # characters of one file or folder name, in car mode
    max_files_per_folder: int = Field(255, ge=10, le=10000)  # entries in one folder, in car mode
    lyrics: Literal["none", "embedded", "lrc_sidecar"] = "lrc_sidecar"
    playlists: bool = True

    @field_validator("base_folder")
    @classmethod
    def _folder(cls, v: str) -> str:
        return clean_folder(v) if v.strip("/ ") else ""


class FlashDrive(FileDeviceMixin, Device):
    type_name = "flashdrive"
    format_filesystems = FILESYSTEMS
    display_name = "USB flash drive"
    Settings: ClassVar[type[BaseModel]] = FlashDriveSettings
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats=set(),  # no limit unless the settings name formats
        supports_podcasts=True,
        supports_audiobooks=True,
        supports_video=True,  # copied as they are: a player decides what it plays
        lyrics="lrc_sidecar",
    )

    @property
    def s(self) -> FlashDriveSettings:
        settings: FlashDriveSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        if mount is not None:
            if any((mount / m).exists() for m in FOREIGN_MARKERS):
                return None
            if blk is None:
                if not (mount / STATE_DIR).is_dir():
                    return None
                return ProbeResult(
                    type_name=cls.type_name, suggested_name=mount.name, confidence=10
                )
        if blk is None or blk.tran != "usb" or not blk.is_disk:
            return None
        if (blk.usb_vendor_id or "").casefold() == APPLE_USB_VENDOR:
            return None  # an iPod: writing Music/ next to iPod_Control would confuse it
        parts = blk.partitions()
        data = next((p for p in parts if p.fstype), None) or (blk if blk.fstype else None)
        name = " ".join(x for x in (blk.vendor, blk.model) if x).strip() or "USB drive"
        return ProbeResult(
            type_name=cls.type_name,
            model=None,
            suggested_name=(data.label if data and data.label else name),
            identity=DeviceIdentity(usb_serial=blk.serial, fs_uuid=data.uuid if data else None),
            confidence=10,
            data_partition=data.path if data else None,
            details={"fstype": data.fstype if data else None},
        )

    def setting_profile(self) -> dict[str, Any]:
        """Formats and playlists from the settings, where they differ from the class defaults."""
        out: dict[str, Any] = {}
        if set(self.s.formats) != self.capabilities.formats:
            out["formats"] = set(self.s.formats)
        if not self.s.playlists:
            out["supports_playlists"] = False
        return out

    def path_rules(self) -> PathRules:
        rules = super().path_rules()
        if not self.s.car_mode:
            return rules
        return rules.model_copy(
            update={
                "ascii_only": True,
                "max_path_len": min(rules.max_path_len, self.s.max_path_len),
                "max_name_len": min(rules.max_name_len, self.s.max_name_len),
                "max_files_per_folder": self.s.max_files_per_folder,
            }
        )

    def _playlist_format(self) -> tuple[str, str]:
        # car stereos read plain .m3u with Windows line ends; the names are ASCII then
        return (".m3u", "\r\n") if self.s.car_mode else super()._playlist_format()

    def model_capabilities(self) -> DeviceCapabilities:
        return (
            super()
            .model_capabilities()
            .model_copy(update={"path_rules": self.path_rules(), "lyrics": self.s.lyrics})
        )

    async def format(self, name: str, fs: str | None = None) -> str | None:
        """Whole disk: new partition table + one `fs` partition (default vfat), remounted at our mountpoint.
        Updates `config.identity.fs_uuid` (a new file system has a new UUID)."""
        guard = self.ctx.guard if self.ctx else None
        if guard is None or self.ctx is None or self.block_path is None:
            raise RuntimeError(
                f"{self.id}: format needs the block device (not mounted from a disk)"
            )
        if fs is not None and fs not in FILESYSTEMS:
            raise ValueError(f"unsupported file system {fs!r} (one of {FILESYSTEMS})")
        part = await format_disk(guard, self.block_path, fs or "vfat", name)  # type: ignore[arg-type]
        m = await mount(guard, part, self.id, self.ctx.config.mount_root)
        self.mount = m.path
        self._path_rules = None
        (self.mount / STATE_DIR).mkdir(exist_ok=True)
        disks = await asyncio.to_thread(scan_block_devices, False)
        _, node = find_disk_of(part, disks)
        self.config.identity = self.config.identity.model_copy(update={"fs_uuid": node.uuid})
        return part
