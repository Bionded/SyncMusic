# SPDX-License-Identifier: GPL-3.0-or-later
"""Integrity check jobs of a device: read every file back against the manifest, report, and
delete the broken files so the next sync writes them again. Split out of core/sync.py."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from syncmusic.core import integrity
from syncmusic.core.sync import SyncError
from syncmusic.infra import ffmpeg
from syncmusic.infra.jobs import JobContext, JobInfo

if TYPE_CHECKING:
    from syncmusic.core.sync import SyncService


async def integrity_check(svc: SyncService, device_id: str, deep: bool = False) -> JobInfo:
    """Read-only check of the device against its manifest, every file read back (`deep`: decoded too); the report is the job's result (`last_integrity`)."""
    cfg = await svc.config(device_id)
    if svc._network(cfg):
        raise SyncError(f"{cfg.name} is a server: its files cannot be read back here", "invalid")
    if svc.ctx.jobs.device_busy(cfg.id):
        raise SyncError(f"{cfg.name} is busy", "busy")
    await svc.open_device(cfg.id)  # not connected: refused now, not in the job

    async def job(ctx: JobContext) -> dict[str, Any]:
        device = await svc.open_device(cfg.id)
        manifest = await device.read_manifest()
        found = await device.read_contents()
        decode = ffmpeg.decode_problem if deep else None
        report = await asyncio.to_thread(
            integrity.check, device.mount, manifest, found, decode=decode, progress=ctx.progress
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
        ctx.progress(1.0, report["summary"])
        return report

    return svc.ctx.jobs.submit("check", f"check {cfg.name}", job, device_id=cfg.id)


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
