# Add a device

A device type is **one class** (in one file, or in its own folder when it has helpers) plus **one line** in
`syncmusic/core/registry.py`. It must pass the shared device contract suite.

## 1. The class
Subclass `Device` (`syncmusic/base/device.py`). Players that are a plain file tree (USB mass storage) also take
`FileDeviceMixin` (`syncmusic/devices/_filedevice.py`): it implements copying, sidecars, `.m3u8` playlists, contents,
free space and the manifest, so you only write detection, settings and capabilities.

Worked example - a mass-storage player that keeps music in `MUSIC/` and is recognised by a `PLAYER.INF` file:
```python
# syncmusic/devices/acme.py
# SPDX-License-Identifier: GPL-3.0-or-later
"""Acme X1 player: a FAT32 file tree with music under MUSIC/ (identified by /PLAYER.INF)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.device import Device, ProbeResult
from syncmusic.core.models import DeviceIdentity
from syncmusic.devices._filedevice import FileDeviceMixin
from syncmusic.devices.paths import PathLayout
from syncmusic.infra.blockdev.identify import BlockDeviceInfo

ACME_USB_VENDOR = "1234"


class AcmeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layout: PathLayout = PathLayout(music="MUSIC/{album_artist}/{album}/{track:02} - {title}")
    playlists: bool = True


class Acme(FileDeviceMixin, Device):
    type_name = "acme"                      # stored in configs: never change it later
    display_name = "Acme X1"
    Settings: ClassVar[type[BaseModel]] = AcmeSettings
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats={"mp3", "flac", "aac"}, lyrics="lrc_sidecar"
    )

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        if mount is not None and (mount / "PLAYER.INF").is_file():
            return ProbeResult(type_name=cls.type_name, suggested_name="Acme X1", confidence=60)
        if blk is None or (blk.usb_vendor_id or "").casefold() != ACME_USB_VENDOR:
            return None
        data = next((p for p in blk.partitions() if p.fstype), None)
        return ProbeResult(
            type_name=cls.type_name,
            suggested_name="Acme X1",
            identity=DeviceIdentity(usb_serial=blk.serial, fs_uuid=data.uuid if data else None),
            confidence=60,                  # higher than the generic flash drive (10)
            data_partition=data.path if data else None)

    def model_capabilities(self) -> DeviceCapabilities:
        return super().model_capabilities().model_copy(update={"path_rules": self.path_rules()})
```
Rules of thumb:
- **Blocking work** (file I/O, databases) runs in `asyncio.to_thread`; never block the event loop.
- **Crash safety**: anything written before `commit()` must be undone or ignored by the next `begin()`.
- A player with its own database (like the iPod or HiBy) implements `put_track`/`remove_track`/`commit` itself and
  writes the database in `commit()`; one with listening data implements `read_feedback` and `reset_feedback`.
- Never touch a disk directly: mounts and formats go through `syncmusic.infra.blockdev` with the guard.
- UI strings (setting labels, help) go through `syncmusic/webui/static/js/i18n.js`.

## 2. Register it
```python
# syncmusic/core/registry.py
DEVICES = {
    ...
    "acme": "syncmusic.devices.acme:Acme",
}
```
The Register dialog, the settings form (generated from `AcmeSettings`) and detection pick it up from there.

## 3. Contract suite
Add a factory to `tests/contracts/harness.py` that builds the device on a temporary tree, and an entry in `DEVICES`:
```python
def _acme_device(tmp: Path) -> Device:
    from syncmusic.devices.acme import Acme

    mount = tmp / "acme"
    (mount / ".syncmusic").mkdir(parents=True, exist_ok=True)
    (mount / "PLAYER.INF").touch()
    return Acme(DeviceConfig(id="acme1", type="acme", name="Acme"), mount, None)

DEVICES = {..., "acme": DeviceHarness(make=_acme_device, manifest_path=MANIFEST, can_format=False)}
```
`uv run pytest tests/contracts -k acme` runs every device contract test against it (put/commit round trip,
crash recovery, probe, contents, playlists, feedback when `inject_feedback` is given).

## 4. Emulated image (optional, recommended)
`tests/emulated.py` builds loop images that look like real USB devices (partition table, file system, USB identity
from `tests/emulated.toml`). Add a builder for your player's layout and an identity entry, then write an
`@pytest.mark.emulated` scenario in `tests/integration/` (detect → register → sync → unplug). It needs `sudo -n`.

## 5. Docs
Add the player to `docs/04-devices.md` (what works, what does not, how to prepare it).
