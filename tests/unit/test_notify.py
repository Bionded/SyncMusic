"""Webhook notifications against a local HTTP receiver."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from syncmusic.infra import notify
from tests.unit.test_api_sync import ALL, _apply, _register, env, music  # noqa: F401


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Receiver:
    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.delay = 0.0
        self.got = asyncio.Event()

    async def hook(self, request: Request) -> Response:
        await asyncio.sleep(self.delay)
        self.payloads.append(await request.json())
        self.got.set()
        return Response(status_code=204)


@pytest.fixture
async def receiver():
    rec = Receiver()
    port = _free_port()
    app = Starlette(routes=[Route("/hook", rec.hook, methods=["POST"])])
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    task = asyncio.create_task(srv.serve())
    while not srv.started:  # noqa: ASYNC110 - uvicorn only exposes a flag
        await asyncio.sleep(0.02)
    rec.url = f"http://127.0.0.1:{port}/hook?token=s3cret"  # type: ignore[attr-defined]
    yield rec
    srv.should_exit = True
    await task


async def _wait_for(rec: Receiver, n: int) -> None:
    for _ in range(200):
        if len(rec.payloads) >= n:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{len(rec.payloads)} payloads, expected {n}: {rec.payloads}")


async def _sync(app, c, tmp_path):
    await _register(c, tmp_path / "flash", name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    return await _apply(app, c, plan["id"], first_sync="keep")


async def test_sync_notifications_payload(env, receiver, tmp_path, caplog):
    app, c = env
    r = await c.patch("/api/v1/settings", json={"webhook_url": receiver.url})
    assert r.status_code == 200
    caplog.set_level(logging.DEBUG)
    report = await _sync(app, c, tmp_path)
    await _wait_for(receiver, 2)
    started, finished = receiver.payloads
    assert started["event"] == "sync.started" and finished["event"] == "sync.finished"
    assert set(finished) == {"event", "ts", "device", "summary", "report_url"}
    assert finished["device"] == {"id": "flash", "name": "Flash"}
    assert finished["summary"] == f"Flash: sync {report['status']}"
    assert finished["report_url"].endswith("/api/v1/sync-reports?device=flash")
    assert started["report_url"] is None
    # the URL (may hold a token) is never logged by SyncMusic; httpx logs request URLs at INFO, which
    # setup_logging always silences (WARNING)
    assert [
        r for r in caplog.records if r.name.startswith("syncmusic") and "s3cret" in r.getMessage()
    ] == []


async def test_event_filter(env, receiver, tmp_path):
    app, c = env
    await c.patch(
        "/api/v1/settings", json={"webhook_url": receiver.url, "webhook_events": ["sync.finished"]}
    )
    await _sync(app, c, tmp_path)
    await _wait_for(receiver, 1)
    await asyncio.sleep(0.2)
    assert [p["event"] for p in receiver.payloads] == ["sync.finished"]


async def test_slow_receiver_never_delays_a_sync(env, receiver, tmp_path, monkeypatch, caplog):
    app, c = env
    # No wall-clock bound (flaky under -n 3 load): a sync that waited for the receiver would only return
    # after the send timed out and logged the failure, so at return the failure must not be logged yet.
    monkeypatch.setattr(notify, "TIMEOUT_S", 4.0)
    receiver.delay = 6.0
    await c.patch("/api/v1/settings", json={"webhook_url": receiver.url})
    await _sync(app, c, tmp_path)
    assert receiver.payloads == [] and "failed" not in caplog.text  # the sync did not wait
    for _ in range(200):
        if "notification sync.started via" in caplog.text:
            break
        await asyncio.sleep(0.05)
    assert "notification sync.started via WebhookNotifier(host='127.0.0.1') failed" in caplog.text


async def test_receiver_down_is_logged(env, tmp_path, caplog):
    app, c = env
    await c.patch("/api/v1/settings", json={"webhook_url": f"http://127.0.0.1:{_free_port()}/x"})
    report = await _sync(app, c, tmp_path)
    assert report["status"] == "succeeded"
    for _ in range(100):
        if "notification sync.finished" in caplog.text:
            break
        await asyncio.sleep(0.05)
    assert "ConnectError" in caplog.text


async def test_no_webhook_no_sending(env, tmp_path, monkeypatch):
    app, c = env
    sent: list[dict] = []

    async def fake(self, payload):
        sent.append(payload)

    monkeypatch.setattr(notify.WebhookNotifier, "send", fake)
    await _sync(app, c, tmp_path)
    await asyncio.sleep(0.1)
    assert sent == [] and app.notifications.notifiers == []


def test_summaries():
    from syncmusic.infra.events import Event

    unknown = Event(type="device.unknown", data={"disk": "/dev/sdx", "model": "FlashStick"})
    assert notify.summary(unknown, None) == "unknown device connected: FlashStick (/dev/sdx)"
    failed = Event(type="sync.failed", data={"device_id": "f", "error": "boom"})
    assert notify.summary(failed, "Flash") == "Flash: sync failed: boom"


def test_setup_logging_silences_httpx_urls(tmp_path):
    from syncmusic.infra.log import setup_logging

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    noisy = {
        n: logging.getLogger(n).level
        for n in ("httpx", "httpcore", "aiosqlite", "PIL", "uvicorn.access")
    }
    try:
        setup_logging("DEBUG", "kv", tmp_path / "logs")
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    finally:
        for h in root.handlers:
            if h not in handlers:
                h.close()
        root.handlers[:] = handlers
        root.setLevel(level)
        for n, lvl in noisy.items():
            logging.getLogger(n).setLevel(lvl)


def test_a_partial_or_failed_sync_counts_as_failed():
    """Settings' rows "Full sync finished" / "Full sync failed or partial"."""
    ok = notify.Event(type="sync.finished", data={"status": "succeeded"})
    assert notify.filter_key(ok) == "sync.finished"
    for status in ("partial", "failed"):
        assert (
            notify.filter_key(notify.Event(type="sync.finished", data={"status": status}))
            == "sync.failed"
        )
    assert notify.filter_key(notify.Event(type="device.connected")) == "device.connected"


