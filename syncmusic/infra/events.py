# SPDX-License-Identifier: GPL-3.0-or-later
"""In-process event bus: publish/subscribe with per-subscriber asyncio queues."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Event(BaseModel):
    type: str  # e.g. "device.connected", "sync.finished", "job.progress"
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = {}


class EventBus:
    def __init__(self, queue_size: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size

    def publish(self, type_: str, **data: Any) -> Event:
        event = Event(type=type_, data=data)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("event subscriber queue full, dropping %s", type_)
        return event

    def subscribe(self, queue_size: int | None = None) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(queue_size or self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

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
