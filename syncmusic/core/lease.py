# SPDX-License-Identifier: GPL-3.0-or-later
"""The device lease (`.syncmusic/lease.json`): which installation is writing to the player right now.

Written before a sync, an update or a wipe, refreshed every minute, released at the end. Another installation that
finds a fresh lease of someone else refuses to start ("being synced by <name> since <time>"); a lease whose
heartbeat is older than ten minutes is stale (that installation died or the player was unplugged) and is taken over
with a warning. Matters most for network devices, which several hosts reach at once."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from syncmusic.base.device import Device
from syncmusic.core.models import UtcDatetime, utcnow

log = logging.getLogger(__name__)

FILE_NAME = "lease.json"
HEARTBEAT_S = 60.0
STALE_S = 600.0


class Lease(BaseModel):
    instance_id: str
    name: str = ""
    job_id: str = ""
    started_at: UtcDatetime
    heartbeat_at: UtcDatetime


class LeaseHeld(RuntimeError):
    def __init__(self, lease: Lease) -> None:
        when = lease.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
        super().__init__(f"being synced by {lease.name or lease.instance_id} since {when}")
        self.lease = lease


def parse(raw: bytes | None) -> Lease | None:
    """The lease in a file, None when there is none (absent, empty or unreadable)."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return Lease.model_validate(data) if data else None
    except (ValueError, ValidationError):
        return None


def blocking(lease: Lease | None, instance_id: str, now: dt.datetime | None = None) -> bool:
    """Another installation holds a fresh lease."""
    if lease is None or lease.instance_id == instance_id:
        return False
    age = ((now or utcnow()) - lease.heartbeat_at).total_seconds()
    return age < STALE_S


async def current(device: Device) -> Lease | None:
    return parse(await device.read_state(FILE_NAME))


@contextlib.asynccontextmanager
async def held(
    device: Device,
    instance_id: str,
    name: str,
    job_id: str,
    check: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[None]:
    """Hold the device's lease for the block; LeaseHeld when another installation holds a fresh one. `check`
    (raises when the device is gone) runs before every heartbeat so a heartbeat never writes to a bare
    mountpoint. Without an instance id (tools, tests) there is no lease."""
    if not instance_id:
        yield
        return
    old = await current(device)
    if blocking(old, instance_id):
        assert old is not None
        raise LeaseHeld(old)
    if old is not None and old.instance_id != instance_id:
        log.warning(
            "taking over a stale lease of %s (last heartbeat %s)",
            old.name or old.instance_id,
            old.heartbeat_at.isoformat(),
        )
    now = utcnow()
    lease = Lease(
        instance_id=instance_id, name=name, job_id=job_id, started_at=now, heartbeat_at=now
    )
    await device.write_state(FILE_NAME, lease.model_dump_json().encode())

    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            if check is not None:
                await check()
            lease.heartbeat_at = utcnow()
            await device.write_state(FILE_NAME, lease.model_dump_json().encode())

    task = asyncio.create_task(beat(), name=f"lease-{job_id}")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        try:
            if check is not None:
                await check()
            await device.delete_state(FILE_NAME)
        except Exception as e:  # noqa: BLE001 - the device may be gone; a stale lease expires by itself
            log.info("could not release the device lease: %s", e)
