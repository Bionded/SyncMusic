# SPDX-License-Identifier: GPL-3.0-or-later
"""HiBy player running the Compás firmware, used as USB storage.

File sync: Music/, Podcasts/ (with `database.ignore`, so podcasts stay out of the music library),
Audiobooks/ (the player keeps it out of the music library itself), `Playlists/*.m3u` with relative paths,
`.lrc` sidecars, `cover.jpg` per album. The player re-indexes the card on unplug and at boot; SyncMusic never
writes its library database.

Probe: a card whose root holds `.compas/` (written by the player); `.compas/device.json` names the model, the
player version, the plugin API and the database format. A card with both `.compas/` and the SonixOS
`.local/library.db` is Compás (it outranks the SonixOS class). The Audiobooks and Podcasts plugins are detected by
their assets in `.plugin-assets/<Name>/`: without a plugin the player cannot play that kind, so it is not synced.
The player's own files (hidden entries, themes, radio list, firmware updates, `database.(un)ignore`, the plugins'
state inside Podcasts/ and Audiobooks/ and the podcast episodes the plugin downloaded) are never touched, also
not by Wipe.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict

from syncmusic.base.capabilities import CoverSpec, DeviceCapabilities, PathRules
from syncmusic.base.device import Device, DeviceSession, ProbeResult
from syncmusic.base.progress import report_progress
from syncmusic.core.manifest import STATE_DIR
from syncmusic.core.models import (
    DeviceFeedback,
    DeviceIdentity,
    DeviceTrackRef,
    ItemRef,
    Manifest,
    ManifestEntry,
    MediaKind,
    PreparedItem,
    Track,
)
from syncmusic.core.reverse import is_small_rewind
from syncmusic.devices._filedevice import FileDeviceMixin
from syncmusic.devices.HibyR1 import compas_feedback as cf
from syncmusic.devices.HibyR1 import compas_sdtool as sd
from syncmusic.devices.HibyR1.compas_stats import StatsUpdate, patch_stats
from syncmusic.devices.HibyR1.sonixos import (
    COMPAS_USB_PRODUCT,
    FORMATS,
    HIBY_USB_VENDOR,
    PLAYER_DB_RESET_WARNING,
    HibyData,
    hiby_feedback,
)
from syncmusic.devices.paths import PathLayout, path_key
from syncmusic.infra.blockdev.identify import BlockDeviceInfo

log = logging.getLogger(__name__)

COMPAS_DIR = ".compas"
DEVICE_JSON = "device.json"
PLUGIN_ASSETS = ".plugin-assets"
PODCAST_DIR = "Podcasts"
AUDIOBOOK_DIR = "Audiobooks"
PODCAST_PROGRESS = ".progress.tsv"  # the Podcasts plugin's episode list (downloads, positions)
IGNORE_FILE = "database.ignore"
CARD_PREFIX = re.compile(
    r"^data/mnt/sd_\d+/"
)  # the card as the player addresses it (absolute paths)
FAVOURITES_STATE = (
    "compas-favourites.json"  # in our state dir: the favourites seen at the last reverse sync
)
# in our state dir: the positions SyncMusic last wrote into the plugins' files (a plugin that was not reloaded
# overwrites them with its old ones at its next save; the next sync writes ours again)
POSITIONS_STATE = "compas-positions.json"
# in our state dir: album key -> {"ref": the library's album, "stars": what player and library agreed on last}
ALBUM_RATINGS_STATE = "compas-album-ratings.json"
# in our state dir: the player version (.compas/device.json) of the last sync, to name a firmware update
PLAYER_VERSION_STATE = "compas-player-version.txt"
# the player's files at the card root, kept by Wipe (every hidden entry is kept too, see wipe_keeps)
TAGCACHE_FORMAT = 8  # the `database_format` of .compas/device.json whose rows compas_stats patches
PLAYER_FILES = (COMPAS_DIR, ".plugins", PLUGIN_ASSETS, IGNORE_FILE, "database.unignore")
PLAYER_FILES_FOLDED = ("themes", "radio.txt")  # matched without case


def read_device_info(mount: Path) -> dict[str, Any]:
    """`.compas/device.json` (model, player_version, plugin_api, database_format …); {} when absent or damaged
    (older players do not write it)."""
    try:
        data = json.loads((mount / COMPAS_DIR / DEVICE_JSON).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def installed_plugins(mount: Path) -> set[str]:
    """Names of the plugins whose assets are on the card (`.plugin-assets/<Name>/`)."""
    try:
        return {p.name for p in (mount / PLUGIN_ASSETS).iterdir() if p.is_dir()}
    except OSError:
        return set()


def podcast_downloads(mount: Path) -> set[str]:
    """Card-relative paths of the episodes the Podcasts plugin downloaded (from its `.progress.tsv`: tab separated,
    `P` lines, field 7 = the percent-encoded absolute path on the player); the records SyncMusic wrote for its own
    episodes are not the plugin's."""
    try:
        text = (mount / PODCAST_DIR / PODCAST_PROGRESS).read_text("utf-8", errors="replace")
    except OSError:
        return set()
    out: set[str] = set()
    marker = f"/{PODCAST_DIR}/"
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) >= 7 and fields[0] == "P" and fields[6]:
            if unquote(fields[1]).partition("|")[2].startswith(cf.OUR_GUID):
                continue  # an episode SyncMusic listed, not one the plugin downloaded
            path = unquote(fields[6])
            if marker in path:
                out.add(PODCAST_DIR + "/" + path.split(marker, 1)[1])
    return out


