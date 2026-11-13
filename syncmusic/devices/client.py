# SPDX-License-Identifier: GPL-3.0-or-later
"""A paired Wi-Fi client (a phone app or another player that talks the client API): a device without a mount.

A sync writes the client's files like a flash drive's tree, into an **outbox** folder on the server (`mount`, the
network device's local folder). The client downloads them over the client API and acks each one; an acked file
leaves the outbox (it is on the client now). So the outbox holds exactly what the client still has to fetch, and an
interrupted download simply stays there for the next session.

What the client holds is its **inventory** (sent with every sync): a manifest entry whose file is neither in the
outbox nor in the inventory is missing and added again. A removed track whose file the client holds becomes a
**queued delete** the client works through and acks. Space and capabilities are what the client reported last.
All of it lives in `.syncmusic/client.json` next to the manifest.

Listening data comes up as an append-only **event log** (plays, ratings, favourites, positions - what the user did,
never a table diff). Events are deduplicated by their id and kept in `.syncmusic/events.json` until a reverse sync
consumed them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import threading
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from syncmusic.base.capabilities import DeviceCapabilities, PathRules
from syncmusic.base.device import UNKNOWN_SPACE, Device, DeviceSession, ProbeResult
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import (
    DeviceContents,
    DeviceFeedback,
    DeviceItem,
    DeviceTrackRef,
    Manifest,
    PlayEvent,
    PositionInfo,
)
from syncmusic.devices._filedevice import MEDIA_EXTS, FileDeviceMixin
from syncmusic.devices.paths import PathLayout
from syncmusic.devices.Rockbox.rockbox_feedback import counts_as_play
from syncmusic.infra.blockdev.identify import BlockDeviceInfo

STATE_FILE = "client.json"
EVENTS_FILE = "events.json"
EVENTS_MAX = 1000  # per request
SEEN_DAYS = 90  # how long a consumed event's id still dedups a resend
_events_lock = threading.Lock()  # read-modify-write of an events file (API and reverse sync)
# the client's codec names → ours
FORMAT_NAMES = {"vorbis": "ogg", "pcm": "wav", "m4a": "aac"}
KNOWN_FORMATS = frozenset({"mp3", "aac", "alac", "flac", "ogg", "opus", "wav", "aiff", "wma"})


class ClientSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layout: PathLayout = PathLayout()
    playlists: bool = True
    # a `play` event the client did not mark completed counts from this much of the track (scrobble rule)
    play_min_fraction: float = Field(0.5, gt=0, le=1)
    play_min_seconds: float = Field(240, gt=0)


class ClientEvent(BaseModel):
    """One thing the user did on the client. Unknown types and fields are kept (forward compatible)."""

    model_config = ConfigDict(extra="allow")

    eid: str = Field(min_length=1, max_length=64)
    seq: int = Field(0, ge=0)
    type: str = Field(min_length=1, max_length=32)
    at: dt.datetime | None = None
    clock: Literal["rtc", "ntp", "unknown"] = "unknown"
    key: str | None = Field(None, max_length=512)
    fid: str | None = Field(None, max_length=128)
    completed: bool = False
    played_s: float | None = Field(None, ge=0)
    value: int | None = Field(None, ge=0, le=5)
    on: bool | None = None
    position_s: float | None = Field(None, ge=0)
    finished: bool = False
    received_at: dt.datetime | None = None  # set by the server

    def when(self) -> dt.datetime:
        """The time "newest wins" decisions use: the client's clock unless it does not know the time."""
        at = self.at if self.clock != "unknown" else None
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=dt.UTC)
        return at or self.received_at or dt.datetime.now(dt.UTC)


