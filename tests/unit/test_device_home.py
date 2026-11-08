# SPDX-License-Identifier: GPL-3.0-or-later
"""The home instance of a device: set at the first sync, home-only actions (wipe, start fresh) refused for
another instance until it is made home; Register continues a device synced before."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

from pathlib import Path

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import InstanceInfo
from tests.contracts.harness import rockbox_tree
from tests.unit.test_api_sync import _apply, env, music  # noqa: F401

OTHER = InstanceInfo(id="other-instance", name="Kitchen box")


async def _synced(app, c, mount: Path) -> str:
    body = {"name": "Mini", "type": "rockbox", "mount_path": str(mount), "library_ids": ["music"]}
    dev = (await c.post("/api/v1/devices/register", json=body)).json()["id"]
    plan = (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    return dev


def _hand_to_other(mount: Path) -> None:
    m = manifest_io.read(mount)
    assert m is not None
    m.header = m.header.model_copy(update={"home": OTHER})
    manifest_io.write(mount, m)


async def test_first_sync_makes_this_instance_home(env, tmp_path):
    app, c = env
    dev = await _synced(app, c, rockbox_tree(tmp_path / "mini"))
    info = (await c.get(f"/api/v1/devices/{dev}/home")).json()
    assert info["is_home"] is True
    assert info["home"]["id"] == info["instance_id"] == app.instance_id
    assert info["home"]["since"]


async def test_another_home_blocks_wipe_until_rebind(env, tmp_path):
    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    dev = await _synced(app, c, mount)
    _hand_to_other(mount)
    info = (await c.get(f"/api/v1/devices/{dev}/home")).json()
    assert info["is_home"] is False and info["home"]["name"] == "Kitchen box"

    r = await c.post(f"/api/v1/devices/{dev}/wipe", json={"confirm": "Mini"})
    assert r.status_code == 409 and "Kitchen box" in r.json()["message"], r.text
    # a sync by this instance keeps the other home
    plan = (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()
    await _apply(app, c, plan["id"])
    assert manifest_io.read(mount).header.home.id == OTHER.id

    r = await c.post(f"/api/v1/devices/{dev}/make-home")
    assert r.status_code == 200 and r.json()["is_home"] is True, r.text
    assert manifest_io.read(mount).header.home.id == app.instance_id
    r = await c.post(f"/api/v1/devices/{dev}/wipe", json={"confirm": "Mini"})
    assert r.status_code == 202, r.text
    await app.jobs.wait(r.json()["id"])


async def test_register_continue_keeps_the_device_identity(env, tmp_path):
    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    dev = await _synced(app, c, mount)
    uid = manifest_io.read(mount).header.device_uid
    _hand_to_other(mount)
    assert (await c.delete(f"/api/v1/devices/{dev}")).status_code == 204

    body = {"name": "Mini", "type": "rockbox", "mount_path": str(mount), "library_ids": ["music"]}
    r = await c.post("/api/v1/devices/register", json={**body, "continuity": "fresh"})
    assert r.status_code == 409 and "Kitchen box" in r.json()["message"], r.text

    r = await c.post("/api/v1/devices/register", json={**body, "continuity": "continue"})
    assert r.status_code in (200, 201), r.text
    cfg = r.json()
    assert cfg["on_connect"] == "manual"  # auto-sync on connect stays the home's
    plan = (await c.post(f"/api/v1/devices/{cfg['id']}/plan", json={})).json()
    assert plan["first_sync"] is False and not plan["needs_confirmation"], plan
    assert manifest_io.read(mount).header.device_uid == uid


async def test_register_continue_needs_a_manifest(env, tmp_path):
    _app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    body = {"name": "Mini", "type": "rockbox", "mount_path": str(mount), "continuity": "continue"}
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 400 and "nothing to continue" in r.json()["message"]


async def test_register_continues_by_default_and_peek_says_whose(env, tmp_path):
    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    peek = (await c.get("/api/v1/devices/register/peek", params={"mount_path": str(mount)})).json()
    assert peek["synced_before"] is False
    dev = await _synced(app, c, mount)
    uid = manifest_io.read(mount).header.device_uid
    _hand_to_other(mount)
    assert (await c.delete(f"/api/v1/devices/{dev}")).status_code == 204
    peek = (await c.get("/api/v1/devices/register/peek", params={"mount_path": str(mount)})).json()
    assert peek["synced_before"] is True and peek["is_home"] is False
    assert peek["home"]["name"] == "Kitchen box"

    body = {"name": "Mini", "type": "rockbox", "mount_path": str(mount), "library_ids": ["music"]}
    cfg = (await c.post("/api/v1/devices/register", json=body)).json()
    plan = (await c.post(f"/api/v1/devices/{cfg['id']}/plan", json={})).json()
    assert plan["first_sync"] is False and not plan["needs_confirmation"]
    assert manifest_io.read(mount).header.device_uid == uid


async def test_register_keep_files_is_a_new_identity(env, tmp_path):
    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    dev = await _synced(app, c, mount)
    assert (await c.delete(f"/api/v1/devices/{dev}")).status_code == 204
    body = {
        "name": "Mini",
        "type": "rockbox",
        "mount_path": str(mount),
        "continuity": "keep",
        "library_ids": ["music"],
    }
    cfg = (await c.post("/api/v1/devices/register", json=body)).json()
    assert cfg["first_sync_choice"] == "keep"
    plan = (await c.post(f"/api/v1/devices/{cfg['id']}/plan", json={})).json()
    assert plan.get("needs_confirmation"), plan  # another registration's files: confirmed or wiped


async def test_player_settings_are_left_to_the_home(env, tmp_path):
    """the clock is set only by the home: the mounted manifest naming another home says no."""
    from syncmusic.infra.blockdev.mount import Mounted

    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    dev = await _synced(app, c, mount)
    app.sync._mounts[dev] = Mounted(mount, "/dev/sdz1", "vfat", owned=False)
    try:
        assert await app.sync._home_of_mounted(dev) is True
        _hand_to_other(mount)
        assert await app.sync._home_of_mounted(dev) is False
    finally:
        app.sync._mounts.pop(dev, None)


async def test_rockbox_play_log_is_left_to_the_home(env, tmp_path):
    """Another home: the play log switch is refused and a sync does not touch the player's config.cfg."""
    from syncmusic.infra.blockdev.mount import Mounted

    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    dev = await _synced(app, c, mount)
    config = mount / ".rockbox" / "config.cfg"
    before = config.read_bytes() if config.exists() else None
    _hand_to_other(mount)
    app.sync._mounts[dev] = Mounted(mount, "/dev/sdz1", "vfat", owned=False)
    try:
        r = await c.post(f"/api/v1/devices/{dev}/play-log", json={"on": True})
        assert r.status_code == 409 and r.json()["error"] == "not_home", r.text
    finally:
        app.sync._mounts.pop(dev, None)
    cfg = (await c.get(f"/api/v1/devices/{dev}")).json()["config"]
    r = await c.patch(
        f"/api/v1/devices/{dev}", json={"settings": {**cfg["settings"], "enable_play_log": True}}
    )
    assert r.status_code == 200, r.text
    plan = (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()
    await _apply(app, c, plan["id"])
    after = config.read_bytes() if config.exists() else None
    assert after == before
