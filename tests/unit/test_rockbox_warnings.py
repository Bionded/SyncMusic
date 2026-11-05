"""Rockbox records no listening data unless the player is told to: logging and runtime data are off by
default, and then the reverse sync must say so instead of silently finding nothing."""

from __future__ import annotations

import pytest

from syncmusic.core.models import DeviceConfig
from syncmusic.core.reverse import ReverseSync
from syncmusic.devices.Rockbox import rockbox_feedback as rf
from syncmusic.devices.Rockbox.rockbox import Rockbox
from syncmusic.infra.db import Database
from syncmusic.infra.store import Store
from tests.contracts.harness import rockbox_tree

# the head of a mini 2G config.cfg (Rockbox 4.0): neither setting present = both off
MINI_CFG = """# .cfg file created by rockbox 4.0 - http://www.rockbox.org

volume: -23
backlight timeout: 10
scrollbar: off
start directory: /<HDD0>/Music/
database scan paths: /Music
qs left: shuffle
"""


def _dev(tmp_path, cfg: str | None, play_log: bool = True) -> Rockbox:
    mount = rockbox_tree(tmp_path / "rb")
    if cfg is not None:
        (mount / ".rockbox" / "config.cfg").write_text(cfg)
    settings = {} if play_log else {"enable_play_log": False}
    return Rockbox(DeviceConfig(id="rb", type="rockbox", name="RB", settings=settings), mount, None)


def test_parse_config():
    cfg = rf.parse_config(MINI_CFG + "Play Log: on\n  # x: y\n")
    assert cfg["volume"] == "-23" and cfg["start directory"] == "/<HDD0>/Music/"
    assert rf.records_listening_data(cfg) and "# x" not in cfg
    assert not rf.records_listening_data(rf.parse_config(MINI_CFG))


@pytest.mark.parametrize("cfg", [MINI_CFG, None, ""])
async def test_warning_when_nothing_is_recorded(tmp_path, cfg):
    assert await _dev(tmp_path, cfg, play_log=False).feedback_warnings() == [
        rf.PLAY_LOG_OFF_WARNING
    ]


@pytest.mark.parametrize("cfg", [MINI_CFG, None, ""])
async def test_play_log_setting_on_says_the_sync_switches_it_on(tmp_path, cfg):
    # the default enable_play_log writes `play log: on` itself; the user is not asked to do it by hand
    assert await _dev(tmp_path, cfg).feedback_warnings() == [rf.PLAY_LOG_PENDING_WARNING]


@pytest.mark.parametrize(
    "extra", ["play log: on\n", "gather runtime data: on\n", "gather runtime data: ON\n"]
)
async def test_no_warning_with_a_source_enabled(tmp_path, extra):
    assert await _dev(tmp_path, MINI_CFG + extra).feedback_warnings() == []


async def test_no_warning_with_the_scrobbler_plugin_log(tmp_path):
    dev = _dev(tmp_path, MINI_CFG)
    (dev.mount / ".scrobbler.log").write_text("#AUDIOSCROBBLER/1.1\n")
    assert await dev.feedback_warnings() == []


async def test_reverse_report_carries_the_warning(tmp_path):
    db = Database(tmp_path / "sd.db")
    await db.open()
    try:
        dev = _dev(tmp_path, MINI_CFG)
        report = await ReverseSync(Store(db)).run(dev.config, dev, {})
        assert report.warnings == [rf.PLAY_LOG_PENDING_WARNING] and report.errors == []
        dev.config.reverse_sync.plays = dev.config.reverse_sync.ratings = False
        assert (await ReverseSync(Store(db)).run(dev.config, dev, {})).warnings == []
    finally:
        await db.close()
