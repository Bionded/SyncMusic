"""Applier with the fake library and the flash drive / fake device."""

from __future__ import annotations

import asyncio
import os

import pytest

from syncmusic.core import manifest as manifest_io
from syncmusic.core.applier import Applier, DeviceIOError, DeviceLostError
from syncmusic.core.models import ActionKind, DeviceConfig, LibraryConfig, Rule, RuleSet
from syncmusic.core.planner import plan_sync
from syncmusic.core.prepare import Chain, NoCache, Preparer
from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.infra.cache import ProcessingCache
from syncmusic.infra.db import Database
from tests.fakes import MARKER, FakeDevice, FakeLibrary, FakeTrailer

KB = 1024


class Stop(Exception):
    """Named like JobCancelled: treated as a cancellation."""


Stop.__name__ = "JobCancelled"


@pytest.fixture
async def preparer(tmp_path):
    db = Database(tmp_path / "sd.db")
    await db.open()
    yield Preparer(ProcessingCache(db, tmp_path / "cache"), tmp_path / "work")
    await db.close()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    for i, name in enumerate(["Один", "Два", "Три", "Чотири"]):
        (root / f"{name}.mp3").write_bytes(os.urandom(20 * KB + i))
    return FakeLibrary(
        LibraryConfig(id="lib", type="fake", name="L", settings={"root": str(root)}), None
    )


def _cfg(device_type="flashdrive", rules=None, **settings) -> DeviceConfig:
    return DeviceConfig(
        id="dev",
        type=device_type,
        name="D",
        library_ids=["lib"],
        rule_set=RuleSet(rules=rules or [Rule(id="all", criteria={"all": []}, as_playlist="Все")]),
        settings=settings,
    )


def _flash(tmp_path) -> FlashDrive:
    mount = tmp_path / "flash"
    (mount / ".syncmusic").mkdir(parents=True, exist_ok=True)
    return FlashDrive(_cfg(), mount, None)


def _fake(tmp_path, capacity) -> FakeDevice:
    mount = tmp_path / "fake"
    mount.mkdir(exist_ok=True)
    (mount / MARKER).touch()
    return FakeDevice(_cfg("fake", capacity=capacity), mount, None)


async def _plan(dev, library, chain=None):
    snap = await library.snapshot()
    caps = dev.effective_capabilities()
    return plan_sync(dev.config, {"lib": snap}, await dev.read_manifest(), caps, chain or Chain([]))


async def _sync(dev, library, preparer, chain=None, **kw):
    chain = chain or Chain([])
    plan = await _plan(dev, library, chain)
    return plan, await Applier(dev, {"lib": library}, chain, preparer, **kw).apply(plan)


class Recording(FlashDrive):
    calls: list[str]

    def _rec(self, name):
        self.calls.append(name)

    async def remove_track(self, s, ref):
        self._rec("remove")
        await super().remove_track(s, ref)

    async def put_track(self, s, item):
        self._rec("put")
        return await super().put_track(s, item)

    async def set_playlists(self, s, playlists):
        self._rec("playlists")
        await super().set_playlists(s, playlists)

    async def commit(self, s, manifest):
        self._rec("commit")
        await super().commit(s, manifest)


async def test_first_sync_then_rerun_is_noop(tmp_path, library, preparer):
    dev = _flash(tmp_path)
    plan, rep = await _sync(dev, library, preparer)
    assert rep.status == "succeeded" and rep.counts["add"] == 4 and rep.bytes_written > 80 * KB
    m = await dev.read_manifest()
    assert len(m.items) == 4 and m.playlists == ["Все"]
    assert all(e.chain_hash and e.meta_hash and e.source_state for e in m.items.values())
    assert (dev.mount / "Playlists" / "Все.m3u8").read_text().count("../Music/") == 4
    plan2 = await _plan(dev, library)
    assert {a.kind for a in plan2.actions} == {ActionKind.KEEP}
    rep2 = await Applier(dev, {"lib": library}, Chain([]), preparer).apply(plan2)
    assert rep2.bytes_written == 0 and rep2.counts["keep"] == 4 and rep2.status == "succeeded"


async def test_order_removals_puts_playlists_commit(tmp_path, library, preparer):
    mount = tmp_path / "flash"
    (mount / ".syncmusic").mkdir(parents=True)
    dev = Recording(_cfg(), mount, None)
    dev.calls = []
    await _sync(dev, library, preparer)
    # no longer selected (a source gone from the library would be an orphan and stay)
    dev.config = _cfg(
        rules=[Rule(id="all", criteria={"isNot": {"title": "Один"}}, as_playlist="Все")]
    )
    (library.root / "Пʼять.mp3").write_bytes(os.urandom(1000))
    dev.calls = []
    await _sync(dev, library, preparer)
    assert dev.calls == ["remove", "put", "playlists", "commit"]


async def test_replace_and_rule_change(tmp_path, library, preparer):
    dev = _flash(tmp_path)
    await _sync(dev, library, preparer)
    (library.root / "Два.mp3").write_bytes(b"new version" * 100)
    plan, rep = await _sync(dev, library, preparer)
    assert plan.count(ActionKind.REPLACE) == 1 and rep.counts["replace"] == 1
    m = await dev.read_manifest()
    two = next(e for e in m.items.values() if "Два" in e.path)
    assert (dev.mount / two.path).read_bytes() == b"new version" * 100
    dev.config = _cfg(rules=[Rule(id="one", criteria={"is": {"title": "Один"}})])
    plan, rep = await _sync(dev, library, preparer)
    assert rep.counts["remove"] == 3 and len((await dev.read_manifest()).items) == 1
    assert not (dev.mount / two.path).exists()
    assert not (dev.mount / "Playlists" / "Все.m3u8").exists()


async def test_chain_runs_for_puts(tmp_path, library, preparer):
    dev = _flash(tmp_path)
    _, rep = await _sync(dev, library, preparer, chain=Chain([FakeTrailer({}, None)]))
    m = await dev.read_manifest()
    one = next(e for e in m.items.values() if "Один" in e.path)
    assert (dev.mount / one.path).read_bytes().endswith(b"TAG".ljust(128, b"\0"))


