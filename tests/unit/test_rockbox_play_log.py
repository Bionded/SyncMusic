"""Rockbox: the play log is switched on (or off) in /.rockbox/config.cfg when the device is written, per-device
setting `enable_play_log` (default on), and at once from Device settings."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core.models import DeviceConfig
from syncmusic.devices.Rockbox import rockbox_feedback as rf
from syncmusic.devices.Rockbox.rockbox import Rockbox, RockboxSettings
from tests.contracts.harness import rockbox_tree
from tests.unit.test_api_sync import ALL, _apply, _register, env, music  # noqa: F401
from tests.unit.test_rockbox_warnings import MINI_CFG


def test_with_play_log_on():
    out = rf.with_play_log_on(MINI_CFG)
    assert out == MINI_CFG + "play log: on\n"  # appended, everything else as it was
    assert rf.with_play_log_on(out) is None  # already on: no rewrite
    off = MINI_CFG.replace("volume: -23\n", "volume: -23\nPlay Log: off\n")
    assert rf.with_play_log_on(off) == MINI_CFG.replace(
        "volume: -23\n", "volume: -23\nPlay Log: on\n"
    )
    crlf = "volume: -23\r\nscrollbar: off"  # CRLF, no newline at the end
    assert rf.with_play_log_on(crlf) == "volume: -23\r\nscrollbar: off\r\nplay log: on\r\n"
    assert rf.with_play_log_on("") == "play log: on\n"
    commented = "# play log: off\n"
    assert rf.with_play_log_on(commented) == commented + "play log: on\n"


def test_with_play_log_on_off_on_keeps_every_other_line():
    """on → off → on rewrites only the `play log` lines (all of them: the last one wins in Rockbox)."""
    on = rf.with_play_log(MINI_CFG, True)
    off = rf.with_play_log(on, False)
    assert off == MINI_CFG + "play log: off\n"
    assert rf.with_play_log(off, False) is None  # already off: no rewrite
    assert rf.with_play_log(off, True) == on
    assert rf.with_play_log("", False) == "play log: off\n"
    dup = "play log: on\nvolume: -23\nPLAY LOG: on\n"
    assert rf.with_play_log(dup, False) == "play log: off\nvolume: -23\nPLAY LOG: off\n"
    states = [rf.play_log_state(x) for x in (on, off, MINI_CFG, None, dup)]
    assert states == ["on", "off", "unset", "unset", "on"]
    assert rf.play_log_state("play log: on\nplay log: off\n") == "off"  # the last one wins


def test_setting_defaults_to_on():
    assert RockboxSettings().enable_play_log is True
    assert RockboxSettings.model_validate({"playlists": True}).enable_play_log is True
    assert RockboxSettings.model_validate({"enable_play_log": False}).enable_play_log is False
    props = RockboxSettings.model_json_schema()["properties"]
    assert (
        props["enable_play_log"]["default"] is True
        and props["enable_play_log"]["type"] == "boolean"
    )


async def _commit(dev: Rockbox) -> None:
    s = await dev.begin()
    await dev.commit(s, s.manifest or (await dev.read_manifest()) or _empty(dev))


def _empty(dev: Rockbox):
    from syncmusic.core.models import Manifest, ManifestHeader

    return Manifest(header=ManifestHeader(device_id=dev.id))


async def test_commit_switches_the_play_log_on_once(tmp_path):
    mount = rockbox_tree(tmp_path / "rb")
    cfg_file = mount / ".rockbox" / "config.cfg"
    cfg_file.write_text(MINI_CFG)
    dev = Rockbox(DeviceConfig(id="rb", type="rockbox", name="RB"), mount, None)
    assert await dev.feedback_warnings() == [rf.PLAY_LOG_PENDING_WARNING]  # not "enable it"
    await dev.read_feedback(None)
    assert cfg_file.read_text() == MINI_CFG  # reading never changes it
    await _commit(dev)
    assert cfg_file.read_text() == MINI_CFG + "play log: on\n"
    mtime = cfg_file.stat().st_mtime_ns
    await _commit(dev)
    assert cfg_file.stat().st_mtime_ns == mtime  # idempotent: not rewritten
    assert await dev.feedback_warnings() == []
    assert not list(cfg_file.parent.glob("*syncmusic-tmp*"))


async def test_option_off_switches_the_play_log_off(tmp_path):
    """off writes `play log: off`; a player without a config is left alone (off is the default)."""
    mount = rockbox_tree(tmp_path / "rb")
    cfg_file = mount / ".rockbox" / "config.cfg"
    cfg_file.write_text(MINI_CFG + "play log: on\n")
    cfg = DeviceConfig(id="rb", type="rockbox", name="RB", settings={"enable_play_log": False})
    dev = Rockbox(cfg, mount, None)
    assert await dev.feedback_warnings() == [
        rf.PLAY_LOG_OFF_WARNING
    ]  # after the write nothing is recorded
    await _commit(dev)
    assert cfg_file.read_text() == MINI_CFG + "play log: off\n"
    assert await dev.play_log_state() == "off"
    assert await dev.feedback_warnings() == [rf.PLAY_LOG_OFF_WARNING]  # 's warning stays
    cfg_file.unlink()
    await _commit(dev)
    assert not cfg_file.exists()


async def test_sync_through_the_api(env, tmp_path):
    app, c = env
    mount = rockbox_tree(tmp_path / "rb")
    cfg_file = mount / ".rockbox" / "config.cfg"
    cfg_file.write_text(MINI_CFG)
    cfg = await _register(c, mount, name="Mini", type="rockbox")
    assert cfg["settings"] == {}  # the field is absent: default on
    types = (await c.get("/api/v1/device-types")).json()
    schema = next(t for t in types if t["type"] == "rockbox")["settings_schema"]
    assert schema["properties"]["enable_play_log"]["default"] is True  # the UI's toggle

    plan = (await c.post("/api/v1/devices/mini/plan", json={})).json()
    assert rf.PLAY_LOG_PENDING_WARNING in plan["warnings"]
    assert rf.PLAY_LOG_OFF_WARNING not in plan["warnings"]
    assert cfg_file.read_text() == MINI_CFG  # planning never writes it
    await _apply(app, c, plan["id"], first_sync="keep")
    assert cfg_file.read_text() == MINI_CFG + "play log: on\n"

    r = await c.patch("/api/v1/devices/mini", json={"settings": {"enable_play_log": False}})
    assert r.status_code == 200 and r.json()["settings"] == {"enable_play_log": False}
    got = (await c.get("/api/v1/devices/mini")).json()["config"]["settings"]
    assert got == {"enable_play_log": False}
    plan = (await c.post("/api/v1/devices/mini/plan", json={})).json()
    assert rf.PLAY_LOG_OFF_WARNING in plan["warnings"]
    await _apply(app, c, plan["id"])
    assert cfg_file.read_text() == MINI_CFG + "play log: off\n"  # option off: switched off


async def test_toggle_applies_at_once_without_a_sync(env, tmp_path):
    """GET shows the setting and the player's state; POST changes both at once, no job; busy refused;
    a player that is not connected gets it at the next write."""
    app, c = env
    mount = rockbox_tree(tmp_path / "rb")
    cfg_file = mount / ".rockbox" / "config.cfg"
    cfg_file.write_text(MINI_CFG)
    await _register(c, mount, name="Mini", type="rockbox")
    got = (await c.get("/api/v1/devices/mini/play-log")).json()
    assert got == {"setting": True, "state": "unset"}
    r = await c.post("/api/v1/devices/mini/play-log", json={"on": True})
    assert r.status_code == 200 and r.json() == {"setting": True, "state": "on", "applied": True}
    assert cfg_file.read_text() == MINI_CFG + "play log: on\n"
    r = await c.post("/api/v1/devices/mini/play-log", json={"on": False})
    assert r.json() == {"setting": False, "state": "off", "applied": True}
    assert cfg_file.read_text() == MINI_CFG + "play log: off\n"
    cfg = (await c.get("/api/v1/devices/mini")).json()["config"]
    assert cfg["settings"]["enable_play_log"] is False
    assert app.jobs.jobs() == []  # not a sync
    async with app.jobs.device_lock("mini"):
        r = await c.post("/api/v1/devices/mini/play-log", json={"on": True})
        assert r.status_code == 409 and "busy" in r.text
    # not connected: the setting changes, the file at the next write
    moved = tmp_path / "away"
    mount.rename(moved)
    r = await c.post("/api/v1/devices/mini/play-log", json={"on": True})
    assert r.json() == {"setting": True, "state": None, "applied": False}
    moved.rename(mount)
    assert cfg_file.read_text() == MINI_CFG + "play log: off\n"
    flash = await _register(c, tmp_path / "flash", name="Flash")
    r = await c.post(f"/api/v1/devices/{flash['id']}/play-log", json={"on": True})
    assert r.status_code == 400 and "not a Rockbox" in r.text
