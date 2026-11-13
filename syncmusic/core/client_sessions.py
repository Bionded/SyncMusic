# SPDX-License-Identifier: GPL-3.0-or-later
"""Sync sessions of Wi-Fi clients (`devices/client.py`): the client reports its state, the server runs the normal
plan + apply into the client's outbox, and hands out what is in the outbox (to fetch) and the queued deletes as the
session's plan. The client downloads, acks and finishes at its own pace; every ack is committed at once, so an
unfinished session loses nothing (the next one lists what is still in the outbox).

Sessions live in memory: after a restart a client gets `session_expired` and simply starts a new one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import mimetypes
import secrets
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

from syncmusic.core.models import Manifest, MediaKind, Track
from syncmusic.infra.clients import ClientError, Clients
from syncmusic.infra.jobs import JobStatus

if TYPE_CHECKING:
    from syncmusic.core.sync import SyncService
    from syncmusic.devices.client import ClientDevice

log = logging.getLogger(__name__)

SESSION_HOURS = 24
PAGE_MAX = 500
# events that arrive in a burst (a client uploading its offline backlog) are handed on once
EVENTS_DEBOUNCE_S = 20.0
SECTIONS = ("fetch", "delete", "state")
Status = Literal["planning", "needs_review", "ready", "failed", "finished"]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _empty_object_as_list(value: Any) -> Any:
    """JSON encoders of some languages (Lua) cannot tell an empty list from an empty object: accept `{}` as `[]`."""
    return [] if value == {} else value


AsList = BeforeValidator(_empty_object_as_list)


class InventoryItem(BaseModel):
    path: str
    size: int = 0
    fid: str | None = None
    key: str | None = None


class Inventory(BaseModel):
    kind: Literal["full", "delta"] = "full"
    since: str | None = None
    items: Annotated[list[InventoryItem], AsList] = []
    removed: Annotated[list[str], AsList] = []  # device paths


class SyncRequest(BaseModel):
    mode: Literal["full", "update"] = "full"
    capabilities: dict[str, Any] | None = None
    inventory: Inventory = Inventory()
    events_cursor: str | None = None


class AckDone(BaseModel):
    path: str
    fid: str | None = None
    key: str | None = None


class AckFailed(BaseModel):
    path: str
    fid: str | None = None
    key: str | None = None
    error: Literal["no_space", "io", "unsupported", "other"] = "other"
    message: str = ""


class AckRequest(BaseModel):
    done: Annotated[list[AckDone], AsList] = []
    deleted: Annotated[list[str], AsList] = []  # device paths
    failed: Annotated[list[AckFailed], AsList] = []
    storage: dict[str, Any] | None = None


class FinishRequest(BaseModel):
    status: Literal["complete", "partial", "cancelled"] = "complete"


class ClientSession(BaseModel):
    id: str
    device_id: str
    mode: Literal["full", "update"]
    status: Status = "planning"
    message: str = ""
    job_id: str | None = None
    plan_id: str | None = None
    created_at: dt.datetime = Field(default_factory=_now)
    expires_at: dt.datetime
    fetch: list[dict[str, Any]] = []
    delete: list[dict[str, Any]] = []
    state: list[dict[str, Any]] = []  # the library's listening data of every item on the client
    playlists: dict[str, Any] = {"set": [], "remove": []}
    fids: dict[str, str] = {}  # fid -> device path (in the outbox)
    # download ticket: lets a downloader that cannot send headers fetch this session's own files
    ticket: str = Field(default_factory=lambda: secrets.token_urlsafe(24))
    done: set[str] = set()  # acked device paths
    deleted: set[str] = set()
    failed: dict[str, str] = {}  # device path -> error
    bytes_done: int = 0
    cancel: bool = False

    def totals(self) -> dict[str, int]:
        return {
            "fetch_bytes": sum(int(f["size"]) for f in self.fetch),
            "fetch_count": len(self.fetch),
            "delete_count": len(self.delete),
        }

    def progress(self) -> dict[str, int]:
        return {
            "done": len(self.done) + len(self.deleted),
            "total": len(self.fetch) + len(self.delete),
            "bytes_done": self.bytes_done,
            "bytes_total": self.totals()["fetch_bytes"],
        }


def track_meta(t: Track) -> dict[str, Any]:
    """What a client with its own database shows for a track (the file's tags say the same)."""
    return {
        "title": t.title,
        "artist": t.artist,
        "album_artist": t.album_artist,
        "album": t.album,
        "track": t.track_no,
        "disc": t.disc_no,
        "year": t.year,
        "genre": t.genres[0] if t.genres else None,
        "duration_s": t.duration_s,
        "kind": t.kind.value,
    } | listening_state(None, t)


def listening_state(key: str | None, t: Track) -> dict[str, Any]:
    """The library's listening data of an item: rating 0-5 (None = unrated), favourite, play count and, for
    podcasts and audiobooks, the resume position."""
    out: dict[str, Any] = {"key": key} if key is not None else {}
    out |= {"rating": t.rating, "favourite": t.starred, "play_count": t.play_count}
    if t.kind in (MediaKind.PODCAST, MediaKind.AUDIOBOOK):
        out["position_s"] = t.position_s
    return out


class ClientSessions:
    def __init__(self, sync: SyncService, clients: Clients) -> None:
        self.sync = sync
        self.clients = clients
        self.sessions: dict[str, ClientSession] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._deliver: dict[str, asyncio.Task[None]] = {}  # device id -> debounced event delivery
        self.events_debounce_s = EVENTS_DEBOUNCE_S
        self._locks: dict[str, asyncio.Lock] = {}  # one state change of a client at a time
        self._fid_cache: dict[
            tuple[str, str, int, int], str
        ] = {}  # (device, path, size, mtime) -> fid

    async def device(self, device_id: str) -> ClientDevice:
        from syncmusic.devices.client import ClientDevice

        dev = await self.sync.open_device(device_id)
        if not isinstance(dev, ClientDevice):
            raise ClientError("not_found", "not a client device", 404)
        return dev

    async def ensure_device(
        self, device_id: str, name: str, capabilities: dict[str, Any] | None
    ) -> None:
        """The device of a paired client (made at its first pairing; a re-pair keeps it and its settings)."""
        from syncmusic.core.models import DeviceConfig
        from syncmusic.core.proc_templates import with_tag_step
        from syncmusic.core.sync import suggested_processors
        from syncmusic.devices.client import ClientDevice

        cfg = await self.sync.store.device(device_id)
        new = cfg is None
        if cfg is None:
            cfg = DeviceConfig(id=device_id, type="client", name=name, on_connect="manual")
        outbox = self._outbox_root(device_id)
        await asyncio.to_thread(outbox.mkdir, parents=True, exist_ok=True)
        dev = ClientDevice(cfg, outbox, self.sync.ctx)
        if capabilities:
            await asyncio.to_thread(dev.report, capabilities, None)
        if new:
            # converted for what the client plays, as Register suggests for any device
            cfg.processors = with_tag_step(suggested_processors(dev.effective_capabilities()))
            await self.sync.store.put_device(cfg)
            log.info("client %s: device %r created", device_id, name)

    def _expire(self) -> None:
        now = _now()
        for sid in [s.id for s in self.sessions.values() if s.expires_at <= now]:
            del self.sessions[sid]

    def open_for(self, device_id: str) -> ClientSession | None:
        self._expire()
        return next(
            (
                s
                for s in self.sessions.values()
                if s.device_id == device_id and s.status in ("planning", "ready")
            ),
            None,
        )

    # --- start ------------------------------------------------------------------------------------

    async def start(self, device_id: str, req: SyncRequest) -> ClientSession:
        """Take the client's report and plan; an open session of the device is replaced (the client started over)."""
        dev = await self.device(device_id)
        inv = req.inventory
        if inv.kind == "delta" and inv.since != str(dev.state.inventory_cursor):
            raise ClientError(
                "inventory_required", "send the full inventory (the server's state differs)", 409
            )
        old = self.open_for(device_id)
        if old is not None and old.status == "planning":
            return old  # still planning: the same session
        if old is not None:
            del self.sessions[old.id]
        items = {i.path: i.size for i in inv.items}
        async with self._lock(device_id):
            dev = await self.device(device_id)
            await asyncio.to_thread(
                dev.report, req.capabilities, items, inv.removed, full=inv.kind == "full"
            )
        if req.capabilities is not None:
            await self.clients.update_capabilities(device_id, req.capabilities)
        self.sync.client_sync_started(device_id)
        s = ClientSession(
            id=uuid.uuid4().hex[:16],
            device_id=device_id,
            mode=req.mode,
            expires_at=_now() + dt.timedelta(hours=SESSION_HOURS),
        )
        self.sessions[s.id] = s
        self._tasks[s.id] = asyncio.create_task(self._plan(s, first_inventory=not items))
        return s

    async def _plan(self, s: ClientSession, *, first_inventory: bool) -> None:
        try:
            await self._run_sync(s, first_inventory=first_inventory)
            if s.status == "planning":
                await self._fill(s)
                s.status = "ready"
        except ClientError as e:
            s.status, s.message = "failed", str(e)
        except Exception as e:  # noqa: BLE001 - the client sees the reason, the server logs it
            log.warning("client sync %s: %s", s.device_id, e, exc_info=True)
            s.status, s.message = "failed", str(e)
        finally:
            self._tasks.pop(s.id, None)

    async def _run_sync(self, s: ClientSession, *, first_inventory: bool) -> None:
        """The server's half: plan and apply into the outbox (update: an update sync). A plan the user must
        confirm leaves the session `needs_review`; the client tries again after the user applied it."""
        sync = self.sync
        if sync.ctx.jobs.device_busy(s.device_id):
            running = sync.ctx.jobs.running_for_device(s.device_id)
            if running is not None:
                await sync.ctx.jobs.wait(running.id)
        if s.mode == "update":
            job = await sync.update_sync(s.device_id)
        else:
            pc = await sync.plan(s.device_id, auto=True)
            plan = pc.plan
            s.plan_id = plan.id
            first: Literal["keep"] | None = None
            if plan.first_sync:
                if not first_inventory or pc.existing:
                    return self._review(s, ["first sync: the client already holds files"])
                first = "keep"  # nothing on the client yet: nothing to keep, adopt or wipe
            # a first sync onto an empty client confirms nothing; anything else asks as for any device
            if (plan.needs_confirmation and not first) or plan.does_not_fit:
                reasons = plan.confirmation_reasons + (
                    ["does not fit"] if plan.does_not_fit else []
                )
                return self._review(s, reasons)
            job = await sync.apply(plan.id, first_sync=first)
        s.job_id = job.id
        info = await sync.ctx.jobs.wait(job.id)
        if info.status != JobStatus.SUCCEEDED:
            raise ClientError("server_error", info.error or f"sync job {info.status.value}", 500)

    def _review(self, s: ClientSession, reasons: list[str]) -> None:
        s.status, s.message = "needs_review", "; ".join(reasons) or "confirm the sync on the server"
        if s.plan_id is not None:
            self.sync.wait_for_user(s.device_id, s.message, s.plan_id)

    async def _fill(self, s: ClientSession) -> None:
        """The session's plan from the outbox (fetch) and the queued deletes, in manifest order."""
        dev = await self.device(s.device_id)
        manifest = await dev.read_manifest()
        by_path: dict[str, tuple[str | None, str]] = {}  # device path -> (key, role)
        order: list[str] = []
        for key, e in (manifest.items if manifest else {}).items():
            by_path[e.path] = (key, "track")
            order.append(e.path)
            for side in e.sidecars:
                by_path.setdefault(side, (key, "sidecar"))
                order.append(side)
        playlist_paths = set((manifest.playlist_paths if manifest else {}).values())
        outbox = await asyncio.to_thread(dev.outbox)
        rank = {p: i for i, p in enumerate(order)}
        ordered = sorted(outbox, key=lambda p: (rank.get(p, len(rank)), p))

        def fids() -> dict[str, str]:
            out = {}
            for rel in ordered:
                st = (dev.mount / rel).stat()
                ck = (dev.id, rel, st.st_size, st.st_mtime_ns)
                if ck not in self._fid_cache:
                    self._fid_cache[ck] = sha256_of(dev.mount / rel)
                out[rel] = self._fid_cache[ck]
            return out

        fid_of = await asyncio.to_thread(fids)
        tracks = await self._tracks(s.device_id, manifest)
        held = dev.state.inventory
        s.fetch = []
        for rel in ordered:
            item_key, role = by_path.get(
                rel, (None, "playlist" if rel in playlist_paths else "file")
            )
            s.fetch.append(
                {
                    "op": "replace" if rel in held else "add",
                    "key": item_key,
                    "fid": fid_of[rel],
                    "size": outbox[rel],
                    "mime": mimetypes.guess_type(rel)[0] or "application/octet-stream",
                    "path": rel,
                    "role": role,
                    "url": f"files/{fid_of[rel]}?ticket={s.ticket}",
                }
                | (
                    {"meta": track_meta(tracks[item_key])}
                    if role == "track" and item_key in tracks
                    else {}
                )
            )
        s.fids = {f["fid"]: f["path"] for f in s.fetch}
        s.delete = [
            {"key": by_path.get(rel, (None, ""))[0], "path": rel} for rel in dev.state.deletes
        ]
        s.state = [
            listening_state(key, t)
            for key, t in tracks.items()
            if manifest and key in manifest.items
        ]
        s.playlists = {
            "set": [
                {"name": n, "path": p}
                for n, p in sorted((manifest.playlist_paths if manifest else {}).items())
            ],
            "remove": [],
        }

    async def _tracks(self, device_id: str, manifest: Manifest | None) -> dict[str, Track]:
        """The library's view of every item in the manifest that a library can tell about now (key -> track)."""
        if manifest is None:
            return {}
        cfg = await self.sync.config(device_id)
        snaps = await self.sync._readable_snapshots(cfg, manifest)
        out: dict[str, Track] = {}
        for key, e in manifest.items.items():
            snap = snaps.get(e.ref.library_id)
            t = snap.track(e.ref.item_id) if snap is not None else None
            if t is not None:
                out[key] = t
        return out

    # --- reading ----------------------------------------------------------------------------------

    def get(self, device_id: str, session_id: str) -> ClientSession:
        self._expire()
        s = self.sessions.get(session_id)
        if s is None or s.device_id != device_id:
            raise ClientError("session_expired", "no such sync session (expired or finished)", 410)
        return s

    def view(self, s: ClientSession, limit: int = PAGE_MAX) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session_id": s.id,
            "status": s.status,
            "mode": s.mode,
            "expires_at": s.expires_at.isoformat(),
        }
        if s.status == "planning":
            out["retry_after_s"] = 2
        elif s.status in ("needs_review", "failed"):
            out["message"] = s.message
            if s.status == "needs_review":
                out["retry_after_s"] = 60
        if s.status == "ready":
            plan: dict[str, Any] = {
                "session_id": s.id,
                "plan_id": s.plan_id,
                "mode": s.mode,
                "totals": s.totals(),
                "playlists": s.playlists,
                "expires_at": s.expires_at.isoformat(),
                "next": {},
            }
            for section in SECTIONS:
                page = self.page(s, section, None, limit)
                plan[section] = page["items"]
                plan["next"][section] = page["next"]
            out["plan"] = plan
            out["progress"] = s.progress()
        return out

    def page(
        self, s: ClientSession, section: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        if section not in SECTIONS:
            raise ClientError("invalid", f"unknown section {section!r}", 400)
        if s.status != "ready":
            raise ClientError("not_ready", "the plan is not ready yet", 409)
        items = {"fetch": s.fetch, "delete": s.delete, "state": s.state}[section]
        try:
            start = int(cursor or 0)
        except ValueError:
            raise ClientError("invalid", "bad cursor", 400) from None
        limit = max(1, min(limit, PAGE_MAX))
        end = start + limit
        return {"items": items[start:end], "next": str(end) if end < len(items) else None}

    def status(self, device_id: str, dev: ClientDevice, poll_s: int) -> dict[str, Any]:
        """What the client's poll learns: whether there is something to sync."""
        open_ = self.open_for(device_id)
        wanted = "none"
        reason = ""
        if self.sync.sync_requested(device_id):
            wanted, reason = "full", "requested on the server"
        elif dev.state.deletes or dev.outbox():
            wanted, reason = "full", "files are waiting for the client"
        return {
            "sync_wanted": wanted,
            "reason": reason,
            "plan_ready": open_ is not None and open_.status == "ready",
            "poll_s": poll_s,
            "server_time": _now().isoformat(),
        }

    # --- delivery -------------------------------------------------------------------------------

    def file_of(self, device_id: str, fid: str) -> tuple[Path, str] | None:
        """(outbox file, device path) of `fid` when an open session of this device lists it; else None."""
        self._expire()
        for s in self.sessions.values():
            if s.device_id == device_id and s.status == "ready" and fid in s.fids:
                rel = s.fids[fid]
                return self._outbox_root(device_id) / rel, rel
        return None

    def file_by_ticket(self, ticket: str, fid: str) -> tuple[str, Path, str] | None:
        """(device id, outbox file, device path) of `fid` when the open session with this ticket lists it."""
        self._expire()
        for s in self.sessions.values():
            if secrets.compare_digest(s.ticket, ticket) and s.status == "ready" and fid in s.fids:
                rel = s.fids[fid]
                return s.device_id, self._outbox_root(s.device_id) / rel, rel
        return None

    def _outbox_root(self, device_id: str) -> Path:
        return self.sync.ctx.config.data_dir / "network" / device_id

    async def ack(self, device_id: str, session_id: str, body: AckRequest) -> dict[str, Any]:
        """Commit what the client reports done (it holds the file now: the outbox copy goes), deleted or failed.
        Idempotent; a path the session does not list is refused before anything changes."""
        s = self.get(device_id, session_id)
        if s.status != "ready":
            raise ClientError("not_ready", "the plan is not ready yet", 409)
        fetch = {f["path"]: f for f in s.fetch}
        deletes = {d["path"] for d in s.delete}
        for d in body.done:
            f = fetch.get(d.path)
            if f is None or (d.fid is not None and d.fid != f["fid"]):
                raise ClientError("not_in_plan", f"{d.path!r} is not in this plan", 409)
        for rel in body.deleted:
            if rel not in deletes:
                raise ClientError("not_in_plan", f"delete {rel!r} is not in this plan", 409)
        async with self._lock(device_id):
            dev = await self.device(device_id)

            def work() -> None:
                for d in body.done:
                    if d.path not in s.done:
                        dev.delivered(d.path)
                        s.done.add(d.path)
                        s.bytes_done += int(fetch[d.path]["size"])
                        s.failed.pop(d.path, None)
                for rel in body.deleted:
                    dev.deleted(rel)
                    s.deleted.add(rel)
                if isinstance((body.storage or {}).get("free"), int):
                    dev.state.storage_free = int(body.storage["free"])  # type: ignore[index]
                dev.save_state()

            await asyncio.to_thread(work)
        for fail in body.failed:
            s.failed[fail.path] = f"{fail.error}: {fail.message}".rstrip(": ")
            log.warning("client %s could not take %s: %s", device_id, fail.path, fail.error)
        progress = s.progress()
        self.sync.ctx.bus.publish("client.progress", device_id=device_id, **progress)
        return {"progress": progress, "cancel": s.cancel}

    async def finish(self, device_id: str, session_id: str, status: str) -> dict[str, Any]:
        """The client is done with the session (complete, partial or cancelled): what it acked stays committed,
        what it did not stays in the outbox for the next session."""
        s = self.get(device_id, session_id)
        async with self._lock(device_id):
            dev = await self.device(device_id)
            dev.state.inventory_cursor += 1
            await asyncio.to_thread(dev.save_state)
        s.status = "finished"
        self.sessions.pop(s.id, None)
        progress = s.progress()
        log.info(
            "client %s finished its sync (%s): %d of %d done, %d failed",
            device_id,
            status,
            progress["done"],
            progress["total"],
            len(s.failed),
        )
        self.sync.ctx.bus.publish(
            "client.sync_finished",
            device_id=device_id,
            status=status,
            failed=len(s.failed),
            **progress,
        )
        return {
            "inventory_cursor": str(dev.state.inventory_cursor),
            "progress": progress,
            "failed": s.failed,
        }

    def cancel(self, device_id: str) -> bool:
        """The user stopped the client's sync on the server: its next ack is told to stop."""
        s = self.open_for(device_id)
        if s is None:
            return False
        s.cancel = True
        return True

    def _lock(self, device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())

    # --- events without a sync ----------------------------------------------------------------------

    def events_arrived(self, device_id: str) -> None:
        """New listening data: hand it to the libraries soon (debounced), not only at the client's next sync."""
        old = self._deliver.get(device_id)
        if old is not None and not old.done():
            old.cancel()
        self._deliver[device_id] = asyncio.create_task(self._deliver_events(device_id))

    async def _deliver_events(self, device_id: str) -> None:
        await asyncio.sleep(self.events_debounce_s)
        if self.open_for(device_id) is not None or self.sync.ctx.jobs.device_busy(device_id):
            return  # a running sync (or the one the client is starting) takes them in its reverse step
        from syncmusic.core.sync import SyncError

        try:
            # an update sync: its reverse step sends the events; what changed lands in the outbox
            await self.sync.update_sync(device_id, only_if_changed=True)
        except SyncError as e:
            log.info("client %s: events wait for the next sync: %s", device_id, e)
        except Exception:
            log.exception("client %s: could not hand on its events", device_id)

    async def close(self) -> None:
        for t in list(self._deliver.values()):
            t.cancel()
        for t in list(self._tasks.values()):
            t.cancel()
