# SPDX-License-Identifier: GPL-3.0-or-later
"""Integrity check jobs of a device: read every file back against the manifest, report, and
delete the broken files so the next sync writes them again. Split out of core/sync.py."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

from syncmusic.core import integrity
from syncmusic.core.manifest import ManifestError
from syncmusic.core.sync import SyncError
from syncmusic.infra import ffmpeg
from syncmusic.infra.jobs import JobCancelled, JobContext, JobInfo

if TYPE_CHECKING:
    from syncmusic.core.sync import SyncService

STEP_POLL_S = 1.0  # how often a preparation step updates its line and looks for a cancel
ESTIMATE_TIMEOUT_S = 5.0


async def _step[T](ctx: JobContext, message: str, aw: Awaitable[T]) -> T:
    """Await one preparation step with a status line that shows it is alive, and give up on it as soon as the user
    cancels (the device read behind it cannot be interrupted; its result is dropped)."""
    task = asyncio.ensure_future(aw)
    started = time.monotonic()
    ctx.progress(0.0, message)
    while True:
        done, _ = await asyncio.wait({task}, timeout=STEP_POLL_S)
        if done:
            return task.result()
        if ctx.cancelled:
            task.cancel()
            raise JobCancelled()
        ctx.progress(None, f"{message} ({int(time.monotonic() - started)} s)", log=False)


async def integrity_check(
    svc: SyncService, device_id: str, deep: bool = False, mode: str | None = None
) -> JobInfo:
    """Read-only check of the device against its manifest; the report is the job's result (`last_integrity`).
    `mode`: quick (sizes only, the default), full (every file read back) or deep (also decoded; `deep=True`)."""
    mode = mode or ("deep" if deep else "quick")
    if mode not in integrity.MODES:
        raise SyncError(f"unknown check mode {mode!r}", "invalid")
    cfg = await svc.config(device_id)
    if svc._network(cfg):
        raise SyncError(f"{cfg.name} is a server: its files cannot be read back here", "invalid")
    if svc.ctx.jobs.device_busy(cfg.id):
        raise SyncError(f"{cfg.name} is busy", "busy")
    await svc.open_device(cfg.id)  # not connected: refused now, not in the job

    async def job(ctx: JobContext) -> dict[str, Any]:
        from syncmusic.devices._filedevice import (  # core loads before devices
            MEDIA_EXTS,
            FileDeviceMixin,
        )

        device = await _step(ctx, "opening the device", svc.open_device(cfg.id))
        manifest = await _step(ctx, "reading the manifest", device.read_manifest())

        def cancelled() -> bool:
            return ctx.cancelled

        def status(fraction: float | None, message: str) -> None:
            ctx.progress(fraction, message, log=False)

        try:
            if isinstance(device, FileDeviceMixin):  # paths and sizes are enough: no tags
                found = await _step(
                    ctx,
                    "listing the player's files",
                    asyncio.to_thread(
                        integrity.list_files,
                        device.mount,
                        device._walk,
                        MEDIA_EXTS,
                        cancelled=cancelled,
                        progress=status,
                    ),
                )
            else:
                found = await _step(ctx, "reading the player's contents", device.read_contents())
        except integrity.Cancelled:
            raise JobCancelled() from None
        report = await asyncio.to_thread(
            integrity.check,
            device.mount,
            manifest,
            found,
            read_back=mode != "quick",
            decode=(lambda p: ffmpeg.decode_problem(p, cancelled=cancelled))
            if mode == "deep"
            else None,
            progress=status,
            cancelled=cancelled,
        )
        report["summary"] = integrity.summary(report)
        if report["io_error"]:
            await svc._record_health(
                cfg, error=f"integrity check: {report['io_error']}", job_id=ctx.info.id
            )
            raise SyncError(
                f"the device's storage failed while reading {report['io_error']} - it may be failing; copy "
                "what you need and check it on a computer",
                "io_error",
            )
        ctx.progress(None if report["cancelled"] else 1.0, report["summary"])
        return report  # cancelled: the job ends 'cancelled' and keeps this partial report

    return svc.ctx.jobs.submit("check", f"check {cfg.name}", job, device_id=cfg.id)


async def integrity_estimate(svc: SyncService, device_id: str) -> dict[str, Any]:
    """What a full check would read: {files, bytes, mb_s, full_s}. The speed is the read speed of the last
    full check, else the device's usual write speed (reads are not slower), else unknown (mb_s/full_s null)."""
    cfg = await svc.config(device_id)
    files = size = 0
    try:
        device = await asyncio.wait_for(svc.open_device(cfg.id), ESTIMATE_TIMEOUT_S)
        manifest = await asyncio.wait_for(device.read_manifest(), ESTIMATE_TIMEOUT_S)
    except (TimeoutError, SyncError, OSError, ManifestError):  # a damaged manifest estimates 0
        manifest = None
    if manifest is not None:
        files = len(manifest.items)
        size = sum(e.size or 0 for e in manifest.items.values())
    mb_s: float | None = None
    for j in svc.ctx.jobs.jobs():
        r = j.result
        if (
            j.device_id == cfg.id
            and j.kind == "check"
            and isinstance(r, dict)
            and r.get("mode", "full") != "quick"
            and r.get("bytes_read", 0) > 50_000_000
            and j.started_at
            and j.finished_at
        ):
            secs = (j.finished_at - j.started_at).total_seconds()
            if secs > 0:
                mb_s = r["bytes_read"] / secs / 1e6
                break
    if mb_s is None:
        h = await svc.health(cfg.id)
        mb_s = h.get("usual_mb_s") or h.get("last_mb_s")
    full_s = round(size / (mb_s * 1e6)) if mb_s and size else None
    return {
        "files": files,
        "bytes": size,
        "mb_s": round(mb_s, 1) if mb_s else None,
        "full_s": full_s,
    }


