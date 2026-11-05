# SPDX-License-Identifier: GPL-3.0-or-later
"""Stock iPod: tracks live in the iPod's own database (iTunesDB via pygpod).

Files are copied by us to `iPod_Control/Music/Fxx/<4 random chars>.<ext>` (listed in the inflight list first,
fsync + rename, like the file devices) and then referenced in place (`Database.add_track(copy=False)`), so an
interrupted sync leaves no unlisted files. `device_ref` = the track's dbid; the manifest lives under
`/.syncmusic/` on the data partition. `commit()`: iTunesDB (+ hash, ArtworkDB; crash-safe in pygpod), then
the manifest.

Feedback: the iPod records plays/ratings/bookmarks in `Play Counts` (positional, consumed by the next database
write). `begin()` and `read_feedback()` open the database with the counts merged in memory, so the absolute values
(play count, last played, rating, bookmark) are always current; `read_feedback()` reports the difference to the
manifest's last-known values (`last_playcount`, `last_rating`, `last_position_s`). A forward sync therefore never
loses plays, and stale positional entries can never be applied to the wrong tracks.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import secrets
import shutil
import string
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator

from syncmusic.base.capabilities import CoverSpec, DeviceCapabilities, PathRules
from syncmusic.base.device import Device, DeviceSession, ProbeResult
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import (
    DeviceContents,
    DeviceFeedback,
    DeviceIdentity,
    DeviceItem,
    DevicePlaylist,
    DeviceTrackRef,
    Manifest,
    MediaKind,
    PlayEvent,
    PositionInfo,
    PreparedItem,
    Track,
)
from syncmusic.devices._filedevice import (
    PATCH_MAX,
    changed_range,
    copy_atomic,
    patch_file,
    roll_back_patch,
)
from syncmusic.devices.wallclock import wall_to_utc
from syncmusic.infra.blockdev import format as fmt
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.guard import IPOD_DATA_PARTTYPES
from syncmusic.infra.blockdev.identify import BlockDeviceInfo, find_disk_of, scan_block_devices
from syncmusic.processors import tagmap

log = logging.getLogger(__name__)

APPLE_USB_VENDOR = "05ac"
MUSIC_DIR = PurePosixPath("iPod_Control/Music")
NAME_CHARS = string.ascii_uppercase + string.digits
EXT_FOR_FORMAT = {"mp3": "mp3", "aac": "m4a", "alac": "m4a", "wav": "wav", "aiff": "aif"}
# bytes per item in the commit reserve: iTunesDB (~1.4 KB/track, staged next to the old one) + ArtworkDB
DB_BYTES_PER_TRACK = 1500
ARTDB_BYTES_PER_TRACK = 800
MANIFEST_BYTES_PER_TRACK = 400
# the iPod firmware fails on an iTunesDB of 64 MB and more; SyncMusic stays below 63 MiB
ITUNESDB_LIMIT = 63 * 1024 * 1024
ITUNESDB = PurePosixPath("iPod_Control/iTunes/iTunesDB")
DEVICE_FILE_MAX = 1024 * 1024  # iPod_Control/Device files kept across a format (settings are tiny)
# copied by the device DB backup; the .ithmb artwork files are too big and can be rebuilt by a sync
BACKUP_FILES = (
    "iPod_Control/iTunes/iTunesDB",
    "iPod_Control/iTunes/Play Counts",
    "iPod_Control/iTunes/iTunesStats",
    "iPod_Control/iTunes/PlayCounts.plist",
    "iPod_Control/Artwork/ArtworkDB",
    ".syncmusic/manifest.jsonl",
)


class IPodSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    covers: bool = True  # write cover art to the ArtworkDB
    # set the iPod's clock on connect (before the first mount), at a sync and at eject - always while unmounted:
    # the iPod writes its FAT in reaction. False for configs stored; registration
    # turns it on for new devices.
    time_sync: bool = False
    # the iPod's time zone (IANA name): the clock is set in it and the play times it records are read in it;
    # None = this server's zone. `examples` feeds the form's suggestion list.
    timezone: str | None = Field(
        None, json_schema_extra={"examples": sorted(available_timezones())}
    )

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            ZoneInfo(v.strip())
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown time zone {v!r} (an IANA name like Europe/Lisbon)") from e
        return v.strip()


def with_guid(sysinfo: bytes, serial: str | None) -> bytes:
    """SysInfo content with a FirewireGuid line. iPods report the FireWire GUID as USB serial (16 hex digits):
    enough for pygpod's hash58. A stock restore leaves SysInfo empty."""
    if b"firewireguid" in sysinfo.lower() or not serial or len(serial) != 16:
        return sysinfo
    if sysinfo and not sysinfo.endswith(b"\n"):
        sysinfo += b"\n"
    return sysinfo + f"FirewireGuid: 0x{serial}\n".encode()


def _local_wall_to_utc(wall: dt.datetime, zone: str | None = None) -> dt.datetime:
    """iPod timestamps are wall-clock time in its zone (pygpod returns them as if UTC); `zone` None = the host's."""
    return wall_to_utc(wall, zone)


