"""Application settings: simultaneous device syncs, the webhook test, versions for the About card."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from syncmusic.core.models import AppSettings
from syncmusic.infra.events import EventBus
from syncmusic.infra.jobs import WAITING_FOR_SLOT, JobRunner, JobStatus
from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures


async def _blocked_jobs(runner: JobRunner, n: int, kind: str = "sync", device: str = "d"):
    gates = [asyncio.Event() for _ in range(n)]

    def fn(gate):
        async def run(ctx):
            await gate.wait()

        return run

    jobs = [
        runner.submit(kind, f"j{i}", fn(g), device_id=f"{device}{i}") for i, g in enumerate(gates)
    ]
    await asyncio.sleep(0.05)
    return jobs, gates


async def test_device_syncs_wait_for_a_free_slot():
    runner = JobRunner(EventBus())
    runner.set_device_job_limit(1)
    (a, b), (ga, gb) = await _blocked_jobs(runner, 2)
    assert a.status is JobStatus.RUNNING
    assert b.status is JobStatus.QUEUED and b.message == WAITING_FOR_SLOT
    ga.set()
    await runner.wait(a.id)
    await asyncio.sleep(0.05)
    assert b.status is JobStatus.RUNNING  # the freed slot went to b
    gb.set()
    await runner.wait(b.id)
    assert b.status is JobStatus.SUCCEEDED


async def test_two_device_syncs_run_at_once_and_a_third_waits_at_limit_2():
    runner = JobRunner(EventBus())
    runner.set_device_job_limit(2)
    (a, b, c), (ga, gb, gc) = await _blocked_jobs(runner, 3)
    assert a.status is JobStatus.RUNNING and b.status is JobStatus.RUNNING
    assert c.status is JobStatus.QUEUED and c.message == WAITING_FOR_SLOT
    gb.set()
    await runner.wait(b.id)
    await asyncio.sleep(0.05)
    assert c.status is JobStatus.RUNNING and a.status is JobStatus.RUNNING
    ga.set()
    gc.set()
    for j in (a, c):
        await runner.wait(j.id)


async def test_raising_the_limit_starts_waiting_jobs_and_other_kinds_never_wait():
    runner = JobRunner(EventBus())
    runner.set_device_job_limit(1)
    (a, b, c), gates = await _blocked_jobs(runner, 3)
    assert [j.status for j in (a, b, c)].count(JobStatus.RUNNING) == 1
    runner.set_device_job_limit(None)
    await asyncio.sleep(0.05)
    assert all(j.status is JobStatus.RUNNING for j in (a, b, c))
    (r,), (gr,) = await _blocked_jobs(runner, 1, kind="refresh", device="lib")
    assert r.status is JobStatus.RUNNING
    for g in (*gates, gr):
        g.set()
    for j in (a, b, c, r):
        await runner.wait(j.id)


async def test_a_waiting_job_can_be_cancelled():
    runner = JobRunner(EventBus())
    runner.set_device_job_limit(1)
    (a, b), (ga, _) = await _blocked_jobs(runner, 2)
    assert runner.cancel(b.id)
    await runner.wait(b.id)
    assert b.status is JobStatus.CANCELLED and a.status is JobStatus.RUNNING
    ga.set()
    await runner.wait(a.id)


async def test_the_setting_reaches_the_runner(env):
    app, c = env
    r = await c.patch("/api/v1/settings", json={"max_device_syncs": 2})
    assert r.status_code == 200 and r.json()["max_device_syncs"] == 2
    assert app.jobs.device_job_limit == 2
    await c.patch("/api/v1/settings", json={"max_device_syncs": None})
    assert app.jobs.device_job_limit is None


@pytest.fixture
def receiver():
    got: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            got.append(json.loads(body))
            self.send_response(404 if self.path.endswith("/gone") else 204)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", got
    srv.shutdown()


async def test_webhook_test_sends_to_the_given_or_stored_url(env, receiver):
    app, c = env
    base, got = receiver
    r = await c.post("/api/v1/settings/webhook-test", json={})
    assert r.status_code == 400 and r.json()["error"] == "invalid"  # nothing stored yet

    r = await c.post("/api/v1/settings/webhook-test", json={"url": f"{base}/hook"})
    assert r.json() == {"ok": True, "message": "sent"}
    assert got[-1]["event"] == "test" and got[-1]["summary"] == "SyncMusic test notification"

    await c.patch("/api/v1/settings", json={"webhook_url": f"{base}/secret-token/gone"})
    r = await c.post("/api/v1/settings/webhook-test", json={"url": "********"})  # the mask = stored
    body = r.json()
    assert body["ok"] is False and "404" in body["message"]
    assert "secret-token" not in body["message"]  # the URL is a secret
    assert len(got) == 2


async def test_version_lists_pygpod_and_python(env):
    _, c = env
    v = (await c.get("/api/v1/version")).json()
    assert v["version"] and v["pygpod"] == "0.2.3" and v["python"].count(".") == 2


async def test_webhook_url_and_events_are_checked(env):
    """a non-HTTP webhook URL or an unknown event name is refused with 400 naming the field."""
    app, c = env
    r = await c.patch("/api/v1/settings", json={"webhook_url": "ftp://x/secret-token"})
    assert r.status_code == 400 and r.json()["error"] == "invalid"
    assert "webhook_url" in r.text and "http" in r.text and "secret-token" not in r.text
    r = await c.patch("/api/v1/settings", json={"webhook_events": ["sync.finished", "nope"]})
    assert r.status_code == 400 and "webhook_events" in r.text and "'nope'" in r.text
    r = await c.patch(
        "/api/v1/settings",
        json={
            "webhook_url": "https://hooks.example/x",
            "webhook_events": ["sync.failed", "reverse.finished"],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["webhook_events"] == ["sync.failed", "reverse.finished"]
    r = await c.patch("/api/v1/settings", json={"webhook_url": ""})
    assert r.status_code == 200 and r.json()["webhook_url"] is None


async def test_a_stored_value_that_is_invalid_now_falls_back_to_its_default(env):
    app, c = env
    await app.store.db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("webhook_events", '["nope"]')
    )
    settings = await app.store.app_settings()
    assert settings.webhook_events == AppSettings().webhook_events
    assert (await c.get("/api/v1/settings")).status_code == 200
