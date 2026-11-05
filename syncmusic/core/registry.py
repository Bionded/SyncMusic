# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit registry: type name → implementation class. No auto-discovery.

Entries are "module:Class" strings imported on first use, so core never imports impl modules at
import time. Adding an implementation = one line here.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from syncmusic.base.device import Device
    from syncmusic.base.library import Library
    from syncmusic.base.processor import Processor

LIBRARIES: dict[str, str] = {  # the first is the Add library form's default
    "subsonic": "syncmusic.libraries.subsonic:SubsonicLibrary",
    "folder": "syncmusic.libraries.folder:FolderLibrary",
    "audiobookshelf": "syncmusic.libraries.audiobookshelf:AudiobookshelfLibrary",
    "jellyfin": "syncmusic.libraries.jellyfin:JellyfinLibrary",
}
# types offered before and not now: a stored library of one loads as UnsupportedLibrary (always unavailable with
# this message; syncmusic/libraries/plex.py stays in the tree for a later video library)
RETIRED_LIBRARIES: dict[str, str] = {
    "plex": "Plex is not supported in this version: attach another library to this device",
}
DEVICES: dict[str, str] = {
    "android": "syncmusic.devices.Android.mtp:AndroidMtp",
    "flashdrive": "syncmusic.devices.flashdrive:FlashDrive",
    "ftp": "syncmusic.devices.ftp:FtpDevice",
    "hiby": "syncmusic.devices.HibyR1.sonixos:Hiby",
    "ipod": "syncmusic.devices.iPod.ipod:IPod",
    "rockbox": "syncmusic.devices.Rockbox.rockbox:Rockbox",
    "smb": "syncmusic.devices.smb:SmbDevice",
}
PROCESSORS: dict[str, str] = {
    "covers": "syncmusic.processors.covers:Covers",
    "lyrics": "syncmusic.processors.lyrics:LyricsProcessor",
    "retag": "syncmusic.processors.retag:Retag",
    "transcode": "syncmusic.processors.transcode:Transcode",
    "video": "syncmusic.processors.video:Video",
}


def _load(table: dict[str, str], kind: str, name: str) -> Any:
    try:
        target = table[name]
    except KeyError:
        raise KeyError(
            f"unknown {kind} type {name!r} (known: {', '.join(sorted(table))})"
        ) from None
    module, _, cls = target.partition(":")
    return getattr(importlib.import_module(module), cls)


# former type names ("navidrome" is the Subsonic-compatible type); stored configs are rewritten too
LIBRARY_ALIASES: dict[str, str] = {"navidrome": "subsonic"}


def library_class(name: str) -> type[Library]:
    if name in RETIRED_LIBRARIES:
        from syncmusic.libraries.unsupported import UnsupportedLibrary

        return UnsupportedLibrary
    cls: type[Library] = _load(LIBRARIES, "library", LIBRARY_ALIASES.get(name, name))
    return cls


def device_class(name: str) -> type[Device]:
    cls: type[Device] = _load(DEVICES, "device", name)
    return cls


def processor_class(name: str) -> type[Processor]:
    cls: type[Processor] = _load(PROCESSORS, "processor", name)
    return cls


def library_classes() -> list[type[Library]]:
    return [library_class(n) for n in LIBRARIES]


def device_classes() -> list[type[Device]]:
    return [device_class(n) for n in DEVICES]


def processor_classes() -> list[type[Processor]]:
    return [processor_class(n) for n in PROCESSORS]
