"""Device settings' Wipe for a Rockbox player: a job that deletes every file except
`/.rockbox` after the typed name; other types are refused (they format)."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

from tests.contracts.harness import hiby_tree, rockbox_tree
from tests.unit.test_api_sync import _apply, env, music  # noqa: F401


async def test_wipe_a_rockbox_player(env, tmp_path):
    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    (mount / "Other").mkdir()
    (mount / "Other" / "notes.txt").write_text("x")
    body = {"name": "Mini", "type": "rockbox", "mount_path": str(mount), "library_ids": ["music"]}
    dev = (await c.post("/api/v1/devices/register", json=body)).json()["id"]
    plan = (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    assert list(mount.rglob("*.mp3"))

    r = await c.post(f"/api/v1/devices/{dev}/wipe", json={"confirm": "mini"})
    assert r.status_code >= 400 and "confirm" in r.json()["message"]
    r = await c.post(f"/api/v1/devices/{dev}/wipe", json={"confirm": "Mini"})
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded" and job.kind == "wipe", job.error
    left = sorted(p.name for p in mount.iterdir())
    assert ".rockbox" in left and "Other" not in left and not list(mount.rglob("*.mp3"))
    # the next plan is a first sync again
    assert (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()["first_sync"] is True


async def test_sync_and_wipe_a_hiby_player(env, tmp_path):
    """a virtual HiBy R1 syncs (playlists and our state too) and Wipe keeps its /.local databases."""
    app, c = env
    mount = hiby_tree(tmp_path / "r1")
    body = {"name": "R1", "type": "hiby", "mount_path": str(mount), "library_ids": ["music"]}
    dev = (await c.post("/api/v1/devices/register", json=body)).json()["id"]
    plan = (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    assert list((mount / "Music").rglob("*.mp3"))

    r = await c.post(f"/api/v1/devices/{dev}/wipe", json={"confirm": "R1"})
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded" and job.result["type"] == "hiby", job.error
    assert (mount / ".local" / "library.db").is_file() and not list(mount.rglob("*.mp3"))


async def test_wipe_is_for_rockbox_only(env, tmp_path):
    _app, c = env
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "type": "flashdrive", "mount_path": str(tmp_path / "stick")}
    dev = (await c.post("/api/v1/devices/register", json=body)).json()["id"]
    r = await c.post(f"/api/v1/devices/{dev}/wipe", json={"confirm": "Stick"})
    assert r.status_code == 400 and "format it instead" in r.json()["message"]


async def test_a_wiped_device_is_not_synced_until_the_next_full_sync(env, tmp_path):
    """after a wipe the device and the devices list name the wipe instead of the old sync; a full sync
    clears it; the mark cannot be set by hand."""
    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    body = {"name": "Mini", "type": "rockbox", "mount_path": str(mount), "library_ids": ["music"]}
    dev = (await c.post("/api/v1/devices/register", json=body)).json()["id"]
    plan = (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    info = (await c.get(f"/api/v1/devices/{dev}")).json()
    assert info["erased"] is None and info["last_report"]["status"] == "succeeded"

    r = await c.post(f"/api/v1/devices/{dev}/wipe", json={"confirm": "Mini"})
    await app.jobs.wait(r.json()["id"])
    info = (await c.get(f"/api/v1/devices/{dev}")).json()
    assert info["erased"]["how"] == "wiped" and info["erased"]["removed"] > 0
    row = next(d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == dev)
    assert row["erased"] == info["erased"]
    r = await c.patch(f"/api/v1/devices/{dev}", json={"erased": None})
    assert r.status_code == 400 and "erased" in r.json()["message"]

    plan = (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    assert (await c.get(f"/api/v1/devices/{dev}")).json()["erased"] is None
