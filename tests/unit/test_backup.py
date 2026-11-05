"""Backup and restore of the application settings."""

# ruff: noqa: F811 - the music fixture from test_api_sync

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from tests.unit.test_api_sync import _register, music  # noqa: F401 - fixture


@pytest.fixture
async def client(config, music):
    facade = AppFacade(Application(config))
    await facade.app.start()
    api = create_api(facade, manage_lifecycle=False)
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            lib = {
                "id": "music",
                "type": "folder",
                "name": "Music",
                "settings": {"root": str(music)},
            }
            assert (await c.post("/api/v1/libraries", json=lib)).status_code == 201
            yield facade, c
    finally:
        await facade.app.stop()


async def test_round_trip(client, tmp_path):
    facade, c = client
    dev = await _register(c, tmp_path / "flash", name="Флешка")
    await c.patch(
        "/api/v1/settings", json={"cache_max_gb": 3, "webhook_url": "http://h/x?token=s3cret"}
    )
    r = await c.get("/api/v1/settings/backup")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["format"] == "syncmusic-backup" and data["secrets"] is False
    assert data["settings"]["webhook_url"] is None and "s3cret" not in r.text
    assert [d["name"] for d in data["devices"]] == ["Флешка"]
    assert any(t["name"] == "Флешка rules" for t in data["templates"])  # its own template
    assert not any(
        t["id"].startswith("default.") and not t.get("edited") for t in data["templates"]
    )

    # changed afterwards: a restore brings the backup's state back
    await c.patch(f"/api/v1/devices/{dev['id']}", json={"name": "Renamed"})
    await c.patch("/api/v1/settings", json={"cache_max_gb": 9})
    (tmp_path / "extra").mkdir()
    lib2 = {
        "id": "extra",
        "type": "folder",
        "name": "Extra",
        "settings": {"root": str(tmp_path / "extra")},
    }
    r = await c.post("/api/v1/libraries", json=lib2)
    assert r.status_code == 201, r.text
    preview = (await c.post("/api/v1/settings/backup/preview", json=data)).json()
    assert (preview["libraries"], preview["devices"], preview["secrets"]) == (1, 1, False)
    assert preview["removes"] == {  # added after the backup, gone with the restore
        "libraries": ["Extra"],
        "devices": [],
        "templates": [],
        "processor_templates": [],
    }
    old = facade.app
    r = await c.post("/api/v1/settings/backup/restore", json=data)
    assert r.status_code == 200, r.text
    assert facade.app is not old
    assert [x["id"] for x in (await c.get("/api/v1/libraries")).json()] == ["music"]
    devices = (await c.get("/api/v1/devices")).json()["registered"]
    assert [d["name"] for d in devices] == ["Флешка"]
    stored = (await c.get("/api/v1/settings")).json()
    assert (
        stored["cache_max_gb"] == 3 and stored["webhook_url"] == "********"
    )  # the current secret kept


async def test_secrets_only_when_asked_and_bad_files_refused(client):
    _facade, c = client
    await c.patch("/api/v1/settings", json={"webhook_url": "http://h/x?token=s3cret"})
    data = (await c.get("/api/v1/settings/backup", params={"secrets": "true"})).json()
    assert data["secrets"] is True and data["settings"]["webhook_url"].endswith("s3cret")
    r = await c.post("/api/v1/settings/backup/preview", json={"format": "other"})
    assert r.status_code == 400 and "not a SyncMusic backup" in r.json()["message"]
    bad = {**data, "settings": {**data["settings"], "cache_max_gb": -1}}
    r = await c.post("/api/v1/settings/backup/restore", json=bad)
    assert r.status_code == 400 and "settings are invalid" in r.json()["message"]
    odd = {**data, "devices": [{"id": "x", "type": "walkman", "name": "W"}]}
    preview = (await c.post("/api/v1/settings/backup/preview", json=odd)).json()
    assert preview["warnings"] == ["device 'W' has the unknown type 'walkman'"]