class SlowTrailer(FakeTrailer):
    async def run(self, item, caps):
        await asyncio.sleep(0.2)
        return await super().run(item, caps)


async def test_report_splits_writing_from_waiting_for_prepares(tmp_path, library, preparer):
    """a sync bound by its processors shows it as `wait_seconds`, not as slow writes."""
    dev = _flash(tmp_path)
    _, rep = await _sync(dev, library, preparer, chain=Chain([SlowTrailer({}, None)]), workers=1)
    puts = rep.counts["add"]
    assert puts >= 2
    assert rep.wait_seconds >= 0.2 * puts * 0.9
    assert rep.write_seconds < rep.wait_seconds


async def test_failing_item_does_not_stop_sync(tmp_path, library, preparer):
    dev = _flash(tmp_path)
    plan = await _plan(dev, library)
    (library.root / "Три.mp3").unlink()  # gone between plan and apply
    rep = await Applier(dev, {"lib": library}, Chain([]), preparer, workers=3).apply(plan)
    assert rep.status == "partial" and rep.counts["add"] == 3
    assert [f.title for f in rep.failures] == ["Три"]
    assert len((await dev.read_manifest()).items) == 3
    plan2 = await _plan(dev, library)
    assert plan2.count(ActionKind.ADD) == 0  # Три no longer in the library


