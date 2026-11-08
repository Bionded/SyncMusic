# SPDX-License-Identifier: GPL-3.0-or-later
"""a registered device whose type this version does not know: its page loads and it can be removed."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

from tests.unit.test_api_sync import _register, env, music  # noqa: F401


async def test_unknown_device_type_page_loads_and_delete_works(env, tmp_path):
    app, c = env
    dev = await _register(c, tmp_path / "flash")
    cfg = await app.store.device(dev["id"])
    await app.store.put_device(cfg.model_copy(update={"type": "usb_drive"}))

    r = await c.get(f"/api/v1/devices/{dev['id']}")
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["unknown_type"] is True
    assert info["config"]["type"] == "usb_drive"
    assert (await c.get("/api/v1/devices")).status_code == 200

    assert (await c.delete(f"/api/v1/devices/{dev['id']}")).status_code in (200, 204)
    assert await app.store.device(dev["id"]) is None


async def test_known_device_type_is_flagged_known(env, tmp_path):
    _app, c = env
    dev = await _register(c, tmp_path / "flash")
    assert (await c.get(f"/api/v1/devices/{dev['id']}")).json()["unknown_type"] is False
