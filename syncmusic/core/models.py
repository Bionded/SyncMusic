# SPDX-License-Identifier: GPL-3.0-or-later
"""Pydantic domain models. Pure data: no I/O, no imports from impl/interface packages."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_serializer,
    field_validator,
    model_validator,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(v: datetime) -> datetime:
    return v.replace(tzinfo=UTC) if v.tzinfo is None else v


# Datetimes are always timezone-aware (naive input is taken as UTC).
UtcDatetime = Annotated[datetime, AfterValidator(_aware)]
Rating = Annotated[int, Field(ge=0, le=5)]  # stars
NonNegInt = Annotated[int, Field(ge=0)]


# ---------------------------------------------------------------------------------------------
# Media items
# ---------------------------------------------------------------------------------------------


class MediaKind(StrEnum):
    MUSIC = "music"
    PODCAST = "podcast"
    AUDIOBOOK = "audiobook"
    VIDEO = "video"  # folder libraries only; a device takes it only with supports_video


class ItemRef(BaseModel):
    """Globally unique reference: registered library id + the library's own item id."""

    model_config = ConfigDict(frozen=True)

    library_id: str
    item_id: str

    def key(self) -> str:
        return f"{self.library_id}/{self.item_id}"

    @classmethod
    def parse(cls, key: str) -> ItemRef:
        lib, _, item = key.partition("/")
        return cls(library_id=lib, item_id=item)


class Track(BaseModel):
    ref: ItemRef
    kind: MediaKind = MediaKind.MUSIC
    title: str
    artist: str | None = None
    album_artist: str | None = None
    album: str | None = None
    album_id: str | None = None
    artist_id: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    # discs of the release (tag, else the album's highest disc number in the library)
    disc_total: int | None = None
    year: int | None = None
    genres: list[str] = []
    duration_s: float | None = None
    size_bytes: NonNegInt = 0
    format: str = ""  # container/codec family: "flac", "mp3", "aac", "alac", "opus", "ogg", ...
    codec: str | None = None
    bitrate_kbps: int | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    rating: Rating | None = None  # 0–5 stars, None = unrated
    starred: bool = False
    play_count: NonNegInt = 0
    last_played: UtcDatetime | None = None
    date_added: UtcDatetime | None = None
    date_modified: UtcDatetime | None = None
    compilation: bool = False
    comment: str | None = None
    path: str | None = None  # path relative to the library root (if the library knows it)
    has_cover: bool = False
    has_lyrics: bool = False
    # podcasts/audiobooks
    series: str | None = None  # podcast name / audiobook title or series
    author: str | None = None  # audiobook author (falls back to artist)
    chapter_index: int | None = None
    position_s: float | None = None  # resume position known by the library
    finished: bool = False
    published: UtcDatetime | None = None  # podcast episode publication date
    # change detection: anything that changes when the source file changes (size/mtime, etag)
    source_state: str = ""
    extra: dict[str, Any] = {}  # library-specific fields usable by rules (`extra.<key>`)


_TRACK_FIELDS_SET: set[str] = set(Track.model_fields)
_REF_FIELDS_SET: set[str] = set(ItemRef.model_fields)


def compact_tracks(tracks: Iterable[Track]) -> None:
    """Let many long-lived tracks share one "fields set" object: Pydantic keeps a set of the explicitly set field
    names per instance (~0.7 KB per track, half of a snapshot track's memory). Only `exclude_unset` dumps read it,
    and no code dumps tracks that way, so marking every field as set changes nothing."""
    for t in tracks:
        object.__setattr__(t, "__pydantic_fields_set__", _TRACK_FIELDS_SET)
        object.__setattr__(t.ref, "__pydantic_fields_set__", _REF_FIELDS_SET)


class Playlist(BaseModel):
    ref: ItemRef
    name: str
    track_refs: list[ItemRef] = []
    smart: bool = False


class LibrarySnapshot(BaseModel):
    library_id: str
    state_token: str
    taken_at: UtcDatetime = Field(default_factory=utcnow)
    tracks: dict[str, Track] = {}  # by item_id
    playlists: dict[str, Playlist] = {}
    # shown in the plan of a device that keeps orphans of this library (what the library no longer provides)
    notice: str | None = None
    _books: dict[str, Track] | None = PrivateAttr(default=None)

    @property
    def books(self) -> dict[str, Track]:
        """Derived book tracks of the multi-file audiobooks, by book id (core/books.py)."""
        if self._books is None:
            from syncmusic.core.books import book_tracks

            self._books = book_tracks(self.tracks)
        return self._books

    def track(self, item_id: str) -> Track | None:
        """A listed track or a derived book track."""
        return self.tracks.get(item_id) or self.books.get(item_id)

    def has(self, item_id: str) -> bool:
        return item_id in self.tracks or item_id in self.books


_LRC_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")


class Lyrics(BaseModel):
    synced: bool = False
    text: str = ""  # plain text, or LRC text when synced
    lines: list[tuple[float, str]] = []  # (time_s, line) for synced lyrics
    language: str | None = None

    def to_lrc(self) -> str:
        if self.lines:
            out = []
            for t, line in self.lines:
                cs = round(max(0.0, t) * 100)  # centiseconds first, so 59.999 s → [01:00.00]
                m, s = divmod(cs, 6000)
                out.append(f"[{m:02d}:{s // 100:02d}.{s % 100:02d}]{line}")
            return "\n".join(out) + "\n"
        return self.text if self.text.endswith("\n") else self.text + "\n"

    @classmethod
    def from_text(cls, text: str) -> Lyrics:
        """Parse LRC (`[mm:ss.xx]line`, several stamps per line allowed) or plain text."""
        lines: list[tuple[float, str]] = []
        for raw in text.splitlines():
            stamps = _LRC_STAMP.findall(raw)
            if not stamps:
                continue
            body = _LRC_STAMP.sub("", raw).strip()
            for mm, ss in stamps:
                lines.append((int(mm) * 60 + float(ss.replace(":", ".")), body))
        if lines:
            lines.sort(key=lambda x: x[0])
            return cls(synced=True, text=text, lines=lines)
        return cls(synced=False, text=text.strip())


class HealthStatus(BaseModel):
    ok: bool
    message: str = ""
    details: dict[str, Any] = {}