async def test_first_item_failure_is_logged_in_the_copy_step(tmp_path, library, preparer):
    """the job's log step follows the progress phase; a failure of the first (only) item is logged under
    copy, not under the step before it (reverse)."""
    import logging

    from syncmusic.infra.events import EventBus
    from syncmusic.infra.jobs import JobRunner
    from syncmusic.infra.log import ContextFilter

    dev = _flash(tmp_path)
    plan = await _plan(dev, library)
    for a in plan.actions[1:]:
        a.kind = ActionKind.KEEP  # one add only
    for f in library.root.iterdir():
        f.unlink()  # gone between plan and apply
    seen: list[tuple[str, str | None]] = []

    class Grab(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                seen.append((record.getMessage(), record.ctx.get("step")))

    handler = Grab()
    handler.addFilter(ContextFilter())
    logging.getLogger("syncmusic").addHandler(handler)
    try:

        async def sync(ctx):
            ctx.progress(None, None, phase="reverse")
            app = Applier(dev, {"lib": library}, Chain([]), preparer, progress=ctx.progress)
            return await app.apply(plan)

        runner = JobRunner(EventBus())
        await runner.wait(runner.submit("sync", "s", sync, device_id="dev").id)
    finally:
        logging.getLogger("syncmusic").removeHandler(handler)
    assert seen and {step for _, step in seen} == {"copy"}, seen


async def test_path_layout_change_moves_the_files(tmp_path, library, preparer):
    """a changed path layout re-writes the files already on the device at the new paths (replace, reason
    'path layout changed'; not 'processing changed': the layout is no part of the chain hash), the old ones go."""
    dev = _flash(tmp_path)
    _, rep = await _sync(dev, library, preparer)
    assert rep.status == "succeeded" and rep.counts["add"] == 4
    old = {e.path for e in (await dev.read_manifest()).items.values()}
    assert all(p.startswith("Music/") for p in old)
    moved = FlashDrive(_cfg(layout={"music": "Музика/{title}"}), dev.mount, None)
    plan, rep = await _sync(moved, library, preparer)
    assert {(a.kind, a.reason) for a in plan.actions} == {
        (ActionKind.REPLACE, "path layout changed")
    }
    assert rep.status == "succeeded" and rep.counts["replace"] == 4
    m = await moved.read_manifest()
    assert sorted(e.path for e in m.items.values()) == sorted(
        f"Музика/{n}.mp3" for n in ["Один", "Два", "Три", "Чотири"]
    )
    assert {e.layout for e in m.items.values()} == {"Музика/{title}"}
    assert all((moved.mount / e.path).is_file() for e in m.items.values())
    assert not any((moved.mount / p).exists() for p in old) and not (moved.mount / "Music").exists()
    assert {a.kind for a in (await _plan(moved, library)).actions} == {ActionKind.KEEP}
    # car mode renames them too: ASCII names on the next sync, and back when it is off again
    # (the device settings are part of the chain hash: "processing changed: device")
    car = FlashDrive(_cfg(layout={"music": "Музика/{title}"}, car_mode=True), dev.mount, None)
    plan, rep = await _sync(car, library, preparer)
    assert {a.kind for a in plan.actions} == {ActionKind.REPLACE}
    paths = [e.path for e in (await car.read_manifest()).items.values()]
    assert rep.counts["replace"] == 4 and all(p.isascii() for p in paths), paths
    assert {a.kind for a in (await _plan(car, library)).actions} == {ActionKind.KEEP}
    assert {a.kind for a in (await _plan(moved, library)).actions} == {ActionKind.REPLACE}


async def test_android_path_layout_change_moves_the_files(tmp_path, library, preparer):
    """an Android player (fake MTP) takes the flash drive's path layout; a changed template
    re-writes its files at the new paths over MTP on the next sync and deletes the old ones."""
    from syncmusic.devices.Android.mtp import AndroidMtp
    from tests.fake_mtp import FakeMtpBackend

    storage = tmp_path / "storage"
    storage.mkdir()
    backend = FakeMtpBackend(storage)

    def player(**settings) -> AndroidMtp:
        dev = AndroidMtp(_cfg("android", usb_serial="S1", **settings), storage, None)
        dev.backend_factory = lambda s, *_: backend  # type: ignore[method-assign]
        return dev

    dev = player()
    assert dev.s.base_path == ""  # the storage root by default
    _, rep = await _sync(dev, library, preparer)
    assert rep.status == "succeeded" and rep.counts["add"] == 4
    old = {e.path for e in (await dev.read_manifest()).items.values()}
    assert all(p.startswith("Music/") and (storage / p).is_file() for p in old)
    moved = player(layout={"music": "Музика/{title}"})
    plan, rep = await _sync(moved, library, preparer)
    assert {(a.kind, a.reason) for a in plan.actions} == {
        (ActionKind.REPLACE, "path layout changed")
    }
    assert rep.status == "succeeded" and rep.counts["replace"] == 4
    new = sorted(e.path for e in (await moved.read_manifest()).items.values())
    assert new == sorted(f"Музика/{n}.mp3" for n in ["Один", "Два", "Три", "Чотири"])
    assert all((storage / p).is_file() for p in new) and not any(
        (storage / p).exists() for p in old
    )
    assert {a.kind for a in (await _plan(moved, library)).actions} == {ActionKind.KEEP}


async def test_processing_change_says_what_changed(tmp_path, library, preparer, caplog):
    """the manifest keeps the processing parts' inputs; a changed device part names its old → new
    values in the replace reason, the mass re-process confirmation and one INFO log line; unchanged: no line."""
    import logging

    dev = _flash(tmp_path)
    _, rep = await _sync(dev, library, preparer)
    m = await dev.read_manifest()
    (inputs,) = m.header.processing_inputs.values()  # one copy for all four items
    assert "wav" in inputs["formats"] and inputs["space_steps"] is False
    caplog.set_level(logging.INFO, logger="syncmusic.core.planner")
    assert {a.kind for a in (await _plan(dev, library)).actions} == {ActionKind.KEEP}
    assert "processing inputs changed" not in caplog.text
    fewer = FlashDrive(_cfg(formats=["mp3", "flac"]), dev.mount, None)
    plan = await _plan(fewer, library)
    want = "device: formats -aac -ogg -opus -wav"
    assert {a.reason for a in plan.actions} == {f"processing changed: device - {want}"}
    assert any(want in r for r in plan.confirmation_reasons), plan.confirmation_reasons
    assert caplog.text.count("dev: processing inputs changed: " + want) == 1
    # a manifest written before keeps no inputs: the reason stays short, the log says so
    m.header.processing_inputs = {}
    caplog.clear()
    snap = await library.snapshot()
    caps = fewer.effective_capabilities()
    old = plan_sync(fewer.config, {"lib": snap}, m, caps, Chain([]))
    assert {a.reason for a in old.actions} == {"processing changed: device"}
    assert "device: previous values unknown (stored from now on)" in caplog.text


async def test_space_guard_skips_items_that_do_not_fit(tmp_path, library, preparer):
    dev = _fake(tmp_path, capacity=0)
    reserve = dev.commit_reserve(3)
    dev.settings.capacity = reserve + 2 * 20 * KB + 100  # two files fit with the reserve, not more
    _, rep = await _sync(dev, library, preparer)
    assert rep.counts["add"] == 2 and len(rep.skipped_for_space) == 2 and rep.status == "partial"
    m = await dev.read_manifest()
    assert len(m.items) == 2


async def test_final_progress_names_partial_outcome(tmp_path, library, preparer):
    msgs: list[str] = []
    dev = _fake(tmp_path, capacity=0)
    dev.settings.capacity = dev.commit_reserve(3) + 2 * 20 * KB + 100
    _, rep = await _sync(dev, library, preparer, progress=lambda f, m, **kw: msgs.append(m))
    assert msgs[-1] == rep.message == "partial: 2 synced, 2 skipped for space, 0 failed"
    msgs.clear()
    _, ok = await _sync(
        _flash(tmp_path), library, preparer, progress=lambda f, m, **kw: msgs.append(m)
    )
    assert ok.status == "succeeded" and msgs[-1] == "done" and ok.message == ""


async def test_progress_reports_phases_and_counters(tmp_path, library, preparer):
    """the job view's phases and counters come with the progress: copy (file N of M, bytes, failed)
    then database."""
    stats: list[dict] = []
    _, rep = await _sync(
        _flash(tmp_path), library, preparer, progress=lambda f, m, **kw: stats.append(kw)
    )
    phases = [s["phase"] for s in stats if "phase" in s]
    assert phases[0] == "copy" and phases[-1] == "database" and set(phases) == {"copy", "database"}
    copies = [s for s in stats if s.get("phase") == "copy"]
    # announced before the first item, then one report per file
    assert [s["files_done"] for s in copies] == list(range(0, rep.counts["add"] + 1))
    assert {s["files_total"] for s in copies} == {rep.counts["add"]}
    assert copies[-1]["bytes"] == rep.bytes_written > 0 and copies[-1]["failed"] == 0


async def test_commit_reserve_removes_last_synced_tracks(tmp_path, library, preparer):
    class GrowingDB(FakeDevice):
        def commit_reserve(self, n):  # small while putting, the DB "grows" at commit time (52 KB)
            return 1000 if getattr(self, "putting", True) else 1000 + 50 * KB

        async def set_playlists(self, s, playlists):
            self.putting = False

    mount = tmp_path / "fake"
    mount.mkdir()
    (mount / MARKER).touch()
    dev = GrowingDB(_cfg("fake", capacity=4 * 20 * KB + 6 + 30 * KB), mount, None)
    plan, rep = await _sync(dev, library, preparer)
    assert len(rep.removed_for_commit) == 2 and rep.status == "partial"
    keys = [a.ref.key() for a in plan.actions if a.kind == ActionKind.ADD]
    assert rep.removed_for_commit == keys[:-3:-1]  # the last two added, newest first
    m = await dev.read_manifest()
    assert set(m.items) == set(keys[:2])
    assert len((await dev.read_contents()).items) == 2


async def test_cancellation_commits_done_items(tmp_path, library, preparer):
    dev = _flash(tmp_path)
    puts = []
    real_put = dev.put_track

    async def put(s, item):
        puts.append(item)
        return await real_put(s, item)

    dev.put_track = put

    def check():
        if len(puts) >= 2:
            raise Stop()

    msgs: list[str] = []
    fractions: list[float | None] = []
    plan = await _plan(dev, library)
    rep = await Applier(
        dev,
        {"lib": library},
        Chain([]),
        preparer,
        check_cancelled=check,
        progress=lambda f, m, **kw: (msgs.append(m), fractions.append(f)),
    ).apply(plan)
    assert rep.status == "cancelled" and rep.counts["add"] == 2
    # the final job message names the cancelled outcome, not 'done'
    assert msgs[-1] == rep.message == "cancelled: 2 synced, 0 skipped for space, 0 failed"
    # the final update keeps the fraction reached instead of claiming 100 %
    # (the first update announces the copy step before any file)
    assert fractions[0] is None and fractions[-1] is None
    assert all(f is not None and f < 1 for f in fractions[1:-1])
    m = await dev.read_manifest()
    assert len(m.items) == 2 and not m.playlists  # playlists skipped after a cancel
    assert not list((tmp_path / "work").glob("*")) if (tmp_path / "work").exists() else True


async def test_lost_device_stops_writes_and_fails(tmp_path, library, preparer):
    """check_device raising after the second put: no further put, no playlists, no commit."""
    mount = tmp_path / "flash"
    (mount / ".syncmusic").mkdir(parents=True)
    dev = Recording(_cfg(), mount, None)
    dev.calls = []

    async def check():
        if dev.calls.count("put") >= 2:
            raise DeviceLostError("device Zorvex was unmounted during the sync")

    plan = await _plan(dev, library)
    with pytest.raises(DeviceLostError):
        await Applier(dev, {"lib": library}, Chain([]), preparer, check_device=check).apply(plan)
    assert dev.calls == ["put", "put"]
    assert await dev.read_manifest() is None


async def test_failed_commit_cleans_up_next_sync(tmp_path, library, preparer):
    dev = _flash(tmp_path)

    async def boom(s, manifest):
        raise OSError("device gone")

    real_commit = dev.commit
    dev.commit = boom
    plan = await _plan(dev, library)
    with pytest.raises(OSError):
        await Applier(dev, {"lib": library}, Chain([]), preparer).apply(plan)
    assert await dev.read_manifest() is None
    dev.commit = real_commit
    music = dev.mount / "Music"
    assert not music.exists() or not [
        p for p in music.rglob("*.mp3")
    ]  # abort removed the session's files
    _, rep = await _sync(dev, library, preparer)
    assert rep.status == "succeeded" and len((await dev.read_manifest()).items) == 4


async def test_interrupted_sync_leftovers_cleaned(tmp_path, library, preparer):
    dev = _flash(tmp_path)
    await _sync(dev, library, preparer)
    # a crashed sync: a put without commit (the process died: no abort)
    s = await dev.begin()
    from tests.contracts.harness import work_item

    ref = await dev.put_track(s, work_item(tmp_path, "Crash", artist="X"))
    assert (dev.mount / ref.path).exists()
    _, rep = await _sync(dev, library, preparer)
    assert not (dev.mount / ref.path).exists() and rep.status == "succeeded"


async def test_file_deleted_on_device_is_added_again(tmp_path, library, preparer):
    """a managed file deleted outside SyncMusic is planned as an add, not kept forever."""
    dev = _flash(tmp_path)
    await _sync(dev, library, preparer)
    m = await dev.read_manifest()
    victim = next(e for e in m.items.values() if e.path.endswith("Два.mp3"))
    (dev.mount / victim.path).unlink()
    assert await dev.missing(m) == {victim.ref.key()}
    snap = await library.snapshot()
    caps = dev.effective_capabilities()
    plan = plan_sync(dev.config, {"lib": snap}, m, caps, Chain([]), missing=await dev.missing(m))
    adds = [a for a in plan.actions if a.kind is ActionKind.ADD]
    assert [a.reason.split(" · ")[0] for a in adds] == ["missing on the device"]  # + its rule
    assert plan.count(ActionKind.KEEP) == 3 and adds[0].size_delta > 0
    rep = await Applier(dev, {"lib": library}, Chain([]), preparer).apply(plan)
    assert rep.counts["add"] == 1 and (dev.mount / victim.path).is_file()
    m2 = await dev.read_manifest()
    assert not await dev.missing(m2)
    plan2 = await _plan(dev, library)
    assert {a.kind for a in plan2.actions} == {ActionKind.KEEP}


async def test_failed_readd_of_a_file_lost_on_the_device_drops_its_entry(
    tmp_path, library, preparer
):
    """a file deleted on the device by hand is planned as an add; when that add fails (the source cannot be
    read), the stale manifest entry goes too - one failure for the item, the sync partial, nothing the read-back
    verification would report again; the next plan adds it again."""
    dev = _flash(tmp_path)
    await _sync(dev, library, preparer)
    m = await dev.read_manifest()
    victim = next(e for e in m.items.values() if e.path.endswith("Два.mp3"))
    (dev.mount / victim.path).unlink()
    snap = await library.snapshot()
    caps = dev.effective_capabilities()
    plan = plan_sync(dev.config, {"lib": snap}, m, caps, Chain([]), missing=await dev.missing(m))
    source = library.root / "Два.mp3"
    data = source.read_bytes()
    source.unlink()  # the re-add fails in the prepare step
    rep = await Applier(dev, {"lib": library}, Chain([]), preparer).apply(plan)
    assert rep.status == "partial" and [f.ref for f in rep.failures] == [victim.ref.key()]
    m2 = await dev.read_manifest()
    assert victim.ref.key() not in m2.items and len(m2.items) == 3
    assert not await dev.missing(m2)  # what the verification checks
    source.write_bytes(data)
    plan2 = await _plan(dev, library)
    assert [a.title for a in plan2.actions if a.kind is ActionKind.ADD] == ["Два"]


async def test_missing_file_no_longer_selected_frees_nothing(tmp_path, library, preparer):
    dev = _flash(tmp_path)
    await _sync(dev, library, preparer)
    m = await dev.read_manifest()
    victim = next(e for e in m.items.values() if e.path.endswith("Два.mp3"))
    (dev.mount / victim.path).unlink()
    (library.root / "Два.mp3").unlink()
    snap = await library.snapshot()
    plan = plan_sync(
        dev.config,
        {"lib": snap},
        m,
        dev.effective_capabilities(),
        Chain([]),
        missing=await dev.missing(m),
    )
    (rm,) = [a for a in plan.actions if a.kind is ActionKind.REMOVE]
    assert rm.size_delta == 0
    rep = await Applier(dev, {"lib": library}, Chain([]), preparer).apply(plan)
    assert rep.status == "succeeded" and rep.counts["remove"] == 1


async def test_changed_transcode_target_writes_the_new_output(tmp_path, preparer):
    """Chain A (FLAC -> MP3) synced, then chain B (FLAC -> AAC): the device gets B's output, not A's cached
    file under B's chain hash."""
    from syncmusic.core.models import ProcessorSlot
    from syncmusic.infra import ffmpeg
    from syncmusic.libraries.folder import FolderLibrary
    from tests.media import make_music

    root = tmp_path / "lib"
    make_music(root / "Скрябін" / "01 Група крові.flac", title="Група крові", artist="Скрябін")
    lib = FolderLibrary(
        LibraryConfig(id="lib", type="folder", name="F", settings={"root": str(root)}), None
    )

    def chain(to: str) -> Chain:
        rule = {"from": ["flac"], "to": to, "when": "always"}
        return Chain.from_slots([ProcessorSlot(type="transcode", settings={"rules": [rule]})])

    dev = _flash(tmp_path)
    for to in ("mp3", "aac"):
        plan, rep = await _sync(dev, lib, preparer, chain=chain(to))
        assert rep.status == "succeeded", rep
        assert rep.counts["add" if to == "mp3" else "replace"] == 1, (to, rep.counts, plan.actions)
        (entry,) = (await dev.read_manifest()).items.values()
        path = dev.mount / entry.path
        assert (await ffmpeg.probe(path)).codec == to
        assert path.stat().st_size == entry.size
        assert entry.chain_hash == plan.actions[0].chain_hash
    plan = await _plan(dev, lib, chain("aac"))
    assert {a.kind for a in plan.actions} == {ActionKind.KEEP}


async def test_device_io_error_stops_the_sync_at_once(tmp_path, library, preparer):
    """Failing storage (EIO, user's iPod mini): the first I/O error stops the sync, no further put, no commit,
    no abort that would touch the failing storage again."""
    import errno

    mount = tmp_path / "flash"
    (mount / ".syncmusic").mkdir(parents=True)
    dev = Recording(_cfg(), mount, None)
    dev.calls = []

    async def put_track(s, item):
        dev.calls.append("put")
        raise OSError(errno.EIO, "Input/output error")

    async def abort(s):
        dev.calls.append("abort")

    dev.put_track = put_track
    dev.abort = abort
    plan = await _plan(dev, library)
    with pytest.raises(DeviceIOError, match="may be failing"):
        await Applier(dev, {"lib": library}, Chain([]), preparer, workers=1).apply(plan)
    assert dev.calls == ["put"]
    assert await dev.read_manifest() is None


@pytest.mark.parametrize("workers", [1, 3])
async def test_prepares_run_ahead_up_to_workers(tmp_path, library, preparer, monkeypatch, workers):
    """up to `workers` tracks are prepared (download + transcode) at once while one is written; never
    more, and the result is the same as one at a time."""
    import asyncio
    import contextlib

    real = preparer.prepared
    running = peak = 0

    @contextlib.asynccontextmanager
    async def slow(*a, **kw):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)  # a transcode
        running -= 1
        async with real(*a, **kw) as work:
            yield work

    monkeypatch.setattr(preparer, "prepared", slow)
    dev = _flash(tmp_path)
    plan = await _plan(dev, library)
    rep = await Applier(dev, {"lib": library}, Chain([]), preparer, workers=workers).apply(plan)
    assert rep.status == "succeeded" and rep.counts["add"] == 4
    assert peak == workers
    assert len((await dev.read_manifest()).items) == 4


