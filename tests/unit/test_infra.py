import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from syncmusic.infra.config import load_config
from syncmusic.infra.db import Database
from syncmusic.infra.events import EventBus
from syncmusic.infra.jobs import JobRunner, JobStatus
from syncmusic.infra.log import redact


def test_config_precedence(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('port = 1111\nhost = "127.0.0.1"\nmode = "web"\n')
    cfg = load_config(f, env={"SYNCMUSIC_PORT": "2222"}, overrides={"mode": "daemon"})
    assert cfg.port == 2222 and cfg.host == "127.0.0.1" and cfg.mode == "daemon"


def test_config_default_mode_from_build():
    from syncmusic import _build

    assert load_config(env={}).mode == _build.DEFAULT_MODE


def test_redact():
    s = redact("GET /rest/ping?u=bob&t=abc123&s=salt password=hunter2 api_token=xyz")
    assert "abc123" not in s and "hunter2" not in s and "xyz" not in s and "salt" not in s
    assert "u=bob" in s


async def test_db_migrations(tmp_path):
    db = Database(tmp_path / "x.sqlite3")
    await db.open()
    version = await db.schema_version()
    assert version >= 1
    await db.execute("INSERT INTO settings(key, value) VALUES (?, ?)", ("a", "1"))
    row = await db.fetchone("SELECT value FROM settings WHERE key='a'")
    assert row["value"] == "1"
    await db.close()
    db2 = Database(tmp_path / "x.sqlite3")
    await db2.open()  # re-open: the schema is not created again (tests/unit/test_db_schema.py)
    assert await db2.schema_version() == version
    assert (await db2.fetchone("SELECT value FROM settings WHERE key='a'"))["value"] == "1"
    await db2.close()


async def test_event_bus():
    bus = EventBus()
    async with bus.stream() as q:
        bus.publish("x.y", a=1)
        ev = await asyncio.wait_for(q.get(), 1)
    assert ev.type == "x.y" and ev.data == {"a": 1}


async def test_jobs_serialise_per_device_and_cancel():
    bus = EventBus()
    jobs = JobRunner(bus, max_workers=2)
    order: list[str] = []
    gate = asyncio.Event()

    async def first(ctx):
        order.append("first-start")
        await gate.wait()
        order.append("first-end")

    async def second(ctx):
        order.append("second")

    j1 = jobs.submit("sync", "a", first, device_id="d1")
    j2 = jobs.submit("sync", "b", second, device_id="d1")
    await asyncio.sleep(0.05)
    assert order == ["first-start"] and jobs.device_busy("d1")
    gate.set()
    await jobs.wait(j1.id)
    await jobs.wait(j2.id)
    assert order == ["first-start", "first-end", "second"]

    async def long(ctx):
        for _ in range(100):
            await asyncio.sleep(0.01)
            ctx.check_cancelled()

    j3 = jobs.submit("sync", "c", long)
    await asyncio.sleep(0.02)
    assert jobs.cancel(j3.id)
    assert (await jobs.wait(j3.id)).status == JobStatus.CANCELLED

    async def boom(ctx):
        raise ValueError("bad")

    j4 = jobs.submit("x", "boom", boom)
    info = await jobs.wait(j4.id)
    assert info.status == JobStatus.FAILED and "bad" in (info.error or "")
    j5 = jobs.submit("x", "blk", lambda ctx: ctx.run_blocking(sum, [1, 2]))
    assert (await jobs.wait(j5.id)).result == 3
    await jobs.shutdown()


async def test_health_endpoint(config):
    app = Application(config)
    api = create_api(AppFacade(app), manage_lifecycle=False)
    await app.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            r = await c.get("/api/v1/health")
            assert r.status_code == 200 and r.json()["status"] == "ok"
            assert (await c.get("/api/v1/version")).json()["version"]
    finally:
        await app.stop()


@pytest.mark.integration
def test_daemon_starts_and_serves_health(tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import time

    import httpx

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {
        **os.environ,
        "SYNCMUSIC_DATA_DIR": str(tmp_path / "d"),
        "SYNCMUSIC_CACHE_DIR": str(tmp_path / "c"),
        "SYNCMUSIC_MOUNT_ROOT": str(tmp_path / "m"),
        "SYNCMUSIC_DETECT_DEVICES": "0",  # never scan or probe the real USB disks
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "syncmusic",
            "run",
            "--mode",
            "daemon",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
    )
    try:
        for _ in range(100):
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/api/v1/health", timeout=1)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert r.json()["status"] == "ok"
    finally:
        proc.terminate()
        proc.wait(10)


def test_tests_never_detect_devices():
    """every test process (and the daemons it starts) has detection off and the allowlist set."""
    import os

    from tests.conftest import HARDWARE_TOML

    assert os.environ["SYNCMUSIC_DETECT_DEVICES"] == "0"
    assert os.environ["SYNCMUSIC_DEVICE_ALLOWLIST"] == str(HARDWARE_TOML)
    cfg = load_config()
    assert cfg.detect_devices is False and cfg.device_allowlist == HARDWARE_TOML


async def test_cancel_marks_the_job_cancelling_once():
    """A cancel is announced right away (UI: "Cancelling…", no second cancel), the job stops at a safe point."""
    import asyncio

    from syncmusic.infra.events import EventBus
    from syncmusic.infra.jobs import JobRunner, JobStatus

    bus = EventBus()
    q = bus.subscribe()
    runner = JobRunner(bus)
    go = asyncio.Event()

    async def job(ctx):
        await go.wait()
        ctx.check_cancelled()

    info = runner.submit("sync", "sync x", job, device_id="d")
    await asyncio.sleep(0.05)
    assert runner.cancel(info.id) and runner.cancel(info.id)
    assert runner.get(info.id).cancelling
    go.set()
    await runner.wait(info.id)
    assert runner.get(info.id).status is JobStatus.CANCELLED
    marks = []
    while not q.empty():
        ev = q.get_nowait()
        if ev.type == "job.progress" and ev.data.get("cancelling"):
            marks.append(ev)
    assert len(marks) == 1  # announced once


async def test_cancel_after_commit_leaves_the_job_succeeded():
    """an update that committed its write and is then yielded to a user action ends 'succeeded', not
    'cancelled' (the write happened); one cancelled before its commit still ends 'cancelled'."""
    runner = JobRunner(EventBus())
    for commit, expected in ((True, JobStatus.SUCCEEDED), (False, JobStatus.CANCELLED)):
        go = asyncio.Event()

        async def job(ctx, commit=commit, go=go):
            ctx.committed = commit
            await go.wait()

        info = runner.submit("update", "update x", job, device_id="d")
        await asyncio.sleep(0.05)
        assert runner.cancel(info.id)
        go.set()
        await runner.wait(info.id)
        assert runner.get(info.id).status is expected


async def test_job_finished_carries_the_outcome_of_its_report():
    """a sync job that succeeds with failed items finishes with outcome "partial" (the sidebar's state)."""
    bus = EventBus()
    q = bus.subscribe()
    runner = JobRunner(bus)

    async def partial(ctx):
        return {"status": "partial", "failures": [{"ref": "t1"}]}

    async def plain(ctx):
        return 3

    for fn, want in ((partial, "partial"), (plain, None)):
        info = runner.submit("sync", "sync x", fn, device_id="d")
        await runner.wait(info.id)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        done = [e for e in events if e.type == "job.finished"]
        assert done[-1].data["status"] == "succeeded" and done[-1].data["outcome"] == want


async def test_job_progress_carries_the_stored_log_line():
    """a live log shows the same timestamped line the finished job's log has; a progress without a message
    adds no line."""
    bus = EventBus()
    q = bus.subscribe()
    runner = JobRunner(bus)

    async def job(ctx):
        ctx.progress(0.1, "copying Скрябін – Група крові")
        ctx.progress(0.2)

    info = runner.submit("sync", "sync x", job, device_id="d")
    await runner.wait(info.id)
    lines = []
    while not q.empty():
        ev = q.get_nowait()
        if ev.type == "job.progress":
            lines.append(ev.data["log_line"])
    assert lines == [runner.get(info.id).log[0], None]
    assert lines[0].endswith(" copying Скрябін – Група крові") and lines[0][:4].isdigit()
