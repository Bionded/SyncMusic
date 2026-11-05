"""core/prepare.py: original fetch + processor chain + processing cache."""

from __future__ import annotations

import subprocess
import sys

import httpx
import pytest

from syncmusic.base.capabilities import (
    NON_OUTPUT_FIELDS,
    OUTPUT_FIELDS,
    CoverSpec,
    DeviceCapabilities,
    PathRules,
)
from syncmusic.base.library import body_chunks
from syncmusic.core.models import ItemRef, LibraryConfig, ProcessorSlot, Track
from syncmusic.core.prepare import Chain, Preparer, caps_hash, initial_plan_item
from syncmusic.infra import ffmpeg
from syncmusic.infra.cache import ProcessingCache
from syncmusic.infra.db import Database
from syncmusic.libraries.folder import FolderLibrary
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.fake_navidrome import FakeNavidrome, song
from tests.media import big_cover, embed_cover, make_music

CAPS = DeviceCapabilities(
    formats={"mp3", "aac", "alac"},
    cover=CoverSpec(max_size=300),
    lyrics="lrc_sidecar",
)
SLOTS = [
    ProcessorSlot(type="transcode", settings={"rules": [{"from": ["flac"], "to": "alac"}]}),
    ProcessorSlot(type="retag"),
    ProcessorSlot(type="covers", settings={"mode": "embed"}),
    ProcessorSlot(type="lyrics"),
]


@pytest.fixture
async def cache(tmp_path):
    db = Database(tmp_path / "sd.db")
    await db.open()
    yield ProcessingCache(db, tmp_path / "cache")
    await db.close()


@pytest.fixture
def ffmpeg_runs(monkeypatch):
    runs: list[list[str]] = []
    real = ffmpeg.run_ffmpeg

    async def counting(args, **kw):
        runs.append(args)
        return await real(args, **kw)

    measured = ffmpeg.run_ffmpeg_measured

    async def counting_measured(args, **kw):  # music: the transcode pass measures ReplayGain
        runs.append(args)
        return await measured(args, **kw)

    monkeypatch.setattr(ffmpeg, "run_ffmpeg", counting)
    monkeypatch.setattr(ffmpeg, "run_ffmpeg_measured", counting_measured)
    return runs


@pytest.fixture
async def folder(tmp_path):
    root = tmp_path / "lib"
    path = make_music(
        root / "Скрябін" / "01 Група крові.flac", title="Група крові", artist="Скрябін"
    )
    embed_cover(path, big_cover(800, 800))
    path.with_suffix(".lrc").write_text("[00:01.00]Тепле місце\n", encoding="utf-8")
    make_music(root / "Скрябін" / "02 Пісня.mp3", title="Пісня", artist="Скрябін")
    lib = FolderLibrary(
        LibraryConfig(id="f", type="folder", name="F", settings={"root": str(root)}), None
    )
    snap = await lib.snapshot()
    by_name = {t.path.rsplit("/", 1)[-1]: t for t in snap.tracks.values()}
    return lib, by_name, root


async def test_chain_applied_and_cached(tmp_path, cache, folder, ffmpeg_runs):
    lib, tracks, root = folder
    flac = tracks["01 Група крові.flac"]
    original = (root / flac.path).read_bytes()
    chain = Chain.from_slots(SLOTS)
    prep = Preparer(cache, tmp_path / "work")
    item = initial_plan_item(flac)
    planned = chain.plan(item, CAPS)

    async with prep.prepared(lib, item, chain, CAPS) as w:
        assert w.steps_done == ["transcode", "retag", "covers", "lyrics"]  # in chain order
        info = await ffmpeg.probe(w.path)
        assert info.codec == "alac" and info.tags["title"] == "Група крові" and info.has_cover
        assert w.plan.file.format == planned.file.format == "alac"
        assert w.plan.file.ext == planned.file.ext == "m4a"
        assert w.sidecars[".lrc"].read_text(encoding="utf-8") == "[00:01.00]Тепле місце\n"
        assert w.cover is not None and w.cover.exists()
        assert w.path.is_relative_to(cache.root)  # served from the cache entry
        assert w.notes["encoder"] == "alac"
    assert len(ffmpeg_runs) == 1
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").iterdir())
    assert (root / flac.path).read_bytes() == original  # library original untouched

    async with prep.prepared(lib, item, chain, CAPS) as w2:  # second time: cache hit
        assert w2.steps_done == ["transcode", "retag", "covers", "lyrics"]
        assert w2.sidecars[".lrc"].exists() and (await ffmpeg.probe(w2.path)).codec == "alac"
    assert len(ffmpeg_runs) == 1


