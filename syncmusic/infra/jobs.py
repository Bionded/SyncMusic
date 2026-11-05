# SPDX-License-Identifier: GPL-3.0-or-later
"""JobRunner: named async jobs, one job per device at a time, cooperative cancellation."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from syncmusic.infra.events import EventBus
from syncmusic.infra.log import log_context

log = logging.getLogger(__name__)
T = TypeVar("T")


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobInfo(BaseModel):
    id: str
    kind: str  # sync | update | plan | format | refresh | ...
    name: str
    device_id: str | None = None
    library_id: str | None = None  # a library refresh (the job view links it)
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    message: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    result: Any = None
    log: list[str] = []
    # seconds until done, from the recent progress rate; None = cannot be estimated (yet)
    eta_s: float | None = None
    # a cancel was requested; the job stops at its next safe point (the UI shows "Cancelling…", no second cancel)
    cancelling: bool = False
    # what a sync job is doing: phase reverse|copy|database|verify, files_done, files_total, bytes, failed
    stats: dict[str, Any] = {}


def outcome(info: JobInfo) -> str | None:
    """The status a job's result reports for itself (a sync report: `partial` when items failed)."""
    result = info.result
    return (
        result.get("status")
        if isinstance(result, dict) and isinstance(result.get("status"), str)
        else None
    )


class JobCancelled(Exception):
    """Raised by JobContext.check_cancelled() at a safe point."""


class Eta:
    """Time left from progress samples: the rate over the last WINDOW_S seconds of progress (the
    throughput of transcode + copy now, not since the start), smoothed; None until MIN_S seconds and either
    MIN_PROGRESS of progress or, for a large sync that needs long for that, MIN_STEPS progress steps (finished files)
    over MIN_STEPS_S seconds were seen since the phase started; while nothing moves the last estimate
    stays."""

    WINDOW_S = 120.0
    MIN_S = 8.0
    MIN_PROGRESS = 0.02
    MIN_STEPS = 10
    MIN_STEPS_S = 30.0
    SMOOTH = 0.3  # weight of a new rate sample

    def __init__(self) -> None:
        self._samples: deque[tuple[float, float]] = deque()
        self._rate: float | None = None  # smoothed progress per second
        self._eta: float | None = None
        self._start: float | None = None  # fraction at the start of the phase
        self._started_at = 0.0  # time of the phase's first sample
        self._steps = 0  # samples that moved the fraction since then

    def add(self, now: float, fraction: float) -> float | None:
        if self._samples and fraction < self._samples[-1][1]:
            self._samples.clear()  # went back (a new phase): start over
            self._rate = self._eta = None
        if not self._samples:
            self._start, self._started_at, self._steps = fraction, now, 0
        elif fraction > self._samples[-1][1]:
            self._steps += 1
        self._samples.append((now, fraction))
        while len(self._samples) > 2 and now - self._samples[1][0] >= self.WINDOW_S:
            self._samples.popleft()
        t0, f0 = self._samples[0]
        if fraction >= 1.0:
            self._eta = 0.0
        elif (
            now - t0 >= self.MIN_S
            and fraction > f0
            and (
                fraction - (self._start or 0.0) >= self.MIN_PROGRESS
                or (self._steps >= self.MIN_STEPS and now - self._started_at >= self.MIN_STEPS_S)
            )
        ):
            # the rate is smoothed, not the time left: a steady rate gives the exact countdown
            raw = (fraction - f0) / (now - t0)
            old = self._rate
            self._rate = raw if old is None else self.SMOOTH * raw + (1 - self.SMOOTH) * old
            self._eta = (1.0 - fraction) / self._rate
        return self._eta

    def at(self, now: float) -> float | None:
        """The estimate as time goes by without new progress (it counts down; it never goes below 0)."""
        if self._eta is None or not self._samples:
            return None
        return max(0.0, self._eta - (now - self._samples[-1][0]))


