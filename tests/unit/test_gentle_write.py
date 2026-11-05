"""Gentle write mode: vfat mounted without `flush`, a full sync writes in batches with a file
system sync and a pause in between, an optional write rate limit; off by default, not offered for FTP."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import errno

from syncmusic.core import applier
from syncmusic.core.applier import DeviceLostError, WritePacer, gentle_pacer, storage_failure
from syncmusic.core.models import DeviceConfig, SyncSettings
from syncmusic.core.sync import suggests_gentle
from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.infra import notify
from syncmusic.infra.blockdev import mount as mnt
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


async def test_batches_sync_and_pause():
    clock, flushes = _Clock(), []

    async def flush():
        flushes.append(clock.t)

    pacer = WritePacer(2, 2.0, None, flush, sleep=clock.sleep, clock=clock)
    for _ in range(5):
        await pacer.after_file(1000)
    assert len(flushes) == 2 and clock.sleeps == [2.0, 2.0]


async def test_rate_limit_waits_until_the_average_is_under_it():
    clock = _Clock()

    async def flush():
        pass

    pacer = WritePacer(1000, 0, 1_000_000, flush, sleep=clock.sleep, clock=clock)
    await pacer.after_file(3_000_000)  # 3 MB at 1 MB/s: 3 s
    clock.t += 1.0  # the next file took 1 s to write
    await pacer.after_file(1_000_000)
    assert clock.sleeps == [3.0]  # 4 MB in 4 s: no further wait


def test_off_by_default_and_the_vfat_mount_option():
    assert gentle_pacer(SyncSettings(), applier.Path("/x")) is None
    pacer = gentle_pacer(SyncSettings(gentle_write=True, gentle_rate_mb_s=2.5), applier.Path("/x"))
    assert pacer is not None and (pacer.batch, pacer.pause_s, pacer.rate) == (25, 2.0, 2_500_000)
    assert "flush" not in mnt.mount_options("vfat", 1, 1)  # never


async def test_a_full_sync_in_gentle_mode_syncs_after_each_batch(env, music, tmp_path, monkeypatch):
    app, c = env
    synced = []

    async def syncfs(path):
        synced.append(path)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(applier, "_syncfs", syncfs)
    monkeypatch.setattr(applier.asyncio, "sleep", no_sleep)
    await _register(c, tmp_path / "flash", name="Flash")
    gentle = {"gentle_write": True, "gentle_batch_files": 2, "gentle_pause_s": 0}
    r = await c.patch("/api/v1/devices/flash", json={"sync": gentle})
    assert r.status_code == 200, r.text
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    report = await _apply(app, c, plan["id"], first_sync="keep")
    assert report["counts"]["add"] == 3 and synced == [tmp_path / "flash"]  # 3 files, batches of 2

    types = (await c.get("/api/v1/device-types")).json()
    schema = {t["type"]: set(t["sync_schema"]["properties"]) for t in types}
    assert "gentle_write" in schema["flashdrive"] and "gentle_write" in schema["ipod"]
    assert "gentle_write" not in schema["ftp"]


async def _failing_sync(app, c, tmp_path, monkeypatch, error, gentle=False):
    """A full sync of a flash drive whose writes raise `error`: (sync.failed events, last report)."""

    async def put(self, s, item):
        raise error

    monkeypatch.setattr(FlashDrive, "put_track", put)
    await _register(c, tmp_path / "flash", name="Flash")
    if gentle:
        r = await c.patch("/api/v1/devices/flash", json={"sync": {"gentle_write": True}})
        assert r.status_code == 200, r.text
    q = app.bus.subscribe(1000)
    plan = (await c.post("/api/v1/devices/flash/plan", json={})).json()
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    await app.jobs.wait(r.json()["id"])
    events = []
    while not q.empty():
        e = q.get_nowait()
        if e.type == "sync.failed":
            events.append(e)
    return events, (await app.store.reports("flash"))[0]


async def test_an_io_error_suggests_gentle_mode_but_never_turns_it_on(
    env, music, tmp_path, monkeypatch
):
    app, c = env
    events, report = await _failing_sync(
        app, c, tmp_path, monkeypatch, OSError(errno.EIO, "Input/output error")
    )
    assert report.status == "failed" and report.suggest_gentle
    assert [e.data["suggest_gentle"] for e in events] == [True]
    assert "gentle write mode" in notify.summary(events[0], "Flash")
    assert (await app.store.device("flash")).sync.gentle_write is False


async def test_no_suggestion_when_gentle_is_on(env, music, tmp_path, monkeypatch):
    app, c = env
    _, report = await _failing_sync(
        app, c, tmp_path, monkeypatch, OSError(errno.EIO, "Input/output error"), gentle=True
    )
    assert report.status == "failed" and not report.suggest_gentle


async def test_no_suggestion_for_a_failure_that_is_not_the_storage(
    env, music, tmp_path, monkeypatch
):
    app, c = env
    events, report = await _failing_sync(app, c, tmp_path, monkeypatch, RuntimeError("boom"))
    assert report.status == "partial" and not report.suggest_gentle and events == []


def test_storage_failure_follows_the_cause_chain():
    try:
        try:
            raise DeviceLostError("gone")
        except DeviceLostError as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError as e:
        assert storage_failure(e)
    assert not storage_failure(ValueError("x"))
    cfg = DeviceConfig(id="f", name="F", type="ftp")
    assert not suggests_gentle(cfg, DeviceLostError("gone"))  # a network device: no mount


def test_the_app_write_limit_paces_every_disk_device():
    """Settings → Sync defaults' write speed limit: a pacer without gentle mode (no batches, no
    flush), and in gentle mode the lower of the two limits."""
    pacer = gentle_pacer(SyncSettings(), applier.Path("/x"), 3.0)
    assert pacer is not None and pacer.rate == 3_000_000 and pacer.pause_s == 0
    gentle = SyncSettings(gentle_write=True, gentle_rate_mb_s=5.0)
    assert gentle_pacer(gentle, applier.Path("/x"), 3.0).rate == 3_000_000
    assert gentle_pacer(gentle, applier.Path("/x"), 8.0).rate == 5_000_000
    assert gentle_pacer(SyncSettings(gentle_write=True), applier.Path("/x"), None).rate is None