# ---------------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------------


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a misspelt key must not silently widen the rule

    id: str
    name: str = ""
    comment: str = ""
    enabled: bool = True
    libraries: list[str] | None = None  # restrict to these attached libraries (None = all)
    kind: list[MediaKind] | None = None
    criteria: dict[
        str, Any
    ] = {}  # nsp-compatible tree ({"all": [...]} / {"any": [...]} / condition)
    sort: str | None = None  # nsp sort syntax: "field", "-year,title", "random"
    order: Literal["asc", "desc"] = "asc"
    # extension: a random order gets a new seed every week / month, so a rotation also swaps unplayed items
    random_period: Literal["none", "week", "month"] = "none"
    limit: int | None = None
    limit_percent: int | None = None
    limit_size: str | None = None  # extension: "10GB"
    limit_duration: str | None = None  # extension: "48h"
    # extension: every library playlist of the rule's libraries goes to the device (built-in All).
    # Playlists named by an inPlaylist condition go to the device anyway unless skip_playlists.
    include_playlists: bool = False
    skip_playlists: bool = False  # extension: opt-out, "Don't put these playlists on the device"
    as_playlist: str | None = None  # extension: create a device playlist with the rule's result
    nsp_extra: dict[
        str, Any
    ] = {}  # Navidrome-only keys (public, refreshDelay) kept for .nsp round-trip


# Which selected items fill a device limit first; "priority" = the rule priority order alone.
# "random" is stable per device, so a sync does not churn the device.
FillKey = Literal["priority", "rating", "recently_played", "play_count", "recently_added", "random"]


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_ids: list[str] = []
    rules: list[Rule] = []
    excludes: list[Rule] = []
    mode: Literal["mirror", "additive"] = "mirror"
    priority: list[str] = []  # rule ids in priority order for space trimming
    # a limit for the whole device: at most this many bytes (device files after processing) / tracks;
    # what does not fit is chosen by `fill_order` (ties: rule priority, then rule order)
    limit_bytes: Annotated[int, Field(ge=1)] | None = None
    limit_tracks: Annotated[int, Field(ge=1)] | None = None
    fill_order: list[FillKey] = []


class RuleTemplate(BaseModel):
    id: str
    name: str
    # the libraries the template draws from: one, several, or [] = all of the device's libraries
    # (the built-ins). Its rules select only from these.
    library_ids: list[str] = []
    rules: list[Rule] = []
    excludes: list[Rule] = []
    builtin: bool = False  # the former global built-ins; none, kept for old clients
    # a library's default template: generated from code for this library id until it is edited;
    # `edited` = stored as user data since (and part of the settings backup)
    default_of: str | None = None
    edited: bool = False
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    # a copy imported from a player: the original's `<instance id>:<template id>`, the installation it came from
    # and the original's `updated_at` when it was copied ("updated on <name>" when the player has a newer one)
    origin_uid: str | None = None
    origin_name: str = ""
    origin_updated_at: UtcDatetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _one_library(cls, data: Any) -> Any:
        """`library_id` (one library, stored templates and old clients) → `library_ids`."""
        if isinstance(data, dict) and "library_id" in data:
            data = dict(data)
            lid = data.pop("library_id")
            data["library_ids"] = [lid] if lid else []
        return data


class Selection(BaseModel):
    """Result of evaluating a RuleSet: ordered item refs with the rule ids that selected each."""

    items: dict[str, list[str]] = {}  # ItemRef.key() -> rule ids (insertion order = priority order)
    # ItemRef.key() -> id of the first exclude rule that matched it (the plan's reason for a removal)
    excluded: dict[str, str] = {}
    playlists: list[DevicePlaylist] = []
    warnings: list[str] = []
    # the media kinds the enabled rules sync, whatever the device plays (None = not known: every kind counts)
    kinds: set[MediaKind] | None = None

    def refs(self) -> list[ItemRef]:
        return [ItemRef.parse(k) for k in self.items]


# ---------------------------------------------------------------------------------------------
# Registration (persisted)
# ---------------------------------------------------------------------------------------------


class LibraryAccount(BaseModel):
    """Another user of the library (a family member's Navidrome/ABS user) that devices can sign in
    as: its settings override the library's (username/password, api_token, …)."""

    name: str = Field(min_length=1)
    settings: dict[str, Any] = {}


class LibraryConfig(BaseModel):
    id: str
    type: str
    name: str
    enabled: bool = True
    settings: dict[str, Any] = {}
    # extra accounts, account id → account; devices pick one in `DeviceConfig.library_accounts`
    accounts: dict[str, LibraryAccount] = {}
    # stable id of the source behind it (`Library.source_id`), set when it is first read; never edited.
    # `source_id_now`: what the source reports now when that differs - the library points somewhere else
    source_id: str | None = None
    source_id_now: str | None = None

    @field_validator("type")
    @classmethod
    def _former_type(cls, v: str) -> str:
        """`navidrome` is the Subsonic-compatible type (same settings, same library id)."""
        return "subsonic" if v == "navidrome" else v


class DeviceIdentity(BaseModel):
    usb_serial: str | None = None
    # further serials of the same device (the iPod mini reports another one in Apple disk mode than
    # under Rockbox); matched and accepted by the guard like `usb_serial`
    usb_serial_aliases: list[str] = []
    fs_uuid: str | None = None
    # unique id written to the device's manifest header (`device_uid`, a uuid set at register): tells this
    # registration's manifest apart from one written by another registration with the same slug id
    manifest_device_id: str | None = None

    def serials(self) -> list[str]:
        return [x for x in (self.usb_serial, *self.usb_serial_aliases) if x]

    def guard_ids(self) -> list[str]:
        """The identities the blockdev guard accepts for this device: every serial and the fs UUID."""
        return [*self.serials(), *([self.fs_uuid] if self.fs_uuid else [])]

    def format_guard_ids(self) -> list[str] | None:
        """Identities for a format (new fs UUID): the serials, or None (removable-only check) without a serial."""
        return self.guard_ids() if self.serials() else None


class ProcessorSlot(BaseModel):
    type: str
    enabled: bool = True
    settings: dict[str, Any] = {}


class ProcessorTemplate(BaseModel):
    """A reusable processor chain. A device applies it once (a copy of the chain) or stays linked
    (`DeviceConfig.processor_template_id`): every change of the template is then copied into the device's chain."""

    id: str
    name: str
    device_type: str | None = None  # made for this device type (the built-ins); None = any
    processors: list[ProcessorSlot] = []
    builtin: bool = False  # shipped with the app (core/proc_templates.py): read-only, can be copied
    updated_at: UtcDatetime = Field(default_factory=utcnow)


