"""Server-sent events: the stream itself, and a sync seen through a real HTTP server."""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
import uvicorn

from syncmusic.api.events import CLIENT_QUEUE, event_stream
from syncmusic.api.server import ApiServer, create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from syncmusic.infra.events import EventBus
from tests.unit.test_api_sync import music  # noqa: F401 - fixture


async def _never() -> bool:
    return False


async def _next_data(stream) -> dict:
    while True:
        msg = await anext(stream)
        if msg.startswith("data: "):
            assert msg.endswith("\n\n")
            return json.loads(msg[len("data: ") :])


async def test_events_in_order_with_type_ts_data():
    bus = EventBus()
    stream = event_stream(bus, _never)
    assert await anext(stream) == ": connected\n\n"  # subscribed from here on
    bus.publish("job.created", job_id="j1")
    bus.publish("job.progress", job_id="j1", progress=0.5)
    first, second = await _next_data(stream), await _next_data(stream)
    assert first["type"] == "job.created" and first["data"] == {"job_id": "j1"} and first["ts"]
    assert second["type"] == "job.progress" and second["data"]["progress"] == 0.5
    await stream.aclose()
    assert bus.subscriber_count == 0


async def test_type_filter_and_keepalive():
    bus = EventBus()
    stream = event_stream(bus, _never, types=["sync."], keepalive_s=0.05)
    await anext(stream)
    bus.publish("job.created", job_id="j1")
    bus.publish("sync.started", device_id="d")
    assert (await _next_data(stream))["type"] == "sync.started"
    assert await asyncio.wait_for(anext(stream), 1) == ": keepalive\n\n"
    await stream.aclose()


async def test_disconnect_unsubscribes():
    bus = EventBus()
    gone = False

    async def disconnected() -> bool:
        return gone

    stream = event_stream(bus, disconnected, keepalive_s=0.05)
    await anext(stream)
    assert bus.subscriber_count == 1
    gone = True
    with pytest.raises(StopAsyncIteration):
        while True:
            await asyncio.wait_for(anext(stream), 1)
    assert bus.subscriber_count == 0


async def test_stalled_client_never_blocks_publishers():
    bus = EventBus()
    stream = event_stream(bus, _never)
    await anext(stream)  # subscribed, then never read again
    other = bus.subscribe()
    for i in range(CLIENT_QUEUE * 3):
        bus.publish("job.progress", n=i)  # returns at once, the client's queue just fills up
    assert other.qsize() == CLIENT_QUEUE * 3  # other subscribers get everything
    events = [await _next_data(stream) for _ in range(CLIENT_QUEUE)]
    assert [e["data"]["n"] for e in events] == list(range(CLIENT_QUEUE))  # the oldest are kept
    await stream.aclose()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def server(config, music, tmp_path):  # noqa: F811
    app = Application(config)
    api = create_api(AppFacade(app))
    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(api, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    task = asyncio.create_task(srv.serve())
    while not srv.started:  # noqa: ASYNC110 - uvicorn only exposes a flag
        await asyncio.sleep(0.02)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as c:
        r = await c.post(
            "/api/v1/libraries",
            json={"id": "music", "type": "folder", "name": "M", "settings": {"root": str(music)}},
        )
        assert r.status_code == 201, r.text
        yield app, c
    srv.should_exit = True
    await task


async def test_sync_seen_through_sse(server, tmp_path):
    app, c = server
    flash = tmp_path / "flash"
    flash.mkdir()
    r = await c.post(
        "/api/v1/devices/register",
        json={
            "name": "Flash",
            "mount_path": str(flash),
            "type": "flashdrive",
            "library_ids": ["music"],
        },
    )
    assert r.status_code == 201, r.text
    seen: list[dict] = []
    baseline = app.bus.subscriber_count  # the app's own (notifications)
    async with c.stream("GET", "/api/v1/events", params={"types": "job.,sync."}) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = resp.aiter_lines()
        assert await anext(lines) == ": connected"
        plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
        r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
        assert r.status_code == 202, r.text
        async for line in lines:
            if line.startswith("data: "):
                seen.append(json.loads(line[6:]))
                if seen[-1]["type"] == "job.finished":
                    break
    types = [e["type"] for e in seen]
    assert types[:2] == ["job.created", "job.started"] and types[-1] == "job.finished"
    assert "sync.started" in types and types.index("sync.started") < types.index("sync.finished")
    assert "job.progress" in types and all(e["type"].startswith(("job.", "sync.")) for e in seen)
    assert seen[-1]["data"]["status"] == "succeeded"
    for _ in range(100):  # the server notices the closed connection and unsubscribes
        if app.bus.subscriber_count == baseline:
            break
        await asyncio.sleep(0.05)
    assert app.bus.subscriber_count == baseline


async def test_stop_ends_open_event_streams_quickly_and_cleanly(config, caplog):
    """stopping the server while a client holds GET /events open ends the stream at once: no wait for
    the graceful-shutdown timeout, no ERROR ('Exception in ASGI application', CancelledError) in the log."""
    import logging
    import time

    app = Application(config)
    port = _free_port()
    srv = ApiServer(
        uvicorn.Config(
            create_api(AppFacade(app)),
            host="127.0.0.1",
            port=port,
            log_config=None,
            lifespan="on",
            timeout_graceful_shutdown=5,
        )
    )
    task = asyncio.create_task(srv.serve())
    while not srv.started:  # noqa: ASYNC110 - uvicorn only exposes a flag
        await asyncio.sleep(0.02)
    caplog.set_level(logging.INFO)
    async with (
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as c,
        c.stream("GET", "/api/v1/events") as resp,
    ):
        lines = resp.aiter_lines()
        assert await anext(lines) == ": connected"
        started = time.monotonic()
        srv.should_exit = True
        await asyncio.wait_for(task, 10)
        elapsed = time.monotonic() - started
    assert elapsed < 2, elapsed
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]
    assert app.bus.subscriber_count == 0 or not app.started
