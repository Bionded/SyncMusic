"""Planning progress: steps on the event bus, one plan per device, Cancel."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

import asyncio

from syncmusic.base.progress import on_progress
from syncmusic.core.models import LibraryConfig
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.fake_navidrome import FakeNavidrome, song
from tests.unit.test_api_sync import _register, env, music  # noqa: F401


def _events(app):
    q = app.ctx.bus.subscribe()

    def drain():
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return [e for e in out if e.type.startswith("plan.")]

    return drain


async def test_plan_reports_its_steps(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash")
    drain = _events(app)
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    events = drain()
    steps = [e.data["step"] for e in events if e.type == "plan.progress"]
    # each step in order (the first event is the start, the library step again with its fraction)
    assert [s for i, s in enumerate(steps) if not i or s != steps[i - 1]] == [
        "libraries",
        "device",
        "compare",
        "checks",
    ]
    lib = [e.data for e in events if e.data.get("step") == "libraries" and e.data.get("detail")]
    assert lib and lib[0]["detail"] == "Music" and lib[0]["device_id"] == "my-flash"
    assert events[-1].type == "plan.finished" and events[-1].data["plan_id"] == plan["id"]
    assert (await c.get("/api/v1/planning")).json() == []


async def test_second_request_joins_and_cancel_stops(env, tmp_path, monkeypatch):
    app, c = env
    await _register(c, tmp_path / "flash")
    real, calls, gate = app.sync.plan, [], asyncio.Event()

    async def slow_plan(device_id, **kw):
        calls.append(device_id)
        await gate.wait()
        return await real(device_id, **kw)

    monkeypatch.setattr(app.sync, "plan", slow_plan)
    first = asyncio.create_task(c.post("/api/v1/devices/my-flash/plan", json={}))
    second = asyncio.create_task(c.post("/api/v1/devices/my-flash/plan", json={}))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if calls:
            break
    running = (await c.get("/api/v1/planning")).json()
    assert [p["device_id"] for p in running] == ["my-flash"] and running[0]["step"] == "libraries"
    gate.set()
    a, b = (await first).json(), (await second).json()
    assert calls == ["my-flash"] and a["id"] == b["id"]  # one plan for both requests

    gate.clear()
    third = asyncio.create_task(c.post("/api/v1/devices/my-flash/plan", json={}))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(calls) == 2:
            break
    assert (await c.delete("/api/v1/devices/my-flash/plan")).json() == {"cancelled": True}
    r = await third
    assert r.status_code == 409 and r.json()["error"] == "cancelled"
    assert (await c.get("/api/v1/planning")).json() == []
    assert (await c.delete("/api/v1/devices/my-flash/plan")).json() == {"cancelled": False}


async def test_subsonic_snapshot_reports_pages():
    server = FakeNavidrome(native=False)
    for i in range(5):
        server.add(song(f"s{i}", f"Song {i}"))
    lib = SubsonicLibrary(
        LibraryConfig(
            id="nd",
            type="navidrome",
            name="ND",
            settings={
                "url": "http://nd.test",
                "username": "admin",
                "password": "pw",
                "page_size": 2,
            },
        ),
        None,
    )
    lib.transport = server
    seen: list[tuple[int, int | None]] = []
    with on_progress(lambda done, total: seen.append((done, total))):
        snap = await lib.snapshot()
    assert len(snap.tracks) == 5
    assert seen == [(2, 5), (4, 5), (5, 5)]
    await lib.close()
