"""Speed profiles of emulated devices: a throttled loop device writes and reads at the profile's rate (within
20 %), the app's sync measures a write speed in that range, and nothing of the limit is left behind after a detach
or a killed run plus the cleanup. Unthrottled disks stay unlimited."""

from __future__ import annotations

import subprocess
import time

import pytest

from syncmusic.core.sync import RegisterRequest
from tests import cleanup
from tests.emulated import (
    SPEED_PROFILES,
    _maj_min,
    attach,
    flash,
    io_max_line,
    limited_loops,
    navidrome_config,
    needs_sudo,
    running_app,
    set_speed,
    sh,
    speed_for,
)
from tests.factories import own_rules

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

MB = 1024**2


def test_speed_for_env(monkeypatch):
    monkeypatch.delenv("SM_EMU_SPEED", raising=False)
    assert speed_for("emulated_hiby", None) is None
    assert speed_for("emulated_hiby", "usb2_stick") == "usb2_stick"
    monkeypatch.setenv("SM_EMU_SPEED", "real")
    assert speed_for("emulated_hiby", None) == "sd_player"
    assert speed_for("emulated_hiby", "usb3_fast") == "usb3_fast"
    monkeypatch.setenv("SM_EMU_SPEED", "fast")
    assert speed_for("emulated_hiby", "sd_player") is None
    monkeypatch.delenv("SM_EMU_SPEED")
    with pytest.raises(ValueError):
        speed_for("emulated_hiby", "floppy")


def test_io_max_line():
    assert io_max_line("7:3", None) == "7:3 rbps=max wbps=max riops=max wiops=max"
    assert io_max_line("7:3", "sd_player") == (
        f"7:3 rbps={20 * MB} wbps={8 * MB} riops=500 wiops=500"
    )


def _timed_dd(*args: str) -> float:
    started = time.monotonic()
    sh("dd", *args, "bs=1M", "status=none")
    return time.monotonic() - started


def _drop_caches() -> None:
    sh("sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches")


@pytest.mark.slow  # ~20 s of throttled I/O
def test_sd_player_profile_rate():
    prof = SPEED_PROFILES["sd_player"]
    with attach("emulated_flash", 160, speed="sd_player") as disk:
        assert _maj_min(disk.dev) in limited_loops()
        wrote = _timed_dd("if=/dev/zero", f"of={disk.dev}", "count=100", "oflag=direct")
        assert wrote == pytest.approx(100 / prof["write_mb_s"], rel=0.2)
        _drop_caches()
        read = _timed_dd(f"if={disk.dev}", "of=/dev/null", "count=100", "iflag=direct")
        assert read == pytest.approx(100 / prof["read_mb_s"], rel=0.2)
        maj_min = _maj_min(disk.dev)
    assert maj_min not in limited_loops()


def test_no_profile_is_unlimited(monkeypatch):
    monkeypatch.delenv("SM_EMU_SPEED", raising=False)
    with attach("emulated_flash", 160) as disk:
        assert _maj_min(disk.dev) not in limited_loops()
        wrote = _timed_dd("if=/dev/zero", f"of={disk.dev}", "count=100", "oflag=direct")
        assert wrote < 100 / SPEED_PROFILES["usb3_fast"]["write_mb_s"]


def test_cleanup_lifts_limit_of_killed_run():
    """A killed run detaches nothing and lifts nothing: the cleanup lifts the limit of a loop device that is no
    longer attached (it would slow down the next user of that loop number)."""
    with attach("emulated_flash", 32) as disk:
        maj_min = _maj_min(disk.dev)
        set_speed(disk.dev, "sd_player")
        subprocess.run(["sudo", "-n", "losetup", "-d", disk.dev], check=True)  # the limit stays
        assert maj_min in limited_loops()
        disk.dev = ""
        cleanup.lift_stale_limits()  # what test-cleanup.sh runs for it
        assert maj_min not in limited_loops()


@pytest.mark.slow  # a real sync onto a throttled stick
async def test_app_sync_reports_profile_write_speed(services, tmp_path):
    prof = SPEED_PROFILES["sd_player"]
    with attach("emulated_flash", 1024, speed="sd_player") as disk:
        await flash(disk, "vfat", "SLOW")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            rules = {"rules": [{"id": "t", "criteria": {"is": {"kind": "music"}}, "limit": 30}]}
            cfg = await app.sync.register(
                RegisterRequest(
                    name="Slow Stick",
                    disk=blk.path,
                    rule_set=await own_rules(app, "Slow Stick", rules, ["nd"]),
                    on_connect="manual",
                )
            )
            pc = await app.sync.plan(cfg.id)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error
            rep = job.result
            assert rep["bytes_written"] > 20 * MB, rep["bytes_written"]
            mb_s = rep["bytes_written"] / MB / rep["write_seconds"]
            assert mb_s == pytest.approx(prof["write_mb_s"], rel=0.2), mb_s
