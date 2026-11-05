"""the first sync needs keep / adopt / wipe, and the choice is persisted (device config + report)."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

from tests.unit.test_api_sync import _apply, _register, _removable_true, env, music  # noqa: F401


async def test_first_sync_choice_is_required_and_persisted(env, monkeypatch, tmp_path):
    app, c = env
    monkeypatch.setattr(type(app.sync), "_removable", _removable_true)
    dev = await _register(c, tmp_path / "flash")
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan")).json()
    assert plan["first_sync"]
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"confirm": True})
    assert r.status_code == 409 and "keep, adopt or wipe" in r.json()["message"]
    cfg = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]
    assert cfg["first_sync_choice"] is None

    await _apply(app, c, plan["id"], first_sync="adopt")
    info = (await c.get(f"/api/v1/devices/{dev['id']}")).json()
    assert info["config"]["first_sync_choice"] == "adopt"
    assert info["last_report"]["first_sync"] == "adopt"

    plan2 = (await c.post(f"/api/v1/devices/{dev['id']}/plan")).json()
    assert not plan2["first_sync"]
    await _apply(app, c, plan2["id"], confirm=True)
    info = (await c.get(f"/api/v1/devices/{dev['id']}")).json()
    assert info["last_report"]["first_sync"] is None  # not a first sync any more
    assert info["config"]["first_sync_choice"] == "adopt"  # the stored choice stays
