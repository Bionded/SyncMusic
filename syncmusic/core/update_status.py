# SPDX-License-Identifier: GPL-3.0-or-later
"""The device's automatic-update status for the UI: state, next check, the waiting
library-change slot. Read-only functions taking the SyncService; split out of core/sync.py."""

from __future__ import annotations

import datetime as dt
import time
from typing import TYPE_CHECKING, Any

from syncmusic.core.models import DeviceConfig
from syncmusic.core.plan_adjust import update_summary

if TYPE_CHECKING:
    from syncmusic.core.sync import SyncService


async def update_state(svc: SyncService, cfg: DeviceConfig) -> dict[str, Any]:
    """The device's automatic update for the UI's update line: `state` off (updates disabled), running,
     paused (a full sync, another job or a plan waiting for review holds the device), none (no update yet), failed,
     skipped (it did not run, `error` says why) or ok; `summary` = what the last one changed per kind. A skipped or
     failed update older than a full sync that wrote the device is superseded by it: ok at the full sync's time
    ."""
    last = await svc.store.last_report(cfg.id, "update")
    if last is not None and last.status in ("failed", "skipped"):
        full = await svc.store.last_report(cfg.id, "full")
        if (
            full is not None
            and full.status in ("succeeded", "partial")
            and full.started_at > last.started_at
        ):
            last = full.model_copy(update={"counts": {}})
    nxt = next_update_check(svc, cfg)
    other = svc.ctx.jobs.running_for_device(cfg.id)
    if not cfg.update_sync:
        state = "off"
    elif svc.ctx.jobs.unfinished_for_device(cfg.id, "update") is not None:
        state = "running"
    elif (other is not None and other.kind != "update") or (svc._waiting.get(cfg.id) or {}).get(
        "plan_id"
    ):
        state = "paused"
    elif last is None:
        state = "none"
    else:
        state = {"failed": "failed", "skipped": "skipped"}.get(last.status, "ok")
    return {
        "state": state,
        "last_at": (last.finished_at or last.started_at).isoformat() if last else None,
        "next_check": nxt.isoformat() if nxt else None,
        "error": last.message if last and last.status in ("failed", "skipped") else None,
        "summary": update_summary(last) if last else {},
        "change_at": await change_slot(svc, cfg),
    }


async def change_slot(svc: SyncService, cfg: DeviceConfig) -> str | None:
    """When a library change that waits for its slot (`update_change_gap_min`) gets its update; None:
    no change waiting (the UI says 'Library changed - update at …')."""
    if cfg.id not in svc._update_dirty or not cfg.update_sync or not svc._connected(cfg):
        return None
    ran = svc._update_ran.get(cfg.id)
    gap = (await svc.store.app_settings()).update_change_gap_min * 60
    left = 0.0 if ran is None else max(0.0, ran + gap - time.monotonic())
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=left)).isoformat()


def next_update_check(svc: SyncService, cfg: DeviceConfig) -> dt.datetime | None:
    """When the periodic update check of a connected device is due; None: off, not connected, or
    only on connect and on changes. A check marked by a library change runs at the next tick before that."""
    if not cfg.update_sync or not cfg.update_every_min or not svc._connected(cfg):
        return None
    last = svc._update_last.get(cfg.id)
    if last is None:
        return None
    left = max(0.0, last + cfg.update_every_min * 60 - time.monotonic())
    return dt.datetime.now(dt.UTC) + dt.timedelta(seconds=left)
