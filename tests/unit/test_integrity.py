"""Device integrity check on a folder device: missing, broken, foreign files, orphans; the fix
deletes broken files so the next sync writes them again; an I/O error stops the check."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import errno
from pathlib import Path

from syncmusic.core import integrity
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401


async def _synced(app, c, tmp_path):
    dev = await _register(c, tmp_path / "flash", name="Flash")
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    manifest = await (await app.sync.open_device(dev["id"])).read_manifest()
    return dev, manifest


async def test_check_finds_and_fix_repairs(env, tmp_path):
    app, c = env
    dev, manifest = await _synced(app, c, tmp_path)
    mount = tmp_path / "flash"
    entries = sorted(manifest.items.values(), key=lambda e: e.path)
    (mount / entries[0].path).unlink()  # missing
    (mount / entries[1].path).write_bytes(b"short")  # broken: truncated
    (mount / "Other").mkdir()
    (mount / "Other" / "x.mp3").write_bytes(b"x" * 100)  # foreign

    r = await c.post(f"/api/v1/devices/{dev['id']}/integrity")
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    last = (await c.get(f"/api/v1/devices/{dev['id']}/integrity")).json()
    rep = last["report"]
    assert [m["path"] for m in rep["missing"]] == [entries[0].path]
    assert [b["path"] for b in rep["broken"]] == [entries[1].path]
    assert "size 5 bytes" in rep["broken"][0]["reason"]
    assert [f["path"] for f in rep["foreign"]] == ["Other/x.mp3"] and rep["wasted_bytes"] == 100
    assert rep["checked"] == len(entries) - 1
    assert rep["summary"].startswith(
        f"{len(entries) - 1} files checked, 1 missing, 1 broken, 1 not from SyncMusic"
    )

    r = await c.post(f"/api/v1/devices/{dev['id']}/integrity/fix")
    assert r.status_code == 202, r.text
    fix = (await app.jobs.wait(r.json()["id"])).result
    assert fix["removed"] == 1 and fix["paths"] == [entries[1].path]
    assert not (mount / entries[1].path).exists() and (mount / "Other" / "x.mp3").exists()
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["counts"].get("add") == 2  # the next sync writes both again


async def test_deep_check_decodes(env, tmp_path):
    app, c = env
    dev, manifest = await _synced(app, c, tmp_path)
    entry = sorted(manifest.items.values(), key=lambda e: e.path)[0]
    path = tmp_path / "flash" / entry.path
    data = bytearray(path.read_bytes())
    data[len(data) // 2 :] = b"\xff" * (len(data) - len(data) // 2)  # same size, garbage audio
    path.write_bytes(bytes(data))
    r = await c.post(f"/api/v1/devices/{dev['id']}/integrity", params={"deep": "true"})
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded" and job.result["deep"] is True
    assert [b["path"] for b in job.result["broken"]] == [entry.path]


async def test_fix_updates_the_report_and_the_update_sync_passes(env, tmp_path):
    """after Delete broken files the card lists them as missing (no second delete), and an update sync
    does not fail its read-back on them - they wait for the full sync. no update.failed for it."""
    app, c = env
    dev, manifest = await _synced(app, c, tmp_path)
    mount = tmp_path / "flash"
    entries = sorted(manifest.items.values(), key=lambda e: e.path)
    for e in entries[:2]:
        (mount / e.path).write_bytes(b"short")
    await app.jobs.wait((await c.post(f"/api/v1/devices/{dev['id']}/integrity")).json()["id"])
    r = await c.post(f"/api/v1/devices/{dev['id']}/integrity/fix")
    assert (await app.jobs.wait(r.json()["id"])).result["removed"] == 2

    rep = (await c.get(f"/api/v1/devices/{dev['id']}/integrity")).json()["report"]
    assert rep["broken"] == [] and rep["fixed"] == 2
    assert sorted(m["path"] for m in rep["missing"]) == [e.path for e in entries[:2]]
    assert "2 missing" in rep["summary"] and "broken" not in rep["summary"]
    assert (await c.post(f"/api/v1/devices/{dev['id']}/integrity/fix")).status_code >= 400

    q = app.bus.subscribe()
    job = await app.jobs.wait((await c.post(f"/api/v1/devices/{dev['id']}/update")).json()["id"])
    assert job.status == "succeeded", job.error
    assert job.result["status"] == "succeeded", job.result["message"]
    assert any(
        "2 items are missing" in w and "writes them again" in w for w in job.result["warnings"]
    )
    events = [q.get_nowait().type for _ in range(q.qsize())]
    assert "update.finished" in events and "update.failed" not in events

    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"])
    assert all((mount / e.path).stat().st_size == e.size for e in entries[:2])


async def test_update_whose_read_back_fails_announces_it(env, tmp_path, monkeypatch):
    """a failed update report (its verification) publishes update.failed once."""
    app, c = env
    dev, _ = await _synced(app, c, tmp_path)

    async def fails(cfg, device, report, plan=None, waiting=None):
        report.status, report.message = "failed", "verification failed: 1 of 2 items are not on it"

    monkeypatch.setattr(app.sync, "_verify_written", fails)
    q = app.bus.subscribe()
    job = await app.jobs.wait((await c.post(f"/api/v1/devices/{dev['id']}/update")).json()["id"])
    assert job.result["status"] == "failed"
    failed = [e for e in (q.get_nowait() for _ in range(q.qsize())) if e.type == "update.failed"]
    assert len(failed) == 1 and "verification failed" in failed[0].data["error"]


def test_io_error_stops(tmp_path, monkeypatch):
    from syncmusic.core.models import (
        DeviceContents,
        ItemRef,
        Manifest,
        ManifestEntry,
        ManifestHeader,
    )

    for name in ("a.mp3", "b.mp3"):
        (tmp_path / name).write_bytes(b"x")
    m = Manifest(
        header=ManifestHeader(device_id="d"),
        items={
            n: ManifestEntry(ref=ItemRef(library_id="l", item_id=n), device_ref=n, path=n, size=1)
            for n in ("a.mp3", "b.mp3")
        },
    )

    def failing(path: Path) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(integrity, "_read_all", failing)
    rep = integrity.check(tmp_path, m, DeviceContents())
    assert rep["io_error"] == "a.mp3: Input/output error" and rep["checked"] == 1
    assert integrity.summary(rep).startswith("stopped: the device's storage failed")
