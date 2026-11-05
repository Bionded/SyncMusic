# ruff: noqa: F811 - fixtures imported from test_api_sync

"""'Sync all connected': skips with a reason, waits for confirmations, a summary event."""

from __future__ import annotations

import asyncio
import shutil

from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401


async def _finished(q):
    for _ in range(600):
        while not q.empty():
            e = q.get_nowait()
            if e.type == "sync_all.finished":
                return e.data
        await asyncio.sleep(0.05)
    raise AssertionError("no sync_all.finished")


async def test_sync_all(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "a", name="A")
    await _register(c, tmp_path / "b", name="B")
    shutil.rmtree(tmp_path / "b")  # B is not connected
    q = app.bus.subscribe()

    r = await c.post("/api/v1/devices/sync-all", json={})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["devices"] == ["a"]
    assert body["skipped"] == [{"device_id": "b", "name": "B", "reason": "not connected"}]
    # A's first sync asks keep / wipe: it waits, like an automatic sync
    assert await _finished(q) == {"mode": "full", "done": 0, "waiting": 1, "failed": 0}

    plan = await app.sync.plan("a")
    await _apply(app, c, plan.plan.id, first_sync="keep")
    r = await c.post("/api/v1/devices/sync-all", json={"mode": "update"})
    assert r.json()["devices"] == ["a"]
    assert await _finished(q) == {"mode": "update", "done": 1, "waiting": 0, "failed": 0}
    r = await c.post("/api/v1/devices/sync-all")
    assert await _finished(q) == {"mode": "full", "done": 1, "waiting": 0, "failed": 0}
