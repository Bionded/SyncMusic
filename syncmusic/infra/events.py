# SPDX-License-Identifier: GPL-3.0-or-later
"""In-process event bus: publish/subscribe with per-subscriber asyncio queues."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Event(BaseModel):
    type: str  # e.g. "device.connected", "sync.finished", "job.progress"
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = {}


# events a slow subscriber may lose: the next one of the same kind replaces them
DROPPABLE = frozenset({"job.progress"})
WARN_EVERY_S = 60.0


class EventBus:
    def __init__(self, queue_size: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size
        self._dropped: dict[
            int, tuple[float, int]
        ] = {}  # id(queue) -> (last warning, dropped since)

    def publish(self, type_: str, **data: Any) -> Event:
        event = Event(type=type_, data=data)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._overflow(q, event)
        return event

    def _overflow(self, q: asyncio.Queue[Event], event: Event) -> None:
        """A subscriber that does not keep up: its queued progress events collapse into the newest one (only the
        latest matters), so every other event still reaches it. One warning a minute."""
        queued = [*_drain(q), event]
        # the newest progress event of each job, in queue order next to the events that must survive
        newest: dict[tuple[str, Any], int] = {}
        for i, e in enumerate(queued):
            if e.type in DROPPABLE:
                newest[(e.type, e.data.get("job_id", e.data.get("job")))] = i
        keep_idx = set(newest.values())
        kept = [e for i, e in enumerate(queued) if e.type not in DROPPABLE or i in keep_idx]
        if len(kept) > q.maxsize:  # too many: progress goes first, then the oldest other events
            kept = [e for e in kept if e.type not in DROPPABLE] or kept
            kept = kept[-q.maxsize :]
        for e in kept:
            q.put_nowait(e)
        dropped = len(queued) - len(kept)
        last, count = self._dropped.get(id(q), (0.0, 0))
        count += dropped
        now = time.monotonic()
        if now - last >= WARN_EVERY_S:
            log.warning("slow event subscriber: dropped %d queued event(s)", count)
            self._dropped[id(q)] = (now, 0)
        else:
            self._dropped[id(q)] = (last, count)

    def subscribe(self, queue_size: int | None = None) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(queue_size or self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)
        self._dropped.pop(id(q), None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @contextlib.asynccontextmanager
    async def stream(self) -> AsyncIterator[asyncio.Queue[Event]]:
        q = self.subscribe()
        try:
            yield q
        finally:
            self.unsubscribe(q)


def _drain(q: asyncio.Queue[Event]) -> list[Event]:
    out: list[Event] = []
    with contextlib.suppress(asyncio.QueueEmpty):
        while True:
            out.append(q.get_nowait())
    return out
