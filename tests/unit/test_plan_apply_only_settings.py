"""Apply-only sync settings keep a pending plan: 'Sync without the cache' / 'Turn on gentle writing'
from the plan page must not outdate the plan the user is looking at."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

from tests.unit.test_api_sync import ALL, _register, env, music  # noqa: F401


async def _plan(c):
    r = await c.post("/api/v1/devices/flash/plan")
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_no_cache_and_gentle_keep_the_plan(env, tmp_path):
    app, c = env
    cfg = await _register(c, tmp_path / "flash", name="Flash")
    plan_id = await _plan(c)
    for change in (
        {"no_cache": True},
        {"gentle_write": True, "gentle_pause_s": 0},
        {"verify": "off"},
    ):
        r = await c.patch("/api/v1/devices/flash", json={"sync": {**cfg["sync"], **change}})
        assert r.status_code == 200, r.text
        cfg = r.json()
        assert (await c.get(f"/api/v1/plans/{plan_id}")).status_code == 200, change
    r = await c.post(f"/api/v1/plans/{plan_id}/apply", json={"first_sync": "keep"})
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    assert (await app.store.device("flash")).sync.no_cache  # applied with the current config


async def test_plan_changing_settings_still_outdate_it(env, tmp_path):
    app, c = env
    cfg = await _register(c, tmp_path / "flash", name="Flash")
    plan_id = await _plan(c)
    r = await c.patch(
        "/api/v1/devices/flash", json={"sync": {**cfg["sync"], "safety_margin_pct": 5}}
    )
    assert r.status_code == 200, r.text
    assert (await c.get(f"/api/v1/plans/{plan_id}")).status_code == 404
    plan_id = await _plan(c)
    r = await c.patch("/api/v1/devices/flash", json={"processors": []})
    assert r.status_code == 200, r.text
    assert (await c.get(f"/api/v1/plans/{plan_id}")).status_code == 404
