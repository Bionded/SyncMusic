"""Test connection checks the values in the editor, saved or not: `POST /libraries/{id}/check` with
`settings` merges them over the stored ones (a secret sent back as the mask keeps the stored value) and stores
nothing."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core.sync import SyncService
from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures


async def test_check_with_edited_settings(env, tmp_path, monkeypatch):
    app, c = env
    assert (await c.post("/api/v1/libraries/music/check")).json()["ok"] is True
    r = await c.post(
        "/api/v1/libraries/music/check", json={"settings": {"root": str(tmp_path / "nope")}}
    )
    assert r.status_code == 200 and r.json()["ok"] is False
    assert (await c.post("/api/v1/libraries/music/check", json={})).json()["ok"] is True
    stored = (await c.get("/api/v1/libraries/music")).json()["settings"]["root"]
    assert stored != str(tmp_path / "nope")  # nothing stored
    assert (await c.post("/api/v1/libraries/music/check")).json()["ok"] is True
    r = await c.post("/api/v1/libraries/music/check", json={"settings": {"root": 5}})
    assert r.status_code == 200 and r.json()["ok"] is False  # invalid values: a result, not a 500
    assert (await c.post("/api/v1/libraries/nope/check", json={"settings": {}})).status_code == 404

    seen = []

    async def fake_check(self, cfg):
        seen.append(cfg)
        from syncmusic.core.models import HealthStatus

        return HealthStatus(ok=False, message="unreachable")

    monkeypatch.setattr(SyncService, "check_unsaved_library", fake_check)
    nd = {
        "id": "nd",
        "type": "navidrome",
        "name": "ND",
        "settings": {"url": "http://127.0.0.1:1/", "username": "u", "password": "stored-pw"},
    }
    assert (await c.post("/api/v1/libraries", json=nd)).status_code == 201
    masked = {"url": "http://127.0.0.1:2/", "password": "********"}
    r = await c.post("/api/v1/libraries/nd/check", json={"settings": masked})
    assert r.json() == {"ok": False, "message": "unreachable", "details": {}}
    cfg = seen[-1]
    password = cfg.settings["password"]
    assert password == "stored-pw" and cfg.settings["url"] == "http://127.0.0.1:2/"
    got = (await c.get("/api/v1/libraries/nd")).json()["settings"]
    assert got["url"] == "http://127.0.0.1:1/"  # the stored library is unchanged