def test_prepare_workers_follow_the_processing_pool():
    from syncmusic.infra.events import EventBus
    from syncmusic.infra.jobs import JobRunner

    jobs = JobRunner(EventBus(), max_workers=3)
    assert jobs.prepare_workers == 3
    jobs.resize(2)
    assert jobs.prepare_workers == 2
    jobs.set_prepare_workers(0)
    assert jobs.prepare_workers == 1
    jobs.executor.shutdown(wait=False)


async def test_checkpoints_keep_what_was_written_before_the_device_was_lost(
    tmp_path, library, preparer, monkeypatch
):
    """with a checkpoint after every file, a device lost before the third file keeps the first two; the
    next sync writes only the other two and ends like an uninterrupted sync."""
    dev = Recording(_cfg(), tmp_path / "flash", None)
    (dev.mount / ".syncmusic").mkdir(parents=True)
    dev.calls = []

    async def lost_on_third_put():
        if dev.calls.count("put") == 2 and dev.calls[-1] == "commit":
            raise DeviceLostError("unplugged")

    from types import SimpleNamespace

    from syncmusic.core import applier

    # every file takes 100 s, a commit no time: a checkpoint is due after every file
    clock = SimpleNamespace(monotonic=lambda: 100.0 * dev.calls.count("put"))
    monkeypatch.setattr(applier, "time", clock)
    with pytest.raises(DeviceLostError):
        await _sync(dev, library, preparer, checkpoint_s=1, check_device=lost_on_third_put)
    monkeypatch.undo()
    assert dev.calls == ["put", "commit", "put", "commit"]
    m = await dev.read_manifest()
    assert len(m.items) == 2
    assert (m.header.interrupted.done, m.header.interrupted.total) == (2, 4)

    dev.calls = []
    plan, rep = await _sync(dev, library, preparer)
    assert (plan.count(ActionKind.KEEP), plan.count(ActionKind.ADD)) == (2, 2)
    assert rep.status == "succeeded" and rep.counts["add"] == 2
    assert dev.calls == ["put", "put", "playlists", "commit"]  # no checkpoint due within 60 s
    assert (await dev.read_manifest()).header.interrupted is None

    ref = Recording(_cfg(), tmp_path / "ref", None)
    (ref.mount / ".syncmusic").mkdir(parents=True)
    ref.calls = []
    await _sync(ref, library, preparer)
    files = lambda root: sorted(  # noqa: E731
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and ".syncmusic" not in p.parts
    )
    assert files(dev.mount) == files(ref.mount)
    assert (await dev.read_manifest()).items.keys() == (await ref.read_manifest()).items.keys()


