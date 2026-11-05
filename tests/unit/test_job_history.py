"""Finished jobs survive a restart: stored in the jobs table, restored at start, trimmed to the limit."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from syncmusic.infra import jobs as jobs_mod


async def _run_jobs(app: Application) -> list[str]:
    async def ok(ctx):
        ctx.progress(0.5, "half way")
        return {"tracks": 3}

    async def boom(ctx):
        raise RuntimeError("broken")

    ids = [app.jobs.submit("refresh", "refresh music", ok).id]
    ids.append(app.jobs.submit("sync", "sync stick", boom, device_id="stick").id)
    for i in ids:
        await app.jobs.wait(i)
    return ids


async def test_finished_jobs_and_their_logs_survive_a_restart(config):
    app = Application(config)
    await app.start()
    ok_id, failed_id = await _run_jobs(app)
    await app.stop()

    app2 = Application(config)
    await app2.start()
    try:
        api = create_api(AppFacade(app2), manage_lifecycle=False)
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            listed = (await c.get("/api/v1/jobs")).json()
            assert [j["id"] for j in listed] == [failed_id, ok_id]  # newest first
            job = (await c.get(f"/api/v1/jobs/{ok_id}")).json()
            assert job["status"] == "succeeded" and job["result"] == {"tracks": 3}
            assert job["log"][0].endswith(" half way")
            failed = (await c.get(f"/api/v1/jobs/{failed_id}")).json()
            assert failed["status"] == "failed" and failed["error"] == "RuntimeError: broken"
            assert failed["device_id"] == "stick"
            assert (await c.post(f"/api/v1/jobs/{ok_id}/cancel")).json() == {"cancelled": False}
    finally:
        await app2.stop()


async def test_history_is_trimmed_in_the_database(config, monkeypatch):
    monkeypatch.setattr(jobs_mod, "HISTORY_LIMIT", 3)
    monkeypatch.setattr("syncmusic.infra.store.HISTORY_LIMIT", 3)
    app = Application(config)
    await app.start()
    try:
        for _ in range(3):
            await _run_jobs(app)
        rows = await app.db.fetchall("SELECT id FROM jobs")
        assert len(rows) == 3
        assert len(await app.store.recent_jobs()) == 3
    finally:
        await app.stop()


async def test_unserialisable_result_is_dropped_not_the_job(config):
    app = Application(config)
    await app.start()
    try:

        async def odd(ctx):
            return object()

        job_id = app.jobs.submit("plan", "plan x", odd).id
        await app.jobs.wait(job_id)
        (stored,) = await app.store.recent_jobs()
        assert stored.id == job_id and stored.status == "succeeded" and stored.result is None
    finally:
        await app.stop()


async def test_job_cut_off_by_a_crash_is_shown_as_interrupted_with_its_log(config, monkeypatch):
    """A running job is stored at its start and while it runs; after a crash (no graceful stop, the next app
    start finds it still `running`) it is listed as failed/interrupted with the log so far."""
    import asyncio

    monkeypatch.setattr(jobs_mod, "PERSIST_INTERVAL", 0.05)
    app = Application(config)
    await app.start()
    release = asyncio.Event()

    async def long_sync(ctx):
        ctx.progress(0.25, "copied 2 of 8")
        await release.wait()

    job_id = app.jobs.submit("sync", "sync ipod", long_sync, device_id="ipod").id
    for _ in range(100):
        stored = {j.id: j for j in await app.store.recent_jobs()}
        if job_id in stored and stored[job_id].log:
            break
        await asyncio.sleep(0.02)
    assert stored[job_id].status == "running" and stored[job_id].progress == 0.25

    app2 = Application(config)  # the "next start" while the first process is gone
    await app2.start()
    try:
        job = app2.jobs.get(job_id)
        assert (
            job is not None and job.status == "failed" and job.error == jobs_mod.INTERRUPTED_ERROR
        )
        assert job.log[0].endswith(" copied 2 of 8") and job.device_id == "ipod"
        (again,) = [j for j in await app2.store.recent_jobs() if j.id == job_id]
        assert again.status == "failed"  # stored, so it stays failed after yet another restart
        assert app2.jobs.running_for_device("ipod") is None
    finally:
        await app2.stop()
        release.set()
        await app.stop()


async def test_running_job_is_stored_and_its_final_state_wins(config, monkeypatch):
    monkeypatch.setattr(jobs_mod, "PERSIST_INTERVAL", 0.01)
    app = Application(config)
    await app.start()
    try:

        async def chatty(ctx):
            import asyncio

            for i in range(5):
                ctx.progress(i / 5, f"step {i}")
                await asyncio.sleep(0.02)
            return "ok"

        job_id = app.jobs.submit("refresh", "refresh", chatty).id
        await app.jobs.wait(job_id)
        (stored,) = await app.store.recent_jobs()
        assert stored.id == job_id and stored.status == "succeeded" and stored.result == "ok"
        assert len(stored.log) == 5 and stored.error is None
    finally:
        await app.stop()
