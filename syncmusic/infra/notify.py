# SPDX-License-Identifier: GPL-3.0-or-later
"""Notifications: selected EventBus events go out to notifiers. One class per channel (webhook in v1;
Telegram/ntfy would be another Notifier).

The in-app notifications centre is independent of the webhook settings: every event of `INBOX_EVENTS`
is stored (`Store.add_notification`, the newest 500 kept) with a level and a UI link, and announced as a
`notification.created` event; the web UI words the message from the event and its data.

Payload: `{event, ts, device: {id, name}, summary, report_url}`. Sending runs in the service's own task with a
timeout, so a slow or unreachable receiver never delays a sync; failures are logged (without the URL, which may
carry a token).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from syncmusic import USER_AGENT
from syncmusic.core.models import INBOX_EVENTS, QuietHours
from syncmusic.infra.events import Event, EventBus

log = logging.getLogger(__name__)

DEFAULT_EVENTS = (
    "device.connected",
    "device.unknown",
    "sync.started",
    "sync.needs_confirmation",
    "sync.db_size_warning",
    "sync.finished",
    "sync.failed",
)
TIMEOUT_S = 5.0


def filter_key(event: Event) -> str:
    """The event name the settings filter on: a `sync.finished` that is partial or failed counts as `sync.failed`
    (Settings' rows "Full sync finished" / "Full sync failed or partial")."""
    if event.type == "sync.finished" and event.data.get("status") in ("partial", "failed"):
        return "sync.failed"
    return event.type


class Inbox(Protocol):
    """Where in-app notifications are stored (the Store)."""

    async def add_notification(
        self,
        event: str,
        level: str,
        data: dict[str, Any],
        device_id: str | None = None,
        device_name: str | None = None,
        link: str | None = None,
    ) -> dict[str, Any]: ...


def inbox_entry(event: Event) -> tuple[str, str | None] | None:
    """(level, UI link) of an event for the notifications centre; None: not stored."""
    d = event.data
    dev = f"/devices/{quote(str(d['device_id']))}" if d.get("device_id") else None
    match event.type:
        case "device.connected" | "device.formatted":
            return "info", dev
        case "device.unknown":
            return "info", "/devices/register"
        case "device.error" | "sync.failed" | "update.failed":
            job = d.get("job_id")
            return "error", f"{dev}/sync/{quote(str(job))}" if dev and job else dev
        case "sync.needs_confirmation":
            plan = d.get("plan_id")
            return "warning", f"{dev}/plan/{quote(str(plan))}" if dev and plan else dev
        case "sync.db_size_warning":
            return ("error" if d.get("level") == "over" else "warning"), dev
        case "sync.finished":
            status = d.get("status")
            level = {"failed": "error", "partial": "warning"}.get(str(status), "info")
            job = d.get("job_id")
            return level, f"{dev}/sync/{quote(str(job))}" if dev and job else dev
        case "update.sent":
            job = d.get("job_id")
            return "info", f"{dev}/sync/{quote(str(job))}" if dev and job else dev
        case "storage.low":
            return "warning", "/settings"
        case "app.update_available" | "app.updated":
            return "info", "/settings"
        case "app.update_failed":
            return "error", "/settings"
        case "device.health_warning":
            return "warning", dev
        case "reverse.finished":
            if d.get("failed") or d.get("errors"):
                return "error", f"{dev}/history" if dev else None
            return None
    return None


class Notifier(ABC):
    """One notification channel."""

    @abstractmethod
    async def send(self, payload: dict[str, Any]) -> None:
        """Deliver one notification; raise on failure (the service logs it)."""


class WebhookNotifier(Notifier):
    """POST the payload as JSON to a URL."""

    transport: httpx.AsyncBaseTransport | None = None  # tests

    def __init__(self, url: str, timeout_s: float | None = None) -> None:
        self.url = url
        self.timeout_s = timeout_s or TIMEOUT_S

    async def send(self, payload: dict[str, Any]) -> None:
        async with httpx.AsyncClient(
            timeout=self.timeout_s, transport=self.transport, headers={"User-Agent": USER_AGENT}
        ) as c:
            r = await c.post(self.url, json=payload)
            r.raise_for_status()

    def __repr__(self) -> str:  # the URL may carry a token
        return f"WebhookNotifier(host={urlsplit(self.url).hostname!r})"


def summary(event: Event, device_name: str | None) -> str:
    d = event.data
    who = device_name or d.get("model") or d.get("disk") or "device"
    match event.type:
        case "device.connected":
            return f"{who} connected"
        case "device.unknown":
            return f"unknown device connected: {who} ({d.get('disk')})"
        case "sync.started":
            return f"{who}: sync started"
        case "sync.needs_confirmation":
            return f"{who}: sync needs confirmation ({d.get('reason', '')})"
        case "sync.db_size_warning":
            mib = 1024 * 1024
            state = "over its limit" if d.get("level") == "over" else "close to its limit"
            return (
                f"{who}: the device database would be {d.get('estimated', 0) / mib:.1f} MiB after the sync, "
                f"{state} of {d.get('limit', 0) / mib:.0f} MiB"
            )
        case "sync.finished":
            return f"{who}: sync {d.get('status', 'finished')}"
        case "device.health_warning":
            return f"{who}: storage may be failing - {d.get('warning', '')}"
        case "app.update_available":
            return f"SyncMusic {d.get('latest', '')} is available ({d.get('notes', 0)} changes)"
        case "app.updated":
            return f"SyncMusic was updated from {d.get('previous', '?')} to {d.get('version', '?')}"
        case "app.update_failed":
            reason = (
                f": {d['reason']}" if d.get("reason") else " and went back to the previous version"
            )
            return f"SyncMusic {d.get('version', '?')} could not be installed{reason}"
        case "storage.low":
            gb = 1000**3  # decimal GB like the setting
            return f"SyncMusic's disk has {d.get('free', 0) / gb:.1f} GB free (warning below {d.get('limit', 0) / gb:.0f} GB)"
        case "sync.failed":
            hint = (
                " - gentle write mode (Sync settings) may help" if d.get("suggest_gentle") else ""
            )
            return f"{who}: sync failed: {d.get('error', '')}{hint}"
    return f"{who}: {event.type}"


class Notifications:
    """Subscribes to the bus while running; `configure` swaps notifiers and the event filter at any time."""

    def __init__(
        self,
        bus: EventBus,
        device_name: Callable[[str], Awaitable[str | None]],
        base_url: str,
        inbox: Inbox | None = None,
    ) -> None:
        self.bus = bus
        self.inbox = inbox
        self.device_name = device_name
        self.base_url = base_url.rstrip("/")
        self.notifiers: list[Notifier] = []
        self.events: set[str] = set(DEFAULT_EVENTS)
        self.inbox_events: set[str] = set(INBOX_EVENTS)
        self.quiet: QuietHours | None = None
        self.now: Callable[[], datetime] = datetime.now  # host local time (tests replace it)
        self._queue: asyncio.Queue[Event] | None = None
        self._task: asyncio.Task[None] | None = None

    def configure(
        self,
        webhook_url: str | None,
        events: list[str] | None = None,
        inbox_events: list[str] | None = None,
        quiet: QuietHours | None = None,
    ) -> None:
        self.notifiers = [WebhookNotifier(webhook_url)] if webhook_url else []
        self.events = set(DEFAULT_EVENTS if events is None else events)
        self.inbox_events = set(INBOX_EVENTS if inbox_events is None else inbox_events)
        self.quiet = quiet

    async def start(self) -> None:
        if self._task is None:
            # the default size: job and log events pass through this queue too
            self._queue = self.bus.subscribe()
            self._task = asyncio.create_task(self._run(self._queue), name="notifications")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._queue is not None:
            self.bus.unsubscribe(self._queue)
            self._queue = None

    async def _run(self, q: asyncio.Queue[Event]) -> None:
        while True:
            event = await q.get()
            key = filter_key(event)
            if self.inbox is not None and event.type in INBOX_EVENTS and key in self.inbox_events:
                try:
                    await self.store_entry(self.inbox, event)
                except Exception:  # noqa: BLE001 - a broken event never stops notifications
                    log.exception("in-app notification for %s failed", event.type)
            if key not in self.events or not self.notifiers:
                continue
            if self.quiet is not None and self.quiet.covers(self.now().strftime("%H:%M")):
                log.info("notification %s not sent: quiet hours", event.type)
                continue
            try:
                payload = await self.payload(event)
            except Exception:  # noqa: BLE001 - a broken event never stops notifications
                log.exception("notification payload for %s failed", event.type)
                continue
            for n in list(self.notifiers):
                try:
                    await asyncio.wait_for(n.send(payload), TIMEOUT_S + 1)
                except Exception as e:  # noqa: BLE001 - logged, never raised into the app
                    log.warning(
                        "notification %s via %r failed: %s", event.type, n, type(e).__name__
                    )

    async def store_entry(self, inbox: Inbox, event: Event) -> None:
        entry = inbox_entry(event)
        if entry is None:
            return
        level, link = entry
        device_id = event.data.get("device_id")
        name = await self.device_name(device_id) if device_id else None
        data = {
            k: v for k, v in event.data.items() if k not in ("device_ratings", "device_positions")
        }
        n = await inbox.add_notification(
            event.type, level, data, device_id=device_id, device_name=name, link=link
        )
        self.bus.publish("notification.created", id=n["id"], level=level, event=event.type)

    async def payload(self, event: Event) -> dict[str, Any]:
        device_id = event.data.get("device_id")
        name = await self.device_name(device_id) if device_id else None
        report_url = (
            f"{self.base_url}/api/v1/sync-reports?device={device_id}"
            if event.type in ("sync.finished", "sync.failed") and device_id
            else None
        )
        return {
            "event": event.type,
            "ts": event.ts.isoformat(),
            "device": {"id": device_id, "name": name or event.data.get("model")},
            "summary": summary(event, name),
            "report_url": report_url,
        }
