# SPDX-License-Identifier: GPL-3.0-or-later
"""Capability models of libraries and devices."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from syncmusic.core.models import MediaKind


class LibraryCapabilities(BaseModel):
    kinds: set[MediaKind] = {MediaKind.MUSIC}
    supports_ratings: bool = False
    supports_scrobble: bool = False
    supports_positions: bool = False
    supports_playlists: bool = False
    # favourites (Subsonic star/unstar) taken back from devices with hearts
    supports_starred: bool = False
    # album / artist / playlist pictures by their own ids (Subsonic getCoverArt): the browser's thumbnails
    group_covers: bool = False


class PathRules(BaseModel):
    """File naming constraints of a device filesystem."""

    max_path_len: int = 255  # whole relative path (characters)
    max_name_len: int = 255  # one path component, counted in `name_len_unit`
    name_len_unit: Literal["utf16", "bytes"] = (
        "utf16"  # FAT/exFAT/NTFS: UTF-16 units; ext4: UTF-8 bytes
    )
    forbidden_chars: str = '<>:"/\\|?*'
    strip_trailing: str = " ."  # FAT/NTFS: trailing dots/spaces are not allowed
    case_sensitive: bool = False
    max_file_size: int | None = None  # FAT32: 4 GiB - 1
    ascii_only: bool = False
    # entries in one folder (car stereos); a full folder spills into "<folder> (2)"; None = no limit
    max_files_per_folder: int | None = None

    @classmethod
    def for_filesystem(cls, fstype: str | None) -> PathRules:
        fs = (fstype or "").lower()
        if fs in ("vfat", "fat", "fat32", "msdos"):
            return cls(max_file_size=4 * 1024**3 - 1, max_path_len=240)
        if fs == "exfat":
            return cls(max_path_len=240)
        if fs in ("ntfs", "ntfs3"):
            return cls(max_path_len=240)
        if fs in ("ext4", "ext3", "ext2", "btrfs", "xfs", "f2fs", "tmpfs", "zfs"):
            return cls(
                forbidden_chars="/",
                strip_trailing="",
                case_sensitive=True,
                max_path_len=1000,
                name_len_unit="bytes",
            )
        return cls(max_file_size=4 * 1024**3 - 1, max_path_len=240)


class CoverSpec(BaseModel):
    max_size: int = 500  # px, square bounding box
    format: Literal["jpeg", "png"] = "jpeg"
    progressive: bool = False  # Rockbox needs baseline JPEG
    embed: bool = True
    sidecar_name: str | None = None  # e.g. "cover.jpg"


class DeviceCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")  # overrides are user input: reject misspelled keys

    formats: set[str] = set()  # playable codecs, e.g. {"mp3", "aac", "alac", "wav"}
    max_sample_rate: int | None = None
    max_bit_depth: int | None = None
    # the player handles at most this many tracks (device profile)
    max_tracks: int | None = None
    supports_music: bool = True
    supports_podcasts: bool = False
    supports_audiobooks: bool = False
    supports_video: bool = False  # no device plays video until its part of 341 is built
    # the only video the device plays: "ipod" = H.264 Baseline 320×240 (the `video` processor makes it)
    video_profile: Literal["ipod"] | None = None
    supports_playlists: bool = True
    supports_ratings_read: bool = False
    supports_ratings_write: bool = False
    supports_playcounts_read: bool = False
    supports_positions_read: bool = False
    supports_time_sync: bool = False
    has_rtc: bool | None = None  # None = unknown; affects play timestamps
    needs_device_db: bool = False
    path_rules: PathRules = PathRules()
    # file devices: the path template per media kind the files are written with (settings `layout`); a file
    # written with another template is moved by the next sync. Empty: paths are not the user's (iPod)
    path_layout: dict[MediaKind, str] = {}
    # music of a release with 2+ discs (settings `layout.music_multi_disc`); "" = `path_layout`
    path_layout_multi_disc: str = ""

    def layout_for(self, kind: MediaKind, disc_total: int | None) -> str:
        """The path template a track of `kind` is written with."""
        if kind is MediaKind.MUSIC and (disc_total or 0) >= 2 and self.path_layout_multi_disc:
            return self.path_layout_multi_disc
        return self.path_layout.get(kind, "")

    cover: CoverSpec | None = None
    lyrics: Literal["none", "embedded", "lrc_sidecar"] = "none"
    db_overhead_bytes: int = 0
    # lossless plays but costs CPU/battery (Rockbox on slow targets): suggest converting it to MP3
    prefer_lossy: bool = False

    def supports_kind(self, kind: MediaKind) -> bool:
        return {
            MediaKind.MUSIC: self.supports_music,
            MediaKind.PODCAST: self.supports_podcasts,
            MediaKind.AUDIOBOOK: self.supports_audiobooks,
            MediaKind.VIDEO: self.supports_video,
        }[kind]

    def with_overrides(self, overrides: dict[str, object]) -> DeviceCapabilities:
        """These capabilities with `overrides` (field name -> value) on top. `cover_size` (device profile) is the cover's `max_size`; None turns covers off, a size without a cover spec adds the default one."""
        if not overrides:
            return self.model_copy(deep=True)
        data = self.model_dump()
        rest = dict(overrides)
        if COVER_SIZE in rest:
            size = rest.pop(COVER_SIZE)
            if size is None:
                data["cover"] = None
            else:
                data["cover"] = {**(data["cover"] or CoverSpec().model_dump()), "max_size": size}
        data.update(rest)
        return DeviceCapabilities.model_validate(data)


# Fields that change the file a processor produces: only these go into the "device" part of the chain hash
# (- a new informational field must not re-process every file). Every DeviceCapabilities field is
# in exactly one of the two sets (tests/unit/test_prepare.py checks it): classify each new field. Values equal to
# their default are left out of the hash, so a new field (also one in PathRules or CoverSpec) changes nothing until
# a device sets it.
OUTPUT_FIELDS = frozenset(
    {
        "formats",
        "max_sample_rate",
        "max_bit_depth",
        "video_profile",
        "cover",
        "lyrics",
        "prefer_lossy",  # the transcode processor's lossless → MP3 suggestion
        "needs_device_db",  # covers: embedded vs given to the device DB
        "path_rules",  # the file names (car mode: ASCII): the files are written again
    }
)
# where a file goes, how many fit, what the device reports back: no file changes for them
NON_OUTPUT_FIELDS = frozenset(
    {
        "max_tracks",
        "supports_music",
        "supports_podcasts",
        "supports_audiobooks",
        "supports_video",
        "supports_playlists",
        "supports_ratings_read",
        "supports_ratings_write",
        "supports_playcounts_read",
        "supports_positions_read",
        "supports_time_sync",
        "has_rtc",
        "path_layout",
        "path_layout_multi_disc",
        "db_overhead_bytes",
    }
)

# profile field (core/profile.py) that is not a capability field of its own: `cover.max_size`
COVER_SIZE = "cover_size"