class ReverseSyncSettings(BaseModel):
    enabled: bool = True
    plays: bool = True
    ratings: bool = True
    positions: bool = True
    # device edits of library playlists go back to the library (players that edit playlists)
    playlists: bool = True
    # no play thresholds here: the Rockbox settings `play_min_fraction` / `play_min_seconds` are the only ones
    # ; DeviceConfig moves values stored by older builds there


# device types whose new devices combine multi-file audiobooks into one file by default
COMBINE_AUDIOBOOKS_TYPES = frozenset({"ipod"})


class SyncSettings(BaseModel):
    rating_priority: Literal["device", "library"] = "device"
    backup_device_db: bool = False
    backup_keep: int = Field(5, ge=0)
    # no clock switch here: the iPod setting `time_sync` is the only one; a stored `time_sync` key
    # from older configs is ignored (pydantic's default extra="ignore")
    safety_margin_pct: float = Field(1.0, ge=0, le=50)
    safety_margin_min_bytes: int = Field(100 * 1024 * 1024, ge=0)
    # cap so the margin does not grow with big disks; the minimum wins if it is larger
    safety_margin_max_bytes: int = Field(500 * 1024 * 1024, ge=0)
    # gentle write mode for players that hang under many small writes (CF/SD-modded iPods): the
    # full sync writes `gentle_batch_files` files, syncs
    # the file system and pauses `gentle_pause_s`; optional write rate limit. Disk devices only; updates unaffected
    gentle_write: bool = False
    # unmount other programs' mounts of the device (a host automounter) during the first minute after the app
    # mounted it; disk devices only
    unmount_foreign: bool = False
    gentle_batch_files: int = Field(25, ge=1, le=1000)
    gentle_pause_s: float = Field(2.0, ge=0, le=60)
    gentle_rate_mb_s: float | None = Field(None, gt=0, le=1000)
    # read back after a full sync: every item, a stable 10 % sample (the flush and
    # re-mount still happen, they are what catches lost writes), or not at all
    verify: Literal["all", "sample", "off"] = "all"
    # no-cache mode: prepared files are not stored in the processing cache; at most
    # `no_cache_files` items (together about `no_cache_budget_mb`) wait in memory for the device write, an item
    # above `no_cache_file_max_mb` is spooled to /var/tmp and deleted after its write
    no_cache: bool = False
    no_cache_files: int = Field(4, ge=1, le=32)
    no_cache_budget_mb: int = Field(1000, ge=16, le=8000)
    no_cache_file_max_mb: int = Field(256, ge=1, le=4000)
    # a multi-file audiobook as one chaptered .m4b; the default comes from the device type when the
    # device is created (stock iPod yes - it shows every file as a book -, the others no)
    combine_audiobooks: bool = False
    # eject the device when a sync finished successfully: never, after a full sync, after any sync
    # incl. updates - so it can be unplugged right away
    eject_after: Literal["off", "full", "any"] = "off"
    # reverse playlist sync: what happens when a device playlist cannot simply go back - both
    # sides reordered it, or a playlist made on the device is named like a library playlist. ask: nothing is
    # changed, the plan lists it (a per-conflict choice); merge: union (the device's order for a
    # reorder); replace: the device's copy wins; ignore: the library's stays; rename: saved as "<name> (<device>)"
    playlist_conflicts: Literal["ask", "merge", "replace", "ignore", "rename"] = "ask"


# events a webhook can be sent for; `webhook_events` must name only these
NOTIFICATION_EVENTS = (
    "device.connected",
    "device.unknown",
    "device.ejected",
    "device.error",
    "device.formatted",
    "device.time_sync_unsupported",
    "sync.started",
    "sync.needs_confirmation",
    "sync.db_size_warning",
    "sync.finished",
    "sync.failed",
    "reverse.finished",
    "update.failed",
    "update.sent",  # an automatic update sent listening data back
    "storage.low",
    "app.update_available",
    "app.updated",
    "app.update_failed",
    "device.health_warning",
)
# what the in-app notifications centre can store; `inbox_events` picks from these
INBOX_EVENTS = (
    "device.connected",
    "device.unknown",
    "device.error",
    "device.formatted",
    "sync.needs_confirmation",
    "sync.db_size_warning",
    "sync.finished",
    "sync.failed",
    "update.failed",
    "update.sent",
    "reverse.finished",
    "storage.low",
    "app.update_available",
    "app.updated",
    "app.update_failed",
    "device.health_warning",
)


class QuietHours(BaseModel):
    """No webhook between `start` and `end`, host local time; may wrap midnight (23:00 → 07:30)."""

    start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
    end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]

    def covers(self, hhmm: str) -> bool:
        if self.start <= self.end:
            return self.start <= hhmm < self.end
        return hhmm >= self.start or hhmm < self.end


class SyncWindow(BaseModel):
    """Automatic syncs may start between `start` and `end` (host local time) on `days` (0 = Monday); may wrap
    midnight (23:00 → 06:00 belongs to the day it starts on)."""

    start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
    end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
    days: list[Annotated[int, Field(ge=0, le=6)]] = [0, 1, 2, 3, 4, 5, 6]


