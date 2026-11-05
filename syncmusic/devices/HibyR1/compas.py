# SPDX-License-Identifier: GPL-3.0-or-later
"""HiBy player running the Compás firmware (Beta stub, research: the developer notes).

Plain file sync only: Music/, Podcasts/ (with `database.ignore`, so podcasts stay out of the music library),
Audiobooks/ (the player keeps it out of the music library itself), `Playlists/*.m3u8` with relative paths,
`.lrc` sidecars, `cover.jpg` per album. The player re-indexes the card on unplug and at boot. No library
database writes and no reverse sync yet: the option to choose is in the developer notes.

Probe: a card whose root holds `.compas/` (written by the player). A card with both `.compas/` and the SonixOS
`.local/library.db` is Compás (it outranks the SonixOS class). The player's own files (hidden folders, themes,
`radio.txt`, firmware updates, `database.(un)ignore`) are never touched, also not by Wipe.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from syncmusic.base.capabilities import CoverSpec, DeviceCapabilities
from syncmusic.base.device import Device, DeviceSession, ProbeResult
from syncmusic.core.manifest import STATE_DIR
from syncmusic.core.models import DeviceIdentity
from syncmusic.devices._filedevice import FileDeviceMixin
from syncmusic.devices.HibyR1.sonixos import FORMATS, HIBY_USB_VENDOR
from syncmusic.devices.paths import PathLayout
from syncmusic.infra.blockdev.identify import BlockDeviceInfo

COMPAS_DIR = ".compas"
PODCAST_DIR = "Podcasts"
IGNORE_FILE = "database.ignore"
# the player's files at the card root, kept by Wipe (hidden folders like .plugins are kept too, see wipe)
PLAYER_FILES = (COMPAS_DIR, ".plugins", "themes", "radio.txt", IGNORE_FILE, "database.unignore")


class CompasSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formats: set[str] = set(FORMATS)
    layout: PathLayout = PathLayout()
    lyrics: Literal["none", "embedded", "lrc_sidecar"] = "lrc_sidecar"
    playlists: bool = True


class HibyCompas(FileDeviceMixin, Device):
    type_name = "hiby_compas"
    display_name = "HiBy R1 - Compás (Beta)"
    Settings: ClassVar[type[BaseModel]] = CompasSettings
    format_filesystems = ("exfat", "vfat")
    WIPE_KEEP = (*FileDeviceMixin.WIPE_KEEP, *PLAYER_FILES)
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats=set(FORMATS),
        supports_podcasts=True,
        supports_audiobooks=True,
        supports_playlists=True,
        cover=CoverSpec(max_size=600, embed=True, sidecar_name="cover.jpg"),
        lyrics="lrc_sidecar",
    )

    @property
    def s(self) -> CompasSettings:
        settings: CompasSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        if mount is None or not (mount / COMPAS_DIR).is_dir():
            return None
        hiby_usb = blk is not None and (blk.usb_vendor_id or "").casefold() == HIBY_USB_VENDOR
        data = None
        if blk is not None:
            data = next((p for p in blk.partitions() if p.fstype), None) or (
                blk if blk.fstype else None
            )
        return ProbeResult(
            type_name=cls.type_name,
            model=None,
            suggested_name=(data.label if data and data.label else None) or "HiBy Compás",
            identity=DeviceIdentity(
                usb_serial=blk.serial if blk else None, fs_uuid=data.uuid if data else None
            ),
            confidence=90
            if hiby_usb or blk is None
            else 85,  # above SonixOS (80) on a card with both
            data_partition=data.path if data else None,
            details={"fstype": data.fstype if data else None},
        )

    def setting_profile(self) -> dict[str, object]:
        out: dict[str, object] = {"formats": set(self.s.formats)} if self.s.formats else {}
        if not self.s.playlists:
            out["supports_playlists"] = False
        return out

    def model_capabilities(self) -> DeviceCapabilities:
        return (
            super()
            .model_capabilities()
            .model_copy(update={"path_rules": self.path_rules(), "lyrics": self.s.lyrics})
        )

    def wipe_keeps(self, name: str) -> bool:
        """Also every hidden entry except our state dir, and firmware updates (*.upt)."""
        return (
            super().wipe_keeps(name)
            or (name.startswith(".") and name != STATE_DIR.name)
            or name.lower().endswith(".upt")
        )

    async def begin(self) -> DeviceSession:
        """Also marks Podcasts/ with `database.ignore`: the player indexes every other folder as music."""
        s = await super().begin()
        folder = self.mount / PODCAST_DIR
        folder.mkdir(exist_ok=True)
        (folder / IGNORE_FILE).touch(exist_ok=True)
        return s