class EventLog(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pending: list[ClientEvent] = []
    seen: dict[str, str] = {}  # eid -> when it arrived (ISO), consumed or not
    acked_seq: int = 0


class ClientState(BaseModel):
    """What the server knows about the client: its last report and the work queued for it."""

    model_config = ConfigDict(extra="ignore")

    capabilities: dict[str, Any] = {}
    storage_total: int | None = None
    storage_free: int | None = None
    # device path -> size of the files the client holds (its inventory)
    inventory: dict[str, int] = {}
    # device paths the client must delete (removed tracks, their sidecars, old playlists)
    deletes: list[str] = []
    inventory_cursor: int = 0
    last_seen: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


def client_formats(reported: list[str] | None) -> set[str] | None:
    """The formats the client named, in our names; None = it named none (no limit)."""
    if not reported:
        return None
    out = {FORMAT_NAMES.get(f.lower(), f.lower()) for f in reported}
    return out & KNOWN_FORMATS or None


class ClientDevice(FileDeviceMixin, Device):
    type_name = "client"
    display_name = "Wi-Fi client"
    network = True
    paired = True
    Settings: ClassVar[type[BaseModel]] = ClientSettings
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(
        formats=set(),  # what the client reports
        supports_podcasts=True,
        supports_audiobooks=True,
        lyrics="lrc_sidecar",
        # names any phone file system takes (Android's FAT/exFAT cards) that also fit the outbox on the
        # server's own file system (255 bytes of UTF-8 per name)
        path_rules=PathRules(max_path_len=240, name_len_unit="bytes"),
    )

    def __init__(self, config, mount, ctx) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config, mount, ctx)
        self.state = self.load_state()

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        return None  # paired, never detected

    # --- client state -----------------------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.mount / manifest_io.STATE_DIR / STATE_FILE

    def load_state(self) -> ClientState:
        try:
            return ClientState.model_validate_json(self._state_path.read_bytes())
        except FileNotFoundError:
            return ClientState()

    def save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_io.atomic_write(self._state_path, self.state.model_dump_json().encode("utf-8"))

    @property
    def _events_path(self) -> Path:
        return self.mount / manifest_io.STATE_DIR / EVENTS_FILE

    def load_events(self) -> EventLog:
        try:
            return EventLog.model_validate_json(self._events_path.read_bytes())
        except FileNotFoundError:
            return EventLog()

    def _save_events(self, log: EventLog) -> None:
        self._events_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_io.atomic_write(self._events_path, log.model_dump_json().encode("utf-8"))

    def add_events(self, events: list[ClientEvent]) -> dict[str, int]:
        """Store the client's events; one it sent before (same `eid`) is ignored. Returns the cursor the client
        may drop its copies up to (`acked_seq`) and how many were new / duplicates."""
        now = dt.datetime.now(dt.UTC)
        with _events_lock:
            log = self.load_events()
            new = dup = 0
            for e in events:
                if e.eid in log.seen:
                    dup += 1
                else:
                    new += 1
                    log.seen[e.eid] = now.isoformat()
                    log.pending.append(e.model_copy(update={"received_at": now}))
                log.acked_seq = max(log.acked_seq, e.seq)
            cutoff = (now - dt.timedelta(days=SEEN_DAYS)).isoformat()
            pending = {e.eid for e in log.pending}
            log.seen = {k: v for k, v in log.seen.items() if v >= cutoff or k in pending}
            self._save_events(log)
        return {"acked_seq": log.acked_seq, "accepted": new, "duplicates": dup}

    def _drop_events(self, eids: set[str]) -> None:
        if not eids:
            return
        with _events_lock:
            log = self.load_events()
            log.pending = [e for e in log.pending if e.eid not in eids]
            self._save_events(log)

    def report(
        self,
        capabilities: dict[str, Any] | None,
        inventory: dict[str, int] | None,
        removed: list[str] | None = None,
        *,
        full: bool = True,
    ) -> None:
        """Take the client's report (sync start): capabilities, storage and its inventory - all of it (`full`) or
        what changed since the last one. Saved at once."""
        st = self.state
        if capabilities is not None:
            st.capabilities = capabilities
            storage = capabilities.get("storage") or {}
            if isinstance(storage.get("total"), int):
                st.storage_total = storage["total"]
            if isinstance(storage.get("free"), int):
                st.storage_free = storage["free"]
        if inventory is not None:
            st.inventory = dict(inventory) if full else {**st.inventory, **inventory}
        for rel in removed or ():
            st.inventory.pop(rel, None)
        if full or inventory or removed:
            st.inventory_cursor += 1
        held = set(st.inventory)
        st.deletes = [d for d in st.deletes if d in held]  # deleted on the client already
        self.save_state()

    def outbox(self) -> dict[str, int]:
        """Device path -> size of every file waiting for the client (the outbox tree)."""
        return {self._rel(p): p.stat().st_size for p in self._walk()}

    def delivered(self, rel: str) -> None:
        """The client acked `rel`: it holds it now, the outbox copy goes (and its empty folders)."""
        p = self.mount / rel
        base = self.mount.resolve()
        if not p.resolve().is_relative_to(base) or p.resolve() == base:
            raise ValueError(f"not a device path: {rel!r}")
        size = p.stat().st_size if p.is_file() else self.state.inventory.get(rel, 0)
        p.unlink(missing_ok=True)
        d = p.parent
        while d.resolve() != base and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent
        self.state.inventory[rel] = size

    def deleted(self, rel: str) -> None:
        """The client acked the delete of `rel`."""
        self.state.inventory.pop(rel, None)
        if rel in self.state.deletes:
            self.state.deletes.remove(rel)

    # --- Device -----------------------------------------------------------------------------------

    def detected_profile(self) -> dict[str, Any]:
        caps = self.state.capabilities
        out: dict[str, Any] = {}
        formats = client_formats(caps.get("formats"))
        if formats is not None:
            out["formats"] = formats
        for key in ("max_sample_rate", "max_bit_depth"):
            if isinstance(caps.get(key), int):
                out[key] = caps[key]
        kinds = caps.get("media_kinds")
        if isinstance(kinds, list):
            out["supports_music"] = "music" in kinds
            out["supports_podcasts"] = "podcast" in kinds
            out["supports_audiobooks"] = "audiobook" in kinds
        features = caps.get("features") or {}
        if features.get("playlists") is False:
            out["supports_playlists"] = False
        limits = caps.get("limits") or {}
        if isinstance(limits.get("max_tracks"), int):
            out["max_tracks"] = limits["max_tracks"]
        return out

    def setting_profile(self) -> dict[str, Any]:
        return {} if self.settings.playlists else {"supports_playlists": False}

    def path_rules(self) -> PathRules:
        rules = self.capabilities.path_rules
        reported = (self.state.capabilities.get("layout") or {}).get("path_rules") or {}
        update: dict[str, Any] = {}
        if isinstance(reported.get("max_component"), int):
            update["max_name_len"] = min(rules.max_name_len, reported["max_component"])
        if isinstance(reported.get("forbidden"), str):
            update["forbidden_chars"] = "".join(
                sorted(set(rules.forbidden_chars) | set(reported["forbidden"]))
            )
        limits = self.state.capabilities.get("limits") or {}
        if isinstance(limits.get("max_file_bytes"), int):
            update["max_file_size"] = limits["max_file_bytes"]
        return rules.model_copy(update=update) if update else rules

    def model_capabilities(self) -> DeviceCapabilities:
        return super().model_capabilities().model_copy(update={"path_rules": self.path_rules()})

    def label_for(self, name: str, fs: str | None, current: str | None) -> str | None:
        return None

    async def free_space(self) -> int:
        """The client's last reported free space less what waits for it in the outbox."""
        if self.state.storage_free is None:
            return UNKNOWN_SPACE
        pending = sum((await asyncio.to_thread(self.outbox)).values())
        queued = sum(self.state.inventory.get(d, 0) for d in self.state.deletes)
        return max(0, self.state.storage_free - pending + queued)

    async def capacity(self) -> int:
        return UNKNOWN_SPACE if self.state.storage_total is None else self.state.storage_total

    async def missing(self, manifest: Manifest) -> set[str]:
        """Entries whose file is neither on the client (inventory) nor waiting for it (outbox)."""
        held = self.state.inventory
        root = str(self.mount)

        def work() -> set[str]:
            return {
                k
                for k, e in manifest.items.items()
                if e.path not in held and not os.path.isfile(os.path.join(root, e.path))
            }

        return await asyncio.to_thread(work)

    async def read_contents(self) -> DeviceContents:
        """The client's files and the ones waiting for it."""
        files = {**self.state.inventory, **(await asyncio.to_thread(self.outbox))}
        items = [
            DeviceItem(device_ref=rel, path=rel, size=size, title=PurePosixPath(rel).stem)
            for rel, size in sorted(files.items())
            if PurePosixPath(rel).suffix.lower() in MEDIA_EXTS and rel not in self.state.deletes
        ]
        lists = sorted(
            PurePosixPath(rel).stem
            for rel in files
            if rel.lower().endswith((".m3u8", ".m3u")) and rel not in self.state.deletes
        )
        return DeviceContents(items=items, playlists=lists)

    async def read_feedback(self, manifest: Manifest | None) -> DeviceFeedback:
        """The stored events as feedback, in time order (the newest rating, heart and position win). Events that
        change nothing (skips, short plays, unknown items or types) are consumed right here; the rest by
        `reset_feedback` (same instance), so events that arrive meanwhile wait for the next run."""
        if manifest is None:
            return DeviceFeedback()
        log = await asyncio.to_thread(self.load_events)
        fb = DeviceFeedback()
        used: set[str] = set()
        idle: set[str] = set()
        for e in sorted(log.pending, key=lambda e: (e.when(), e.seq)):
            if e.type == "playlist_edit":  # read_playlists applies them; consumed with this run
                used.add(e.eid)
                continue
            entry = manifest.items.get(e.key or "")
            if entry is None:
                fb.unmapped += 1
                idle.add(e.eid)
                continue
            key = entry.ref.key()
            if e.type == "play":
                length = int((entry.duration_s or 0) * 1000)
                if e.completed or counts_as_play(
                    int((e.played_s or 0) * 1000),
                    length,
                    self.settings.play_min_fraction,
                    self.settings.play_min_seconds,
                ):
                    fb.plays.append(
                        PlayEvent(
                            ref=entry.ref,
                            played_at=e.when(),
                            played_at_estimated=e.clock == "unknown",
                            source_device=self.id,
                        )
                    )
                    used.add(e.eid)
                    continue
            elif e.type == "rating" and e.value is not None:
                fb.ratings[key] = e.value or None
                used.add(e.eid)
                continue
            elif e.type == "favourite" and e.on is not None:
                fb.stars[key] = e.on
                used.add(e.eid)
                continue
            elif e.type == "position" and e.position_s is not None:
                fb.positions[key] = PositionInfo(
                    position_s=e.position_s, finished=e.finished, updated_at=e.when()
                )
                used.add(e.eid)
                continue
            idle.add(e.eid)
        await asyncio.to_thread(self._drop_events, idle)
        self._read_events = used
        return fb

    async def read_playlists(self, manifest: Manifest) -> dict[str, list[str]] | None:
        """The client's playlists for the reverse playlist sync: each as last synced, with the user's pending
        `playlist_edit` events applied in order (`name`, `ops`: add / remove / move of an item `key` at `index`);
        a name the server does not know is a playlist made on the client."""
        log = await asyncio.to_thread(self.load_events)
        edits = sorted(
            (e for e in log.pending if e.type == "playlist_edit"), key=lambda e: (e.when(), e.seq)
        )
        lists = {
            name: [manifest.items[k].path for k in refs if k in manifest.items]
            for name, refs in manifest.playlist_refs.items()
        }
        for e in edits:
            extra = e.model_extra or {}
            name, ops = extra.get("name"), extra.get("ops")
            if not isinstance(name, str) or not name.strip() or not isinstance(ops, list):
                continue
            paths = lists.setdefault(name.strip(), [])
            for op in ops:
                entry = manifest.items.get(str(op.get("key", ""))) if isinstance(op, dict) else None
                if entry is None:
                    continue
                kind, index = op.get("op"), op.get("index")
                if kind in ("remove", "move") and entry.path in paths:
                    paths.remove(entry.path)
                if kind in ("add", "move"):
                    at = (
                        index if isinstance(index, int) and 0 <= index <= len(paths) else len(paths)
                    )
                    paths.insert(at, entry.path)
        return lists

    async def reset_feedback(self, s: DeviceSession, consumed: DeviceFeedback) -> None:
        await asyncio.to_thread(self._drop_events, getattr(self, "_read_events", set()))
        self._read_events = set()

    async def begin(self) -> DeviceSession:
        s = await super().begin()
        rules = s.data["rules"]
        from syncmusic.devices import paths

        # the client's files keep their names taken although they left the outbox
        s.data["taken"] |= {paths.path_key(r, rules) for r in self.state.inventory}
        return s

    async def remove_track(self, s: DeviceSession, ref: DeviceTrackRef) -> None:
        await super().remove_track(s, ref)
        for rel in (ref.path, *ref.sidecars):
            if rel in self.state.inventory and rel not in self.state.deletes:
                self.state.deletes.append(rel)

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        # playlists the sync dropped: the client's copies go too
        old = set((s.manifest.playlist_paths if s.manifest else {}).values())
        new = s.data.get("playlist_paths", manifest.playlist_paths)
        keep = {p for n, p in new.items() if n in manifest.playlists}
        for rel in old - keep:
            if rel in self.state.inventory and rel not in self.state.deletes:
                self.state.deletes.append(rel)
        self.save_state()  # the manifest is written last
        await super().commit(s, manifest)

    async def abort(self, s: DeviceSession) -> None:
        await super().abort(s)
        self.state = self.load_state()  # deletes queued by this session are dropped

    async def wipe(self) -> int:
        """Everything SyncMusic put on the client is queued for deletion; the outbox and manifest go."""
        held = sorted(self.state.inventory)
        n = await super().wipe()
        self.state.deletes = held
        self.save_state()
        return n + len(held)