async def test_changed_settings_or_source_miss(tmp_path, cache, folder, ffmpeg_runs):
    lib, tracks, _ = folder
    prep = Preparer(cache, tmp_path / "work")
    item = initial_plan_item(tracks["01 Група крові.flac"])
    chain = Chain.from_slots(SLOTS[:1])
    async with prep.prepared(lib, item, chain, CAPS):
        pass
    other = Chain.from_slots(
        [ProcessorSlot(type="transcode", settings={"rules": [{"from": ["flac"], "to": "aac"}]})]
    )
    assert other.cache_key(item, CAPS) != chain.cache_key(item, CAPS)
    assert other.chain_hash(item, CAPS) != chain.chain_hash(item, CAPS)
    async with prep.prepared(lib, item, other, CAPS) as w:
        assert w.plan.file.format == "aac"
    assert len(ffmpeg_runs) == 2
    changed = item.model_copy(
        update={"track": item.track.model_copy(update={"source_state": "new"})}
    )
    assert chain.cache_key(changed, CAPS) != chain.cache_key(item, CAPS)
    assert chain.chain_hash(changed, CAPS) == chain.chain_hash(item, CAPS)  # processing unchanged
    caps2 = CAPS.model_copy(update={"max_sample_rate": 22050})
    assert chain.cache_key(item, caps2) != chain.cache_key(item, CAPS)


async def test_unchanged_item_is_the_original(tmp_path, cache, folder):
    lib, tracks, root = folder
    mp3 = tracks["02 Пісня.mp3"]
    chain = Chain.from_slots(SLOTS[:1])  # transcode only: mp3 is supported
    async with Preparer(cache, tmp_path / "work").prepared(
        lib, initial_plan_item(mp3), chain, CAPS
    ) as w:
        assert w.path == root / mp3.path and w.steps_done == []
    assert await cache.total_size() == 0  # nothing cached for a pass-through


async def test_cancel_cleans_up(tmp_path, cache, folder):
    lib, tracks, _ = folder
    item = initial_plan_item(tracks["01 Група крові.flac"])
    chain = Chain.from_slots(SLOTS)
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        async with Preparer(cache, tmp_path / "work").prepared(
            lib, item, chain, CAPS, check_cancelled=check
        ):
            pass
    assert not any((tmp_path / "work").iterdir())
    assert await cache.get(chain.cache_key(item, CAPS)) is None


async def test_entry_pinned_while_in_use(tmp_path, cache, folder):
    lib, tracks, _ = folder
    item = initial_plan_item(tracks["01 Група крові.flac"])
    chain = Chain.from_slots(SLOTS)
    async with Preparer(cache, tmp_path / "work").prepared(lib, item, chain, CAPS) as w:
        assert await cache.evict(max_bytes=0) == []
        assert w.path.exists()
    assert await cache.evict(max_bytes=0)


def flac_song(tmp_path, sid="s1"):
    path = make_music(tmp_path / "remote.flac", title="Видалена")
    s = song(sid, "Видалена", suffix="flac", contentType="audio/flac", path="Скрябін/Видалена.flac")
    s["_data"] = path.read_bytes()
    s["size"] = len(s["_data"])
    return s


def navidrome(server):
    lib = SubsonicLibrary(
        LibraryConfig(
            id="nd",
            type="navidrome",
            name="ND",
            settings={"url": "http://nd.test", "username": "admin", "password": "pw"},
        ),
        None,
    )
    lib.transport = server
    return lib


async def test_remote_original_downloaded_once(tmp_path, cache):
    server = FakeNavidrome(native=False)
    server.add(flac_song(tmp_path))
    lib = navidrome(server)
    track = next(iter((await lib.snapshot()).tracks.values()))
    item = initial_plan_item(track)
    prep = Preparer(cache, tmp_path / "work")
    for slots in (
        SLOTS[:1],
        [ProcessorSlot(type="transcode", settings={"rules": [{"to": "aac"}]})],
    ):
        async with prep.prepared(lib, item, Chain.from_slots(slots), CAPS) as w:
            assert w.steps_done == ["transcode"]
    assert server.calls.count("download") == 1
    async with prep.prepared(lib, item, Chain([]), CAPS) as w:  # empty chain: the cached original
        assert (
            w.path.is_relative_to(cache.root) and w.path.read_bytes() == server.songs["s1"]["_data"]
        )
    assert server.calls.count("download") == 1
    await lib.close()


