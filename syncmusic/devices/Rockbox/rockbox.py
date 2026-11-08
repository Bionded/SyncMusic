# SPDX-License-Identifier: GPL-3.0-or-later
"""Rockbox player: a file tree read by Rockbox's own tag database.

Probe: a mounted tree with `/.rockbox/` (target, version and features from `/.rockbox/rockbox-info.txt`).
Rockbox runs on iPods too (USB vendor 05ac): only the tree tells it apart from a stock iPod, so an unmounted
disk is never claimed here (the stock iPod class claims Apple disks with lower confidence).

Capabilities: the playable formats are the codecs installed in `/.rockbox/codecs/` (a broad default when the
folder is missing), `has_rtc` from the `rtc` feature, music only - no podcasts or audiobooks, `.m3u8`
playlists, `.lrc` lyrics and a baseline `cover.jpg` per album folder. Lossless sources are suggested to be
converted to MP3 (`prefer_lossy`): decoding FLAC costs battery on the slow targets.

`wipe()` keeps `/.rockbox` (and Rockbox's logs at the root); `format()` reformats the data partition and
restores `/.rockbox` (firmware, codecs, settings) from a backup without Rockbox's database and play logs - everything
else is erased; never the partition table or a firmware partition (guard).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from syncmusic.base.capabilities import CoverSpec, DeviceCapabilities
from syncmusic.base.context import AppContext
from syncmusic.base.device import Device, DeviceSession, ProbeResult
from syncmusic.core import manifest as manifest_io
from syncmusic.core.manifest import STATE_DIR
from syncmusic.core.models import (
    DeviceConfig,
    DeviceContents,
    DeviceFeedback,
    DeviceIdentity,
    DeviceStats,
    DeviceTrackRef,
    Manifest,
    PreparedItem,
)
from syncmusic.core.mount_path import mount_source
from syncmusic.devices._filedevice import FileDeviceMixin, read_tags
from syncmusic.devices.paths import PLAYLIST_DIR, PathLayout, clean_folder, path_key
from syncmusic.devices.Rockbox import rockbox_feedback as rf
from syncmusic.infra.blockdev import format as fmt
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.identify import BlockDeviceInfo, find_disk_of, scan_block_devices

log = logging.getLogger(__name__)


ROCKBOX_DIR = ".rockbox"
APPLE_USB_VENDOR = "05ac"
INFO_FILE = "rockbox-info.txt"
CONFIG_FILE = "config.cfg"
# Rockbox's play logs at the root of the device (read by the reverse sync): never wiped
ROOT_LOGS = (".scrobbler.log", ".scrobbler-timeless.log")
PLAYBACK_LOG = "playback.log"  # in /.rockbox; Rockbox renames full ones to playback_NNNN.log
# not restored by format(): Rockbox's tag database (rebuilt by Rockbox) and its play logs (incl. rotated ones)
FORMAT_DROP = ("database_*", "playback*.log*")
ROTATED = ".syncmusic-"  # consumed logs: <name>.syncmusic-<UTC time>
DEFAULT_FORMATS = frozenset(
    {"mp3", "flac", "ogg", "opus", "aac", "alac", "wav", "aiff", "wv", "ape", "mpc", "wma"}
)
# /.rockbox/codecs/<name>.codec -> our format name
CODEC_FORMATS = {
    "mpa": "mp3",
    "flac": "flac",
    "vorbis": "ogg",
    "opus": "opus",
    "aac": "aac",
    "alac": "alac",
    "wav": "wav",
    "aiff": "aiff",
    "wavpack": "wv",
    "ape": "ape",
    "mpc": "mpc",
    "wma": "wma",
}


def read_info(mount: Path) -> dict[str, str]:
    """`Key: value` lines of /.rockbox/rockbox-info.txt ({} when missing or unreadable)."""
    try:
        text = (mount / ROCKBOX_DIR / INFO_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def installed_formats(mount: Path) -> set[str]:
    """Formats of the codecs in /.rockbox/codecs (empty when the folder is missing)."""
    try:
        names = [p.stem for p in (mount / ROCKBOX_DIR / "codecs").glob("*.codec")]
    except OSError:
        return set()
    return {CODEC_FORMATS[n] for n in names if n in CODEC_FORMATS}


def _data_partition(blk: BlockDeviceInfo) -> BlockDeviceInfo | None:
    """The biggest FAT partition (on an iPod: never the firmware partition, which has no file system), or
    the whole disk when it carries the file system itself."""
    parts = [p for p in blk.partitions() if p.fstype == "vfat"]
    if parts:
        return max(parts, key=lambda p: p.size or 0)
    return blk if blk.fstype == "vfat" else None


class RockboxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formats: set[str] | None = None  # None = the codecs installed on the device
    layout: PathLayout = PathLayout()
    filesystem: str | None = None  # override the detected filesystem (path rules)
    ascii_only: bool = False
    lyrics: Literal["none", "embedded", "lrc_sidecar"] = "lrc_sidecar"
    playlists: bool = True
    # where the .m3u8 playlists go, relative to the device root; a change moves them on the next sync
    playlist_dir: str = PLAYLIST_DIR
    # scrobble rule for log entries: a play counts at this share of the track or this many seconds
    play_min_fraction: float = Field(0.5, gt=0, le=1)
    play_min_seconds: float = Field(240, gt=0)
    keep_rotated: int = Field(5, ge=0)  # consumed logs are kept for this many reverse syncs
    # Rockbox's play log (/.rockbox/config.cfg `play log: on` / `off`), written whenever the device is written and at
    # once from Device settings (off writes `play log: off`)
    enable_play_log: bool = True

    @field_validator("playlist_dir")
    @classmethod
    def _folder(cls, v: str) -> str:
        return clean_folder(v)


class Rockbox(FileDeviceMixin, Device):
    type_name = "rockbox"
    display_name = "Rockbox player"
    owns_device_tree = True
    music_only = True  # No podcasts or audiobooks, not even by capability overrides
    WIPE_KEEP = (*FileDeviceMixin.WIPE_KEEP, *ROOT_LOGS)
    Settings: ClassVar[type[BaseModel]] = RockboxSettings
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats=set(DEFAULT_FORMATS),
        supports_podcasts=False,  # music only
        supports_audiobooks=False,
        supports_playlists=True,
        supports_ratings_read=True,
        supports_playcounts_read=True,
        cover=CoverSpec(max_size=300, progressive=False, embed=False, sidecar_name="cover.jpg"),
        lyrics="lrc_sidecar",
        prefer_lossy=True,
    )

    def __init__(self, config: DeviceConfig, mount: Path, ctx: AppContext | None) -> None:
        super().__init__(config, mount, ctx)
        self._read_logs: dict[Path, str] = {}  # log file -> sha256, from the last read_feedback

    @property
    def s(self) -> RockboxSettings:
        settings: RockboxSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        if mount is None:
            return cls._probe_apple_disk(blk)
        if not (mount / ROCKBOX_DIR).is_dir():
            return None
        info = read_info(mount)
        data = None
        if blk is not None:
            src = mount_source(mount.resolve())
            data = next((n for n in (*blk.partitions(), blk) if n.path == src), None)
        return ProbeResult(
            type_name=cls.type_name,
            model=info.get("Target"),
            suggested_name=(data.label if data and data.label else None)
            or " ".join(x for x in ("Rockbox", info.get("Target")) if x),
            identity=DeviceIdentity(
                usb_serial=blk.serial if blk else None, fs_uuid=data.uuid if data else None
            ),
            confidence=80,  # above the stock iPod (60/40) and the flash drive (10)
            data_partition=data.path if data else None,
            details={"version": info.get("Version"), "fstype": data.fstype if data else None},
        )

    @classmethod
    def _probe_apple_disk(cls, blk: BlockDeviceInfo | None) -> ProbeResult | None:
        """An unmounted iPod may run Rockbox: offered below the stock iPod (unknown until mounted), with the
        identity and data partition so registering it with `type: rockbox` works."""
        if blk is None or blk.tran != "usb" or not blk.is_disk:
            return None
        if (blk.usb_vendor_id or "").casefold() != APPLE_USB_VENDOR:
            return None
        data = _data_partition(blk)
        return ProbeResult(
            type_name=cls.type_name,
            suggested_name=(data.label if data and data.label else "Rockbox"),
            identity=DeviceIdentity(usb_serial=blk.serial, fs_uuid=data.uuid if data else None),
            confidence=30,
            data_partition=data.path if data else None,
            details={"fstype": data.fstype if data else None},
        )

    def detected_model(self) -> str | None:
        return read_info(self.mount).get("Target") or None

    def detected_profile(self) -> dict[str, Any]:
        """The installed codecs and the `rtc` feature of rockbox-info.txt."""
        info = read_info(self.mount)
        out: dict[str, Any] = {}
        if formats := installed_formats(self.mount):
            out["formats"] = formats
        if "Features" in info:
            out["has_rtc"] = "rtc" in info["Features"].split(":")
        return out

    def setting_profile(self) -> dict[str, Any]:
        out: dict[str, Any] = {"formats": set(self.s.formats)} if self.s.formats else {}
        if not self.s.playlists:
            out["supports_playlists"] = False
        return out

    def model_capabilities(self) -> DeviceCapabilities:
        return (
            super()
            .model_capabilities()
            .model_copy(update={"path_rules": self.path_rules(), "lyrics": self.s.lyrics})
        )

    def playback_logs(self) -> list[Path]:
        """/.rockbox/playback_NNNN.log (oldest first), then /.rockbox/playback.log - those that exist."""
        rb = self.mount / ROCKBOX_DIR
        numbered = sorted(p for p in rb.glob("playback_*.log") if p.stem[9:].isdigit())
        current = rb / PLAYBACK_LOG
        return [p for p in (*numbered, current) if p.is_file()]

    def scrobbler_logs(self) -> list[Path]:
        """/.scrobbler.log and /.scrobbler-timeless.log - those that exist."""
        return [p for p in (self.mount / n for n in ROOT_LOGS) if p.is_file()]

    def read_tagcache(self) -> list[rf.TagcacheEntry] | None:
        """Entries of Rockbox's tag database, None when there is none (or it is unreadable)."""
        rb = self.mount / ROCKBOX_DIR
        try:
            master = (rb / rf.MASTER_FILE).read_bytes()
            names = (rb / f"database_{rf.TAG_FILENAME}.tcd").read_bytes()
        except FileNotFoundError:
            return None
        try:
            return rf.parse_tagcache(master, names)
        except rf.TagcacheError as e:
            log.warning("%s: tagcache not readable: %s", self.id, e)
            return None

    def _tag_index(self, manifest: Manifest) -> rf.TagIndex:
        """Tags of the managed files on the device (what Rockbox logged them as)."""
        tags: dict[str, rf.TrackTags] = {}
        for key, e in manifest.items.items():
            t, _ = read_tags(self.mount / e.path)
            tags[key] = rf.TrackTags(
                artist=t.get("artist") or t.get("albumartist") or "",
                album=t.get("album", ""),
                title=t.get("title", ""),
                filename=PurePosixPath(e.path).name,
            )
        return rf.TagIndex(tags, manifest.items)

    def _stats(self) -> dict[str, DeviceStats]:
        tagcache = self.read_tagcache()
        return rf.stats_by_path(tagcache, self._rules()) if tagcache else {}

    async def read_contents(self) -> DeviceContents:
        """Files on the device with their tagcache values (the baseline when adopted)."""
        contents = await super().read_contents()
        stats = await asyncio.to_thread(self._stats)
        rules = self._rules()
        for item in contents.items:
            item.stats = stats.get(path_key(item.path, rules), DeviceStats())
        return contents

    async def begin(self) -> DeviceSession:
        s = await super().begin()
        s.data["stats"] = await asyncio.to_thread(self._stats)
        return s

    async def put_track(self, s: DeviceSession, item: PreparedItem) -> DeviceTrackRef:
        """Writes the file; `stats` = what the tagcache already holds for that path (usually nothing: 0 plays,
        unrated) - Rockbox's counts cannot be set, so the manifest baseline must start from them."""
        ref = await super().put_track(s, item)
        stats: dict[str, DeviceStats] = s.data["stats"]
        ref.stats = stats.get(path_key(ref.path, self._rules()), DeviceStats())
        return ref

    async def read_playlists(self, manifest: Manifest) -> dict[str, list[str]] | None:
        """Playlists can be edited on the player: they go back to the library."""
        return await self.playlist_files(manifest) if self.s.playlists else None

    async def read_feedback(self, manifest: Manifest | None) -> DeviceFeedback:
        """Plays from the first source that has entries: playback logs, scrobbler logs, tagcache deltas;
        ratings and absolute play counts from the tagcache. Remembers the logs it read for
        `reset_feedback` (same instance)."""
        if manifest is None:
            return DeviceFeedback()
        caps = self.effective_capabilities()

        def work() -> tuple[DeviceFeedback, dict[Path, str]]:
            now = dt.datetime.now(dt.UTC)
            fb = DeviceFeedback()
            read: dict[Path, str] = {}
            playback: list[rf.PlaybackEntry] = []
            for log_file in self.playback_logs():
                data = log_file.read_bytes()
                read[log_file] = rf.sha256_hex(data)
                playback += rf.parse_playback_log(data.decode("utf-8", errors="replace"))
            scrobbler: list[tuple[str | None, list[rf.ScrobblerEntry]]] = []
            for log_file in self.scrobbler_logs():
                data = log_file.read_bytes()
                read[log_file] = rf.sha256_hex(data)
                scrobbler.append(rf.parse_scrobbler_log(data.decode("utf-8", errors="replace")))
            if playback:  # the scrobbler plugin can import playback.log: never count both
                fb.plays, fb.unmapped = rf.playback_plays(
                    playback,
                    manifest,
                    caps.path_rules,
                    has_rtc=caps.has_rtc,
                    now=now,
                    device_id=self.id,
                    min_fraction=self.s.play_min_fraction,
                    min_seconds=self.s.play_min_seconds,
                )
            elif any(entries for _, entries in scrobbler):
                index = self._tag_index(manifest)
                for tz, entries in scrobbler:
                    plays, unmapped = rf.scrobbler_plays(
                        entries, tz, index, now=now, device_id=self.id
                    )
                    fb.plays += plays
                    fb.unmapped += unmapped
            from_logs = bool(playback) or any(entries for _, entries in scrobbler)
            tagcache = self.read_tagcache()
            if tagcache is not None:
                tc = rf.tagcache_feedback(
                    tagcache, manifest, caps.path_rules, now=now, device_id=self.id
                )
                fb.ratings, fb.play_counts = tc.ratings, tc.play_counts
                if not from_logs:
                    fb.plays, fb.unmapped = tc.plays, tc.unmapped
            return fb, (read if from_logs else {})

        fb, self._read_logs = await asyncio.to_thread(work)
        return fb

    def _config_text(self) -> str | None:
        try:
            return (self.mount / ROCKBOX_DIR / CONFIG_FILE).read_text("utf-8", errors="replace")
        except OSError:
            return None  # no config: every setting at its default (off)

    async def feedback_warnings(self) -> list[str]:
        """Warn when, after SyncMusic's next write, the player's config has playback logging and runtime data off
        and no scrobbler plugin log exists: the reverse sync would silently find nothing. With
        `enable_play_log` on SyncMusic switches the log on itself, so the warning says that instead of asking the
        user to do it; off, it says the log is off."""

        def work() -> list[str]:
            cfg = rf.parse_config(self._config_text() or "")
            if self.scrobbler_logs() or cfg.get(rf.CFG_RUNTIME_DATA, "off").lower() == "on":
                return []
            if not self.s.enable_play_log:
                return [rf.PLAY_LOG_OFF_WARNING]
            if rf.records_listening_data(cfg):
                return []
            return [rf.PLAY_LOG_PENDING_WARNING]

        return await asyncio.to_thread(work)

    async def play_log_state(self) -> str:
        """The play log as the player's config has it now: `on`, `off` or `unset` (default: off)."""
        return rf.play_log_state(await asyncio.to_thread(self._config_text))

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        # before the manifest, which is always written last; the player's config is the home instance's
        if manifest_io.is_home(manifest):
            await asyncio.to_thread(self.write_play_log, self.s.enable_play_log)
        await super().commit(s, manifest)

    def write_play_log(self, on: bool) -> bool:
        """`play log: on` / `off` in /.rockbox/config.cfg (only written when it changes; written next to it and
        renamed). Off on a player without a config writes nothing (off is the default). Rockbox reads its config at
        boot, so the change takes effect after the player restarts. True when the file was written."""
        path = self.mount / ROCKBOX_DIR / CONFIG_FILE
        try:
            text = path.read_bytes().decode("utf-8", errors="surrogateescape")
        except FileNotFoundError:
            if not on:
                return False
            text = ""
        new = rf.with_play_log(text, on)
        if new is None:
            return False
        tmp = path.with_name(f"{CONFIG_FILE}.syncmusic-tmp")
        with open(tmp, "wb") as f:
            f.write(new.encode("utf-8", errors="surrogateescape"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        log.info("%s: Rockbox play log switched %s in %s", self.id, "on" if on else "off", path)
        return True

    async def reset_feedback(self, s: DeviceSession, consumed: DeviceFeedback) -> None:
        """After the reverse sync committed: rename the logs the last `read_feedback` used to
        `<name>.syncmusic-<UTC time>` (both kinds, so neither is counted again) and delete rotations
        beyond the newest `keep_rotated`. A log that changed since it was read is left alone."""
        read, self._read_logs = self._read_logs, {}
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")

        def work() -> None:
            for path, digest in read.items():
                if rf.file_sha256(path) == digest:
                    path.rename(path.with_name(f"{path.name}{ROTATED}{stamp}"))
            rotated = {
                p: p.name.rsplit(ROTATED, 1)[1]
                for d in (self.mount, self.mount / ROCKBOX_DIR)
                for p in d.glob(f"*{ROTATED}*")
                if p.is_file()
            }
            stamps = sorted(set(rotated.values()), reverse=True)
            for p, p_stamp in rotated.items():
                if p_stamp not in stamps[: self.s.keep_rotated]:
                    p.unlink(missing_ok=True)

        await asyncio.to_thread(work)

    async def format(self, name: str, fs: str | None = None) -> str | None:
        """Reformat the data partition as FAT32 and restore `/.rockbox` from a backup in the cache dir. The
        data partition is the one mounted at `self.mount`, else the disk's biggest FAT partition (the sync
        service unmounts before formatting: the backup is then read through a read-only probe mount). The
        partition table and any firmware partition are never touched (the guard only allows mkfs on an iPod's
        data partition). Refused when `/.rockbox` cannot be read (nothing to restore: the player would no
        longer boot Rockbox). The database and play logs are not restored (`FORMAT_DROP`): a format erases them
        too. Updates `config.identity.fs_uuid`."""
        guard = self.ctx.guard if self.ctx else None
        if guard is None or self.ctx is None or self.block_path is None:
            raise RuntimeError(
                f"{self.id}: format needs the block device (not mounted from a disk)"
            )
        if fs not in (None, "vfat"):
            raise ValueError("Rockbox needs FAT32 (vfat)")
        part = await asyncio.to_thread(mount_source, self.mount.resolve())
        mounted = part is not None and await asyncio.to_thread((self.mount / ROCKBOX_DIR).is_dir)
        if not mounted:
            disks = await asyncio.to_thread(scan_block_devices, False)
            disk, _ = find_disk_of(self.block_path, disks)
            data = _data_partition(disk)
            if data is None:
                raise RuntimeError(f"{self.id}: no FAT data partition found on {disk.path}")
            part = data.path
        assert part is not None
        cache = self.ctx.config.cache_dir
        await asyncio.to_thread(cache.mkdir, parents=True, exist_ok=True)
        backup = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="rockbox-", dir=cache))

        def save(root: Path) -> None:
            if not (root / ROCKBOX_DIR).is_dir():
                raise RuntimeError(f"{self.id}: /{ROCKBOX_DIR} not readable: refusing to format")
            shutil.copytree(
                root / ROCKBOX_DIR,
                backup / ROCKBOX_DIR,
                ignore=shutil.ignore_patterns(*FORMAT_DROP),
            )

        try:
            if mounted:
                await asyncio.to_thread(save, self.mount)
            else:
                await mnt.probe_read(guard, part, self.ctx.config.mount_root, save)
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, backup, True)
            raise
        try:
            await fmt.make_filesystem(guard, part, "vfat", name)
            m = await mnt.mount(guard, part, self.id, self.ctx.config.mount_root)
            self.mount = m.path
            self._path_rules = None

            def restore() -> None:
                shutil.copytree(backup / ROCKBOX_DIR, self.mount / ROCKBOX_DIR)
                (self.mount / STATE_DIR).mkdir(exist_ok=True)
                mnt.syncfs(self.mount)

            await asyncio.to_thread(restore)
        except BaseException:
            log.error("%s: format failed; /%s backup kept in %s", self.id, ROCKBOX_DIR, backup)
            raise
        await asyncio.to_thread(shutil.rmtree, backup, True)
        disks = await asyncio.to_thread(scan_block_devices, False)
        _, node = find_disk_of(part, disks)
        self.config.identity = self.config.identity.model_copy(update={"fs_uuid": node.uuid})
        return part