class CountingTrailer(FakeTrailer):
    """Records the work dirs and how many items are held between their prepare and their write."""

    live = 0
    peak = 0
    dirs: list = []

    async def run(self, item, caps):
        CountingTrailer.dirs.append(item.work_dir)
        CountingTrailer.live += 1
        CountingTrailer.peak = max(CountingTrailer.peak, CountingTrailer.live)
        await asyncio.sleep(0.05)
        return await super().run(item, caps)


class ReleasingDrive(FlashDrive):
    async def put_track(self, s, item):
        out = await super().put_track(s, item)
        CountingTrailer.live -= 1
        return out


@pytest.mark.parametrize("file_max, where", [(10**9, "mem"), (1, "spool")])
async def test_no_cache_mode(tmp_path, library, preparer, file_max, where):
    """nothing is cached; at most `files` items are held; small items in memory, large ones spooled."""
    CountingTrailer.live = CountingTrailer.peak = 0
    CountingTrailer.dirs = []
    preparer.mem_root, preparer.spool_root = tmp_path / "mem", tmp_path / "spool"
    (tmp_path / "dev" / ".syncmusic").mkdir(parents=True)
    dev = ReleasingDrive(_cfg(), tmp_path / "dev", None)
    nc = NoCache(files=2, budget_bytes=10**9, file_max_bytes=file_max)
    _, rep = await _sync(
        dev, library, preparer, chain=Chain([CountingTrailer({}, None)]), workers=8, no_cache=nc
    )
    assert rep.status == "succeeded" and rep.counts["add"] >= 3
    assert await preparer.cache.total_size() == 0
    assert 1 <= CountingTrailer.peak <= 2
    assert {d.parent for d in CountingTrailer.dirs} == {tmp_path / where}
    assert not any((tmp_path / where).iterdir())  # dropped after the write
    m = await dev.read_manifest()
    one = next(e for e in m.items.values() if "Один" in e.path)
    assert (dev.mount / one.path).read_bytes().endswith(b"TAG".ljust(128, b"\0"))


