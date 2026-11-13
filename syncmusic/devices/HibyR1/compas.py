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

from syncmusic.base.capabilities import CoverSpec, DeviceCapabilities
from syncmusic.base.device import Device, DeviceSession, ProbeResult
from syncmusic.base.progress import report_progress
from syncmusic.core.manifest import STATE_DIR
from syncmusic.core.models import (
    DeviceFeedback,
    DeviceIdentity,
    DeviceTrackRef,
    Manifest,
    ManifestEntry,
    MediaKind,
    PreparedItem,
    Track,
)
from syncmusic.devices._filedevice import FileDeviceMixin
from syncmusic.devices.HibyR1 import compas_feedback as cf
from syncmusic.devices.HibyR1 import compas_sdtool as sd
from syncmusic.devices.HibyR1.compas_stats import StatsUpdate, patch_stats
from syncmusic.devices.HibyR1.sonixos import (
    COMPAS_USB_PRODUCT,
    FORMATS,
    HIBY_USB_VENDOR,
    HibyData,
    hiby_feedback,
)
from syncmusic.devices.paths import PathLayout
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
            .model_copy(update={"path_rules": self.path_rules(), "lyrics": self.s.lyrics})
        )

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
        return s

    def _state_file(self) -> Path:
        return self.mount / STATE_DIR / FAVOURITES_STATE

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
            self._favourites = card.favourites | (seen - card.indexed)
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
                if there is None or (there[0] < pos - 1 and not (there[1] and not done)):
                    # a plugin that kept its old state overwrote ours: not the listener's, ours goes back
                    card.positions.pop(path, None)
                    self._rewrite[path] = (pos, done)
            return hiby_feedback(
                HibyData(card.play_counts, card.favourites, card.positions),
                manifest,
                rules,
                seen & card.indexed,
                now=dt.datetime.now(dt.UTC),
                device_id=self.id,
            )

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

        def work() -> str | None:
            seen = self._seen_favourites()
            updates: dict[str, StatsUpdate] = {}
            hearts: dict[str, tuple[ManifestEntry, bool]] = {}
            for key, e in manifest.items.items():
                if e.orphan or e.kind is not MediaKind.MUSIC:
                    continue
                t, star = tracks.get(key), starred.get(key)
                fav: bool | None = None
                if star is not None and star != e.last_starred and (star or e.last_starred):
                    fav = star if star or e.path in seen else None
                    if fav is not None:
                        hearts[e.path] = (e, star)
                plays = t.play_count if (t is not None and pushed) else None
                last = int(t.last_played.timestamp()) if (pushed and t and t.last_played) else None
                if fav is not None or plays or last:
                    updates[e.path] = StatsUpdate(plays or None, last, fav)
            if not updates and not hearts:
                return None
            try:
                done = patch_stats(self.mount, updates) if updates else {}
            except (cf.CompasDbError, OSError) as err:
                return f"hearts and play counts not written to the player: {err}"
            by_path = {e.path: e for e in manifest.items.values()}
            for path, row in done.items():
                if pushed:
                    by_path[path].last_playcount = row.playcount
            for path, (e, star) in hearts.items():
                if path in done:  # indexed: mirrored now; else at a later sync
                    e.last_starred = star
                    seen = seen | {path} if star else seen - {path}
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
        if manifest is not None:
            await self._patch_stats(s, manifest)
        if s.data.pop("compas_spoken", False):
            # the plugins read their state only when they load: until then they play from the old positions and
            # their next save overwrites ours
            s.data.setdefault("commit_warnings", []).append(RELOAD_HINT)
            s.data["player_notice"] = RELOAD_HINT
        await super().commit(s, manifest)
