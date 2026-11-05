# SPDX-License-Identifier: GPL-3.0-or-later
"""Planning progress: planning a big library takes long, so a plan started from the UI reports its
steps on the event bus (`plan.progress`, then `plan.finished`) and can be asked for (`GET /planning`). One plan per
device at a time: a second request for the same device joins the running one (leaving the page and coming back does not plan again), and Cancel stops it."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from syncmusic.core.models import utcnow
from syncmusic.infra.events import EventBus

PlanStep = Literal["libraries", "device", "compare", "checks"]
STEPS: tuple[PlanStep, ...] = ("libraries", "device", "compare", "checks")
MIN_INTERVAL_S = 0.25  # at most this often a `plan.progress` within one step


class PlanProgress(BaseModel):
    device_id: str
    force: bool = False
    step: PlanStep = "libraries"
    index: int = 0  # of the step in STEPS
    steps: int = len(STEPS)
    fraction: float | None = None  # within the step; None: not known
    items: int = 0  # library items counted so far
    detail: str = ""  # e.g. the library being read
    started_at: datetime

    @property
    def progress(self) -> float | None:
        """Of the whole plan; None while the current step's share is not known."""
        if self.fraction is None:
            return None
        return (self.index + self.fraction) / self.steps

    def public(self) -> dict[str, Any]:
        return {**self.model_dump(mode="json"), "progress": self.progress}


_current: ContextVar[PlanTracker | None] = ContextVar("plan_tracker", default=None)


def step(
    name: PlanStep,
    *,
    fraction: float | None = None,
    items: int | None = None,
    detail: str | None = None,
) -> None:
    """Report a planning step from inside a tracked plan; a no-op for plans nobody follows (auto sync, update)."""
    tracker = _current.get()
    if tracker is not None:
        tracker.update(name, fraction=fraction, items=items, detail=detail)


class PlanTracker:
    def __init__(self, bus: EventBus, progress: PlanProgress) -> None:
        self.bus = bus
        self.p = progress
        self._sent = 0.0

    def update(
        self, name: PlanStep, *, fraction: float | None, items: int | None, detail: str | None
    ) -> None:
        new_step = name != self.p.step
        self.p.step, self.p.index, self.p.fraction = name, STEPS.index(name), fraction
        if items is not None:
            self.p.items = items
        if detail is not None or new_step:
            self.p.detail = detail or ""
        now = time.monotonic()
        if new_step or now - self._sent >= MIN_INTERVAL_S:
            self._sent = now
            self.bus.publish("plan.progress", **self.p.public())


class Planning:
    """The plans started from the UI that are running now, one per device."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._tasks: dict[str, tuple[asyncio.Task[Any], PlanTracker]] = {}

    def running(self) -> list[dict[str, Any]]:
        return [t.p.public() for task, t in self._tasks.values() if not task.done()]

    def get(self, device_id: str) -> dict[str, Any] | None:
        entry = self._tasks.get(device_id)
        return entry[1].p.public() if entry and not entry[0].done() else None

    async def run[T](self, device_id: str, force: bool, plan: Callable[[], Awaitable[T]]) -> T:
        """`plan()` tracked; joins the device's running plan when it was asked with the same `force`. The plan goes
        on when the request that waits for it goes away (the next request joins it)."""
        entry = self._tasks.get(device_id)
        if entry is None or entry[0].done() or entry[1].p.force != force:
            if entry is not None and not entry[0].done():
                entry[0].cancel()  # another force: that plan's result is not wanted any more
            tracker = PlanTracker(
                self.bus, PlanProgress(device_id=device_id, force=force, started_at=utcnow())
            )
            task = asyncio.create_task(self._tracked(tracker, plan), name=f"plan-{device_id}")
            task.add_done_callback(
                lambda t: t.cancelled() or t.exception()
            )  # retrieved even if nobody waits
            entry = (task, tracker)
            self._tasks[device_id] = entry
        result: T = await asyncio.shield(entry[0])
        return result

    async def _tracked[T](self, tracker: PlanTracker, plan: Callable[[], Awaitable[T]]) -> T:
        _current.set(tracker)  # this task's own context
        device_id = tracker.p.device_id
        self.bus.publish("plan.progress", **tracker.p.public())
        try:
            result = await plan()
        except asyncio.CancelledError:
            self.bus.publish("plan.finished", device_id=device_id, cancelled=True)
            raise
        except Exception as e:
            self.bus.publish("plan.finished", device_id=device_id, error=str(e))
            raise
        finally:
            if self._tasks.get(device_id, (None,))[0] is asyncio.current_task():
                del self._tasks[device_id]
        self.bus.publish(
            "plan.finished",
            device_id=device_id,
            plan_id=getattr(getattr(result, "plan", None), "id", None),
        )
        return result

    def cancel(self, device_id: str) -> bool:
        entry = self._tasks.get(device_id)
        if entry is None or entry[0].done():
            return False
        return entry[0].cancel()