async def last_integrity(svc: SyncService, device_id: str) -> dict[str, Any] | None:
    """The newest integrity check of the device: {job_id, finished_at, status, error, report}."""
    fixed: set[str] = set()
    for j in svc.ctx.jobs.jobs():  # newest first: the fixes of a check come before it
        if j.device_id != device_id or j.finished_at is None:
            continue
        if j.kind == "check-fix" and isinstance(j.result, dict):
            fixed.update(j.result.get("paths", []))
        if j.kind == "check":
            report = j.result if isinstance(j.result, dict) else None
            if report is not None and fixed:  # deleted broken files are missing now
                report = integrity.after_fix(report, fixed)
            return {
                "job_id": j.id,
                "finished_at": j.finished_at.isoformat(),
                "status": j.status.value,
                "error": j.error,
                "report": report,
            }
    return None


async def integrity_fix(svc: SyncService, device_id: str) -> JobInfo:
    """Delete the files the last check found broken (only manifest paths, inside the device): the next sync sees
    them missing and writes them again."""
    cfg = await svc.config(device_id)
    last = await last_integrity(svc, device_id)
    broken = (last or {}).get("report", {}) or {}
    check_id = last["job_id"] if last else None
    paths = [b["path"] for b in broken.get("broken", [])]
    if not paths:
        raise SyncError(f"the last check of {cfg.name} found no broken files", "invalid")
    if svc.ctx.jobs.device_busy(cfg.id):
        raise SyncError(f"{cfg.name} is busy", "busy")
    await svc.open_device(cfg.id)

    async def job(ctx: JobContext) -> dict[str, Any]:
        device = await svc.open_device(cfg.id)
        manifest = await device.read_manifest()
        allowed = {e.path for e in manifest.items.values()} if manifest else set()
        root = device.mount.resolve()
        removed: list[str] = []
        for rel in paths:
            target = (device.mount / rel).resolve()
            if rel not in allowed or not target.is_relative_to(root):
                continue  # never anything but a file SyncMusic wrote
            await asyncio.to_thread(target.unlink, True)
            removed.append(rel)
        svc.drop_plans(cfg.id)
        ctx.progress(1.0, f"{len(removed)} broken files deleted; the next sync writes them again")
        # last_integrity folds this into the check's report
        return {"removed": len(removed), "paths": removed, "check_job_id": check_id}

    return svc.ctx.jobs.submit("check-fix", f"fix {cfg.name}", job, device_id=cfg.id)
