"""Storage health: write-speed samples, I/O errors, the warning rule and its notification."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from syncmusic.core import health
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
MB = 1_000_000


def _w(days_ago: float, mb: int, seconds: float, kind: str = "write") -> dict:
    at = (NOW - timedelta(days=days_ago)).isoformat()
    return {"at": at, "kind": kind, "bytes": mb * MB, "seconds": seconds, "detail": ""}


def test_speeds_and_the_warning_rule():
    ok = [_w(9, 100, 10), _w(7, 100, 10), _w(5, 100, 12.5), _w(1, 100, 11)]
    s = health.summary(ok, NOW)
    assert [x["mb_s"] for x in s["speeds"]] == [10.0, 10.0, 8.0, 9.09]
    assert s["usual_mb_s"] == 10.0 and s["warning"] is None
    slow = [*ok, _w(0.5, 100, 40)]
    assert "wrote at 2.5 MB/s, usually 9.54 MB/s" in health.summary(slow, NOW)["warning"]
    small = [*ok, _w(0.5, 1, 10)]  # too small to judge
    assert health.summary(small, NOW)["warning"] is None
    err = [*ok, {**_w(2, 0, 0, "io_error"), "detail": "EIO"}]
    s = health.summary(err, NOW)
    assert s["io_errors"] == 1 and s["warning"].startswith("1 I/O error in the last 30 days")
    old = [*ok, _w(40, 0, 0, "io_error")]
    assert health.summary(old, NOW)["io_errors"] == 0


async def test_a_full_sync_records_a_sample_and_a_storage_failure_warns(env, tmp_path, monkeypatch):
    app, c = env
    monkeypatch.setattr(health, "MIN_SAMPLE_BYTES", 1)
    dev = await _register(c, tmp_path / "flash", name="Flash")
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    h = (await c.get(f"/api/v1/devices/{dev['id']}/health")).json()
    assert len(h["speeds"]) == 1 and h["speeds"][0]["mb_s"] > 0 and h["warning"] is None

    q = app.bus.subscribe()
    cfg = await app.sync.config(dev["id"])
    await app.sync._record_health(
        cfg, error="[Errno 5] Input/output error while writing x.mp3", job_id="job-1"
    )
    h = (await c.get(f"/api/v1/devices/{dev['id']}/health")).json()
    assert h["io_errors"] == 1 and h["warning"] == "1 I/O error in the last 30 days"
    assert h["errors"][0]["job_id"] == "job-1"  # the health card links the job
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [e.data["warning"] for e in events if e.type == "device.health_warning"] == [
        h["warning"]
    ]
    await app.sync._record_health(cfg, error="again")  # still warning: not announced again
    await asyncio.sleep(0)
    assert all(q.get_nowait().type != "device.health_warning" for _ in range(q.qsize()))


def test_an_acknowledgement_hides_the_warning_until_a_newer_problem():
    errs = [{**_w(d, 0, 0, "io_error"), "detail": f"EIO {d}"} for d in (5, 4, 3)]
    acked = [*errs, _w(2, 0, 0, "ack")]
    s = health.summary(acked, NOW)
    assert s["warning"] is None and s["io_errors"] == 3 and s["acknowledged_at"] == acked[-1]["at"]
    assert [e["detail"] for e in s["errors"]] == ["EIO 3", "EIO 4", "EIO 5"]  # newest first
    again = [*acked, {**_w(1, 0, 0, "io_error"), "detail": "EIO 1"}]
    assert health.summary(again, NOW)["warning"].startswith("4 I/O errors")
    ok = [_w(9, 100, 10), _w(7, 100, 10), _w(5, 100, 10), _w(3, 100, 40)]
    assert health.summary([*ok, _w(2, 0, 0, "ack")], NOW)["warning"] is None
    assert health.summary([*ok, _w(2, 0, 0, "ack"), _w(1, 100, 40)], NOW)["warning"]


async def test_acknowledge_undo_and_clear_the_error_history(env, tmp_path):
    app, c = env
    dev = await _register(c, tmp_path / "flash", name="Flash")
    cfg = await app.sync.config(dev["id"])
    await app.store.add_health(cfg.id, "write", 100 * MB, 10)
    for n in range(3):
        await app.sync._record_health(cfg, error=f"[Errno 5] Input/output error {n}")
    url = f"/api/v1/devices/{dev['id']}/health"
    assert (await c.get(url)).json()["warning"] == "3 I/O errors in the last 30 days"
    h = (await c.post(f"{url}/ack")).json()
    assert h["warning"] is None and h["acknowledged_at"]
    rows = (await c.get("/api/v1/devices")).json()["registered"]
    assert [d["health_warning"] for d in rows] == [None]  # the dashboard row is gone
    assert (await c.post(f"{url}/ack?undo=true")).json()["warning"]  # Undo
    await c.post(f"{url}/ack")
    await asyncio.sleep(0.001)
    await app.sync._record_health(cfg, error="[Errno 5] Input/output error 4")
    assert (await c.get(url)).json()["warning"] == "4 I/O errors in the last 30 days"
    h = (await c.delete(f"{url}/errors")).json()
    assert h["io_errors"] == 0 and h["errors"] == [] and h["warning"] is None
    assert len(h["speeds"]) == 1  # write speeds stay
    assert (await c.post("/api/v1/devices/nope/health/ack")).status_code == 404
