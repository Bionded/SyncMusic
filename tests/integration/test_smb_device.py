"""An SMB share as a device in the application: registered with its settings (the login is
checked, the folder created), listed as a connected network device, synced, its contents and space read; Format and
Eject are refused. Runs against the compose samba's writable [devices] share."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

import uuid

import pytest

from tests.integration import services as compose
from tests.unit.test_api_sync import _apply, env, music  # noqa: F401

pytestmark = pytest.mark.integration


def _body(services, name: str, **settings):
    folder = f"app-{name.replace(' ', '-')}-{uuid.uuid4().hex[:8]}"  # a fresh folder per run
    return compose.SMB_DEVICES / folder, {
        "name": name,
        "type": "smb",
        "library_ids": ["music"],
        "settings": {
            "host": services["SMB_HOST"],
            "port": int(services["SMB_PORT"]),
            "remote_path": f"/devices/{folder}",
            **settings,
        },
    }


async def test_register_sync_and_read_an_smb_device(services, env):
    app, c = env
    folder, body = _body(
        services, "Office NAS", username=services["SMB_USER"], password=services["SMB_PASSWORD"]
    )
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 201, r.text
    dev = r.json()
    assert dev["settings"]["password"] == "********"
    assert folder.is_dir()  # the registration check created it
    listed = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == dev["id"]
    )
    assert listed["connected"] and listed["kind"] == "network" and not listed["can_format"]
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["counts"]["add"] == 3, plan
    await _apply(app, c, plan["id"], first_sync="keep")
    assert (folder / ".syncmusic" / "manifest.jsonl").is_file()
    assert len(list(folder.rglob("*.mp3"))) == 3
    assert not list(folder.rglob(".sdtmp-*"))
    contents = (await c.get(f"/api/v1/devices/{dev['id']}/contents")).json()
    assert len(contents) == 3 and not any(r["foreign"] for r in contents)
    space = (await c.get(f"/api/v1/devices/{dev['id']}/space")).json()
    assert not space.get("unknown") and space["capacity"] > 0 and space["free"] > 0, space
    assert (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()["counts"]["add"] == 0
    r = await c.post(f"/api/v1/devices/{dev['id']}/eject")
    assert r.status_code == 400 and "nothing to eject" in r.json()["message"]
    r = await c.post(f"/api/v1/devices/{dev['id']}/format", json={"confirm": "Office NAS"})
    assert r.status_code == 400 and "nothing to format" in r.json()["message"]


async def test_wrong_password_and_guest_on_a_private_share_are_refused(services, env):
    app, c = env
    _, body = _body(services, "Wrong", username=services["SMB_USER"], password="nope")
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code >= 400, r.text
    assert "nope" not in r.text
    _, body = _body(services, "Guest")  # [devices] has no guest access
    assert (await c.post("/api/v1/devices/register", json=body)).status_code >= 400
    assert (await c.get("/api/v1/devices")).json()["registered"] == []