class JobContext:
    """Handed to the job function: progress reporting + cooperative cancellation."""

    def __init__(self, info: JobInfo, bus: EventBus, executor: ThreadPoolExecutor) -> None:
        self.info = info
        self._bus = bus
        self._executor = executor
        self._cancel = asyncio.Event()
        self._eta = Eta()
        # set by a job whose work is committed: a cancel arriving after that no longer makes it 'cancelled' (563)
        self.committed = False

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def request_cancel(self) -> None:
        self._cancel.set()

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    def progress(
        self,
        fraction: float | None = None,
        message: str | None = None,
        log: bool = True,
        **stats: Any,
    ) -> None:
        """`stats` (keyword) are merged into `info.stats` (a sync job's phase and counters).
        `log=False`: the message only updates the status line, not the job log (repeated statuses)."""
        if stats:
            self.info.stats = {**self.info.stats, **stats}
            if stats.get("phase") and (log_context.get() or {}).get("step") != stats["phase"]:
                # the lines logged from now on name the sync step (the Logs page's step filter)
                log_context.set({**(log_context.get() or {}), "step": str(stats["phase"])})
        if fraction is not None:
            self.info.progress = max(0.0, min(1.0, fraction))
            self.info.eta_s = self._eta.add(time.monotonic(), self.info.progress)
        line = None  # the stored log line, sent along so a live log looks like the finished one
        if message is not None:
            self.info.message = message
            if log:
                line = f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}"
                self.info.log.append(line)
                if len(self.info.log) > 2000:
                    del self.info.log[:500]
        self._bus.publish(
            "job.progress",
            job_id=self.info.id,
            device_id=self.info.device_id,
            progress=self.info.progress,
            message=self.info.message,
            eta_s=self.info.eta_s,
            cancelling=self.info.cancelling,
            stats=self.info.stats,
            log_line=line,
        )

    async def run_blocking(self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()  # the worker thread logs with the job's job/device
        return await loop.run_in_executor(self._executor, functools.partial(ctx.run, fn, *args))


JobFn = Callable[[JobContext], Awaitable[Any]]
SLOT_KINDS = ("sync",)  # device jobs counted against `device_job_limit`
WAITING_FOR_SLOT = "Waiting for another device sync to finish"
HISTORY_LIMIT = 200  # finished jobs kept (in memory and in the database)
PERSIST_INTERVAL = 5.0  # s between stores of a running job's progress and log
INTERRUPTED_ERROR = "Interrupted: SyncMusic stopped while this job was running"


class JobRunner:
    def __init__(self, bus: EventBus, max_workers: int | None = None) -> None:
        self._bus = bus
        workers = max_workers or max(1, (os.cpu_count() or 2) - 1)
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sd-work")
        self.max_workers = workers
        # tracks a sync prepares (download + transcode) ahead of the writes, at most: the pool size, so on a
        # Pi 5 three ffmpegs run while the fourth core writes (23.2 s → 17.9 s for 25 FLAC → MP3)
        self.prepare_workers = workers
        self._jobs: dict[str, JobInfo] = {}
        self._contexts: dict[str, JobContext] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._device_locks: dict[str, asyncio.Lock] = {}
        # at most this many SLOT_KINDS jobs run at once (None = no limit); the others stay queued
        self.device_job_limit: int | None = None
        self._slots_used = 0
        self._slots_changed: asyncio.Condition | None = None
        self._history_limit = HISTORY_LIMIT
        # called with every finished job (the app stores it so the history survives a restart)
        self.on_finished: Callable[[JobInfo], Awaitable[None]] | None = None
        # called when a job starts and every PERSIST_INTERVAL while it runs (and changes), so a crash leaves a
        # record of it with the log so far
        self.on_update: Callable[[JobInfo], Awaitable[None]] | None = None
        # called with every job that raised and its exception (a damaged device file system)
        self.on_failed: Callable[[JobInfo, BaseException], None] | None = None

    def resize(self, workers: int) -> None:
        """New processing pool size; only while no job runs (jobs hold the old pool)."""
        old = self.executor
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sd-work")
        self.max_workers = workers
        self.prepare_workers = workers
        old.shutdown(wait=False)

    def set_prepare_workers(self, n: int) -> None:
        self.prepare_workers = max(1, n)

    def set_device_job_limit(self, limit: int | None) -> None:
        """New limit of simultaneous device syncs; applies at once (queued jobs may start)."""
        self.device_job_limit = limit
        self._wake_slot_waiters()

    def _wake_slot_waiters(self) -> None:
        cond = self._slots_changed
        if cond is None:
            return

        async def wake() -> None:
            async with cond:
                cond.notify_all()

        asyncio.get_running_loop().create_task(wake())

    async def _take_slot(self, ctx: JobContext) -> bool:
        """Wait for a free device-sync slot (returns False when the job was cancelled while waiting)."""
        if self._slots_changed is None:
            self._slots_changed = asyncio.Condition()

        def free() -> bool:
            return self.device_job_limit is None or self._slots_used < self.device_job_limit

        async with self._slots_changed:
            if not free() and not ctx.cancelled:
                ctx.progress(message=WAITING_FOR_SLOT)
                await self._slots_changed.wait_for(lambda: free() or ctx.cancelled)
            if ctx.cancelled:
                return False
            self._slots_used += 1
            return True

    async def _release_slot(self) -> None:
        self._slots_used -= 1
        assert self._slots_changed is not None
        async with self._slots_changed:
            self._slots_changed.notify_all()

    def restore(self, history: list[JobInfo]) -> list[JobInfo]:
        """Finished jobs of earlier runs (from the database) as read-only history.

        Jobs still queued/running there were cut off by a crash or power loss: they become `failed` with
        INTERRUPTED_ERROR. Returns those, for the caller to store again.
        """
        interrupted: list[JobInfo] = []
        for info in history:
            if info.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                info.status = JobStatus.FAILED
                info.error = INTERRUPTED_ERROR
                interrupted.append(info)
            self._jobs.setdefault(info.id, info)
        self._trim()
        return interrupted

    def jobs(self) -> list[JobInfo]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> JobInfo | None:
        return self._jobs.get(job_id)

    def device_lock(self, device_id: str) -> asyncio.Lock:
        """The lock device jobs hold; held outside a job it makes the device busy (eject)."""
        return self._device_locks.setdefault(device_id, asyncio.Lock())

    def device_busy(self, device_id: str) -> bool:
        lock = self._device_locks.get(device_id)
        return bool(lock and lock.locked())

    def unfinished_for_device(self, device_id: str, kind: str | None = None) -> JobInfo | None:
        """A job on the device whose task has not ended - also one whose status is already final while it still
        holds the device lock (storing its last progress)."""
        for job_id, task in self._tasks.items():
            j = self._jobs.get(job_id)
            if (
                j is not None
                and j.device_id == device_id
                and not task.done()
                and (kind is None or j.kind == kind)
            ):
                return j
        return None

    def running_for_device(self, device_id: str) -> JobInfo | None:
        for j in self._jobs.values():
            if j.device_id == device_id and j.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                return j
        return None

    def submit(
        self,
        kind: str,
        name: str,
        fn: JobFn,
        device_id: str | None = None,
        library_id: str | None = None,
    ) -> JobInfo:
        info = JobInfo(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            name=name,
            device_id=device_id,
            library_id=library_id,
        )
        ctx = JobContext(info, self._bus, self.executor)
        self._jobs[info.id] = info
        self._contexts[info.id] = ctx
        self._tasks[info.id] = asyncio.create_task(self._run(ctx, fn), name=f"job-{info.id}")
        self._bus.publish("job.created", job_id=info.id, kind=kind, device_id=device_id, name=name)
        self._trim()
        return info

    async def wait(self, job_id: str) -> JobInfo:
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.shield(task)
        return self._jobs[job_id]

    def cancel(self, job_id: str) -> bool:
        ctx = self._contexts.get(job_id)
        if ctx is None or ctx.info.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            return False
        ctx.request_cancel()
        if not ctx.info.cancelling:
            ctx.info.cancelling = True
            ctx.progress()  # tells every open view that the job is being cancelled
        if ctx.info.status is JobStatus.QUEUED:
            self._wake_slot_waiters()  # it may be waiting for a slot
        return True

    async def shutdown(self, timeout: float = 30.0) -> None:
        """Cancel all jobs cooperatively, wait for them to reach a safe point."""
        for job_id in list(self._contexts):
            self.cancel(job_id)
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:
            _, still = await asyncio.wait(pending, timeout=timeout)
            for t in still:
                t.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)

    async def _run(self, ctx: JobContext, fn: JobFn) -> None:
        info = ctx.info
        lock = None
        slot = False
        heartbeat: asyncio.Task[None] | None = None
        stop_heartbeat = asyncio.Event()
        # every record logged by this job (its task, its child tasks, run_blocking) names it
        log_context.set(
            {
                "job": info.id,
                **({"device": info.device_id} if info.device_id else {}),
                **(
                    {"step": "plan"} if info.kind == "plan" else {}
                ),  # a sync's steps follow its phase
            }
        )
        if info.device_id is not None:
            lock = self.device_lock(info.device_id)
            await lock.acquire()
        try:
            if info.kind in SLOT_KINDS:
                slot = await self._take_slot(ctx)
            if ctx.cancelled:
                info.status = JobStatus.CANCELLED
                return
            info.status = JobStatus.RUNNING
            info.started_at = datetime.now(UTC)
            self._bus.publish(
                "job.started", job_id=info.id, kind=info.kind, device_id=info.device_id
            )
            heartbeat = asyncio.create_task(self._heartbeat(info, stop_heartbeat))
            try:
                info.result = await fn(ctx)
                info.status = (
                    JobStatus.CANCELLED
                    if ctx.cancelled and not ctx.committed
                    else JobStatus.SUCCEEDED
                )
                info.progress = 1.0 if info.status == JobStatus.SUCCEEDED else info.progress
            except JobCancelled:
                info.status = JobStatus.CANCELLED
            except asyncio.CancelledError:
                info.status = JobStatus.CANCELLED
                raise
            except Exception as exc:
                log.exception("job %s (%s) failed", info.id, info.name)
                info.status = JobStatus.FAILED
                info.error = f"{type(exc).__name__}: {exc}"
                if self.on_failed is not None:
                    try:
                        self.on_failed(info, exc)
                    except Exception:  # a convenience: never fail the runner for it
                        log.exception("on_failed for job %s", info.id)
        finally:
            info.finished_at = datetime.now(UTC)
            info.eta_s = None
            if heartbeat is not None:
                stop_heartbeat.set()
                await heartbeat  # its last store must not overwrite the final one
            if slot:
                await self._release_slot()
            if lock is not None:
                lock.release()
            self._bus.publish(
                "job.finished",
                job_id=info.id,
                kind=info.kind,
                device_id=info.device_id,
                status=info.status.value,
                error=info.error,
                outcome=outcome(info),
            )
            if self.on_finished is not None:
                try:
                    await self.on_finished(info)
                except Exception:  # the history is a convenience: never fail the job for it
                    log.warning("job %s: could not store it in the history", info.id, exc_info=True)

    async def _heartbeat(self, info: JobInfo, stop: asyncio.Event) -> None:
        """Store the running job at its start and then whenever its progress or log changed."""
        stored: tuple[Any, ...] | None = None
        while True:
            state = (len(info.log), info.progress, info.message)
            if self.on_update is not None and state != stored:
                stored = state
                try:
                    await self.on_update(info)
                except Exception:
                    log.warning("job %s: could not store its progress", info.id, exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), PERSIST_INTERVAL)
                return
            except TimeoutError:
                pass

    def _trim(self) -> None:
        done = [
            j for j in self._jobs.values() if j.status not in (JobStatus.QUEUED, JobStatus.RUNNING)
        ]
        if len(done) <= self._history_limit:
            return
        done.sort(key=lambda j: j.created_at)
        for j in done[: len(done) - self._history_limit]:
            self._jobs.pop(j.id, None)
            self._contexts.pop(j.id, None)
            self._tasks.pop(j.id, None)