def test_quiet_hours_cover_a_window_over_midnight():
    from syncmusic.core.models import QuietHours

    q = QuietHours(start="23:00", end="07:30")
    assert q.covers("23:00") and q.covers("02:15") and q.covers("07:29")
    assert not q.covers("07:30") and not q.covers("12:00") and not q.covers("22:59")
    day = QuietHours(start="09:00", end="17:00")
    assert day.covers("12:00") and not day.covers("08:59") and not day.covers("17:00")


async def test_no_webhook_in_quiet_hours(env, receiver, tmp_path):
    app, c = env
    r = await c.patch(
        "/api/v1/settings",
        json={
            "webhook_url": receiver.url,
            "webhook_events": ["sync.started", "sync.finished"],
            "quiet_hours": {"start": "23:00", "end": "07:30"},
        },
    )
    assert r.status_code == 200, r.text
    app.notifications.now = lambda: datetime(2026, 10, 24, 1, 0)
    await _sync(app, c, tmp_path)
    await asyncio.sleep(0.3)
    assert receiver.payloads == []
    app.notifications.now = lambda: datetime(2026, 10, 24, 12, 0)
    app.bus.publish("sync.started", device_id="flash")
    await _wait_for(receiver, 1)
    bad = await c.patch(
        "/api/v1/settings", json={"quiet_hours": {"start": "25:00", "end": "07:00"}}
    )
    assert bad.status_code == 400


async def test_webhook_sends_the_user_agent():
    """outgoing requests name SyncMusic and its version."""
    import httpx

    from syncmusic import __version__

    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(204)

    n = notify.WebhookNotifier("http://hook.invalid/x")
    n.transport = httpx.MockTransport(handle)
    await n.send({"event": "test"})
    assert seen == [f"SyncMusic/{__version__}"]