async def test_stale_library_size_is_not_a_failure(tmp_path, cache, caplog):
    """Regression: Navidrome served complete files smaller than the size in its database (files
    replaced, not rescanned) and every download failed. A complete body is the file now; one warning per library."""
    server = FakeNavidrome(native=False)
    s = flac_song(tmp_path)
    server.add(s)
    lib = navidrome(server)
    track = next(iter((await lib.snapshot()).tracks.values()))
    s["_data"] = s["_data"][:1000]  # the server now sends less than the snapshot said
    prep = Preparer(cache, tmp_path / "work")
    with caplog.at_level("INFO", logger="syncmusic.core.prepare"):
        for _ in range(2):
            async with prep.prepared(lib, initial_plan_item(track), Chain([]), CAPS) as w:
                assert w.path.read_bytes() == s["_data"]
            await cache.evict(0)  # the second run downloads again
    stale = [r for r in caplog.records if "library says" in r.getMessage()]
    assert stale and stale[0].levelname == "WARNING"
    assert all(r.levelname == "INFO" for r in stale[1:])
    # the wording names a stale scan, and the first mismatch starts one rescan
    assert (
        "out of date" in stale[0].getMessage()
        and "rescan of the library was started" in stale[0].getMessage()
    )
    assert server.calls.count("startScan") == 1
    await lib.close()


async def test_download_shorter_than_content_length_raises():
    async def body():
        yield b"x" * 600

    response = httpx.Response(200, headers={"content-length": "1000"}, content=body())
    with pytest.raises(OSError, match="ended early: got 600 of 1000"):
        async for _ in body_chunks(response, "s1"):
            pass


def test_initial_plan_item():
    t = Track(
        ref=ItemRef(library_id="l", item_id="i"),
        title="x",
        format="flac",
        size_bytes=100,
        sample_rate=96000,
        bit_depth=24,
        path="a/b.FLAC",
    )
    f = initial_plan_item(t).file
    assert (f.format, f.ext, f.size, f.sample_rate, f.bit_depth) == ("flac", "flac", 100, 96000, 24)
    aac = t.model_copy(update={"format": "aac", "path": None, "bit_depth": 16})
    f = initial_plan_item(aac).file
    assert (f.ext, f.bit_depth) == ("m4a", None)


def test_caps_hash_stable_across_processes():
    code = (
        "from syncmusic.base.capabilities import DeviceCapabilities as D;"
        "from syncmusic.core.prepare import caps_hash;"
        "print(caps_hash(D(formats={'mp3','aac','alac','flac','opus','ogg','wav'})))"
    )
    hashes = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("1", "2", "3")
    }
    assert len(hashes) == 1
    assert caps_hash(DeviceCapabilities(formats={"mp3"})) != caps_hash(
        DeviceCapabilities(formats={"aac"})
    )


def test_every_caps_field_is_classified():
    """a new DeviceCapabilities field must say whether it changes the produced file."""
    fields = set(DeviceCapabilities.model_fields)
    assert fields == OUTPUT_FIELDS | NON_OUTPUT_FIELDS and not OUTPUT_FIELDS & NON_OUTPUT_FIELDS
    base = DeviceCapabilities(formats={"mp3"})
    assert caps_hash(base) == caps_hash(base.model_copy(update={"max_tracks": 10, "has_rtc": True}))
    assert caps_hash(base) != caps_hash(base.model_copy(update={"max_sample_rate": 48000}))
    # a new field at its default (here: one added to PathRules) changes nothing
    assert caps_hash(base) == caps_hash(base.model_copy(update={"path_rules": PathRules()}))
    car = PathRules(max_files_per_folder=99)
    assert caps_hash(base) != caps_hash(base.model_copy(update={"path_rules": car}))