# iTunesDB fields taken from the file's tags when the library has no value, and the ones only the file has
FILE_TEXT = {
    "artist": "artist",
    "album": "album",
    "albumartist": "albumartist",
    "genre": "genre",
    "comment": "comment",
    "composer": "composer",
    "grouping": "grouping",
}
FILE_NUMBERS = {
    "year": "date",
    "track_number": "tracknumber",
    "total_tracks": "tracktotal",
    "cd_number": "discnumber",
    "total_cds": "disctotal",
}
SORT_FIELDS = {
    "sort_title": "titlesort",
    "sort_artist": "artistsort",
    "sort_album": "albumsort",
    "sort_albumartist": "albumartistsort",
    "sort_composer": "composersort",
}


def _int(values: list[str] | None) -> int:
    digits = "".join(c for c in (values[0] if values else "")[:4] if c.isdigit())
    return int(digits) if digits else 0


def _with_file_tags(tags: dict[str, Any], own: dict[str, list[str]]) -> dict[str, Any]:
    """pygpod keywords: the library's values on top of the file's own tags - an empty library value never blanks
    a field the file has."""
    out = dict(tags)
    for key, tag in FILE_TEXT.items():
        if not out.get(key) and own.get(tag):
            out[key] = ", ".join(own[tag]) if key in ("artist", "composer") else own[tag][0]
    for key, tag in FILE_NUMBERS.items():
        if not out.get(key) and _int(own.get(tag)):
            out[key] = _int(own.get(tag))
    if not out.get("compilation") and own.get("compilation", ["0"])[0] not in ("0", ""):
        out["compilation"] = True
    return out


def _gain_db(values: list[str] | None) -> float | None:
    """ReplayGain text like '-6.18 dB' → dB (None if missing or unreadable)."""
    m = re.match(r"\s*([-+]?\d+(?:\.\d+)?)", values[0]) if values else None
    return float(m.group(1)) if m else None


def _stars(rating: int) -> int | None:
    """iTunesDB rating 0-100 → 0-5 stars (0 = unrated → None)."""
    return round(rating / 20) if rating > 0 else None