class AppSettings(BaseModel):
    """Application settings changed at runtime (`GET/PATCH /settings`), stored in the `settings` table."""

    model_config = ConfigDict(extra="forbid")

    # how this instance is named on devices and in other instances; "" = the host name
    instance_name: str = Field("", max_length=64)
    cache_max_gb: float = Field(20.0, gt=0)  # processing cache size (LRU eviction above it)
    # combined multi-file audiobooks stay in the cache even for no-cache devices
    cache_combined_books: bool = True
    concurrency: int | None = Field(
        None, ge=1, le=16
    )  # processing threads; None = CPUs - 1 (after a restart)
    max_device_syncs: int | None = Field(
        None, ge=1, le=8
    )  # device syncs / reverse syncs running at once; None = no limit
    webhook_url: str | None = None  # notifications
    webhook_events: list[str] = [
        "device.connected",
        "device.unknown",
        "sync.started",
        "sync.needs_confirmation",
        "sync.db_size_warning",
        "sync.finished",
        "sync.failed",
    ]
    # the in-app notifications centre stores these (a `sync.finished` that is partial or failed counts as
    # `sync.failed` here and for the webhook: Settings' rows "Full sync finished" / "Full sync failed or partial")
    # Settings → Cache & space: sync history (reports, jobs) and rotated log files older than this are
    # deleted (the newest full / update report of each device is kept); `storage.low` when the disk holding the
    # app's data and cache has less free space than `disk_warn_gb` (None = no warning)
    # Settings → Libraries: when a plan asks the library whether its snapshot is still current -
    # before every sync (the library's state token, as before), at most hourly, or only on Refresh in the library
    snapshot_refresh: Literal["sync", "hourly", "manual"] = "sync"
    # a library change starts a device's update at most this often (the change waits for the next slot),
    # and a snapshot expired by a reverse sync is reloaded in the background at most this often
    update_change_gap_min: int = Field(15, ge=1, le=1440)
    # Settings → Conversion: the ffmpeg binary (None = FFMPEG from the environment, else PATH; its
    # ffprobe next to it) and the nice level conversions run at
    ffmpeg_path: str | None = None
    convert_nice: int = Field(10, ge=0, le=19)
    # Settings → Devices: compas-sdtool, which builds a HiBy R1 Compás player's library during the sync (None =
    # looked for next to the SyncMusic binary, then in <data dir>/tools)
    compas_sdtool_path: str | None = None
    # Settings → Maintenance: the app's log level at runtime; None = the configuration file's
    log_level: Literal["DEBUG", "INFO", "WARNING"] | None = None
    # Settings → Updates: which releases count as updates - stable releases only, or the pre-releases
    # built from every change of main too
    update_channel: Literal["stable", "beta"] = "stable"
    # installing them (single-file binaries only): off = notify only; after_syncs = once no job runs (automatic
    # syncs are held back meanwhile); now = at once, running syncs are interrupted and resume after the restart
    auto_update: Literal["off", "after_syncs", "now"] = "off"
    history_days: int = Field(90, ge=1, le=3650)
    log_days: int = Field(14, ge=1, le=365)
    # Settings → Notifications: notifications (read and unread) older than this are deleted by
    # the hourly housekeeping; None = kept (up to the store's count limit)
    notification_days: int | None = Field(7, ge=1, le=3650)
    disk_warn_gb: float | None = Field(5.0, gt=0, le=10_000)
    inbox_events: list[str] = list(INBOX_EVENTS)
    quiet_hours: QuietHours | None = None  # no webhook in this window
    # for devices registered without one; "manual" since the update sync on connect: new files
    # only when the user presses Sync. Stored settings and existing devices keep their value
    default_on_connect: Literal["auto", "ask", "manual"] = "manual"
    # a new device's `sync_windows`; empty = any time
    default_sync_windows: list[SyncWindow] = []
    # Settings → Sync defaults: what a newly registered device starts with (it can change them), and a
    # write speed limit for every disk device's full sync (MB/s; in gentle mode the lower limit wins); None = none
    default_update_sync: bool = True
    # a new device's `update_every_min` (was a fixed 15); None = only on connect and on changes
    default_update_every_min: int | None = Field(60, ge=1, le=1440)
    default_verify: Literal["all", "sample", "off"] = "all"
    default_eject_after: Literal["off", "full", "any"] = (
        "off"  # a new device's `SyncSettings.eject_after` (613)
    )
    # the processing cache for new devices (`SyncSettings.no_cache`) and the memory limits
    # of no-cache mode they start with
    default_no_cache: bool = False
    default_no_cache_files: int = Field(4, ge=1, le=32)
    default_no_cache_budget_mb: int = Field(1000, ge=16, le=8000)
    default_no_cache_file_max_mb: int = Field(256, ge=1, le=4000)
    write_limit_mb_s: float | None = Field(None, gt=0, le=1000)
    # Settings → Backup: automatic database backups - how often, how many are kept, and where
    # (None = <data_dir>/backups); one is also made before an update and before a factory reset
    db_backup_every: Literal["off", "6h", "daily", "weekly"] = "daily"
    db_backup_keep: int = Field(7, ge=1, le=365)
    db_backup_dir: str | None = None
    # Settings → Clients: Wi-Fi client apps may pair and sync (off: the client API answers nothing but an error
    # and nothing is announced); the server announces itself on the LAN via mDNS; how long a pairing code is valid;
    # how often a client asks whether a sync is due
    clients_allowed: bool = False
    clients_mdns: bool = True
    client_code_minutes: int = Field(10, ge=1, le=60)
    client_poll_s: int = Field(300, ge=30, le=86400)

    @field_validator("ffmpeg_path")
    @classmethod
    def _ffmpeg_name(cls, v: str | None) -> str | None:
        """Only an ffmpeg binary: the setting runs the file it names."""
        if v is None or not v.strip():
            return None
        if not v.strip().rsplit("/", 1)[-1].startswith("ffmpeg"):
            raise ValueError("must be the path of an ffmpeg binary (a file named ffmpeg…)")
        return v.strip()

    @field_validator("compas_sdtool_path")
    @classmethod
    def _sdtool_name(cls, v: str | None) -> str | None:
        """Only a compas-sdtool binary: the setting runs the file it names."""
        if v is None or not v.strip():
            return None
        if not v.strip().rsplit("/", 1)[-1].startswith("compas-sdtool"):
            raise ValueError(
                "must be the path of a compas-sdtool binary (a file named compas-sdtool…)"
            )
        return v.strip()

    @field_validator("webhook_url")
    @classmethod
    def _http_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        parts = urlsplit(v.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            # the URL may carry a token: the message does not repeat it
            raise ValueError("must be an http:// or https:// URL")
        return v.strip()

    @field_validator("inbox_events")
    @classmethod
    def _inbox_events(cls, v: list[str]) -> list[str]:
        unknown = [e for e in v if e not in INBOX_EVENTS]
        if unknown:
            raise ValueError(
                f"not an in-app event {', '.join(map(repr, unknown))} (known: {', '.join(INBOX_EVENTS)})"
            )
        return list(dict.fromkeys(v))

    @field_validator("webhook_events")
    @classmethod
    def _known_events(cls, v: list[str]) -> list[str]:
        unknown = [e for e in v if e not in NOTIFICATION_EVENTS]
        if unknown:
            raise ValueError(
                f"unknown event {', '.join(map(repr, unknown))} (known: {', '.join(NOTIFICATION_EVENTS)})"
            )
        return list(dict.fromkeys(v))


class ErasedMark(BaseModel):
    """A Wipe or Format of the device: it is empty since `at`, so the device pages say so instead of the
    last sync's "synced" until the next full sync."""

    how: Literal["wiped", "formatted"]
    at: UtcDatetime = Field(default_factory=utcnow)
    removed: int | None = None  # files a wipe removed


class DeviceConfig(BaseModel):
    id: str
    type: str
    name: str
    identity: DeviceIdentity = DeviceIdentity()
    library_ids: list[str] = []
    # the library account this device signs in as: library id → an id of
    # `LibraryConfig.accounts`; none = the library's main account; plays, stars and positions go there
    library_accounts: dict[str, str] = {}
    # per-device credentials stored, moved into library accounts by `SyncService.load`
    legacy_library_accounts: dict[str, dict[str, Any]] = Field({}, exclude=True)
    rule_set: RuleSet = RuleSet()
    processors: list[ProcessorSlot] = []
    # linked processor template: `processors` follows it; None = the device's own chain
    processor_template_id: str | None = None
    # the quality profile the chain was made from (max / space / weak); None = Custom
    quality_profile: Literal["max", "space", "weak"] | None = None
    on_connect: Literal["auto", "ask", "manual"] = "ask"
    # automatic syncs (on connect, update, daily `sync_at`) start only inside one of these windows; outside they wait
    # for the next one; empty = any time; manual syncs always run
    sync_windows: list[SyncWindow] = []
    # a daily sync at this time of day, host local time "HH:MM": started when the device is
    # connected and idle, at most once a day, within SCHEDULE_WINDOW after it; None = no schedule
    sync_at: (
        Annotated[
            str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", json_schema_extra={"format": "time"})
        ]
        | None
    ) = None
    delete_threshold_pct: int = Field(20, ge=0, le=100)
    reverse_sync: ReverseSyncSettings = ReverseSyncSettings()
    sync: SyncSettings = SyncSettings()
    capability_overrides: dict[str, Any] = {}
    settings: dict[str, Any] = {}
    # a directory used as the device instead of a block device (pre-mounted trees, tests)
    mount_path: str | None = None
    # what the user chose for the first sync (keep / adopt / wipe), stored when it was applied: the
    # plan review preselects it when a first sync has to be planned again (e.g. after an interrupted one)
    first_sync_choice: Literal["keep", "adopt", "wipe"] | None = None
    # an update sync may remove what no rule selects any more; off: removals wait for a full sync
    update_removals: bool = False
    # ...except items played, finished or rated on this device that no rule selects any more because of it (a played
    # episode of an unplayed-only rule): removed by update syncs too, under the delete threshold
    update_remove_played: bool = True
    # items deleted from their library are removed by full and update syncs, under the delete
    # threshold; an empty library or a mass vanish keeps them as orphans. Off: orphans stay until Remove orphans
    remove_deleted: bool = True
    # update syncs on connect and while connected: every N minutes and after library changes;
    # None = only on connect and on changes
    update_sync: bool = True
    update_every_min: int | None = Field(15, ge=1, le=1440)
    # the last Wipe / Format; set by those actions, not editable
    erased: ErasedMark | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_accounts(cls, data: Any) -> Any:
        """Credentials stored in the device itself wait in `legacy_library_accounts` until they are
        moved into an account of the library."""
        if not isinstance(data, dict) or not isinstance(data.get("library_accounts"), dict):
            return data
        accounts = data["library_accounts"]
        legacy = {k: v for k, v in accounts.items() if isinstance(v, dict)}
        if not legacy:
            return data
        return {
            **data,
            "library_accounts": {k: v for k, v in accounts.items() if not isinstance(v, dict)},
            "legacy_library_accounts": {**data.get("legacy_library_accounts", {}), **legacy},
        }

    @model_validator(mode="before")
    @classmethod
    def _combine_audiobooks_default(cls, data: Any) -> Any:
        """`sync.combine_audiobooks` missing (a new device, or one stored before): the device type's
        default - on for the stock iPod, which merged always, otherwise the old `merge_audiobooks` setting."""
        if not isinstance(data, dict):
            return data
        sync = data.get("sync")
        if sync is None:
            sync = {}
        if not isinstance(sync, dict) or "combine_audiobooks" in sync:
            return data
        sync = dict(sync)
        legacy = bool(sync.pop("merge_audiobooks", False))
        sync["combine_audiobooks"] = legacy or data.get("type") in COMBINE_AUDIOBOOKS_TYPES
        return {**data, "sync": sync}

    @model_validator(mode="before")
    @classmethod
    def _move_play_thresholds(cls, data: Any) -> Any:
        """Older builds had a second, unused pair `reverse_sync.min_play_fraction/seconds`: a value
        changed there moves to the Rockbox settings unless those already have one; other device types drop it."""
        if not isinstance(data, dict) or not isinstance(data.get("reverse_sync"), dict):
            return data
        rs = dict(data["reverse_sync"])
        moved = {
            "play_min_fraction": rs.pop("min_play_fraction", None),
            "play_min_seconds": rs.pop("min_play_seconds", None),
        }
        if data.get("type") == "rockbox":
            settings = dict(data.get("settings") or {})
            defaults = {"play_min_fraction": 0.5, "play_min_seconds": 240}
            for key, value in moved.items():
                if key not in settings and value not in (None, defaults[key]) and value > 0:
                    settings[key] = value
            data = {**data, "settings": settings}
        return {**data, "reverse_sync": rs}


# ---------------------------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------------------------


class PlayEvent(BaseModel):
    ref: ItemRef
    played_at: UtcDatetime
    played_at_estimated: bool = False
    source_device: str = ""


class PositionInfo(BaseModel):
    position_s: float
    finished: bool = False
    updated_at: UtcDatetime | None = None


class DeviceFeedback(BaseModel):
    plays: list[PlayEvent] = []
    ratings: dict[str, Rating | None] = {}  # ItemRef.key() -> rating 0–5 / None
    positions: dict[str, PositionInfo] = {}  # ItemRef.key() -> position
    play_counts: dict[str, int] = {}  # ItemRef.key() -> absolute device play count (if known)
    stars: dict[
        str, bool
    ] = {}  # ItemRef.key() -> favourite added (True) / removed (False) on the device
    unmapped: int = 0  # entries that could not be mapped to a managed item

    def is_empty(self) -> bool:
        return not (self.plays or self.ratings or self.positions or self.stars)


# ---------------------------------------------------------------------------------------------
# Device state, planning, reporting
# ---------------------------------------------------------------------------------------------

MANIFEST_VERSION = 2


class ManifestLibrary(BaseModel):
    """A library with items on the device, as the manifest header describes it (no credentials). Entries
    name it by its header key; `id` is the library id on the instance that last wrote it."""

    id: str = ""
    source_id: str | None = None
    type: str = ""
    name: str = ""
    url_hint: str = ""
    instance: str = ""  # id of the instance whose library it is
    probe: list[dict[str, Any]] = []  # a few items on the device: item_id, title, duration_s


class InstanceInfo(BaseModel):
    """A SyncMusic installation that synced the device (the name is a label; logic uses the id)."""

    id: str
    name: str = ""
    app_version: str = ""
    since: UtcDatetime | None = None  # as the home: when it became the home


class Interruption(BaseModel):
    """A sync committed at a checkpoint and not finished yet: `done` of its `total` files written."""

    plan_id: str
    done: int
    total: int
    at: UtcDatetime = Field(default_factory=utcnow)


class ManifestHeader(BaseModel):
    type: Literal["header"] = "header"
    manifest_version: int = MANIFEST_VERSION
    app_version: str = ""
    device_id: str
    device_uid: str | None = (
        None  # DeviceIdentity.manifest_device_id of the writer (None: written before)
    )
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    # set by checkpoints, cleared by the final commit
    interrupted: Interruption | None = None
    # what the entries' `processing` part hashes are made of (Chain.processing_inputs): one copy
    # per hash, so the next plan can say what changed; empty in manifests written before
    processing_inputs: dict[str, dict[str, Any]] = {}
    # libraries with items on the device by their device-local key ("L1", ...); empty in version 1
    libraries: dict[str, ManifestLibrary] = {}
    # the instance that owns the device (set at the first sync) and the one that wrote the manifest last
    home: InstanceInfo | None = None
    last_writer: InstanceInfo | None = None


class ManifestEntry(BaseModel):
    type: Literal["item"] = "item"
    ref: ItemRef
    device_ref: str  # path on device, or device DB id (iPod dbid)
    path: str  # relative path on the device
    size: int
    source_state: str = ""
    chain_hash: str = ""
    meta_hash: str = ""  # hash of metadata written to the device (detects update_meta)
    # what `chain_hash` is made of, per part (Chain.processing): a changed hash names the processor and whether the
    # audio changes; empty for entries written before, filled by the next sync that keeps them
    processing: dict[str, str] = {}
    # the caps_hash scheme of its "device" part (prepare.CAPS_HASH_VERSION); 0 = written before
    caps_v: int = 0
    # the metadata written (planner.meta_snapshot): the plan shows "field: old → new"; empty for
    # entries written before
    meta: dict[str, str] = {}
    sidecars: list[str] = []  # files written with the track (DeviceTrackRef.sidecars)
    # the device's path template the file was written with (caps.path_layout, "" = none / unknown)
    layout: str = ""
    synced_at: UtcDatetime = Field(default_factory=utcnow)
    rules: list[str] = []
    kind: MediaKind = MediaKind.MUSIC
    duration_s: float | None = None
    last_playcount: int = 0
    # plays read from the device by another installation that does not have this item's library: not sent yet;
    # the installation whose library it is sends them at its next reverse sync (iPods reset their counters)
    pending_plays: int = 0
    last_rating: Rating | None = None
    last_position_s: float | None = None
    # the library's favourite last mirrored as a heart (HiBy); None = not yet
    last_starred: bool | None = None
    # its library no longer has the item: kept on the device, no reverse sync, removable
    orphan: bool = False


class Manifest(BaseModel):
    header: ManifestHeader
    items: dict[str, ManifestEntry] = {}  # ItemRef.key() -> entry
    playlists: list[str] = []  # names of device playlists we manage
    # file devices: name -> the playlist file (device-relative), so a moved playlist folder is cleaned up;
    # a name without an entry (written before) is in the default `Playlists/`
    playlist_paths: dict[str, str] = {}
    # name -> a hash of the refs last written, so a sync can tell which playlists changed
    playlist_hashes: dict[str, str] = {}
    # reverse playlist sync: name -> the item keys last written, in order (the merge base), and
    # the library playlist it came from (ItemRef key; absent for rule playlists) with its smart flag
    playlist_refs: dict[str, list[str]] = {}
    playlist_sources: dict[str, str] = {}
    playlist_smart: set[str] = set()
    # playlists created on the device and saved to a library: name -> that playlist's ItemRef key. Not
    # managed (the forward sync never deletes their file); their base is in `playlist_refs` like a managed one's
    playlist_created: dict[str, str] = {}
    # library id in `items` -> its key in `header.libraries` (filled when read, extended when written)
    lib_keys: dict[str, str] = {}

    def by_device_ref(self) -> dict[str, ManifestEntry]:
        return {e.device_ref: e for e in self.items.values()}


class DeviceStats(BaseModel):
    """Listening data a device already holds for a file it cannot be told about (Rockbox's tagcache keeps
    counts per path). The manifest's last-known values start from these, so only later changes count."""

    playcount: int = 0
    rating: Rating | None = None
    position_s: float | None = None  # resume position the device holds for the file (None: none)


class DeviceTrackRef(BaseModel):
    device_ref: str
    path: str
    size: int
    sidecars: list[
        str
    ] = []  # relative paths of files written with the track (.lrc, folder cover.jpg)
    stats: DeviceStats | None = (
        None  # None: the device stores the library's values (iPod: incl. the bookmark)
    )


class DeviceItem(BaseModel):
    """Something physically present on a device (from its DB or a file walk)."""

    device_ref: str
    path: str
    size: int = 0
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration_s: float | None = None
    kind: MediaKind = MediaKind.MUSIC
    stats: DeviceStats | None = None  # see DeviceTrackRef.stats
    # what the device itself says about the item (iPod: its iTunesDB, files: their tags) - Player
    # contents shows these, never an iPod's F00/ABCD.m4a file name
    album_artist: str | None = None
    genre: str | None = None
    year: int | None = None
    track_number: int | None = None
    disc_number: int | None = None


class DeviceContents(BaseModel):
    items: list[DeviceItem] = []
    playlists: list[str] = []
    # playlist name → device_refs of its items in order; a name may have no entry (unknown members)
    playlist_items: dict[str, list[str]] = {}


class DevicePlaylist(BaseModel):
    name: str
    refs: list[ItemRef] = []
    # the library playlist it comes from (None: a rule's `as_playlist`); a smart one is never pushed back
    source: ItemRef | None = None
    smart: bool = False


class PlaylistChange(BaseModel):
    """What a sync does to one device playlist (the plan's Playlists section). `rule`: made by a
    rule's `as_playlist`, else a library playlist (`smart` one included)."""

    name: str
    change: Literal["create", "update", "remove", "same"]
    songs: int = 0
    rule: bool = False
    smart: bool = False


def playlist_hash(refs: list[ItemRef]) -> str:
    """The playlist's songs in order, as stored in `Manifest.playlist_hashes`."""
    return hashlib.sha1("\n".join(r.key() for r in refs).encode()).hexdigest()[:16]


class PlannedFile(BaseModel):
    """Predicted (plan) or actual (work) state of the file that will land on the device."""

    format: str
    codec: str | None = None
    ext: str
    size: int
    bitrate_kbps: int | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    # encoder mode not visible in the fields above (transcode: "vbr 0" vs "cbr"), part of its cache key
    encoding: str | None = None


class PlanItem(BaseModel):
    track: Track
    file: PlannedFile
    steps: list[str] = []  # processor types that will change the item
    sidecars: list[str] = []  # predicted sidecar extensions, e.g. [".lrc"]
    sidecar_bytes: int = 0
    rules: list[str] = []
    enable_space_steps: bool = False  # transcode `when: space_needed` rules active


class WorkItem(BaseModel):
    """A file being processed in the per-sync work dir (the processor chain's unit of work)."""

    plan: PlanItem
    path: Path  # current file in the work dir
    work_dir: Path
    cover: Path | None = None  # prepared cover image (covers processor / library cover)
    lyrics: Lyrics | None = None  # from the library (fetched when a processor `needs` it)
    sidecars: dict[str, Path] = {}  # extension (".lrc") -> file to copy next to the track
    steps_done: list[str] = []
    notes: dict[str, Any] = {}  # e.g. {"encoder": "aac"} recorded by processors

    @property
    def track(self) -> Track:
        return self.plan.track


PreparedItem = WorkItem  # what Device.put_track receives: a WorkItem after the whole chain


class ActionKind(StrEnum):
    ADD = "add"
    REPLACE = "replace"
    UPDATE_META = "update_meta"
    REMOVE = "remove"
    KEEP = "keep"


class MetaChange(BaseModel):
    """One metadata value a sync changes: `old`/`new` as text (a position in seconds, a rating
    0–5); `old` None = not known (written before the device remembered it) or not set."""

    field: str  # a META_FIELDS name, "position" or "metadata" (changed, the earlier values are not known)
    old: str | None = None
    new: str | None = None
    direction: Literal["to_device", "to_library"] = "to_device"
    conflict: str = ""  # both sides changed: which one wins and why


class PlanAction(BaseModel):
    kind: ActionKind
    ref: ItemRef
    title: str = ""
    # a removal of an item its library still knows: `title` is the item's title and this its artist - album
    # ; None = `title` is the file's path on the device
    subtitle: str | None = None
    item: PlanItem | None = None  # add / replace / update_meta / keep
    entry: ManifestEntry | None = (
        None  # existing manifest entry (replace / remove / update_meta / keep)
    )
    size_delta: int = 0
    rules: list[str] = []
    reason: str = ""
    chain_hash: str = ""  # of the planned processing (goes into the manifest entry)
    processing: dict[str, str] = {}  # its parts (Chain.processing)
    # replace: only tag/cover/sidecar processors changed, the audio stays the same
    tags_only: bool = False
    # keep: only the caps hash scheme changed, the applier stores the new chain hash
    rehash: bool = False
    # keep: the source was only touched (timestamp moved, size/cover/metadata the same): the applier stores the
    # library's new source_state, nothing is copied
    restamp: bool = False
    meta_hash: str = ""  # of the library metadata (planner.meta_hash)
    orphan: bool = False  # keep: its library no longer has the item
    deleted: bool = False  # remove: deleted from its library
    changes: list[MetaChange] = []  # the metadata this action changes


class RuleSpace(BaseModel):
    rule_id: str
    name: str = ""
    items: int = 0
    bytes: int = 0
    dropped_items: int = 0
    dropped_bytes: int = 0


class LeftOut(BaseModel):
    key: str  # ItemRef key
    title: str = ""
    size: int = 0


class LimitReport(BaseModel):
    """What the device limit (RuleSet.limit_bytes / limit_tracks) left out of the plan."""

    limit_bytes: int | None = None
    limit_tracks: int | None = None
    fill_order: list[str] = []
    kept_items: int = 0
    kept_bytes: int = 0
    left_out: list[LeftOut] = []
    left_out_bytes: int = 0


# capacity/free of a device that cannot tell its space (FTP without a quota): the plan does not limit it, and the
# API and UI say "unknown" instead of showing the number; JSON carries null for it
UNKNOWN_SPACE = 2**50


class SpaceReport(BaseModel):
    capacity: int = 0
    free: int = 0
    needed: int = 0  # net bytes the plan needs (incl. reserve)
    reserve: int = 0  # db overhead + safety margin
    fits: bool = True
    per_rule: list[RuleSpace] = []
    dropped: list[str] = []  # ItemRef keys dropped by priority trimming (forced sync)
    dropped_detail: list[LeftOut] = []  # the same items with title and size (plan review list)
    suggestions: list[str] = []
    space_steps_used: bool = False
    unknown: bool = False  # the device cannot tell its space (FTP without quota): not checked

    @field_validator("capacity", "free", mode="before")
    @classmethod
    def _null_is_unknown(cls, v: Any) -> Any:
        return UNKNOWN_SPACE if v is None else v

    @field_serializer("capacity", "free")
    def _unknown_is_null(self, v: int) -> int | None:
        return None if v >= UNKNOWN_SPACE else v


class DbSize(BaseModel):
    """Size of a device database with a hard limit (the iPod's iTunesDB)."""

    current: int | None = None  # bytes now (None: no database yet)
    estimated: int = 0  # bytes after applying the plan
    limit: int = 0
    level: Literal["ok", "warn", "over"] = "ok"  # warn: >= DB_WARN_FRACTION of the limit


class PlaylistConflict(BaseModel):
    """A playlist conflict the plan review asks about: `reorder` = reordered on the device and in the
    library, `name_taken` = made on the device with the name of a library playlist."""

    name: str
    kind: Literal["reorder", "name_taken"]


class SyncPlan(BaseModel):
    id: str
    device_id: str
    # the media kinds the device's rules sync (Selection.kinds); a device keeps its own data of the others
    synced_kinds: set[MediaKind] | None = None
    device_uid: str | None = (
        None  # DeviceIdentity.manifest_device_id of the writer (None: written before)
    )
    created_at: UtcDatetime = Field(default_factory=utcnow)
    actions: list[PlanAction] = []
    playlists: list[DevicePlaylist] = []
    playlist_changes: list[PlaylistChange] = []  # the plan's Playlists section
    space: SpaceReport = SpaceReport()
    first_sync: bool = False
    does_not_fit: bool = False
    needs_confirmation: bool = False
    confirmation_reasons: list[str] = []
    # the processing parts' readable inputs by hash; the applier stores them in the manifest header
    processing_inputs: dict[str, dict[str, Any]] = {}
    # removes more than the device's delete threshold: the plan page asks to confirm them
    removals_over_threshold: bool = False
    warnings: list[str] = []
    db: DbSize | None = None  # devices whose database has a size limit
    resuming: Interruption | None = None  # the last sync stopped after a checkpoint
    limit: LimitReport | None = None  # the device limit left items out
    playlist_conflicts: list[PlaylistConflict] = []  # the review chooses per playlist

    def count(self, kind: ActionKind) -> int:
        return sum(1 for a in self.actions if a.kind == kind)


class ItemFailure(BaseModel):
    ref: str
    title: str = ""
    error: str
    # which track and where it failed: "artist – album – title", the step (download, cover, lyrics,
    # processor type, put, remove, metadata, position) and the file on the device when there is one
    item: str = ""
    step: str = ""
    path: str = ""


class ListeningChange(BaseModel):
    """A favourite or rating a reverse sync set in a library, with the value it replaced (for an undo)."""

    kind: Literal["star", "rating"]
    ref: str
    value: bool | Rating | None = None
    previous: bool | Rating | None = None


class ReverseReport(BaseModel):
    plays_found: int = 0
    plays_submitted: int = 0
    plays_duplicate: int = 0
    ratings_pushed: int = 0
    stars_pushed: int = 0  # favourites added/removed on the device, sent as star/unstar
    positions_pushed: int = 0
    # device positions not sent: the library's position moved too and is newer
    positions_superseded: int = 0
    # podcasts/audiobooks played to the end on the device, sent as finished
    finished_pushed: int = 0
    pending: int = 0
    # what is still pending per library and kind ({library id: {"play": 2, "rating": 1}}), and how many rows this
    # run gave up on after the attempt limit (the report line and the device's "Not delivered")
    pending_by: dict[str, dict[str, int]] = {}
    dropped: int = 0
    errors: list[str] = []
    warnings: list[str] = []  # e.g. the player records no listening data (Device.feedback_warnings)
    failed: bool = False  # step 0 raised: the device's listening data was not read
    # ratings changed on the device that win over the plan's library values (pushed, or still pending);
    # with rating_priority=library: the device ratings the forward sync replaces
    device_ratings: dict[str, Rating | None] = {}
    # positions changed on the device (pushed, or still pending): the forward sync leaves those bookmarks alone
    # ; None: positions were not read (reverse sync of positions off, or step 0 failed)
    device_positions: dict[str, float] | None = None
    # items whose play, rating or finish from this device a library took in this run: an update sync may remove
    # those that no rule selects any more because of it
    consumed: list[str] = []
    # library playlists saved from the device's edits: name -> the item keys saved, in order
    playlists_pushed: dict[str, list[str]] = {}
    playlists_created: list[str] = []  # device-made playlists saved to a library
    # playlists not pushed because the policy is "ask": the plan / report lists them
    playlist_conflicts: list[str] = []
    # favourites removed on the device all at once and not sent (a suspected reset player database): the user
    # can still send them as removals from the report (`SyncService.send_held_unstars`)
    held_unstars: list[str] = []
    # favourites and ratings this run set in the libraries, with their previous values
    # (`SyncService.undo_listening_changes` sends the previous ones back); emptied once undone
    changes: list[ListeningChange] = []


class SyncReport(BaseModel):
    id: str
    device_id: str
    plan_id: str | None = None
    # "skipped": an update sync that did not run (before the first full sync, another registration's device)
    status: Literal["running", "succeeded", "partial", "failed", "cancelled", "skipped"] = "running"
    started_at: UtcDatetime = Field(default_factory=utcnow)
    finished_at: UtcDatetime | None = None
    counts: dict[str, int] = {}
    bytes_written: int = 0
    # the device is ejected right after this sync (`SyncSettings.eject_after`)
    auto_eject: bool = False
    write_seconds: float = 0.0  # time spent writing files to the device: its write speed
    # time the writer waited for the next file to be prepared (download + processors); a large share means the
    # sync is bound by the library or the processors, not by the device
    wait_seconds: float = 0.0
    failures: list[ItemFailure] = []
    skipped_for_space: list[str] = []
    removed_for_commit: list[str] = []
    reverse: ReverseReport | None = None
    message: str = ""
    # the choice of a first sync
    first_sync: Literal["keep", "adopt", "wipe"] | None = None
    # "update": an update sync - metadata, ratings, positions, playlists, no new files;
    # the History tab's filter; the reverse sync runs as step 0 of both (the standalone one went)
    kind: Literal["full", "update"] = "full"
    # a full sync of a disk device failed on its storage (I/O error, dropped off USB) while gentle write mode is off:
    # the UI offers to turn it on - never switched on by itself
    suggest_gentle: bool = False
    # what the run held back on purpose (an update's removals above the delete threshold)
    warnings: list[str] = []
    # what the user should do on the player after unplugging (Compás: reload the plugins): eject and notification
    player_notice: str = ""
    # items not written because their library stayed unreachable: planned again by the next sync, no
    # failures; `warnings` names the library
    postponed: int = 0
    # device-made playlists saved to a library before and selected by no rule: removed from the device
    playlists_removed: list[str] = []
    # playlists written to the device in this run because they are new or their songs changed: name -> songs; and
    # managed playlists no rule makes any more, removed from the device (the activity log)
    playlists_written: dict[str, int] = {}
    playlists_dropped: list[str] = []


Selection.model_rebuild()