async def test_changed_transcode_with_the_planned_item_misses(tmp_path, cache, folder, ffmpeg_runs):
    """The applier passes the *planned* item (`PlanAction.item`) to `prepared()`. Changing the transcode
    target (ALAC -> AAC) must not hit the cache entry of the old chain: the device would get the old ALAC while
    the manifest records the new chain hash (found in-10-20)."""
    lib, tracks, _ = folder
    prep = Preparer(cache, tmp_path / "work")
    item = initial_plan_item(tracks["01 Група крові.flac"])
    alac = Chain.from_slots(SLOTS[:2])
    aac = Chain.from_slots(
        [
            ProcessorSlot(type="transcode", settings={"rules": [{"from": ["flac"], "to": "aac"}]}),
            SLOTS[1],
        ]
    )
    async with prep.prepared(lib, alac.plan(item, CAPS), alac, CAPS) as w:
        assert (await ffmpeg.probe(w.path)).codec == "alac"
    assert aac.cache_key(aac.plan(item, CAPS), CAPS) != alac.cache_key(alac.plan(item, CAPS), CAPS)
    async with prep.prepared(lib, aac.plan(item, CAPS), aac, CAPS) as w:
        assert w.plan.file.format == "aac"
        assert (await ffmpeg.probe(w.path)).codec == "aac"


async def test_tiny_cache_passes_through(tmp_path, cache, folder, ffmpeg_runs):
    """an output that does not fit the cache goes to the device from the work dir."""
    cache.max_bytes = 10
    lib, tracks, _root = folder
    item = initial_plan_item(tracks["01 Група крові.flac"])
    work_root = tmp_path / "work"
    async with Preparer(cache, work_root).prepared(lib, item, Chain.from_slots(SLOTS), CAPS) as w:
        assert w.path.is_relative_to(work_root) and w.path.exists()
        assert w.sidecars[".lrc"].exists()
    assert await cache.total_size() == 0
    assert not any(work_root.iterdir())


async def test_tiny_cache_remote_download_passes_through(tmp_path, cache):
    cache.max_bytes = 10
    server = FakeNavidrome(native=False)
    server.add(flac_song(tmp_path))
    lib = navidrome(server)
    item = initial_plan_item(next(iter((await lib.snapshot()).tracks.values())))
    async with Preparer(cache, tmp_path / "work").prepared(lib, item, Chain([]), CAPS) as w:
        assert w.path.read_bytes() == server.songs["s1"]["_data"]
    assert await cache.total_size() == 0
    await lib.close()


async def test_cached_original_kept_while_another_sync_stores_the_same_track(
    tmp_path, cache, monkeypatch
):
    """Two syncs prepare one remote track at once (two devices): while the first fetches the cover and lyrics, the
    second stores the same original again. The first one's cached original must stay readable meanwhile - a store
    deletes an unpinned entry before it moves the new files in."""
    import asyncio
    import threading

    from syncmusic.core.prepare import make_key

    server = FakeNavidrome(native=False)
    server.add(flac_song(tmp_path))
    lib = navidrome(server)
    track = next(iter((await lib.snapshot()).tracks.values()))
    item = initial_plan_item(track)
    prep = Preparer(cache, tmp_path / "work")
    async with prep.prepared(lib, item, Chain([]), CAPS):  # the original is cached now
        pass
    key = make_key("original", track.ref.library_id, track.ref.item_id, track.source_state)
    assert await cache.get(key) is not None
    filling, release = threading.Event(), threading.Event()
    real_fill = ProcessingCache._fill

    def slow_fill(
        self, tmp, files, move
    ):  # the second store, held between its delete and its swap-in
        filling.set()
        release.wait(10)
        return real_fill(self, tmp, files, move)

    monkeypatch.setattr(ProcessingCache, "_fill", slow_fill)
    real_inputs = Preparer._inputs
    seen: list[bool] = []

    async def inputs_while_another_stores(self, library, work, chain):
        other = tmp_path / "other.flac"
        other.write_bytes(server.songs["s1"]["_data"])
        store = asyncio.create_task(
            cache.put(key, {"original.flac": other}, {"name": "original.flac"}, move=True)
        )
        while not filling.is_set() and not store.done():  # noqa: ASYNC110 - set from the store's thread
            await asyncio.sleep(0.01)
        seen.append(work.path.is_file())  # what the first sync's processors would read now
        release.set()
        await store
        return await real_inputs(self, library, work, chain)

    monkeypatch.setattr(Preparer, "_inputs", inputs_while_another_stores)
    async with prep.prepared(lib, item, Chain.from_slots(SLOTS[:1]), CAPS) as w:
        assert w.steps_done == ["transcode"]
    assert seen == [True], "the other sync's store removed the original in use"
    await lib.close()