class IPod(Device):
    type_name = "ipod"
    display_name = "iPod (stock firmware)"
    owns_device_tree = True
    Settings: ClassVar[type[BaseModel]] = IPodSettings
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats={"mp3", "aac", "alac", "wav", "aiff"},
        max_sample_rate=48000,
        max_bit_depth=16,
        supports_podcasts=True,
        supports_audiobooks=True,
        supports_playlists=True,
        supports_ratings_read=True,
        supports_ratings_write=True,
        supports_playcounts_read=True,
        supports_positions_read=True,
        supports_time_sync=True,
        has_rtc=True,
        needs_device_db=True,
        path_rules=PathRules.for_filesystem("vfat"),
        cover=CoverSpec(max_size=500, format="jpeg", embed=False),
        lyrics="embedded",
        # the chain a stock iPod gets (built-in template, registration) has Convert video; whether this model
        # takes videos at all (`supports_video`) is read from its database
        video_profile="ipod",
    )

    def __init__(self, config: Any, mount: Path, ctx: Any) -> None:
        super().__init__(config, mount, ctx)
        self._pg_device: Any = None  # pygpod Device (model detection), loaded lazily

    @property
    def s(self) -> IPodSettings:
        settings: IPodSettings = self.settings  # type: ignore[assignment]
        return settings

    # --- probe / model ------------------------------------------------------------------------

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        if mount is not None:
            apple = blk is not None and (blk.usb_vendor_id or "").casefold() == APPLE_USB_VENDOR
            rockbox = (mount / ".rockbox").exists()
            if not (mount / "iPod_Control").is_dir() and not (rockbox and apple):
                return None
            data = cls._data_partition(blk)
            return ProbeResult(
                type_name=cls.type_name,
                model=cls._model_of(mount),
                suggested_name=cls._db_name(mount) or "iPod",
                identity=DeviceIdentity(
                    usb_serial=blk.serial if blk else None, fs_uuid=data.uuid if data else None
                ),
                # a Rockbox iPod usually dual-boots the Apple firmware: offered after Rockbox, which claims it with
                # more certainty; the first sync creates iPod_Control
                confidence=30 if rockbox else 60,
                data_partition=data.path if data else None,
                details={"rockbox": True} if rockbox else {},
            )
        if blk is None or blk.tran != "usb" or not blk.is_disk:
            return None
        if (blk.usb_vendor_id or "").casefold() != APPLE_USB_VENDOR:
            return None
        data = cls._data_partition(blk)
        return ProbeResult(
            type_name=cls.type_name,
            model=None,
            suggested_name=(data.label if data and data.label else "iPod"),
            identity=DeviceIdentity(usb_serial=blk.serial, fs_uuid=data.uuid if data else None),
            confidence=40,  # Rockbox (also Apple's USB id) claims it with more certainty from /.rockbox
            data_partition=data.path if data else None,
            details={"fstype": data.fstype if data else None},
        )

    @staticmethod
    def _data_partition(blk: BlockDeviceInfo | None) -> BlockDeviceInfo | None:
        """The FAT data partition: the biggest vfat partition, else (unreadable file system) the biggest
        partition with a FAT partition type. Never the firmware partition (type 0, no file system)."""
        if blk is None:
            return None
        parts = [p for p in blk.partitions() if p.fstype == "vfat"]
        if not parts:
            parts = [p for p in blk.partitions() if p.parttype in IPOD_DATA_PARTTYPES]
        if not parts:
            return blk if blk.fstype == "vfat" else None
        return max(parts, key=lambda p: p.size or 0)

    @staticmethod
    def _model_of(mount: Path) -> str | None:
        try:
            from pygpod import Device as PgDevice

            gen = PgDevice.from_mountpoint(str(mount)).generation
        except Exception:  # noqa: BLE001 - probing must never fail
            return None
        return None if gen.name == "UNKNOWN" else gen.name.lower()

    @staticmethod
    def _db_name(mount: Path) -> str | None:
        """The device name (master playlist name) from the iTunesDB, if readable."""
        try:
            from pygpod.db.parser import parse_itunesdb
            from pygpod.device.mountpoint import find_itunesdb

            path = find_itunesdb(str(mount))
            if path is None:
                return None
            root = parse_itunesdb(path.read_bytes())
        except Exception:  # noqa: BLE001
            return None
        for mhsd in root.children:
            if mhsd.fields.get("mhsd_type") != 2:
                continue
            for mhlp in mhsd.children:
                for mhyp in mhlp.children:
                    if mhyp.fields.get("playlist_type") == 1:  # master playlist
                        name = mhyp.get_mhod(1)
                        return str(name) if name else None
        return None

    def _pygpod_device(self) -> Any:
        if self._pg_device is None:
            from pygpod import Device as PgDevice

            self._pg_device = PgDevice.from_mountpoint(str(self.mount))
        return self._pg_device

    def _pygpod_or_none(self) -> Any:
        try:
            return self._pygpod_device()
        except Exception:  # noqa: BLE001 - unknown model: database generic entry and class defaults
            log.debug("%s: model detection failed", self.id, exc_info=True)
            return None

    def detected_model(self) -> str | None:
        pg = self._pygpod_or_none()
        gen = pg.generation.name if pg is not None else "UNKNOWN"
        return None if gen == "UNKNOWN" else gen.lower()

    def detected_profile(self) -> dict[str, Any]:
        """From pygpod's model table (SysInfo): codecs, podcasts, artwork formats, shuffle."""
        pg = self._pygpod_or_none()
        if pg is None:
            return {}
        from pygpod.device.models import supported_audio_codecs

        gen = pg.generation.name
        out: dict[str, Any] = {"formats": set(supported_audio_codecs(pg.generation))}
        if not pg.supports_podcast and gen != "UNKNOWN":
            out["supports_podcasts"] = False
        formats = pg.get_cover_art_formats() if pg.supports_artwork or gen == "UNKNOWN" else None
        biggest = max((max(f.width, f.height) for f in formats or ()), default=None)
        out["cover_size"] = max(biggest, 100) if biggest else None
        if pg.is_shuffle:
            out["supports_playlists"] = False
        return out

    def setting_profile(self) -> dict[str, Any]:
        return {} if self.s.covers else {"cover_size": None}

    def model_capabilities(self) -> DeviceCapabilities:
        caps = super().model_capabilities()
        caps.supports_time_sync = self.s.time_sync
        pg = self._pygpod_or_none()
        if pg is not None:
            from pygpod.device.models import VIDEO_GENERATIONS

            if pg.generation in VIDEO_GENERATIONS:  # 5G, nano 3G+, Classic
                caps.supports_video, caps.video_profile = True, "ipod"
        return caps

    # --- space and state ----------------------------------------------------------------------

    async def free_space(self) -> int:
        st = await asyncio.to_thread(os.statvfs, self.mount)
        return st.f_bavail * st.f_frsize

    async def capacity(self) -> int:
        st = await asyncio.to_thread(os.statvfs, self.mount)
        return st.f_blocks * st.f_frsize

    def commit_reserve(self, manifest_items: int) -> int:
        """The new iTunesDB and ArtworkDB are staged next to the old ones (pygpod's crash-safe save), so
        twice their size, plus the manifest."""
        db = DB_BYTES_PER_TRACK * manifest_items + 64 * 1024
        art = ARTDB_BYTES_PER_TRACK * manifest_items
        return 2 * (db + art) + MANIFEST_BYTES_PER_TRACK * manifest_items + 1024 * 1024

    async def read_manifest(self) -> Manifest | None:
        return await asyncio.to_thread(manifest_io.read, self.mount)

    db_size_limit = ITUNESDB_LIMIT

    async def db_size(self) -> int | None:
        def work() -> int | None:
            try:
                return (self.mount / ITUNESDB).stat().st_size
            except FileNotFoundError:
                return None

        return await asyncio.to_thread(work)

    async def estimate_db_size(self, items: int, playlist_entries: int) -> int:
        """pygpod's estimate (serialises the current database, extrapolates per track) for the plan's totals."""

        def work() -> int:
            if (
                not self._has_db()
            ):  # first sync onto an iPod without a database: estimate from an empty one
                from pygpod import Database, init_ipod

                with tempfile.TemporaryDirectory(prefix="sd-emptydb-") as tmp:
                    init_ipod(tmp, name=self.config.name, music_dirs=0, artworkdb=False)
                    db = Database(tmp)
            else:
                db = self._open_db()
            entries = sum(len(p.tracks) for p in db.playlists if not p.is_master)
            return int(
                db.estimate_db_size(
                    extra_tracks=items - len(db.tracks),
                    extra_playlist_entries=playlist_entries - entries,
                )
            )

        return await asyncio.to_thread(work)

    def _has_db(self) -> bool:
        """False on an iPod that was never synced or whose iTunesDB was deleted: reads see it as empty,
        the first sync (begin) and Rename create the database like libgpod's itdb_init."""
        from pygpod.device.mountpoint import find_itunesdb

        return (self.mount / "iPod_Control").is_dir() and find_itunesdb(str(self.mount)) is not None

    def _create_db(self, name: str) -> None:
        """Write the missing iPod_Control tree and an empty database named `name`; existing files stay."""
        from pygpod import init_ipod

        log.info("%s: no iTunesDB on the iPod, creating an empty one", self.id)
        init_ipod(str(self.mount), name=name)

    def _open_db(self) -> Any:
        from pygpod import Database

        return Database(str(self.mount))  # play counts merged in memory, consumed only by save()

    async def missing(self, manifest: Manifest) -> set[str]:
        """Manifest entries whose track is gone from the database or whose file is gone."""

        def work() -> set[str]:
            dbids = {str(t.dbid) for t in self._open_db().tracks} if self._has_db() else set()
            return {
                k
                for k, e in manifest.items.items()
                if e.device_ref not in dbids or not (self.mount / e.path).is_file()
            }

        return await asyncio.to_thread(work)

    @staticmethod
    def _kind(track: Any) -> MediaKind:
        from pygpod.db.constants import (
            MEDIATYPE_AUDIOBOOK,
            MEDIATYPE_MUSICVIDEO,
            MEDIATYPE_PODCAST,
            MEDIATYPE_TVSHOW,
            MEDIATYPE_VIDEO,
        )

        if track.media_type & (MEDIATYPE_VIDEO | MEDIATYPE_MUSICVIDEO | MEDIATYPE_TVSHOW):
            return MediaKind.VIDEO
        if track.media_type & MEDIATYPE_PODCAST:
            return MediaKind.PODCAST
        if track.media_type & MEDIATYPE_AUDIOBOOK:
            return MediaKind.AUDIOBOOK
        return MediaKind.MUSIC

    def _rel_path(self, track: Any) -> str:
        return "/".join(p for p in track.ipod_path.split(":") if p)

    async def read_contents(self) -> DeviceContents:
        """Everything in the iTunesDB, also what other tools put there; names and tags come from the database
        - the files under iPod_Control/Music/Fxx have meaningless names."""

        def work() -> DeviceContents:
            if not self._has_db():
                return DeviceContents(items=[], playlists=[])
            db = self._open_db()
            items = [
                DeviceItem(
                    device_ref=str(t.dbid),
                    path=self._rel_path(t),
                    size=t.file_size,
                    title=t.title or None,
                    artist=t.artist or None,
                    album=t.album or None,
                    album_artist=t.albumartist or None,
                    genre=t.genre or None,
                    year=t.year or None,
                    track_number=t.track_number or None,
                    disc_number=t.cd_number or None,
                    duration_s=(t.duration_ms / 1000) if t.duration_ms else None,
                    kind=self._kind(t),
                )
                for t in db.tracks
            ]
            playlists = [p for p in db.playlists if not p.is_master]
            return DeviceContents(
                items=items,
                playlists=sorted(p.name for p in playlists),
                playlist_items={p.name: [str(t.dbid) for t in p.tracks] for p in playlists},
            )

        return await asyncio.to_thread(work)

    async def read_feedback(self, manifest: Manifest | None) -> DeviceFeedback:
        if manifest is None or not await asyncio.to_thread(self._has_db):
            return DeviceFeedback()

        def work() -> DeviceFeedback:
            db = self._open_db()
            by_ref = manifest.by_device_ref()
            fb = DeviceFeedback()
            for t in db.tracks:
                entry = by_ref.get(str(t.dbid))
                if entry is None:
                    continue
                key = entry.ref.key()
                fb.play_counts[key] = t.play_count
                delta = t.play_count - entry.last_playcount
                played = (
                    _local_wall_to_utc(t.time_played, self.s.timezone)
                    if t.record.fields.get("time_played")
                    else None
                )
                if delta > 0:
                    last = played or dt.datetime.now(dt.UTC)
                    step = dt.timedelta(
                        seconds=entry.duration_s or (t.duration_ms or 0) / 1000 or 180
                    )
                    fb.plays += [
                        PlayEvent(
                            ref=entry.ref,
                            played_at=last - i * step,
                            played_at_estimated=i > 0,
                            source_device=self.id,
                        )
                        for i in range(delta)
                    ]
                stars = _stars(t.rating)
                if stars != entry.last_rating and (stars is not None or entry.last_rating):
                    fb.ratings[key] = stars
                if entry.kind is not MediaKind.MUSIC and t.bookmark_time:
                    pos = t.bookmark_time / 1000
                    if entry.last_position_s is None or abs(pos - entry.last_position_s) >= 1:
                        fb.positions[key] = PositionInfo(position_s=pos, updated_at=played)
            pending = db.pending_play_counts() if hasattr(db, "pending_play_counts") else []
            fb.unmapped = sum(1 for p in pending if str(p.track.dbid) not in by_ref)
            return fb

        return await asyncio.to_thread(work)

    # --- session ------------------------------------------------------------------------------

    async def _ensure_sysinfo_extended(self) -> None:
        """Write iPod_Control/Device/SysInfoExtended from the iPod (SCSI, like libgpod) when it is missing: a
        stock restore leaves SysInfo empty, and without a model pygpod would save the database without the
        hash the iPod needs."""
        guard = self.ctx.guard if self.ctx else None
        target = self.mount / "iPod_Control" / "Device" / "SysInfoExtended"
        if guard is None or self.block_path is None or await asyncio.to_thread(target.is_file):
            return
        from syncmusic.infra.blockdev import scsi

        ext = await scsi.read_sysinfo_extended(guard, self.block_path)
        if ext:
            await asyncio.to_thread(manifest_io.atomic_write, target, ext)
            self._pg_device = None
            log.info("%s: SysInfoExtended read from the iPod", self.id)

    def backup_db(self) -> Path | None:
        """Copy the database files to `<cache_dir>/device-backups/<id>/<UTC time>/` when the device's sync settings
        ask for it, keeping the newest `backup_keep` (0 = all). Raises when the copy fails: the sync must
        not write the database without the backup it was asked to make."""
        sync = self.config.sync
        if not sync.backup_device_db:
            return None
        if self.ctx is None:
            raise RuntimeError(f"{self.id}: device database backup is on but there is no cache dir")
        root = Path(self.ctx.cache_dir) / "device-backups" / self.id
        target = root / dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
        otg = sorted(
            p.relative_to(self.mount)
            for p in (self.mount / "iPod_Control/iTunes").glob("OTGPlaylistInfo*")
        )
        copied = 0
        for rel in (*BACKUP_FILES, *map(str, otg)):
            src = self.mount / rel
            if src.is_file():
                dst = target / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
        if copied == 0:
            return None  # a freshly formatted iPod: nothing to back up
        if sync.backup_keep:
            for old in sorted(p for p in root.iterdir() if p.is_dir())[: -sync.backup_keep]:
                shutil.rmtree(old)
        log.info("%s: database backed up to %s", self.id, target)
        return target

    async def begin(self) -> DeviceSession:
        await self._ensure_sysinfo_extended()

        def work() -> DeviceSession:
            self.backup_db()  # before anything of this sync is written
            roll_back_patch(self.mount)  # an interrupted in-place tag patch
            manifest = manifest_io.read(self.mount)
            s = DeviceSession(manifest)
            removed = manifest_io.clean_inflight(self.mount, manifest)
            for f in (self.mount / MUSIC_DIR).rglob(".sdtmp-*"):
                f.unlink(missing_ok=True)
            if not self._has_db():
                self._create_db(self.config.name)
            db = self._open_db()
            # Self-heal after a crash between the database write and the manifest write, or after files
            # were deleted outside SyncMusic: drop database tracks of managed/leftover files that are gone.
            managed = {e.path for e in manifest.items.values()} if manifest else set()
            gone = []
            for t in db.tracks:
                rel = self._rel_path(t)
                if (rel in managed or rel in removed) and not (self.mount / rel).is_file():
                    log.warning("%s: dropping database entry of missing file %s", self.id, rel)
                    gone.append(t)
            db.remove_tracks(gone)
            s.data["db"] = db
            s.data["by_dbid"] = {str(t.dbid): t for t in db.tracks}  # O(1) lookups
            s.data["remove"] = []  # tracks removed in this session, dropped from the DB at commit
            s.data["dbids"] = (
                {k: e.device_ref for k, e in manifest.items.items()} if manifest else {}
            )
            return s

        return await asyncio.to_thread(work)

    def _new_rel(self, ext: str, music_dirs: int) -> str:
        while True:
            folder = f"F{secrets.randbelow(music_dirs):02d}"
            name = "".join(secrets.choice(NAME_CHARS) for _ in range(4))
            rel = f"{MUSIC_DIR}/{folder}/{name}.{ext}"
            if not (self.mount / rel).exists():
                return rel

    @staticmethod
    def _tags(track: Track) -> dict[str, Any]:
        """pygpod add_track keywords for a library track."""
        from pygpod.db.constants import (
            MEDIATYPE_AUDIO,
            MEDIATYPE_AUDIOBOOK,
            MEDIATYPE_PODCAST,
            MEDIATYPE_TVSHOW,
            MEDIATYPE_VIDEO,
        )

        tags: dict[str, Any] = {
            "title": track.title,
            "artist": track.artist or "",
            "album": track.album or (track.series or ""),
            "albumartist": track.album_artist or "",
            "genre": track.genres[0] if track.genres else "",
            "year": track.year or 0,
            "track_number": track.track_no or 0,
            "cd_number": track.disc_no or 0,
            "compilation": track.compilation,
            "rating": (track.rating or 0) * 20,
            "comment": track.comment or "",
        }
        if track.kind is MediaKind.PODCAST:
            tags["media_type"] = MEDIATYPE_PODCAST
            # the iPod groups the podcast list by album: the show's name
            tags["album"] = track.series or track.album or "Podcasts"
            tags["category"] = tags["album"]
            if track.comment:
                tags["description"] = track.comment  # the episode's subtitle (ABS)
        elif track.kind is MediaKind.AUDIOBOOK:
            tags["media_type"] = MEDIATYPE_AUDIOBOOK
            tags["artist"] = track.author or track.artist or ""
        elif track.kind is MediaKind.VIDEO:
            # an episode of a show goes under TV Shows, anything else under Movies
            show, season, episode = (track.extra.get(k) for k in ("show", "season", "episode"))
            tags["media_type"] = MEDIATYPE_TVSHOW if show else MEDIATYPE_VIDEO
            if show:
                tags["tvshow"] = str(show)
                tags["album"] = str(show)
                tags["season_number"] = season if isinstance(season, int) else 0
                tags["episode_number"] = episode if isinstance(episode, int) else 0
        else:
            tags["media_type"] = MEDIATYPE_AUDIO
        if track.published is not None and track.kind in (MediaKind.PODCAST, MediaKind.AUDIOBOOK):
            tags["time_released"] = track.published  # the Podcasts view's release date
        if track.kind not in (MediaKind.MUSIC, MediaKind.VIDEO):
            tags["remember_position"] = True
            tags["bookmark_time"] = int((track.position_s or 0) * 1000)
        return tags

    async def put_track(self, s: DeviceSession, item: PreparedItem) -> DeviceTrackRef:
        db = s.data["db"]
        ext = item.plan.file.ext.lstrip(".") or EXT_FOR_FORMAT.get(item.plan.file.format, "mp3")
        rel = self._new_rel(ext, db.device.music_dirs if db.device else 50)
        dest = self.mount / rel
        tags = self._tags(item.track)
        tags["play_count"] = item.track.play_count
        if self.effective_capabilities().cover is not None and item.cover and item.cover.is_file():
            tags["cover_art"] = item.cover.read_bytes()

        def work() -> Any:
            from pygpod.tags import soundcheck_from_gain

            manifest_io.add_inflight(self.mount, [rel])
            copy_atomic(item.path, dest)
            # the file's own tags fill what the library leaves empty: an empty keyword would win
            # over the file in pygpod
            own = tagmap.read(dest).tags
            kw = _with_file_tags(tags, own)
            gain = _gain_db(own.get("replaygain_track_gain"))
            if gain is not None:  # the gain the retag processor wrote; else pygpod reads the tags
                kw["soundcheck"] = soundcheck_from_gain(gain)
            try:  # pygpod validates the content for the model (UnsupportedFormatError)
                t = db.add_track(str(dest), copy=False, **kw)
            except BaseException:
                dest.unlink(missing_ok=True)
                raise
            for attr, key in SORT_FIELDS.items():  # pygpod's add_track takes no sort names or BPM
                if own.get(key):
                    setattr(t, attr, own[key][0])
            if _int(own.get("bpm")):
                t.bpm = _int(own.get("bpm"))
            return t

        s.inflight.append(rel)
        t = await asyncio.to_thread(work)
        s.data["dbids"][item.track.ref.key()] = str(t.dbid)
        s.data["by_dbid"][str(t.dbid)] = t
        return DeviceTrackRef(device_ref=str(t.dbid), path=rel, size=dest.stat().st_size)

    async def rewrite_tags(
        self, s: DeviceSession, ref: DeviceTrackRef, item: PreparedItem
    ) -> DeviceTrackRef | None:
        """Tags-only re-process: patch the changed bytes of the track file in place (journalled like
        the file devices) and update the database entry. None (a normal replace) when the size changed, the
        changed range is large, or the cover cannot be rendered. A cover for the ArtworkDB replaces the track's
        thumbnails in place (pygpod set_artwork)."""
        t = s.data["by_dbid"].get(ref.device_ref)
        if t is None:
            return None
        cover = None
        if self.effective_capabilities().cover is not None and item.cover and item.cover.is_file():
            cover = item.cover.read_bytes()
        rel = self._rel_path(t)
        dest = self.mount / rel
        if not (self.mount / rel).resolve().is_relative_to((self.mount / MUSIC_DIR).resolve()):
            return None
        if not dest.is_file() or dest.stat().st_size != item.path.stat().st_size:
            return None
        span = await asyncio.to_thread(changed_range, item.path, dest)
        if span is not None and span[1] - span[0] > PATCH_MAX:
            return None
        if cover is not None:
            from pygpod import ArtworkError

            try:  # rendered before the old image goes; on error the old cover is kept
                await asyncio.to_thread(s.data["db"].set_artwork, t, cover)
            except (ArtworkError, OSError):  # 0.2.3 raises PIL's UnidentifiedImageError
                return None
        if span is not None:
            await asyncio.to_thread(patch_file, self.mount, rel, item.path, *span)
        await self.update_metadata(s, ref, item.track)
        return DeviceTrackRef(device_ref=ref.device_ref, path=rel, size=dest.stat().st_size)

    async def remove_track(self, s: DeviceSession, ref: DeviceTrackRef) -> None:
        """Deletes the file now; the database entries of all removed tracks go in one batch at commit
        (pygpod remove_tracks: per-track removal is linear in the database size). A crash in
        between leaves entries without files, which begin() drops."""
        t = s.data["by_dbid"].pop(ref.device_ref, None)
        rel = self._rel_path(t) if t is not None else ref.path
        if t is not None:
            s.data["remove"].append(t)

        def work() -> None:
            # never outside Music/ (paths come from the device database / manifest)
            p = (self.mount / rel).resolve()
            if p.is_relative_to((self.mount / MUSIC_DIR).resolve()):
                p.unlink(missing_ok=True)

        await asyncio.to_thread(work)
        for key, dbid in list(s.data["dbids"].items()):
            if dbid == ref.device_ref:
                del s.data["dbids"][key]

    async def update_metadata(self, s: DeviceSession, ref: DeviceTrackRef, track: Track) -> None:
        t = s.data["by_dbid"].get(ref.device_ref)
        if t is None:
            raise KeyError(f"{self.id}: track {ref.device_ref} is not in the iPod database")
        tags = self._tags(track)
        for attr in (  # an empty library value leaves the database's (from the file) alone
            "title",
            "artist",
            "album",
            "albumartist",
            "genre",
            "year",
            "track_number",
            "cd_number",
            "compilation",
            "rating",
            "comment",
        ):
            if attr != "rating" and not tags[attr]:
                continue
            if getattr(t, attr) != tags[attr]:
                setattr(t, attr, tags[attr])
        for attr in ("time_released", "description"):  # spoken word only
            if attr in tags and getattr(t, attr) != tags[attr]:
                setattr(t, attr, tags[attr])

    async def set_position(self, s: DeviceSession, ref: DeviceTrackRef, position_s: float) -> bool:
        t = s.data["by_dbid"].get(ref.device_ref)
        if t is None:
            raise KeyError(f"{self.id}: track {ref.device_ref} is not in the iPod database")
        t.bookmark_time = int(position_s * 1000)
        return True

    async def set_playlists(self, s: DeviceSession, playlists: list[DevicePlaylist]) -> None:
        db = s.data["db"]
        dbids: dict[str, str] = s.data["dbids"]

        def work() -> None:
            by_dbid = {str(t.dbid): t for t in db.tracks}
            old = set(s.manifest.playlists) if s.manifest else set()
            new = {p.name for p in playlists}
            for name in old - new:
                db.delete_playlist_by_name(name)
            for pl in playlists:
                tracks = [by_dbid[dbids[r.key()]] for r in pl.refs if dbids.get(r.key()) in by_dbid]
                db.set_playlist(pl.name, tracks)

        await asyncio.to_thread(work)

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        db = s.data["db"]

        def work() -> None:
            # iTunesDB + ArtworkDB (+ hash), files and directories fsynced by pygpod's transaction;
            # consumes the Play Counts file. Track files were fsynced by copy_atomic.
            db.remove_tracks(s.data["remove"])
            s.data["remove"] = []
            db.save()
            manifest_io.write(self.mount, manifest)
            manifest_io.clear_inflight(self.mount)

        await asyncio.to_thread(work)
        s.inflight.clear()
        s.manifest = manifest

    async def rename(self, name: str) -> None:
        """The iPod's name is its master playlist's name (pygpod copies it to the MHSD type 3 mirror on save).
        Play counts are not applied, so the reverse sync still finds them."""

        def work() -> None:
            from pygpod import Database

            if not self._has_db():
                self._create_db(name)
                return
            db = Database(str(self.mount), apply_play_counts=False)
            master = db.master_playlist
            if master is None:
                raise RuntimeError(f"{self.id}: the iTunesDB has no master playlist")
            master.name = name
            db.save(refresh_smart_playlists=False)

        await asyncio.to_thread(work)

    def label_for(self, name: str, fs: str | None, current: str | None) -> str | None:
        """The data partition's FAT label only when it already has one (iTunes leaves it empty on some models)."""
        return super().label_for(name, fs, current) if current else None

    async def set_time(self, now: dt.datetime) -> None:
        """Set the iPod's clock (vendor SCSI WRITE BUFFER to the whole disk, guard operation `time_sync`). Raises scsi.TimeSyncRejected when the model refuses the command."""
        from syncmusic.infra.blockdev import scsi

        guard = self.ctx.guard if self.ctx else None
        if guard is None or self.block_path is None:
            raise RuntimeError(f"{self.id}: time sync needs the block device (not a directory)")
        await scsi.set_ipod_time(guard, self.block_path, now, self.s.timezone)

    async def format(self, name: str, fs: str | None = None) -> str | None:
        """Data partition only: read SysInfo, `mkfs.vfat -F 32` on the FAT data partition
        through the guard (which refuses the whole disk and the firmware partition), mount, pygpod creates the
        `iPod_Control` tree and an empty database named `name` and restores SysInfo. The partition table and
        the firmware partition are never touched. Updates `config.identity.fs_uuid`."""
        guard = self.ctx.guard if self.ctx else None
        if guard is None or self.ctx is None or self.block_path is None:
            raise RuntimeError(
                f"{self.id}: format needs the block device (not mounted from a disk)"
            )
        if fs not in (None, "vfat"):
            raise ValueError("an iPod needs FAT32 (vfat)")
        root = self.ctx.config.mount_root
        disks = await asyncio.to_thread(scan_block_devices, False)
        disk, _ = find_disk_of(self.block_path, disks)
        data = self._data_partition(disk)
        if data is None or data.type != "part":
            raise RuntimeError(f"{self.id}: no FAT data partition found on {disk.path}")
        if await self._has_rockbox(data.path):
            # a dual-boot Rockbox iPod registered as stock: reformatting would erase Rockbox
            raise NotImplementedError(
                "this iPod has Rockbox installed (/.rockbox): formatting would erase it - register it as a "
                "Rockbox player to wipe its music instead"
            )
        device_files = await self._saved_device_files(data.path)
        sysinfo = {k: v for k, v in device_files.items() if k in ("SysInfo", "SysInfoExtended")}
        sysinfo["SysInfo"] = with_guid(sysinfo.get("SysInfo", b""), disk.serial)
        if not sysinfo.get("SysInfoExtended"):
            from syncmusic.infra.blockdev import scsi

            if ext := await scsi.read_sysinfo_extended(guard, disk.path):
                sysinfo["SysInfoExtended"] = ext
        await fmt.make_filesystem(guard, data.path, "vfat", name)
        m = await mnt.mount(guard, data.path, self.id, root)
        self.mount = m.path
        self._pg_device = None

        def init() -> None:
            from pygpod import init_ipod

            init_ipod(str(self.mount), name=name, sysinfo_files=sysinfo)
            device_dir = self.mount / "iPod_Control" / "Device"
            for (
                fname,
                content,
            ) in device_files.items():  # the iPod's settings (Preferences, clock, ...)
                if fname not in sysinfo:
                    (device_dir / fname).write_bytes(content)
            (self.mount / manifest_io.STATE_DIR).mkdir(exist_ok=True)

        await asyncio.to_thread(init)
        disks = await asyncio.to_thread(scan_block_devices, False)
        _, node = find_disk_of(data.path, disks)
        self.config.identity = self.config.identity.model_copy(update={"fs_uuid": node.uuid})
        return data.path

    @staticmethod
    def _device_files(root: Path) -> dict[str, bytes]:
        """Small files in iPod_Control/Device: SysInfo/SysInfoExtended (identity, hash key) and the iPod's
        own settings (Preferences, clock, alarms); kept across a format."""
        from pygpod.device.mountpoint import read_sysinfo_files

        out = read_sysinfo_files(str(root))
        device_dir = root / "iPod_Control" / "Device"
        if device_dir.is_dir():
            for f in device_dir.iterdir():
                if f.is_file() and f.name not in out and f.stat().st_size <= DEVICE_FILE_MAX:
                    out[f.name] = f.read_bytes()
        return out

    async def _has_rockbox(self, part: str) -> bool:
        """Whether the current file system holds a Rockbox install (False when it does not mount)."""
        assert self.ctx is not None and self.ctx.guard is not None
        if (self.mount / "iPod_Control").is_dir():
            return await asyncio.to_thread((self.mount / ".rockbox").exists)
        try:
            return await mnt.probe_read(
                self.ctx.guard,
                part,
                self.ctx.config.mount_root,
                lambda root: (root / ".rockbox").exists(),
            )
        except mnt.BlockdevError:
            return False

    async def _saved_device_files(self, part: str) -> dict[str, bytes]:
        """iPod_Control/Device files from the current file system (mounted read-only for that), {} if the
        file system does not mount (corrupt) or has none."""
        assert self.ctx is not None and self.ctx.guard is not None
        if (self.mount / "iPod_Control").is_dir():
            return await asyncio.to_thread(self._device_files, self.mount)
        try:
            return await mnt.probe_read(
                self.ctx.guard, part, self.ctx.config.mount_root, self._device_files
            )
        except mnt.BlockdevError as e:
            log.warning("%s: SysInfo not readable before format (%s)", self.id, e)
            return {}

    async def abort(self, s: DeviceSession) -> None:
        """Forget the in-memory database; delete the files this session copied."""
        s.data.pop("db", None)
        await asyncio.to_thread(manifest_io.clean_inflight, self.mount, s.manifest)
        s.inflight.clear()
