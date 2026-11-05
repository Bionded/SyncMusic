"""Settings → Maintenance: log level at runtime, diagnostics, factory reset."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import logging

from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from syncmusic.infra import diagnostics
from tests.unit.test_api_sync import _register, env, music  # noqa: F401


def test_temperature_and_throttling_from_vcgencmd(tmp_path):
    t = diagnostics.temperature(lambda *_: "temp=52.4'C")
    assert (t["detail"], t["state"]) == ("52 °C", "ok")
    assert diagnostics.temperature(lambda *_: "temp=81.0'C")["state"] == "warning"
    zone = tmp_path / "temp"
    zone.write_text("48500\n")
    assert diagnostics.temperature(lambda *_: None, zone)["detail"] == "48 °C"
    assert diagnostics.temperature(lambda *_: None, tmp_path / "none")["state"] == "unknown"
    assert diagnostics.throttling(lambda *_: "throttled=0x0")["state"] == "ok"
    bad = diagnostics.throttling(lambda *_: "throttled=0x50000")
    assert bad["state"] == "warning"
    assert bad["detail"] == (
        "under-voltage since boot, throttled since boot - check the power supply (0x50000)"
    )
    assert diagnostics.throttling(lambda *_: None)["state"] == "unknown"


async def test_log_level_applies_at_once_and_is_restored(env):
    app, c = env
    root = logging.getLogger()
    before = app._log_level
    r = await c.patch("/api/v1/settings", json={"log_level": "DEBUG"})
    assert r.status_code == 200 and root.level == logging.DEBUG
    await c.patch("/api/v1/settings", json={"log_level": None})
    assert root.level == before
    assert (await c.patch("/api/v1/settings", json={"log_level": "TRACE"})).status_code == 400


async def test_diagnostics(env, tmp_path):
    _app, c = env
    checks = (await c.get("/api/v1/diagnostics")).json()
    ids = [x["id"] for x in checks]
    # the doctor's install checks first, then the app's
    assert ids[:7] == ["versions", "ffmpeg", "space", "dirs", "privileges", "udev", "clock"]
    assert ids[-4:] == ["libraries", "usb", "temperature", "throttling"]
    by = {x["id"]: x for x in checks}
    assert by["ffmpeg"]["state"] in ("ok", "warning") and by["libraries"]["detail"] == "Music OK"
    assert {x["state"] for x in checks} <= {"ok", "warning", "error", "unknown"}
    assert all("fix" in x for x in checks) and by["libraries"]["fix"] == ""


async def test_factory_reset(config, music, tmp_path):
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
            await _register(c, tmp_path / "flash", name="Flash")
            await c.patch("/api/v1/settings", json={"cache_max_gb": 3})
            r = await c.post("/api/v1/settings/factory-reset", json={"confirm": "reset"})
            assert r.status_code >= 400 and "reset syncmusic" in r.json()["message"]
            old = facade.app
            r = await c.post("/api/v1/settings/factory-reset", json={"confirm": "reset syncmusic"})
            assert r.status_code == 200, r.text
            assert facade.app is not old and facade.app.started and not old.started
            assert (await c.get("/api/v1/libraries")).json() == []
            assert (await c.get("/api/v1/devices")).json()["registered"] == []
            assert (await c.get("/api/v1/settings")).json()["cache_max_gb"] == 20.0
            assert (tmp_path / "flash").is_dir()  # files on devices are not touched
    finally:
        await facade.app.stop()
