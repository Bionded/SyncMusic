"""Idle daemon soak: with a connected device whose updates find nothing to do, the snapshot
loop must not keep reloading the library (live reloaded 12k tracks every minute and grew from 240 MB to 1.2 GB), and
repeated reloads of a big snapshot must not grow the process steadily. Slow: builds a 1500-track library."""

from __future__ import annotations

import asyncio
import gc
from pathlib import Path

import pytest

from syncmusic.app import Application
from syncmusic.core import sync as sync_mod
from syncmusic.core.models import LibraryConfig
from syncmusic.core.sync import RegisterRequest
from tests.media import make_music

# 280-400 s each alone (1500-track library, 13.8k-item device): covering runs use -m "slow and not heavy"
pytestmark = [pytest.mark.slow, pytest.mark.heavy]

TRACKS = 1500


def _rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    raise AssertionError("no VmRSS")


async def test_idle_daemon_does_not_reload_or_grow(config, tmp_path, monkeypatch):
    monkeypatch.setattr(sync_mod, "SNAPSHOT_LOOP_S", 0.05)
    music = tmp_path / "music"
    for i in range(TRACKS):
        make_music(
            music / f"A{i // 100:02d}" / f"{i:04d}.mp3",
            title=f"T{i}",
            artist=f"A{i // 100}",
            album=f"B{i // 10}",
            seconds=1.0,
        )
    app = Application(config)
    await app.start()
    try:
        await app.store.put_library(
            LibraryConfig(id="m", type="folder", name="M", settings={"root": str(music)})
        )
        lib = await app.sync.library("m")
        stick = tmp_path / "stick"
        stick.mkdir()
        cfg = await app.sync.register(
            RegisterRequest(
                name="Stick", mount_path=str(stick), type="flashdrive", library_ids=["m"]
            )
        )
        pc = await app.sync.plan(cfg.id)
        job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
        assert job.status == "succeeded", job.error
        # an update whose reverse sync pushes nothing (on live: every minute)
        job = await app.jobs.wait((await app.sync.update_sync(cfg.id)).id)
        assert job.status == "succeeded", job.error
        loads = 0
        real = lib.snapshot

        async def counting():
            nonlocal loads
            loads += 1
            return await real()

        monkeypatch.setattr(lib, "snapshot", counting)
        # an idle stretch: many snapshot-loop ticks and update checks, nothing changed anywhere
        for _ in range(40):
            await asyncio.sleep(0.06)
            await app.sync.periodic_updates()
        assert loads == 0, "the idle daemon reloaded the library snapshot"

        # repeated reloads (Refresh, a changed library): memory settles instead of growing per reload
        for _ in range(3):
            await app.sync.snapshots.get(lib, force=True)
        gc.collect()
        base = _rss_mb()
        for _ in range(10):
            await app.sync.snapshots.get(lib, force=True)
        gc.collect()
        grown = _rss_mb() - base
        assert grown < 40, f"RSS grew {grown:.0f} MB over 10 reloads of {TRACKS} tracks"
    finally:
        await app.stop()


ITEMS = 13_800  # a large real-world manifest


async def test_player_contents_of_a_big_device_idle(config, tmp_path, monkeypatch):
    """with Player contents of a 13.8k-item device open, the thumbnail warm-up parses the manifest at
    most once, the contents stay cached while the browser revalidates (no re-read, no new warm-up) and the idle
    daemon uses < 5 % CPU without growing."""
    import os
    import time

    from syncmusic import facade as facade_mod
    from syncmusic.core import manifest as manifest_io
    from syncmusic.devices.flashdrive import FlashDrive
    from syncmusic.facade import AppFacade

    monkeypatch.setattr(
        facade_mod, "WARM_PAUSE_S", 0
    )  # the pause only spreads the load; the test needs no wait
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="T", artist="A", album="B", seconds=1.0)
    app = Application(config)
    await app.start()
    try:
        await app.store.put_library(
            LibraryConfig(id="m", type="folder", name="M", settings={"root": str(music)})
        )
        stick = tmp_path / "stick"
        stick.mkdir()
        cfg = await app.sync.register(
            RegisterRequest(
                name="Stick", mount_path=str(stick), type="flashdrive", library_ids=["m"]
            )
        )
        pc = await app.sync.plan(cfg.id)
        job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
        assert job.status == "succeeded", job.error
        # grow the device to live's size: hard links of the one synced file, one manifest entry each
        manifest = manifest_io.read(stick)
        assert manifest is not None and len(manifest.items) == 1
        (entry,) = manifest.items.values()
        src = stick / entry.path
        items = {}
        for i in range(ITEMS):
            rel = f"Music/X{i // 500:02d}/{i:05d}.mp3"
            (stick / rel).parent.mkdir(parents=True, exist_ok=True)
            os.link(src, stick / rel)
            ref = entry.ref.model_copy(update={"item_id": f"big{i}"})
            items[ref.key()] = entry.model_copy(update={"ref": ref, "device_ref": rel, "path": rel})
        manifest_io.write(stick, manifest.model_copy(update={"items": manifest.items | items}))

        reads = 0
        real = FlashDrive.read_manifest

        async def counting(self):
            nonlocal reads
            reads += 1
            return await real(self)

        monkeypatch.setattr(FlashDrive, "read_manifest", counting)
        facade = AppFacade(app)
        t0 = time.monotonic()
        rows = await facade.device_contents(cfg.id)
        assert len(rows) == ITEMS + 1
        etag = await facade.device_contents_etag(cfg.id)
        assert etag is not None
        warm = facade._thumb_warm[cfg.id][1]
        await asyncio.wait_for(warm, timeout=900)
        busy_s = time.monotonic() - t0
        assert reads == 1, f"{reads} manifest reads for one contents read and its warm-up"

        gc.collect()
        base = _rss_mb()
        cpu0, wall0 = time.process_time(), time.monotonic()
        # idle with the page open: the browser revalidates, the update loop checks; nothing changes
        for _ in range(20):
            await asyncio.sleep(0.5)
            assert await facade.device_contents_etag(cfg.id) == etag, "the contents stamp changed"
            await app.sync.periodic_updates()
        cpu = (time.process_time() - cpu0) / (time.monotonic() - wall0) * 100
        gc.collect()
        grown = _rss_mb() - base
        print(
            f"\n{ITEMS} items: read + warm-up {busy_s:.0f} s, idle CPU {cpu:.1f} %, RSS {base:.0f} MB, "
            f"idle growth {grown:.1f} MB"
        )
        assert reads == 1 and facade._thumb_warm[cfg.id][1] is warm, "the contents were read again"
        assert cpu < 5, f"idle CPU {cpu:.1f} %"
        assert grown < 10, f"RSS grew {grown:.1f} MB while idle"
    finally:
        await app.stop()
