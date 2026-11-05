"""On the real iPod Classic: the clock at a sync.

A sync whose clock is due unmounts the iPod, sets its clock and waits until the disk settled (at least SETTLE_MIN_S).
Requests that arrive meanwhile (Player contents, the sidebar's used space) must wait for the settle and never
mount the data partition during it. Afterwards the sync mounts, writes and verifies; the file system is clean.

The Classic's data partition is formatted (partition table unchanged) and left unmounted, not ejected.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from syncmusic.core import sync as sync_mod
from syncmusic.core.sync import RegisterRequest
from syncmusic.infra.blockdev import scsi
from tests.factories import own_rules
from tests.hardware.conftest import quiesce
from tests.hardware.test_classic_acceptance import _data_listing, _partition_table
from tests.hardware.test_classic_clock_contents_tags import _mounted, _source, app  # noqa: F401
from tests.hardware.test_player_contents import FIVE
from tests.hardware.test_ui_click_through import _fsck
from tests.integration.test_navidrome_local import ENV

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]

NAME = "Sync Clock Classic"


async def test_no_mount_while_the_classic_settles_after_the_sync_clock(app):  # noqa: F811
    a, classic = app
    before = _data_listing()
    table = _partition_table(classic.path)
    cfg = await a.sync.register(
        RegisterRequest(
            name=NAME,
            disk=classic.path,
            rule_set=await own_rules(a, NAME, FIVE, ["nd"]),
            on_connect="manual",
        )
    )
    assert cfg.settings.get("time_sync") is True
    cfg = await a.sync.format(cfg.id, confirm=NAME)
    clock = a.sync._clock_tasks.get(cfg.id)
    if clock is not None:
        await asyncio.shield(clock)
    dev = await a.sync.open_device(cfg.id)  # mounted, as after a connect long ago
    data_part = _source(dev.mount)
    assert data_part.startswith(classic.path)

    # the clock was set more than CLOCK_FRESH_S ago: the next sync sets it again
    a.sync._clock_set[cfg.id] = time.monotonic() - sync_mod.CLOCK_FRESH_S - 1
    events: list[tuple[float, str]] = []
    real_time = a.sync._sync_time

    async def spy(c, device):
        events.append((time.monotonic(), "clock mounted" if _mounted(classic.path) else "clock"))
        return await real_time(c, device)

    a.sync._sync_time = spy
    stop = False

    async def watch() -> None:  # samples /proc/mounts every 0.2 s
        was = _mounted(classic.path)
        while not stop:
            now = _mounted(classic.path)
            if now != was:
                events.append((time.monotonic(), "mount" if now else "unmount"))
                was = now
            await asyncio.sleep(0.2)

    watcher = asyncio.create_task(watch())
    pc = await a.sync.plan(cfg.id)
    job = await a.sync.apply(pc.plan.id, first_sync="keep")

    # as soon as the settle starts, a browser asks for Player contents and the used space
    deadline = time.monotonic() + 60
    while not any(e[1].startswith("clock") for e in events):
        assert time.monotonic() < deadline, "the sync did not set the clock"
        await asyncio.sleep(0.05)
    assert a.sync._setting_clock(cfg.id), (
        "GET /devices does not show setting_clock during the settle"
    )
    t_clock = events[-1][0]
    started = time.monotonic()
    contents = asyncio.create_task(a.sync.contents(cfg.id))
    space = asyncio.create_task(a.sync.space(cfg.id))
    rows, used = await asyncio.gather(contents, space)
    served = time.monotonic()
    info = await a.jobs.wait(job.id)
    stop = True
    await watcher
    a.sync._sync_time = real_time

    assert info.status == "succeeded", info.error
    kinds = [k for _, k in events]
    # the spy saw the disk unmounted when the clock command went out (the 0.2 s watcher may log the unmount later)
    assert "clock" in kinds and "clock mounted" not in kinds, kinds
    mounts = [t for t, k in events if k == "mount"]
    assert mounts, kinds
    assert mounts[0] - t_clock >= scsi.SETTLE_MIN_S - 0.5, (
        f"mounted {mounts[0] - t_clock:.1f} s after the clock command (at least {scsi.SETTLE_MIN_S} s): {kinds}"
    )
    assert served - t_clock >= scsi.SETTLE_MIN_S - 0.5, (
        "a parallel request was served during the settle"
    )
    assert served >= started
    assert isinstance(rows, list) and used, (rows, used)
    assert not a.sync._setting_clock(cfg.id)
    assert len(await a.sync.contents(cfg.id)) == 5

    await quiesce(a, cfg.id)
    await a.sync._unmount(cfg.id)
    left = _entries(data_part)
    _umount_all(data_part)
    await asyncio.to_thread(_fsck, data_part)
    assert _partition_table(classic.path) == table
    assert _data_listing() == before, "the test media changed"
    assert not left, f"{len(left)} mount(s) of {data_part} left after the unmount: {left}"


def _entries(part: str) -> list[str]:
    return [ln for ln in Path("/proc/mounts").read_text().splitlines() if ln.split()[0] == part]


def _umount_all(part: str) -> None:
    """Removes stacked mounts of the partition so the fsck and the teardown see the real state."""
    for _ in range(10):
        entries = _entries(part)
        if not entries:
            return
        subprocess.run(["sudo", "-n", "umount", entries[-1].split()[1]], check=True)
