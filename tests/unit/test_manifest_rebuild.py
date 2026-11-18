# SPDX-License-Identifier: GPL-3.0-or-later
"""Both manifest copies unreadable: the device is flagged, and 'rebuild from the files' moves them aside so the next
sync is a first sync that adopts the files already on the device instead of a wipe."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core import manifest as manifest_io
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401


async def _device(c, name="flash"):
    devices = (await c.get("/api/v1/devices")).json()["registered"]
    return next(d for d in devices if d["id"] == name)


async def test_rebuild_from_files_after_both_copies_are_damaged(env, tmp_path):
    app, c = env
    root = tmp_path / "flash"
    await _register(c, root, name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    synced = manifest_io.read(root)
    assert synced is not None and synced.items
    manifest_io.forget(root)
    for rel in (manifest_io.MANIFEST_PATH, manifest_io.BACKUP_PATH):
        (root / rel).write_bytes(b"\x00\x00garbage\n")

    # neither copy parses: the plan says so and offers the choices
    r = await c.post("/api/v1/devices/flash/plan", json={})
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "manifest_unreadable"
    assert r.json()["choices"] == ["rebuild", "wipe"]
    assert (await _device(c))["manifest_damaged"]

    r = await c.post("/api/v1/devices/flash/manifest/rebuild")
    assert r.status_code == 200, r.text
    assert sorted(r.json()["moved"]) == sorted(
        [str(manifest_io.MANIFEST_PATH), str(manifest_io.BACKUP_PATH)]
    )
    assert (root / ".syncmusic" / "manifest.jsonl.damaged").is_file()
    assert (await _device(c))["manifest_damaged"] is None

    # the next plan is a first sync; adopting takes the files over without copying them again
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    assert plan["first_sync"], plan
    await _apply(app, c, plan["id"], first_sync="adopt")
    rebuilt = manifest_io.read(root)
    assert rebuilt is not None
    assert set(rebuilt.items) == set(synced.items)


async def test_rebuild_refuses_a_readable_manifest(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    r = await c.post("/api/v1/devices/flash/manifest/rebuild")
    assert r.status_code == 400, r.text
    assert manifest_io.read(tmp_path / "flash") is not None


async def test_space_and_home_still_answer_with_damaged_records(env, tmp_path):
    app, c = env
    root = tmp_path / "flash"
    await _register(c, root, name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    manifest_io.forget(root)
    for rel in (manifest_io.MANIFEST_PATH, manifest_io.BACKUP_PATH):
        (root / rel).write_bytes(b"\x00\x00garbage\n")

    # the disk space needs no manifest: the capacity card keeps working, only the split is gone
    r = await c.get("/api/v1/devices/flash/space")
    assert r.status_code == 200, r.text
    space = r.json()
    assert space["manifest_damaged"] is True
    assert space["capacity"] and space["used"] >= 0
    assert space["by_kind"]["music"] == 0
    r = await c.get("/api/v1/devices/flash/home")
    assert r.status_code == 200, r.text
    assert r.json()["manifest_damaged"] is True and r.json()["home"] is None
    # the device is still flagged, so its page offers the rebuild
    assert (await _device(c))["manifest_damaged"]
