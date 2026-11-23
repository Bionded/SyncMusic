# SPDX-License-Identifier: GPL-3.0-or-later
"""Sync sessions of Wi-Fi clients (`devices/client.py`): the client reports its state, the server runs the normal
plan + apply into the client's outbox, and hands out what is in the outbox (to fetch) and the queued deletes as the
session's plan. The client downloads, acks and finishes at its own pace; every ack is committed at once, so an
unfinished session loses nothing (the next one lists what is still in the outbox).

Sessions live in memory: after a restart a client gets `session_expired` and simply starts a new one.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import mimetypes
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import (
    ActionKind,
    ItemRef,
    Manifest,
    MediaKind,
    PlanAction,
    SyncPlan,
    Track,
)
from syncmusic.infra.clients import ClientError, Clients
from syncmusic.infra.jobs import JobStatus

if TYPE_CHECKING:
    from syncmusic.core.sync import SyncService
    from syncmusic.devices.client import ClientDevice

log = logging.getLogger(__name__)

SESSION_HOURS = 24
# a streaming session whose client neither pages nor acks for this long is ended (its job stops)
STREAM_IDLE_S = 1800.0  # long enough for one large file on slow Wi-Fi
LISTEN_GRACE_S = 45.0  # s: a long poll lasts at most 30 s, the app asks again within seconds
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


DIAG_MAX = 512 * 1024  # characters of client log the server keeps


_SECRET = re.compile(r"smc_[A-Za-z0-9_-]{8,}|(?i:bearer)\s+\S+")
_SECRET_FIELD = re.compile(r"(\"(?:token|secret)\"\s*:\s*)\"[^\"]*\"")


def redact(text: str) -> str:
    """Mask what looks like a client token or secret in an uploaded log (JSON stays JSON)."""
    return _SECRET.sub("[redacted]", _SECRET_FIELD.sub(r'\1"[redacted]"', text))


class DiagnosticsRequest(BaseModel):
    log: str = Field("", max_length=DIAG_MAX)
    summary: dict[str, Any] = {}


class DatabaseFile(BaseModel):
    name: str = Field(max_length=200)
    data: str  # base64


class PlayerDatabaseRequest(BaseModel):
    files: list[DatabaseFile] = Field(max_length=500)


class FinishRequest(BaseModel):
    status: Literal["complete", "partial", "cancelled"] = "complete"


class ClientSession(BaseModel):
    id: str
    device_id: str
    mode: Literal["full", "update"]
    status: Status = "planning"
    message: str = ""
    code: str = ""  # why a failed session failed (a protocol error code, e.g. no_templates)
    job_id: str | None = None
    plan_id: str | None = None
    created_at: dt.datetime = Field(default_factory=_now)
    expires_at: dt.datetime
    fetch: list[dict[str, Any]] = []
    delete: list[dict[str, Any]] = []
    state: list[dict[str, Any]] = []  # the library's listening data of every item on the client
    playlists: dict[str, Any] = {"set": [], "remove": []}
    generated: dict[str, Any] | None = (
        None  # a set of server-built files (see ClientDevice.generated_files)
    )
    gen_files: dict[
        str, Path
    ] = {}  # fid -> server file of the generated set (and the card manifest)
    # the manifest the client writes onto its card after a complete sync (see ClientDevice.card_manifest)
    card_manifest: dict[str, Any] | None = None
    after_sync: str = "none"
    fids: dict[str, str] = {}  # fid -> device path (in the outbox)
    # download ticket: lets a downloader that cannot send headers fetch this session's own files
    ticket: str = Field(default_factory=lambda: secrets.token_urlsafe(24))
    done: set[str] = set()  # acked device paths
    deleted: set[str] = set()
    failed: dict[str, str] = {}  # device path -> error
    bytes_done: int = 0
    cancel: bool = False
    may_confirm: bool = False  # needs_review that the client itself may confirm (device setting)
    more: int = 0  # files of the plan held back for the next window (the outbox budget)
    more_bytes: int = 0
    # what earlier windows of the same sync already delivered (files, bytes): progress covers the whole sync
    before: tuple[int, int] = (0, 0)
    # streaming (the client asked for it): the whole plan in one session, files listed as the job prepares them
    stream: bool = False
    job_done: bool = False  # streaming: the sync job ended (the rest of the plan is listed)
    planned: tuple[int, int] = (0, 0)  # streaming: files and bytes the plan fetches
    metas: dict[str, dict[str, Any]] = {}  # streaming: item key -> what the client shows for it
    seen: int = 0  # streaming: entries of the job's written list already looked at
    own_stream: Any = None  # streaming: this session's Stream (devices.client.STREAMS)
    last_seen: float = Field(
        default_factory=time.monotonic
    )  # the client's last call for this session

    def totals(self) -> dict[str, int]:
        count, size = len(self.fetch), sum(int(f["size"]) for f in self.fetch)
        if (
            self.stream and not self.job_done
        ):  # not every file is prepared yet: the plan says how many
            count, size = max(count, self.planned[0]), max(size, self.planned[1])
        return {
            "fetch_bytes": size,
            "fetch_count": count,
            "delete_count": len(self.delete),
            "more_count": self.more,
            "more_bytes": self.more_bytes,
        }

    # (time.monotonic(), bytes_done) at each ack: the transfer rate to the client over the last RATE_WINDOW_S
    acks: list[tuple[float, int]] = []

    def acked(self, now: float) -> None:
        self.acks.append((now, self.bytes_done))
        self.acks = [a for a in self.acks if a[0] >= now - RATE_WINDOW_S] or self.acks[-1:]

    def progress(self, now: float | None = None) -> dict[str, Any]:
        """Files and bytes the client took (acked), and the rate of that transfer to the client in bytes/s over
        the last seconds with the time left at that rate (None until two acks are some time apart)."""
        totals = self.totals()
        total = totals["fetch_bytes"]
        rate: float | None = None
        if len(self.acks) >= 2:
            (t0, b0), (t1, b1) = self.acks[0], self.acks[-1]
            if now is not None and now - t1 > RATE_WINDOW_S:
                rate = 0.0  # nothing acked for a while
            elif t1 - t0 >= 1.0:
                rate = (b1 - b0) / (t1 - t0)
        return {
            "done": len(self.done) + len(self.deleted),
            "total": totals["fetch_count"] + len(self.delete),
            "bytes_done": self.bytes_done,
            "bytes_total": total,
            "rate_bps": round(rate) if rate is not None else None,
            # time left for the whole sync, the windows still held back included
            "eta_s": round((total + self.more_bytes - self.bytes_done) / rate) if rate else None,
            # the whole sync across the windows of the outbox (what earlier windows did + this one + held back)
            "run_done": self.before[0] + len(self.done) + len(self.deleted),
            "run_total": self.before[0] + totals["fetch_count"] + len(self.delete) + self.more,
            "run_bytes_done": self.before[1] + self.bytes_done,
            "run_bytes_total": self.before[1] + total + self.more_bytes,
        }


def track_meta(t: Track) -> dict[str, Any]:
    """What a client with its own database shows for a track (the file's tags say the same)."""
    meta: dict[str, Any] = {
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
    }
    if t.kind is MediaKind.PODCAST:  # what a podcast app lists an episode under
        meta["series"] = t.series
        meta["published"] = t.published.isoformat() if t.published else None
    return meta | listening_state(None, t)


def listening_state(key: str | None, t: Track) -> dict[str, Any]:
    """The library's listening data of an item: rating 0-5 (None = unrated), favourite, play count and, for
    podcasts and audiobooks, the resume position and whether it is finished."""
    out: dict[str, Any] = {"key": key} if key is not None else {}
    out |= {"rating": t.rating, "favourite": t.starred, "play_count": t.play_count}
    if t.kind is MediaKind.MUSIC and t.album_id and t.album:
        # the album's own rating; `album_ref` is what an `album_rating` event names
        out |= {
            "album_ref": ItemRef(library_id=t.ref.library_id, item_id=t.album_id).key(),
            "album_artist": t.album_artist or t.artist or "",
            "album": t.album,
            "album_rating": t.album_rating,
        }
    if t.kind in (MediaKind.PODCAST, MediaKind.AUDIOBOOK):
        out["position_s"] = t.position_s
        out["finished"] = t.finished
    return out


MB = 1024 * 1024
# a streaming session: the server prepares at most this many files / bytes ahead of the client's acks
STREAM_AHEAD_FILES = 4
STREAM_AHEAD_BYTES = 256 * MB
STREAM_PAGE_WAIT_S = (
    25.0  # a fetch page of a streaming session waits this long for the next prepared files
)
RATE_WINDOW_S = 15.0  # the transfer rate to a client is measured over this many seconds of acks


def window(plan: SyncPlan, dev: ClientDevice) -> tuple[int, int]:
    """Hold back the plan's adds and replaces beyond the outbox budget (they stay missing on the client and
    are planned again by the next sync), so the server never prepares the whole library at once. Files of an
    earlier window the client has not acked yet still count. Returns how many files (and bytes) were held back;
    the first file always goes."""
    budget = dev.settings.outbox_budget_mb * MB
    if budget <= 0:
        return 0, 0
    used = sum(dev.outbox().values())
    keep: list[PlanAction] = []
    held = held_bytes = 0
    for a in plan.actions:
        if a.kind in (ActionKind.ADD, ActionKind.REPLACE) and not a.tags_only:
            size = a.item.file.size if a.item is not None and a.item.file.size else 0
            if held or (used + size > budget and used > 0):
                held += 1
                held_bytes += size
                continue
            used += size
        keep.append(a)
    if held:
        plan.actions = keep
        log.info("%s: %d files wait for the next window of the outbox", dev.config.name, held)
    return held, held_bytes


def _stop_stream(s: ClientSession) -> None:
    """A streaming session ended: its job stops at the next file it would prepare (what is done stays)."""
    from syncmusic.devices.client import STREAMS

    st = STREAMS.get(s.device_id)
    if s.stream and st is not None and st is s.own_stream:
        st.stopped = True


class ClientSessions:
    def __init__(self, sync: SyncService, clients: Clients) -> None:
        self.sync = sync
        self.clients = clients
        self._more: dict[str, int] = {}  # device id -> files its last session held back
        # device id -> monotonic time the open app last waited on its status long poll
        self.listened: dict[str, float] = {}
        # device id -> (files, bytes) the windows of an unfinished sync delivered so far
        self._run: dict[str, tuple[int, int]] = {}
        self.sessions: dict[str, ClientSession] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._deliver: dict[str, asyncio.Task[None]] = {}  # device id -> debounced event delivery
        self.events_debounce_s = EVENTS_DEBOUNCE_S
        self._debouncing: set[str] = set()  # devices whose delivery still waits out the debounce
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
        self,
        device_id: str,
        name: str,
        capabilities: dict[str, Any] | None,
        client_os: str = "",
    ) -> None:
        """The device of a paired client (made at its first pairing; a re-pair keeps it and its settings). A
        player that cannot index its own files (`client_os`) gets its library database built on the server."""
        from syncmusic.core.models import DeviceConfig
        from syncmusic.core.proc_templates import with_tag_step
        from syncmusic.core.sync import suggested_processors, wifi_view
        from syncmusic.devices.client import ClientDevice

        cfg = await self.sync.store.device(device_id)
        new = cfg is None
        if cfg is not None and cfg.type != "client":
            cfg = wifi_view(cfg)  # Wi-Fi added to a device of the same card
        if cfg is None:
            cfg = DeviceConfig(
                id=device_id,
                type="client",
                name=name,
                on_connect="manual",
                registration_pending=True,  # the user chooses what it gets in the registration wizard
            )
            if client_os.lower() == "compas":  # reads its database only at start
                cfg.settings = {"player_database": "compas", "after_sync": "restart"}
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
        now, mono = _now(), time.monotonic()
        for s in list(self.sessions.values()):
            idle = s.stream and s.status == "ready" and mono - s.last_seen > STREAM_IDLE_S
            if s.expires_at <= now or idle:
                if idle:
                    log.info("client %s: no word from it for a while, its sync ends", s.device_id)
                _stop_stream(self.sessions.pop(s.id))

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

    def shown_for(self, device_id: str) -> ClientSession | None:
        """The session the device page shows: the open one, else the newest that waits for the user or failed."""
        open_ = self.open_for(device_id)
        if open_ is not None:
            return open_
        waiting = [
            s
            for s in self.sessions.values()
            if s.device_id == device_id and s.status in ("needs_review", "failed")
        ]
        return max(waiting, key=lambda s: s.created_at, default=None)

    # --- start ------------------------------------------------------------------------------------

    async def start(self, device_id: str, req: SyncRequest) -> ClientSession:
        """Take the client's report and plan; an open session of the device is replaced (the client started over)."""
        dev = await self.device(device_id)
        if dev.config.registration_pending:
            raise ClientError(
                "registration_pending", "finish setting up this player on the server first", 409
            )
        inv = req.inventory
        if inv.kind == "delta" and inv.since != str(dev.state.inventory_cursor):
            raise ClientError(
                "inventory_required", "send the full inventory (the server's state differs)", 409
            )
        items = {i.path: i.size for i in inv.items}
        # one check-and-create per device: two starts at the same moment (a retry, a double tap) share a session
        async with self._lock(device_id):
            old = self.open_for(device_id)
            if old is not None and old.status == "planning":
                return old  # still planning: the same session
            if old is not None:
                _stop_stream(self.sessions.pop(old.id))
            dev = await self.device(device_id)
            await asyncio.to_thread(
                dev.report, req.capabilities, items, inv.removed, full=inv.kind == "full"
            )
            s = ClientSession(
                id=uuid.uuid4().hex[:16],
                device_id=device_id,
                mode=req.mode,
                expires_at=_now() + dt.timedelta(hours=SESSION_HOURS),
                before=self._run.get(device_id, (0, 0)),
                stream=bool((req.capabilities or {}).get("stream")) and req.mode != "update",
            )
            self.sessions[s.id] = s
        if req.capabilities is not None:
            await self.clients.update_capabilities(device_id, req.capabilities)
        self.sync.client_sync_started(device_id)
        self._tasks[s.id] = asyncio.create_task(self._plan(s, first_inventory=not items))
        return s

    async def _plan(self, s: ClientSession, *, first_inventory: bool) -> None:
        from syncmusic.core.sync import SyncError

        try:
            await self._run_sync(s, first_inventory=first_inventory)
            if s.status == "planning":
                await self._fill(s)  # streaming: what an earlier session left in the outbox
                if s.stream:  # the generated set describes the whole card: only after the job
                    s.generated, s.gen_files, s.card_manifest = None, {}, None
                s.status = "ready"
                s.acked(time.monotonic())  # the transfer starts now
        except ClientError as e:
            s.status, s.message, s.code = "failed", str(e), e.code
        except SyncError as e:  # a precondition (no templates, library down): no traceback
            log.warning("client sync %s: %s", s.device_id, e)
            s.status, s.message, s.code = "failed", str(e), e.code
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
                if s.stream and await self._attach(s, running.id):
                    return
                await sync.ctx.jobs.wait(running.id)
        if not sync.ctx.jobs.device_busy(s.device_id):  # nothing writes the outbox now
            await asyncio.to_thread((await self.device(s.device_id)).prune_outbox)
        if s.mode == "update":
            job = await sync.update_sync(s.device_id)
        else:
            # tracked: the app and the device page see the planning steps while a big plan runs
            pc = await sync.planning.run(
                s.device_id, False, lambda: sync.plan(s.device_id, auto=True)
            )
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
                dev = await self.device(s.device_id)
                s.may_confirm = dev.settings.client_may_confirm
                return self._review(s, reasons)
            dev = await self.device(s.device_id)
            if s.stream:
                return await self._start_stream(s, plan, first)
            s.more, s.more_bytes = await asyncio.to_thread(window, plan, dev)
            job = await sync.apply(plan.id, first_sync=first)
        s.job_id = job.id
        info = await sync.ctx.jobs.wait(job.id)
        if info.status != JobStatus.SUCCEEDED:
            raise ClientError("server_error", info.error or f"sync job {info.status.value}", 500)

    async def _attach(self, s: ClientSession, job_id: str) -> bool:
        """A sync applied on the server (Apply, a schedule) waits for this client: the session streams that job's
        files instead of planning again. False when the running job is no such sync."""
        from syncmusic.devices.client import STREAMS

        st = STREAMS.get(s.device_id)
        if st is None or st.attached or st.stopped or st.plan is None:
            return False
        st.attached = True
        self._stream_plan(s, st.plan)
        s.plan_id, s.job_id = st.plan.id, job_id
        s.own_stream = st
        self._tasks[f"{s.id}:job"] = asyncio.create_task(self._stream_job(s, job_id))
        return True

    def _stream_plan(self, s: ClientSession, plan: SyncPlan) -> None:
        adds = [
            a
            for a in plan.actions
            if a.kind in (ActionKind.ADD, ActionKind.REPLACE) and a.item is not None
        ]
        s.planned = (len(adds), sum(max(a.item.file.size or 0, 0) for a in adds if a.item))
        s.metas = {a.ref.key(): track_meta(a.item.track) for a in adds if a.item is not None}

    async def _start_stream(
        self, s: ClientSession, plan: SyncPlan, first: Literal["keep"] | None
    ) -> None:
        """Start the sync job without waiting for it: it prepares the files a few ahead of the client's acks
        (`ClientDevice.put_track`), the fetch pages list them as they come, the rest follows when it ends."""
        from syncmusic.devices.client import STREAMS, Stream

        self._stream_plan(s, plan)
        old = STREAMS.get(s.device_id)
        if old is not None:
            old.stopped = True
        STREAMS[s.device_id] = s.own_stream = Stream(STREAM_AHEAD_FILES, STREAM_AHEAD_BYTES)
        job = await self.sync.apply(plan.id, first_sync=first)
        s.job_id = job.id
        self._tasks[f"{s.id}:job"] = asyncio.create_task(self._stream_job(s, job.id))

    async def _stream_job(self, s: ClientSession, job_id: str) -> None:
        from syncmusic.devices.client import STREAMS

        job = asyncio.ensure_future(self.sync.ctx.jobs.wait(job_id))
        try:
            st = s.own_stream
            if st is not None:  # the job commits, then waits for the client to take the rest
                committed = asyncio.ensure_future(st.committed.wait())
                await asyncio.wait({job, committed}, return_when=asyncio.FIRST_COMPLETED)
                committed.cancel()
            info = job.result() if job.done() else None
            while s.status == "planning":  # noqa: ASYNC110 - _plan lists the outbox first, then it is ready
                await asyncio.sleep(0.05)
            if s.status != "ready":
                return  # the session ended meanwhile
            if info is not None and info.status not in (JobStatus.SUCCEEDED, JobStatus.CANCELLED):
                s.status, s.message = "failed", info.error or f"sync job {info.status.value}"
                s.code = "server_error"
                return
            await self._fill(
                s
            )  # what the job did not list one by one: playlists, deletes, the generated set
        except Exception as e:  # noqa: BLE001 - the client sees the reason, the server logs it
            log.warning("client sync %s: %s", s.device_id, e, exc_info=True)
            s.status, s.message = "failed", str(e)
        finally:
            s.job_done = True
            with contextlib.suppress(Exception):
                await job  # the stream stays registered (Stop reaches it) until the job ended
            if s.own_stream is not None and STREAMS.get(s.device_id) is s.own_stream:
                STREAMS.pop(s.device_id, None)  # never a newer session's stream
            self._tasks.pop(f"{s.id}:job", None)

    async def stream_refresh(self, s: ClientSession) -> None:
        """List the files the streaming job wrote since the last look (each once, in write order)."""
        from syncmusic.devices.client import STREAMS

        st = STREAMS.get(s.device_id)
        if st is None or s.job_done:
            return
        new = st.written[s.seen :]
        if not new:
            return
        root = self._outbox_root(s.device_id)
        listed = {f["path"] for f in s.fetch}

        def items() -> list[dict[str, Any]]:
            out = []
            for rel, key, role in new:
                p = root / rel
                if rel in listed or not p.is_file():
                    continue
                st_ = p.stat()
                ck = (s.device_id, str(p), st_.st_size, st_.st_mtime_ns)
                if ck not in self._fid_cache:
                    self._fid_cache[ck] = sha256_of(p)
                f = self._fid_cache[ck]
                out.append(
                    {
                        "op": "add",
                        "key": key,
                        "fid": f,
                        "size": st_.st_size,
                        "mtime": int(st_.st_mtime),
                        "mime": mimetypes.guess_type(rel)[0] or "application/octet-stream",
                        "path": rel,
                        "role": role,
                        "url": f"files/{f}?ticket={s.ticket}",
                    }
                    | ({"meta": s.metas[key]} if role == "track" and key in s.metas else {})
                )
            return out

        got = await asyncio.to_thread(items)
        s.seen += len(new)
        held = (await self.device(s.device_id)).state.inventory
        for f in got:
            if f["path"] in held:
                f["op"] = "replace"
        s.fetch += got
        s.fids |= {f["fid"]: f["path"] for f in got}

    async def page_wait(
        self, s: ClientSession, section: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        """A page of a section; a streaming session's fetch page waits for the next prepared files and says
        `next` until its job ended and every file is listed."""
        if not (s.stream and section == "fetch"):
            return self.page(s, section, cursor, limit)
        start = int(cursor or 0) if (cursor or "0").isdigit() else -1
        if start < 0:
            raise ClientError("invalid", "bad cursor", 400)
        end_at = time.monotonic() + STREAM_PAGE_WAIT_S
        while True:
            s.last_seen = time.monotonic()  # a client waiting on this page is alive
            await self.stream_refresh(s)
            if s.status != "ready" or self.sessions.get(s.id) is not s:
                raise ClientError("server_error", s.message or "the sync stopped", 500)
            if len(s.fetch) > start or s.job_done or time.monotonic() >= end_at:
                break
            await asyncio.sleep(0.2)
        out = self.page(s, section, cursor, limit)
        if out["next"] is None and not s.job_done:
            out["next"] = str(start + len(out["items"]))  # more files are on their way
        return out

    def _review_totals(self, plan_id: str) -> dict[str, int]:
        """What the plan waiting for confirmation would do (for the client's preview)."""
        pc = self.sync.plans.get(plan_id)
        if pc is None:
            return {}
        plan = pc.plan
        adds = [a for a in plan.actions if a.kind in (ActionKind.ADD, ActionKind.REPLACE)]
        return {
            "fetch_count": len(adds),
            "fetch_bytes": sum(max(a.size_delta, 0) for a in adds),
            "delete_count": plan.count(ActionKind.REMOVE),
            "skipped_for_space": len(plan.space.dropped),
        }

    async def confirm(self, device_id: str, session_id: str) -> dict[str, Any]:
        """The client confirmed the plan its user saw on the player: apply it like the web UI's confirm."""
        s = self.get(device_id, session_id)
        if s.status != "needs_review" or not s.may_confirm or s.plan_id is None:
            raise ClientError(
                "confirm_not_allowed", "this sync can only be confirmed on the server", 409
            )
        if s.plan_id not in self.sync.plans:
            raise ClientError("session_expired", "the plan is gone: start a new sync", 410)
        s.status, s.message, s.may_confirm = "planning", "", False
        self._tasks[s.id] = asyncio.create_task(self._confirmed(s))
        return self.view(s)

    async def _confirmed(self, s: ClientSession) -> None:
        try:
            assert s.plan_id is not None
            job = await self.sync.apply(s.plan_id, confirm=True)
            s.job_id = job.id
            info = await self.sync.ctx.jobs.wait(job.id)
            if info.status != JobStatus.SUCCEEDED:
                raise ClientError(
                    "server_error", info.error or f"sync job {info.status.value}", 500
                )
            await self._fill(s)
            s.status = "ready"
            s.acked(time.monotonic())
        except ClientError as e:
            s.status, s.message, s.code = "failed", str(e), e.code
        except Exception as e:  # noqa: BLE001 - the client sees the reason, the server logs it
            log.warning("client sync %s: %s", s.device_id, e, exc_info=True)
            s.status, s.message = "failed", str(e)
        finally:
            self._tasks.pop(s.id, None)

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

        def fid(p: Path) -> tuple[str, int]:
            st = p.stat()
            ck = (dev.id, str(p), st.st_size, st.st_mtime_ns)
            if ck not in self._fid_cache:
                self._fid_cache[ck] = sha256_of(p)
            return self._fid_cache[ck], int(st.st_mtime)

        def fids() -> dict[str, tuple[str, int]]:
            return {rel: fid(dev.mount / rel) for rel in ordered}

        fid_of = await asyncio.to_thread(fids)
        tracks = await self._tracks(s.device_id, manifest)
        held = dev.state.inventory
        listed = {f["path"] for f in s.fetch} if s.stream else set()
        if not s.stream:
            s.fetch = []
        for rel in ordered:
            if rel in listed:
                continue
            item_key, role = by_path.get(
                rel, (None, "playlist" if rel in playlist_paths else "file")
            )
            s.fetch.append(
                {
                    "op": "replace" if rel in held else "add",
                    "key": item_key,
                    "fid": fid_of[rel][0],
                    "size": outbox[rel],
                    "mtime": fid_of[rel][1],
                    "mime": mimetypes.guess_type(rel)[0] or "application/octet-stream",
                    "path": rel,
                    "role": role,
                    "url": f"files/{fid_of[rel][0]}?ticket={s.ticket}",
                }
                | (
                    {"meta": track_meta(tracks[item_key])}
                    if role == "track" and item_key in tracks
                    else {}
                )
            )
        s.fids |= {f["fid"]: f["path"] for f in s.fetch}
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
        gen = await asyncio.to_thread(dev.generated_files, manifest)
        s.generated, s.gen_files = None, {}
        if gen is not None:
            files = []
            for g in gen.files:
                gfid, mtime = await asyncio.to_thread(fid, g.source)
                s.gen_files[gfid] = g.source
                size = (await asyncio.to_thread(g.source.stat)).st_size
                files.append(
                    {
                        "path": g.path,
                        "fid": gfid,
                        "size": size,
                        "mtime": mtime,
                        "url": f"files/{gfid}?ticket={s.ticket}",
                    }
                )
            s.generated = {"set_id": gen.set_id, "commit": gen.commit, "files": files}
        s.card_manifest = None
        card = await asyncio.to_thread(dev.card_manifest)
        if card is not None:
            cfid, mtime = await asyncio.to_thread(fid, card)
            s.gen_files[cfid] = card
            s.card_manifest = {
                "path": str(manifest_io.MANIFEST_PATH),
                "fid": cfid,
                "size": (await asyncio.to_thread(card.stat)).st_size,
                "mtime": mtime,
                "url": f"files/{cfid}?ticket={s.ticket}",
            }
        # what to do after a sync; the client does it only when the sync changed something on it
        s.after_sync = dev.settings.after_sync

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
        s.last_seen = time.monotonic()
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
            out["planning"] = self.planning_view(s)
        elif s.status in ("needs_review", "failed"):
            out["message"] = s.message
            if s.code:
                out["code"] = s.code
            if s.status == "needs_review":
                out["retry_after_s"] = 60
                out["may_confirm"] = s.may_confirm
                if s.may_confirm and s.plan_id is not None:
                    out["review"] = self._review_totals(s.plan_id)
        if s.status == "ready":
            plan: dict[str, Any] = {
                "session_id": s.id,
                "plan_id": s.plan_id,
                "mode": s.mode,
                "totals": s.totals(),
                "playlists": s.playlists,
                "generated": s.generated,
                "card_manifest": s.card_manifest,
                "after_sync": s.after_sync,
                "more": s.more,
                # streaming: files are listed as they are prepared; delete, state, playlists and the generated
                # set are complete once `complete` is true (read the session again after the last fetch page)
                "stream": s.stream,
                "complete": not s.stream or s.job_done,
                "expires_at": s.expires_at.isoformat(),
                "next": {},
            }
            for section in SECTIONS:
                page = self.page(s, section, None, limit)
                if s.stream and section == "fetch" and page["next"] is None and not s.job_done:
                    page["next"] = str(len(s.fetch))  # more files are on their way
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
        if start < 0:
            raise ClientError("invalid", "bad cursor", 400)
        limit = max(1, min(limit, PAGE_MAX))
        end = start + limit
        return {"items": items[start:end], "next": str(end) if end < len(items) else None}

    def planning_view(self, s: ClientSession) -> dict[str, Any]:
        """What the server is doing for a session that is still planning: the plan's step (libraries, device,
        compare, checks) with the items counted so far, else preparing the files; and how long it has run."""
        p = self.sync.planning.get(s.device_id)
        out: dict[str, Any] = {"elapsed_s": round((_now() - s.created_at).total_seconds(), 1)}
        if p is not None:
            keys = ("step", "index", "steps", "fraction", "items", "detail")
            return out | {k: p[k] for k in keys}
        return out | {"step": "prepare" if s.plan_id is not None else "waiting"}

    def app_listening(self, device_id: str) -> bool:
        """True while the client's app is open and waits on its status long poll: a Sync pressed on the server
        reaches it at once."""
        at = self.listened.get(device_id)
        return at is not None and time.monotonic() - at < LISTEN_GRACE_S

    def status(self, device_id: str, dev: ClientDevice, poll_s: int) -> dict[str, Any]:
        """What the client's poll learns: whether there is something to sync."""
        open_ = self.open_for(device_id)
        wanted = "none"
        reason = ""
        if self.sync.sync_requested(device_id):
            wanted, reason = "full", "requested on the server"
        elif dev.state.deletes or dev.outbox():
            wanted, reason = "full", "files are waiting for the client"
        elif self._more.get(device_id):
            wanted, reason = "full", "more files are ready to be prepared for the client"
        return {
            "sync_wanted": wanted,
            "reason": reason,
            # the user pressed Sync now on the server: an open app starts the sync right away
            "requested": self.sync.sync_requested(device_id),
            "plan_ready": open_ is not None and open_.status == "ready",
            "poll_s": poll_s,
            "server_time": _now().isoformat(),
            "registration": "pending" if dev.config.registration_pending else "done",
            "name": dev.config.name,  # the server's name wins: a renamed player shows it on the next poll
        }

    # --- delivery -------------------------------------------------------------------------------

    def file_of(self, device_id: str, fid: str) -> tuple[Path, str] | None:
        """(outbox file, device path) of `fid` when an open session of this device lists it; else None."""
        self._expire()
        for s in self.sessions.values():
            if s.device_id == device_id and s.status == "ready":
                s.last_seen = time.monotonic()
                if fid in s.fids:
                    rel = s.fids[fid]
                    return self._outbox_root(device_id) / rel, rel
                if fid in s.gen_files:
                    return s.gen_files[fid], s.gen_files[fid].name
        return None

    def file_by_ticket(self, ticket: str, fid: str) -> tuple[str, Path, str] | None:
        """(device id, outbox file, device path) of `fid` when the open session with this ticket lists it."""
        self._expire()
        for s in self.sessions.values():
            if secrets.compare_digest(s.ticket, ticket) and s.status == "ready":
                s.last_seen = time.monotonic()
                if fid in s.fids:
                    rel = s.fids[fid]
                    return s.device_id, self._outbox_root(s.device_id) / rel, rel
                if fid in s.gen_files:
                    return s.device_id, s.gen_files[fid], s.gen_files[fid].name
        return None

    # --- diagnostics: the client's log and state summary, sent by the user from the app ------------------

    def _diag_path(self, device_id: str) -> Path:
        return self.sync.ctx.config.data_dir / "network" / f"{device_id}.diagnostics.json"

    def save_diagnostics(
        self, device_id: str, log_text: str, summary: dict[str, Any]
    ) -> dict[str, Any]:
        """Keep the latest upload (secrets that look like tokens are masked once more here)."""
        doc = {
            "received_at": _now().isoformat(),
            "log": redact(log_text),
            "summary": json.loads(redact(json.dumps(summary))),
        }
        path = self._diag_path(device_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return {"received_at": doc["received_at"], "size": path.stat().st_size}

    def diagnostics(self, device_id: str) -> dict[str, Any] | None:
        path = self._diag_path(device_id)
        if not path.is_file():
            return None
        doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return doc

    def diagnostics_info(self, device_id: str) -> dict[str, Any] | None:
        doc = self.diagnostics(device_id)
        if doc is None:
            return None
        return {
            "received_at": doc["received_at"],
            "size": self._diag_path(device_id).stat().st_size,
        }

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
        for fail in body.failed:
            if fail.path not in fetch and fail.path not in deletes:
                raise ClientError("not_in_plan", f"{fail.path!r} is not in this plan", 409)
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
        s.acked(time.monotonic())
        for fail in body.failed:
            s.failed[fail.path] = f"{fail.error}: {fail.message[:500]}".rstrip(": ")
            log.warning("client %s could not take %s: %s", device_id, fail.path, fail.error)
        progress = s.progress()
        self.sync.ctx.bus.publish("client.progress", device_id=device_id, **progress)
        return {"progress": progress, "cancel": s.cancel}

    async def finish(self, device_id: str, session_id: str, status: str) -> dict[str, Any]:
        """The client is done with the session (complete, partial or cancelled): what it acked stays committed,
        what it did not stays in the outbox for the next session."""
        s = self.get(device_id, session_id)
        if s.status == "planning":
            raise ClientError("not_ready", "the plan is not ready yet", 409)
        async with self._lock(device_id):
            dev = await self.device(device_id)
            dev.state.inventory_cursor += 1
            await asyncio.to_thread(dev.save_state)
        s.status = "finished"
        self.sessions.pop(s.id, None)
        _stop_stream(s)
        self._more[device_id] = s.more
        progress = s.progress()
        more = s.more > 0 and status == "complete"
        if more:  # the next window continues this sync: its progress counts on from here
            self._run[device_id] = (progress["run_done"], progress["run_bytes_done"])
        else:
            self._run.pop(device_id, None)
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
            # the plan held files back for a later window: sync again (at once) for the next one
            "more": more,
        }

    def cancel(self, device_id: str) -> bool:
        """The user stopped the client's sync on the server: its next ack is told to stop."""
        s = self.open_for(device_id)
        if s is None:
            return False
        s.cancel = True
        _stop_stream(s)  # a streaming job waiting for a client that no longer acks stops now
        return True

    def drop_device(self, device_id: str) -> None:
        """The client was revoked: every session of it ends at once (tickets stop working, jobs stop)."""
        for s in [s for s in self.sessions.values() if s.device_id == device_id]:
            self.sessions.pop(s.id, None)
            _stop_stream(s)
            for key in (s.id, f"{s.id}:job"):
                t = self._tasks.pop(key, None)
                if t is not None and key == s.id:
                    t.cancel()  # still planning; a running job ends through its stream
        self._run.pop(device_id, None)

    def _lock(self, device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())

    # --- events without a sync ----------------------------------------------------------------------

    def events_arrived(self, device_id: str) -> None:
        """New listening data: hand it to the libraries soon (debounced), not only at the client's next sync."""
        old = self._deliver.get(device_id)
        running = None
        if old is not None and not old.done():
            if device_id in self._debouncing:
                old.cancel()  # still waiting: this one replaces it
            else:
                running = old  # already handing events on: let it finish, then this one follows
        self._deliver[device_id] = asyncio.create_task(self._deliver_events(device_id, running))

    async def _deliver_events(
        self, device_id: str, after: asyncio.Task[None] | None = None
    ) -> None:
        if after is not None:
            await asyncio.wait([after])
        self._debouncing.add(device_id)
        try:
            await asyncio.sleep(self.events_debounce_s)
        finally:
            self._debouncing.discard(device_id)
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