async def test_no_cache_budget_holds_one_item_at_a_time(tmp_path, library, preparer):
    CountingTrailer.live = CountingTrailer.peak = 0
    preparer.mem_root = tmp_path / "mem"
    (tmp_path / "dev" / ".syncmusic").mkdir(parents=True)
    dev = ReleasingDrive(_cfg(), tmp_path / "dev", None)
    nc = NoCache(
        files=4, budget_bytes=1, file_max_bytes=10**9
    )  # every item is above the budget alone
    _, rep = await _sync(
        dev, library, preparer, chain=Chain([CountingTrailer({}, None)]), no_cache=nc
    )
    assert rep.status == "succeeded" and CountingTrailer.peak == 1


async def test_multi_disc_release_moves_into_disc_folders(tmp_path, library, preparer, monkeypatch):
    """a release with 2+ discs goes into CD01/ CD02/ folders. A device synced with the old single
    template keeps its files until the next full sync, which moves them (replace, 'path layout changed')."""
    real = library.snapshot

    async def two_discs():
        snap = await real()
        for i, t in enumerate(sorted(snap.tracks.values(), key=lambda t: t.ref.item_id)):
            t.album, t.album_artist, t.disc_no, t.track_no = (
                "Подвійний",
                "Група",
                1 + i // 2,
                1 + i % 2,
            )
        return snap

    monkeypatch.setattr(library, "snapshot", two_discs)
    old = "Music/{album_artist}/{album}/{disc:02}{track:02} - {title}"
    mount = tmp_path / "flash"
    (mount / ".syncmusic").mkdir(parents=True)
    before = FlashDrive(_cfg(layout={"music": old, "music_multi_disc": old}), mount, None)
    _, rep = await _sync(before, library, preparer)
    assert rep.status == "succeeded"
    assert all("/CD" not in e.path for e in (await before.read_manifest()).items.values())
    now = FlashDrive(_cfg(layout={"music": old}), mount, None)  # the default multi-disc template
    plan, rep = await _sync(now, library, preparer)
    assert {(a.kind, a.reason) for a in plan.actions} == {
        (ActionKind.REPLACE, "path layout changed")
    }
    paths = sorted(e.path for e in (await now.read_manifest()).items.values())
    assert [p.rsplit("/", 2)[1] for p in paths] == ["CD01", "CD01", "CD02", "CD02"], paths
    assert all(p.startswith("Music/Група/Подвійний/CD0") for p in paths)
    assert {a.kind for a in (await _plan(now, library)).actions} == {ActionKind.KEEP}


async def test_flash_base_folder_moves_the_files(tmp_path, library, preparer):
    """the flash drive's base folder: templates start in it; a change moves the files."""
    dev = _flash(tmp_path)
    _, rep = await _sync(dev, library, preparer)
    assert rep.status == "succeeded"
    moved = FlashDrive(_cfg(base_folder="/Моє/Музика/"), dev.mount, None)
    assert moved.s.base_folder == "Моє/Музика"
    plan, rep = await _sync(moved, library, preparer)
    assert {(a.kind, a.reason) for a in plan.actions} == {
        (ActionKind.REPLACE, "path layout changed")
    }
    paths = [e.path for e in (await moved.read_manifest()).items.values()]
    assert len(paths) == 4 and all(p.startswith("Моє/Музика/Music/") for p in paths), paths
    assert not (moved.mount / "Music").exists()
    assert {a.kind for a in (await _plan(moved, library)).actions} == {ActionKind.KEEP}
    with pytest.raises(ValueError):
        FlashDrive(_cfg(base_folder="../x"), dev.mount, None)