SDTOOL_MISSING = (
    "compas-sdtool was not found, so the player indexes the new music itself after unplugging; "
    "download it in Settings → Sync → HiBy R1 with Compás to have the music, hearts and play counts there right away"
)
RELOAD_HINT = (
    "Restart the player or open Settings → System → Plugin Manager → Refresh so Audiobooks and Podcasts "
    "pick up the new positions"
)


def _spoken(path: str | None) -> bool:
    """An audiobook or podcast file: the plugins' business."""
    return bool(path) and str(path).startswith((f"{AUDIOBOOK_DIR}/", f"{PODCAST_DIR}/"))


def _published(value: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


# what the plugins play (Audiobooks.lua `AUDIO`, Podcasts.lua `audio_ext`; m4a/m4b = AAC or ALAC); anything else
# of that kind is converted (the transcode processor's "not supported" rule)
AUDIOBOOK_FORMATS = frozenset(
    {"mp3", "aac", "alac", "flac", "ogg", "opus", "wav", "aiff", "ape", "wma"}
)
PODCAST_FORMATS = frozenset({"mp3", "aac", "alac", "ogg", "opus", "flac"})


class CompasSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formats: set[str] = set(FORMATS)
    layout: PathLayout = PathLayout()
    lyrics: Literal["none", "embedded", "lrc_sidecar"] = "lrc_sidecar"
    playlists: bool = True


def star_ratings(info: dict[str, Any]) -> bool:
    """True for players with 0-5 star ratings (Compás 1.6.0+): tag 16 holds stars and the card syncs ratings
    only; older players (heart, no or an older `player_version`) sync favourites only. Hearts and stars are
    never converted into each other."""
    v = str(info.get("player_version") or "")
    if not v:
        return False  # players before device.json had the heart
    try:
        parts = tuple(int(x) for x in v.lstrip("v").split("-")[0].split(".")[:2])
    except ValueError:
        return True  # a version we cannot read is a newer one
    return parts >= (1, 6)


def star_changes(
    fb: DeviceFeedback, stars: dict[str, int], manifest: Manifest, rules: PathRules
) -> None:
    """The stars the user changed on the player since the last sync, into `fb`: a new rating for the library
    (0 = cleared). Stars never touch the library's favourites."""
    by_path = {path_key(p, rules): n for p, n in stars.items()}
    for key, e in manifest.items.items():
        if e.kind is not MediaKind.MUSIC or e.orphan:
            continue
        now = by_path.get(path_key(e.path, rules))
        if now is None:  # not indexed (yet): nothing to compare
            continue
        if now != (e.last_rating or 0):
            fb.ratings[key] = now or None


class HibyCompas(FileDeviceMixin, Device):
    type_name = "hiby_compas"
    display_name = "HiBy R1 - Compás"
    Settings: ClassVar[type[BaseModel]] = CompasSettings
    format_filesystems = ("exfat", "vfat")
    firmware_siblings = ("hiby",)
    WIPE_KEEP = (*FileDeviceMixin.WIPE_KEEP, *PLAYER_FILES)
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats=set(FORMATS),
        kind_formats={
            MediaKind.AUDIOBOOK: set(AUDIOBOOK_FORMATS),
            MediaKind.PODCAST: set(PODCAST_FORMATS),
        },
        supports_podcasts=True,
        supports_audiobooks=True,
        supports_playlists=True,
        supports_playcounts_read=True,
        # by firmware (model_capabilities): stars on Compás 1.6.0+, the heart before
        supports_ratings_read=True,
        supports_favourites_read=True,
        supports_positions_read=True,
        has_rtc=True,
        cover=CoverSpec(max_size=600, embed=True, sidecar_name="cover.jpg"),
        lyrics="lrc_sidecar",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._favourites: set[str] | None = None  # to remember after the reverse sync committed
        # positions of ours the plugins dropped or rolled back, seen by the last read_feedback: written again
        self._rewrite: dict[str, tuple[float, bool]] = {}

    @property
    def s(self) -> CompasSettings:
        settings: CompasSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        hiby_usb = blk is not None and (blk.usb_vendor_id or "").casefold() == HIBY_USB_VENDOR
        if mount is None:
            # not mounted yet: the HiBy vendor with the Compás storage gadget's product id
            if not (hiby_usb and blk is not None and blk.usb_product_id == COMPAS_USB_PRODUCT):
                return None
        elif not (mount / COMPAS_DIR).is_dir():
            return None
        info = read_device_info(mount) if mount is not None else {}
        data = None
        if blk is not None:
            data = next((p for p in blk.partitions() if p.fstype), None) or (
                blk if blk.fstype else None
            )
        return ProbeResult(
            type_name=cls.type_name,
            model=None,
            suggested_name=(data.label if data and data.label else None)
            or f"HiBy {str(info.get('model') or 'R1').upper()} Compás",
            identity=DeviceIdentity(
                usb_serial=blk.serial if blk else None, fs_uuid=data.uuid if data else None
            ),
            # above SonixOS (80) on a card with both; below it when the player presents the SonixOS
            # gadget (a card with Compás files in a player that runs SonixOS again)
            confidence=75
            if hiby_usb and blk is not None and blk.usb_product_id not in (None, COMPAS_USB_PRODUCT)
            else 90
            if hiby_usb or blk is None
            else 85,
            data_partition=data.path if data else None,
            details={
                "fstype": data.fstype if data else None,
                "firmware": "compas",
                "player_version": info.get("player_version"),
                "player_model": info.get("model"),
                "plugin_api": info.get("plugin_api"),
                "database_format": info.get("database_format"),
                "plugins": sorted(installed_plugins(mount)) if mount is not None else [],
            },
        )

    def detected_profile(self) -> dict[str, Any]:
        """Audiobooks and podcasts only with the player's plugin for them."""
        plugins = installed_plugins(self.mount)
        return {
            "supports_audiobooks": AUDIOBOOK_DIR in plugins,
            "supports_podcasts": PODCAST_DIR in plugins,
        }

    def player_info(self) -> dict[str, Any]:
        """The player version from `device.json` and the plugins found on the card."""
        info = read_device_info(self.mount)
        return {
            "firmware": "Compás",
            "version": info.get("player_version"),
            "plugins": sorted(installed_plugins(self.mount)),
        }

    def setting_profile(self) -> dict[str, object]:
        out: dict[str, object] = {"formats": set(self.s.formats)} if self.s.formats else {}
        if not self.s.playlists:
            out["supports_playlists"] = False
        return out

    def model_capabilities(self) -> DeviceCapabilities:
        return (
            super()
            .model_capabilities()
            .model_copy(
                update={
                    "path_rules": self.path_rules(),
                    "lyrics": self.s.lyrics,
                    **self._listening_caps(),
                }
            )
        )

    def _listening_caps(self) -> dict[str, bool]:
        """Stars or the heart, by the firmware on the card; both while it is not mounted (unknown)."""
        if not (self.mount / ".compas").is_dir():
            return {}
        stars = star_ratings(read_device_info(self.mount))
        return {"supports_ratings_read": stars, "supports_favourites_read": not stars}

    def _playlist_format(self) -> tuple[str, str]:
        """`.m3u` like the player's own playlists: its remote control and editor address `Playlists/<name>.m3u`."""
        return ".m3u", "\n"

    async def read_playlists(self, manifest: Manifest) -> dict[str, list[str]] | None:
        """The player edits `Playlists/*.m3u` in place (append, remove, reorder, rename, new, delete): they go back
        to the library. Entries it writes as absolute player paths (`/data/mnt/sd_0/...`) map to the card."""
        if not self.s.playlists:
            return None
        lists = await self.playlist_files(manifest)
        return {
            name: [CARD_PREFIX.sub("", p, count=1) for p in entries]
            for name, entries in lists.items()
        }

    def wipe_keeps(self, name: str) -> bool:
        """Also every hidden entry except our state dir, firmware updates (*.upt) and the plugins' folders
        (emptied of everything but the plugins' own files by `wipe`)."""
        return (
            super().wipe_keeps(name)
            or name.casefold() in PLAYER_FILES_FOLDED
            or name in (PODCAST_DIR, AUDIOBOOK_DIR)
            or (name.startswith(".") and name != STATE_DIR.name)
            or name.lower().endswith((".upt", ".upt.parked"))
        )

    async def wipe(self) -> int:
        """Also empties Podcasts/ and Audiobooks/, keeping the plugins' own files there: hidden entries
        (catalog, positions, settings), `database.ignore`, OPML subscription lists and the episodes the
        Podcasts plugin downloaded itself."""
        n = await super().wipe()

        def work() -> int:
            cf.write_podcast_episodes(
                self.mount, []
            )  # our records in the plugin's list go with the files
            keep = podcast_downloads(self.mount)
            count = 0
            for top in (PODCAST_DIR, AUDIOBOOK_DIR):
                root = self.mount / top
                if not root.is_dir():
                    continue
                for f in sorted(root.rglob("*"), reverse=True):  # children before their folders
                    rel = f.relative_to(root)
                    if any(part.startswith(".") for part in rel.parts) or (
                        len(rel.parts) == 1
                        and (f.name == IGNORE_FILE or f.name.lower().endswith(".opml"))
                    ):
                        continue
                    if f.is_dir() and not f.is_symlink():
                        if not any(f.iterdir()):
                            f.rmdir()
                    elif f"{top}/{rel.as_posix()}" not in keep:
                        f.unlink()
                        count += 1
            return count

        return n + await asyncio.to_thread(work)

    async def begin(self) -> DeviceSession:
        """Also marks Podcasts/ with `database.ignore`: the player indexes every other folder as music."""
        s = await super().begin()
        folder = self.mount / PODCAST_DIR
        folder.mkdir(exist_ok=True)
        (folder / IGNORE_FILE).touch(exist_ok=True)
        if await asyncio.to_thread(self._library_reset):
            # built again from scratch below, with every favourite and rating of the library
            s.data["compas_db_reset"] = True
            s.data["compas_music_changed"] = True
        return s

    def _library_reset(self) -> bool:
        """The card has files of ours but the player's library is gone or empty: a firmware update or a reset of
        the player cleared it (a damaged one is replaced by a build too, but is not a reset)."""
        if not (self.mount / ".syncmusic").exists():
            return False
        try:
            entries = cf.read_tagcache(self.mount)
        except (cf.CompasDbError, OSError, ValueError):
            return False
        return not entries

    async def feedback_warnings(self) -> list[str]:
        """The plan and the sync report say when the player's library was cleared (see `_library_reset`) and
        when its firmware changed since the last sync."""

        def work() -> list[str]:
            out = [PLAYER_DB_RESET_WARNING] if self._library_reset() else []
            if changed := self._version_change():
                out.append(f"the player's firmware changed from {changed[0]} to {changed[1]}")
            return out

        return await asyncio.to_thread(work)

    def _player_version(self) -> str | None:
        v = read_device_info(self.mount).get("player_version")
        return str(v) if v else None

    def _version_change(self) -> tuple[str, str] | None:
        """(version at the last sync, version now) when the player was updated since; None otherwise."""
        try:
            old = (self.mount / STATE_DIR / PLAYER_VERSION_STATE).read_text("utf-8").strip()
        except OSError:
            return None
        new = self._player_version()
        return (old, new) if old and new and new != old else None

    def _remember_version(self) -> None:
        if (v := self._player_version()) is not None:
            f = self.mount / STATE_DIR / PLAYER_VERSION_STATE
            f.parent.mkdir(exist_ok=True)
            f.write_text(v, encoding="utf-8")

    def _state_file(self) -> Path:
        return self.mount / STATE_DIR / FAVOURITES_STATE

    def _album_baseline(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads((self.mount / STATE_DIR / ALBUM_RATINGS_STATE).read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _patch_album_ratings(self, tracks: dict[str, Track], manifest: Manifest) -> None:
        """Album ratings of the library onto the player (`album://` rows of remote_state.tsv). A library change
        is written only while the player still shows what both agreed on last; a change on the player goes
        to the library through the reverse sync and is agreed on once the library has it."""
        albums: dict[str, tuple[str, int]] = {}
        for key, e in manifest.items.items():
            t = tracks.get(key)
            if e.orphan or e.kind is not MediaKind.MUSIC or t is None or not t.album_id:
                continue
            akey = cf.album_key(t.album_artist or t.artist or "", t.album or "")
            if akey is not None:
                ref = ItemRef(library_id=t.ref.library_id, item_id=t.album_id).key()
                albums[akey] = (ref, t.album_rating or 0)
        base = self._album_baseline()
        if not albums and not base:
            return
        now = cf.album_stars(self.mount)
        writes: dict[str, int] = {}
        for akey, (ref, lib) in albums.items():
            player = now.get(akey, 0)
            was = base.get(akey, {}).get("stars")
            if player == lib:
                base[akey] = {"ref": ref, "stars": lib}
            elif was is None and player == 0 or was is not None and player == was:
                writes[akey] = lib
                base[akey] = {"ref": ref, "stars": lib}
            elif was is None:
                # rated on the player before SyncMusic knew the album: the reverse sync sends it
                base[akey] = {"ref": ref, "stars": lib}
        if writes:
            cf.write_album_stars(self.mount, writes)
        f = self.mount / STATE_DIR / ALBUM_RATINGS_STATE
        f.parent.mkdir(exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(base, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(f)

    def _written_positions(self) -> dict[str, tuple[float, bool]]:
        try:
            raw = json.loads((self.mount / STATE_DIR / POSITIONS_STATE).read_text(encoding="utf-8"))
            return {str(k): (float(v[0]), bool(v[1])) for k, v in raw.items()}
        except (OSError, ValueError, TypeError, IndexError, AttributeError):
            return {}

    def _remember_positions(self, new: dict[str, tuple[float, bool]], on_card: set[str]) -> None:
        """The positions written now on top of the earlier ones still on the card."""
        known = {p: v for p, v in self._written_positions().items() if p in on_card}
        known.update(new)
        f = self.mount / STATE_DIR / POSITIONS_STATE
        f.parent.mkdir(exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({p: list(v) for p, v in sorted(known.items())}), encoding="utf-8")
        tmp.replace(f)

    def _seen_favourites(self) -> set[str]:
        try:
            return set(json.loads(self._state_file().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return set()  # first reverse sync: the hearts already there are all new

    async def read_feedback(self, manifest: Manifest | None) -> DeviceFeedback:
        """Play counts and favourites from the player's tagcache, positions from the plugins' state files.
        A heart counts as removed only for a file the tagcache still knows: a new, reset or damaged database
        (or a file the player has not indexed yet) never unstars anything."""
        if manifest is None:
            return DeviceFeedback()
        rules = self.path_rules()

        def work() -> DeviceFeedback:
            card = cf.read_card(self.mount)
            seen = self._seen_favourites()
            rates = star_ratings(read_device_info(self.mount))
            # a star player: no heart moves (stars are read below as ratings); a heart player: tag 16 = 1 is
            # the heart, any other value never takes one away
            hearts = seen if rates else card.favourites | (seen & card.rated)
            self._favourites = hearts
            self._rewrite = {}
            for path, (pos, done) in self._written_positions().items():
                there = card.positions.get(path)
                state = (
                    cf.AUDIOBOOK_STATE
                    if path.startswith(f"{AUDIOBOOK_DIR}/")
                    else cf.PODCAST_PROGRESS
                )
                if there is None and not (self.mount / state).is_file():
                    continue  # the plugin is gone (or never ran): nothing to put back
                if there is None or (
                    is_small_rewind(there[0], pos)
                    and there[0] < pos - 1
                    and not (there[1] and not done)
                ):
                    # gone, or a slip back of less than REWIND_S (a plugin's old state included): ours goes
                    # back; a rewind of REWIND_S or more is the listener's and wins
                    card.positions.pop(path, None)
                    self._rewrite[path] = (pos, done)
            fb = hiby_feedback(
                HibyData(card.play_counts, hearts, card.positions),
                manifest,
                rules,
                seen,
                now=dt.datetime.now(dt.UTC),
                device_id=self.id,
            )
            if not rates:
                return fb
            star_changes(fb, card.stars, manifest, rules)
            now = cf.album_stars(self.mount)
            for key, was in self._album_baseline().items():
                stars = now.get(key, 0)
                if stars != was["stars"]:
                    fb.ratings[was["ref"]] = stars or None
            return fb

        return await asyncio.to_thread(work)

    async def reset_feedback(self, s: DeviceSession, consumed: DeviceFeedback) -> None:
        """Remember the favourites the last `read_feedback` saw, so each change is sent only once."""
        favourites, self._favourites = self._favourites, None
        if favourites is None:
            return

        def work() -> None:
            f = self._state_file()
            f.parent.mkdir(exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(favourites)), encoding="utf-8")
            tmp.replace(f)

        await asyncio.to_thread(work)

    async def set_position(
        self, s: DeviceSession, ref: DeviceTrackRef, position_s: float, finished: bool = False
    ) -> bool:
        """An audiobook's or a podcast episode's position goes into the plugin's state file at commit (the
        Audiobooks plugin keeps one position per book; the Podcasts plugin only for episodes it lists)."""
        if not ref.path or not ref.path.startswith((f"{AUDIOBOOK_DIR}/", f"{PODCAST_DIR}/")):
            return False
        s.data.setdefault("compas_positions", {})[ref.path] = (position_s, finished)
        s.data["compas_spoken"] = True
        return True

    async def put_track(self, s: DeviceSession, item: PreparedItem) -> DeviceTrackRef:
        """A new audiobook or episode starts at the library's position: the sync records that position as
        written to the device, so it goes into the plugin's state file at commit like a moved one."""
        ref = await super().put_track(s, item)
        if _spoken(ref.path):
            s.data["compas_spoken"] = True
        else:
            s.data["compas_music_changed"] = True
        pos, done = item.track.position_s, item.track.finished
        if (pos or done) and item.track.kind is not MediaKind.MUSIC:
            await self.set_position(s, ref, pos or 0.0, done)
        return ref

    async def _index(self, s: DeviceSession) -> None:
        """Build the player's library on the card with compas-sdtool (compas_sdtool), so the music is there right
        after unplugging; without a usable tool the player indexes it itself after the USB session."""
        added = s.data.pop("compas_music_changed", False)
        if not added and self._library_readable():
            log.info("%s: player library unchanged, not rewritten", self.id)
            return
        data_dir = self.ctx.data_dir if self.ctx is not None else Path("/nonexistent")
        tool = sd.locate(sd.configured(), data_dir)
        if tool is None:
            if added:
                s.data.setdefault("commit_warnings", []).append(SDTOOL_MISSING)
            return
        card_format = read_device_info(self.mount).get("database_format")
        try:
            why = sd.unusable(await sd.info(tool, self.mount), card_format)
            if why is None:
                done = await sd.scan(
                    tool, self.mount, lambda _phase, n, total: report_progress(n, total)
                )
                log.info(
                    "%s: player library built by %s: %s songs, %s files read",
                    self.id,
                    tool,
                    done.get("songs"),
                    done.get("parsed"),
                )
        except sd.SdtoolError as e:
            why = str(e)
        if why is not None:
            log.warning("%s: %s", self.id, why)
            s.data.setdefault("commit_warnings", []).append(
                f"the player's library was not built on this computer ({why}); the player indexes the new music "
                "after unplugging"
            )

    def _library_readable(self) -> bool:
        """The card carries a player library this code can read (else a build replaces it)."""
        try:
            return cf.read_tagcache(self.mount) is not None
        except (cf.CompasDbError, OSError, ValueError):
            return False

    async def rewrite_tags(
        self, s: DeviceSession, ref: DeviceTrackRef, item: PreparedItem
    ) -> DeviceTrackRef | None:
        """New tags go into the player's library like a new file."""
        out = await super().rewrite_tags(s, ref, item)
        if not _spoken(ref.path):
            s.data["compas_music_changed"] = True
        return out

    async def _patch_stats(self, s: DeviceSession, manifest: Manifest) -> None:
        """The library's hearts, play counts and last played into the rows the player has indexed for our files
        (compas_stats). Play counts only after a reverse sync that sent the player's own (else they would be
        lost); a heart moves only when the library's changed since we last mirrored it, and is taken away only
        when the reverse sync has seen it. The manifest's baselines follow, so nothing comes back as new."""
        if read_device_info(self.mount).get("database_format", TAGCACHE_FORMAT) != TAGCACHE_FORMAT:
            return  # another format: the player keeps its own numbers
        tracks: dict[str, Track] = s.data.get("library_tracks") or {}
        starred: dict[str, bool] = s.data.get("library_starred") or {}
        pushed = bool(s.data.get("reverse_ok", True))
        # a cleared library lost every heart and rating we wrote: all of the library's go back, not only changes
        reset = bool(s.data.get("compas_db_reset"))

        rates = star_ratings(read_device_info(self.mount))

        def work() -> str | None:
            seen = self._seen_favourites()
            updates: dict[str, StatsUpdate] = {}
            # path -> (entry, the library's value now): a rating on a star player, a heart on an older one
            moved: dict[str, tuple[ManifestEntry, int | bool | None]] = {}
            for key, e in manifest.items.items():
                if e.orphan or e.kind is not MediaKind.MUSIC:
                    continue
                t, star = tracks.get(key), starred.get(key)
                stars: int | None = None
                fav: bool | None = None
                if rates and t is not None and (t.rating != e.last_rating or (reset and t.rating)):
                    stars = t.rating or 0  # the library's rating changed since the player got it
                    moved[e.path] = (e, t.rating)
                elif (
                    not rates and star is not None and (star != e.last_starred or (reset and star))
                ):
                    fav = star  # the library's heart changed since we last mirrored it
                    if star or e.path in seen:  # taken away only when the reverse sync has seen it
                        moved[e.path] = (e, star)
                    else:
                        fav = None
                plays = t.play_count if (t is not None and pushed) else None
                last = int(t.last_played.timestamp()) if (pushed and t and t.last_played) else None
                if stars is not None or fav is not None or plays or last:
                    expect = (0 if reset else (e.last_rating or 0)) if stars is not None else None
                    updates[e.path] = StatsUpdate(
                        plays or None, last, favourite=fav, stars=stars, expect=expect
                    )
            if not updates:
                return None
            try:
                done = patch_stats(self.mount, updates)
            except (cf.CompasDbError, OSError) as err:
                return f"ratings and play counts not written to the player: {err}"
            by_path = {e.path: e for e in manifest.items.values()}
            for path, row in done.items():
                if pushed:
                    by_path[path].last_playcount = row.playcount
            for path, (e, value) in moved.items():
                row = done.get(path)
                if row is None:
                    continue  # not indexed yet: at a later sync
                if rates:
                    if cf.stars_of(row.rating) == (value or 0):
                        # not changed on the player meanwhile: mirrored now; else the reverse sync takes theirs
                        e.last_rating = value  # type: ignore[assignment]
                elif (row.rating == cf.FAVOURITE) == value:
                    e.last_starred = bool(value)
                    seen = seen | {path} if value else seen - {path}
            f = self._state_file()
            f.parent.mkdir(exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(seen)), encoding="utf-8")
            tmp.replace(f)
            return None

        if note := await asyncio.to_thread(work):
            s.data.setdefault("commit_warnings", []).append(note)

    async def remove_track(self, s: DeviceSession, ref: DeviceTrackRef) -> None:
        await super().remove_track(s, ref)
        if _spoken(ref.path):
            s.data["compas_spoken"] = True
        else:
            s.data["compas_music_changed"] = True

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        """Before the manifest: our podcast episodes listed in the Podcasts plugin, then the positions."""
        if PODCAST_DIR in installed_plugins(self.mount):
            episodes = [
                cf.Episode(
                    path=e.path,
                    show=e.meta.get("series") or e.meta.get("album") or e.path.split("/")[1],
                    title=e.meta.get("title") or Path(e.path).stem,
                    published=_published(e.meta.get("published")),
                    duration_s=e.duration_s,
                )
                for e in manifest.items.values()
                if e.kind is MediaKind.PODCAST
                and e.path.startswith(f"{PODCAST_DIR}/")
                and e.path.count("/") >= 2
            ]
            dropped = await asyncio.to_thread(cf.write_podcast_episodes, self.mount, episodes)
            if dropped:
                log.warning(
                    "%s: %d podcast episode(s) not listed: the plugin's list is full",
                    self.id,
                    dropped,
                )
        written: dict[str, tuple[float, bool]] = s.data.pop("compas_positions", {})
        on_card = {e.path for e in manifest.items.values()} if manifest else set()
        # what a plugin overwrote, unless the library has a newer position (set_position) or the file went
        for path, value in self._rewrite.items():
            if path in on_card:
                written.setdefault(path, value)
                s.data["compas_spoken"] = True
        self._rewrite = {}
        if written:
            now = int(dt.datetime.now(dt.UTC).timestamp())
            durations = {e.path: e.duration_s for e in manifest.items.values()} if manifest else {}
            positions = {
                path: cf.Position(pos, done, durations.get(path))
                for path, (pos, done) in written.items()
            }
            left_out = await asyncio.to_thread(cf.write_positions, self.mount, positions, now)
            kept = {p: v for p, v in written.items() if p not in left_out}
            await asyncio.to_thread(self._remember_positions, kept, on_card)
            if left_out:
                log.warning(
                    "%s: %d audiobook position(s) left out: the plugin's state file is full",
                    self.id,
                    len(left_out),
                )
        await self._index(s)
        await asyncio.to_thread(self._remember_version)
        if manifest is not None:
            await self._patch_stats(s, manifest)
            try:
                # a heart player has no album ratings
                if star_ratings(read_device_info(self.mount)):
                    await asyncio.to_thread(
                        self._patch_album_ratings, s.data.get("library_tracks") or {}, manifest
                    )
            except OSError as err:
                s.data.setdefault("commit_warnings", []).append(
                    f"album ratings not written to the player: {err}"
                )
        if s.data.pop("compas_spoken", False):
            # the plugins read their state only when they load: until then they play from the old positions and
            # their next save overwrites ours
            s.data.setdefault("commit_warnings", []).append(RELOAD_HINT)
            s.data["player_notice"] = RELOAD_HINT
        await super().commit(s, manifest)
