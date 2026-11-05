"""An emulated flash drive and an emulated stock iPod Classic in one application: the scenarios of
tests/hardware/test_rename.py (Rename) and test_player_contents.py (remove from the device), and two devices syncing
at once (only here: every hardware test runs on one device), against the compose stack's Navidrome."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from syncmusic.core.models import ActionKind
from syncmusic.core.sync import RegisterRequest
from tests.emulated import (
    CLASSIC_LAYOUT,
    attach,
    flash,
    ipod,
    navidrome_config,
    needs_sudo,
    running_app,
)
from tests.factories import own_rules
from tests.hardware.test_classic_acceptance import _check_db, _data_listing, _partition_table
from tests.hardware.test_player_contents import contents_scenario
from tests.hardware.test_rename import rename_scenario

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

ALL_MUSIC = {"rules": [{"id": "music", "name": "All music", "criteria": {"is": {"kind": "music"}}}]}


@pytest.fixture
async def app(services, tmp_path):
    with contextlib.ExitStack() as stack:
        stick = stack.enter_context(attach("emulated_flash", 1024))
        classic = stack.enter_context(attach("emulated_ipod_classic", 1024, layout=CLASSIC_LAYOUT))
        await flash(stick, "vfat", "OLD")
        await ipod(classic, tmp_path / "build", name="Old Classic")
        async with running_app(tmp_path, [stick, classic], [navidrome_config(services)]) as run:
            yield run


@pytest.mark.slow  # full acceptance scenario: -m emulated runs it
async def test_rename_flash_and_classic(app):
    await rename_scenario(*app)


@pytest.mark.slow  # full acceptance scenario: -m emulated runs it
async def test_remove_two_tracks_from_flash_and_classic(app):
    await contents_scenario(*app)


@pytest.mark.slow  # full acceptance scenario: -m emulated runs it
async def test_flash_and_classic_sync_at_the_same_time(app):
    a, (stick, classic) = app
    await parallel_scenario(a, stick, classic)


async def parallel_scenario(a, flash, classic):
    """A flash drive and a stock iPod sync at once in one application (devices syncing at once = 2)."""
    a.jobs.set_device_job_limit(2)
    before = _data_listing()
    table = _partition_table(classic.path)
    devices = []
    for disk, name, fs in ((flash, "Parallel Stick", "vfat"), (classic, "Parallel Classic", None)):
        cfg = await a.sync.register(
            RegisterRequest(
                name=name,
                disk=disk.path,
                rule_set=await own_rules(a, name, ALL_MUSIC, ["nd"]),
                on_connect="manual",
            )
        )
        devices.append(await a.sync.format(cfg.id, confirm=name, fs=fs))
    stick, pod = devices
    assert stick.type == "flashdrive" and pod.type == "ipod"
    assert _partition_table(classic.path) == table

    plans = [await a.sync.plan(cfg.id) for cfg in devices]
    for pc in plans:
        assert pc.plan.count(ActionKind.ADD) >= 25, pc.plan.warnings
    jobs = [await a.sync.apply(pc.plan.id, first_sync="keep") for pc in plans]
    done = await asyncio.gather(*(a.jobs.wait(j.id) for j in jobs))
    for job in done:
        assert job.status == "succeeded", job.error
        assert job.result["status"] == "succeeded" and not job.result["failures"], job.result
    first, second = sorted(done, key=lambda j: j.started_at)
    assert second.started_at < first.finished_at, "the two syncs did not run at the same time"

    _, m, db = await _check_db(a, pod.id)
    assert len(m.items) == len(db.tracks) == plans[1].plan.count(ActionKind.ADD)
    dev = await a.sync.open_device(stick.id)
    assert len((await dev.read_manifest()).items) == plans[0].plan.count(ActionKind.ADD)
    assert _data_listing() == before, "the test media changed"