async def test_processing_parts_are_written_and_backfilled(tmp_path, library, preparer):
    """new entries carry the per-processor parts; entries written before get them on a keep."""
    dev = _flash(tmp_path)
    chain = Chain([])
    await _sync(dev, library, preparer, chain=chain)
    m = await dev.read_manifest()
    assert all(set(e.processing) == {"device"} for e in m.items.values())
    parts = {k: e.processing for k, e in m.items.items()}
    for e in m.items.values():
        e.processing = {}
    manifest_io.write(dev.mount, m)
    plan, _ = await _sync(dev, library, preparer, chain=chain)
    assert {a.kind for a in plan.actions} == {ActionKind.KEEP}
    assert {k: e.processing for k, e in (await dev.read_manifest()).items.items()} == parts


class FakeAudio(FakeTrailer):
    """An audio-stage step that writes a new file into the work dir (a transcode stand-in)."""

    type_name = "fake_audio"
    stage = "audio"
    runs = 0

    async def run(self, item, caps):
        FakeAudio.runs += 1
        out = item.work_dir / "audio.mp3"
        out.write_bytes(item.path.read_bytes() + b"AUDIO")
        return item.model_copy(
            update={"path": out, "steps_done": [*item.steps_done, self.type_name]}
        )


async def test_tags_only_change_reprocesses_from_the_device_file(tmp_path, library, preparer):
    """a tag-stage settings change re-runs only the tag steps on the device's file - no audio step."""
    dev = _flash(tmp_path)
    await _sync(dev, library, preparer, chain=Chain([FakeAudio({}, None), FakeTrailer({}, None)]))
    FakeAudio.runs = 0
    chain = Chain([FakeAudio({}, None), FakeTrailer({"marker": "NEW"}, None)])
    plan, rep = await _sync(dev, library, preparer, chain=chain)
    assert {a.kind for a in plan.actions} == {ActionKind.REPLACE} and all(
        a.tags_only for a in plan.actions
    )
    assert rep.status == "succeeded" and rep.counts["replace"] == 4 and FakeAudio.runs == 0
    m = await dev.read_manifest()
    one = next(e for e in m.items.values() if "Один" in e.path)
    data = (dev.mount / one.path).read_bytes()
    assert data.count(b"AUDIO") == 1 and data.endswith(b"NEW".ljust(128, b"\0"))
    assert {a.kind for a in (await _plan(dev, library, chain)).actions} == {ActionKind.KEEP}
    # an audio change processes from the library again
    chain2 = Chain([FakeAudio({"marker": "v2"}, None), FakeTrailer({"marker": "NEW"}, None)])
    plan, rep = await _sync(dev, library, preparer, chain=chain2)
    assert not any(a.tags_only for a in plan.actions) and FakeAudio.runs == 4


async def test_tags_only_change_without_the_device_file_processes_fully(
    tmp_path, library, preparer
):
    dev = _flash(tmp_path)
    await _sync(dev, library, preparer, chain=Chain([FakeAudio({}, None), FakeTrailer({}, None)]))
    m = await dev.read_manifest()
    for e in m.items.values():
        (dev.mount / e.path).unlink()
        e.path = e.path + ".gone"
    manifest_io.write(dev.mount, m)
    FakeAudio.runs = 0
    chain = Chain([FakeAudio({}, None), FakeTrailer({"marker": "NEW"}, None)])
    _, rep = await _sync(dev, library, preparer, chain=chain)
    assert rep.status == "succeeded" and FakeAudio.runs == 4


class FakeRetag(FakeTrailer):
    """A tag step that rewrites its trailer in place (the tags fit their padding: same file size)."""

    type_name = "fake_retag"

    async def run(self, item, caps):
        data = item.path.read_bytes()
        marker = self.settings.marker.encode().ljust(self.TRAILER, b"\0")
        if not data.endswith(b"\0") or len(data) < self.TRAILER:
            data += marker
        else:
            data = data[: -self.TRAILER] + marker
        item.path.write_bytes(data)
        return item.model_copy(update={"steps_done": [*item.steps_done, self.type_name]})


async def test_tags_only_change_patches_the_device_file_in_place(tmp_path, library, preparer):
    """same size → only the changed bytes are written, the file is not removed and written again."""
    dev = _flash(tmp_path)
    await _sync(dev, library, preparer, chain=Chain([FakeAudio({}, None), FakeRetag({}, None)]))
    m = await dev.read_manifest()
    before = {k: (dev.mount / e.path).stat() for k, e in m.items.items()}
    chain = Chain([FakeAudio({}, None), FakeRetag({"marker": "NEW"}, None)])
    plan, rep = await _sync(dev, library, preparer, chain=chain)
    assert all(a.tags_only for a in plan.actions) and rep.counts["replace"] == 4
    m2 = await dev.read_manifest()
    for k, e in m2.items.items():
        st = (dev.mount / e.path).stat()
        assert st.st_ino == before[k].st_ino and st.st_size == before[k].st_size == e.size
        data = (dev.mount / e.path).read_bytes()
        assert data.count(b"AUDIO") == 1 and data.endswith(b"NEW".ljust(128, b"\0"))
    assert rep.bytes_written == 0
    assert not (dev.mount / ".syncmusic" / "patch").exists()
    assert {a.kind for a in (await _plan(dev, library, chain)).actions} == {ActionKind.KEEP}


def test_interrupted_patch_is_rolled_back(tmp_path):
    from syncmusic.devices._filedevice import PATCH_PATH, changed_range, patch_file, roll_back_patch

    old, new = tmp_path / "a.mp3", tmp_path / "new"
    old.write_bytes(b"x" * 100 + b"OLDTAG" + b"y" * 50)
    new.write_bytes(b"x" * 100 + b"NEWTAG" + b"y" * 50)
    assert changed_range(new, old) == (100, 103)
    assert changed_range(old, old) is None
    patch_file(tmp_path, "a.mp3", new, 100, 103)
    assert old.read_bytes() == new.read_bytes() and not (tmp_path / PATCH_PATH).exists()
    # a crash after the journal and a half-written patch: begin() restores the old bytes
    (tmp_path / PATCH_PATH).write_text(
        '{"path": "a.mp3", "offset": 100, "data": "T0xE"}'  # b"OLD"
    )
    roll_back_patch(tmp_path)
    assert old.read_bytes() == b"x" * 100 + b"OLDTAG" + b"y" * 50
    assert not (tmp_path / PATCH_PATH).exists()
    (tmp_path / PATCH_PATH).write_text('{"path": "a.mp')  # the journal itself cut short
    roll_back_patch(tmp_path)
    assert not (tmp_path / PATCH_PATH).exists() and old.read_bytes().startswith(b"x" * 100 + b"OLD")


async def test_writer_waiting_for_a_merge_shows_its_progress(
    tmp_path, library, preparer, monkeypatch
):
    """while the writer waits for a prepare that merges an audiobook, the status line says so (it
    showed the last written item for many minutes)."""
    from syncmusic.core import applier as applier_mod
    from syncmusic.core.prepare import Merge

    monkeypatch.setattr(applier_mod, "STATUS_EVERY_S", 0.01)
    real = preparer.prepared
    first: list[str] = []

    def slow(library, item, *a, **kw):
        key = item.track.ref.key()
        if not first:
            first.append(key)

        async def body():
            if key == first[0]:
                preparer.merging[key] = Merge("Книга", 7200.0, done_s=1800.0)
                await asyncio.sleep(0.1)
                preparer.merging.pop(key)
            return real(library, item, *a, **kw)

        class Ctx:
            async def __aenter__(self):
                self.cm = await body()
                return await self.cm.__aenter__()

            async def __aexit__(self, *exc):
                return await self.cm.__aexit__(*exc)

        return Ctx()

    monkeypatch.setattr(preparer, "prepared", slow)
    messages: list[str | None] = []
    preparing: list[list[str]] = []

    def progress(f, m, **kw):
        messages.append(m)
        if "preparing" in kw:
            preparing.append(kw["preparing"])

    _, rep = await _sync(_flash(tmp_path), library, preparer, progress=progress, workers=1)
    assert rep.status == "succeeded"
    assert "Combining “Книга” - 0.5 of 2.0 h" in messages
    # the sync page's "Preparing" list names the merge while it runs, and empties at the end
    assert any(p and p[0].startswith("Combining “Книга”") for p in preparing)
    assert preparing[-1] == []


async def test_merge_status_logged_once_and_moves_the_bar(tmp_path, library, preparer, monkeypatch):
    """a long merge appended its status to the job log every STATUS_EVERY_S and left the bar at the
    last item; now one log line, later statuses are log=False, and the fraction follows the merge."""
    from syncmusic.core import applier as applier_mod
    from syncmusic.core.prepare import Merge

    monkeypatch.setattr(applier_mod, "STATUS_EVERY_S", 0.01)
    real = preparer.prepared
    first: list[str] = []

    def slow(library, item, *a, **kw):
        key = item.track.ref.key()
        if not first:
            first.append(key)

        async def body():
            if key == first[0]:
                merge = Merge("Книга", 100.0)
                preparer.merging[key] = merge
                for i in range(1, 6):
                    merge.done_s = 20.0 * i
                    await asyncio.sleep(0.05)
                preparer.merging.pop(key)
            return real(library, item, *a, **kw)

        class Ctx:
            async def __aenter__(self):
                self.cm = await body()
                return await self.cm.__aenter__()

            async def __aexit__(self, *exc):
                return await self.cm.__aexit__(*exc)

        return Ctx()

    monkeypatch.setattr(preparer, "prepared", slow)
    calls: list[tuple[float | None, str | None, bool]] = []
    _, rep = await _sync(
        _flash(tmp_path),
        library,
        preparer,
        progress=lambda f, m, log=True, **kw: calls.append((f, m, log)),
        workers=1,
    )
    assert rep.status == "succeeded"
    merge = [(f, log) for f, m, log in calls if m and m.startswith("Combining")]
    assert len(merge) > 3
    assert sum(log for _, log in merge) == 1 and merge[0][1]
    fractions = [f for f, _ in merge]
    assert fractions == sorted(fractions) and fractions[-1] > fractions[0] > 0


def test_preparing_groups_conversions_and_skips_ready_slots():
    """the sync page lists the prepares in flight - conversions grouped by format pair, the rest
    counted; slots already prepared are left out."""
    from collections import deque
    from types import SimpleNamespace

    from syncmusic.core.applier import _Slot
    from syncmusic.core.models import ItemRef, PlanAction, PlanItem, PlannedFile, Track

    loop = asyncio.new_event_loop()
    try:

        def slot(i: int, src: str, dst: str, ready: bool = False) -> _Slot:
            ref = ItemRef(library_id="lib", item_id=str(i))
            item = PlanItem(
                track=Track(ref=ref, title=f"t{i}", format=src),
                file=PlannedFile(format=dst, ext=dst, size=1),
            )
            fut = loop.create_future()
            if ready:
                fut.set_result(None)
            return _Slot(PlanAction(kind=ActionKind.ADD, ref=ref, item=item), fut)

        window = deque(
            [
                slot(1, "flac", "alac"),
                slot(2, "flac", "alac"),
                slot(3, "flac", "alac", ready=True),
                slot(4, "mp3", "mp3"),
                slot(5, "opus", "aac"),
            ]
        )
        fake = SimpleNamespace(preparer=SimpleNamespace(merging={}))
        assert Applier._preparing(fake, window) == [
            "Converting 2 FLAC → ALAC",
            "Converting 1 OPUS → AAC",
            "Preparing 1 file",
        ]
    finally:
        loop.close()
