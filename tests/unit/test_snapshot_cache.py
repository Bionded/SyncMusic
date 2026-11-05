"""SnapshotCache: reuse while the state token is unchanged, reload otherwise."""

from __future__ import annotations

from datetime import timedelta

import pytest

from syncmusic.core.models import ItemRef, LibrarySnapshot, Track
from syncmusic.core.snapshots import SnapshotCache
from syncmusic.infra.db import Database


class CountingLibrary:
    """Just what SnapshotCache uses from a Library."""

    snapshot_max_age: timedelta | None = None

    def __init__(self, lib_id: str = "lib", account: str | None = None) -> None:
        self.id = lib_id
        self.account = account
        self.token = "t1"
        self.loads = 0

    @property
    def cache_key(self) -> str:
        return f"{self.id}@{self.account}" if self.account else self.id

    async def state_token(self) -> str:
        return self.token

    async def snapshot(self) -> LibrarySnapshot:
        self.loads += 1
        tracks = {
            str(i): Track(ref=ItemRef(library_id=self.id, item_id=str(i)), title=f"Трек {i}")
            for i in range(3)
        }
        return LibrarySnapshot(library_id=self.id, state_token=self.token, tracks=tracks)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "sd.db")
    await database.open()
    yield database
    await database.close()


async def test_unchanged_token_reuses_snapshot(db):
    lib = CountingLibrary()
    cache = SnapshotCache(db)
    a = await cache.get(lib)
    b = await cache.get(lib)
    assert lib.loads == 1 and a == b and len(b.tracks) == 3


async def test_changed_token_rescans(db):
    lib = CountingLibrary()
    cache = SnapshotCache(db)
    await cache.get(lib)
    lib.token = "t2"
    snap = await cache.get(lib)
    assert lib.loads == 2 and snap.state_token == "t2"


async def test_survives_restart_via_sqlite(db, tmp_path):
    lib = CountingLibrary()
    first = await SnapshotCache(db).get(lib)
    await db.close()
    db2 = Database(tmp_path / "sd.db")
    await db2.open()
    again = await SnapshotCache(db2).get(lib)  # new process: nothing in memory
    assert lib.loads == 1 and again == first
    await db2.close()
    await db.open()  # fixture teardown closes it


async def test_force_and_invalidate(db):
    lib = CountingLibrary()
    cache = SnapshotCache(db)
    await cache.get(lib)
    await cache.get(lib, force=True)
    assert lib.loads == 2
    await cache.invalidate(lib.id)
    assert await cache.load(lib.id) is None
    await cache.get(lib)
    assert lib.loads == 3


async def test_max_age_forces_reload(db):
    lib = CountingLibrary()
    cache = SnapshotCache(db)
    snap = await cache.get(lib)
    lib.snapshot_max_age = timedelta(minutes=10)
    assert (await cache.get(lib)) is snap  # young enough
    snap.taken_at -= timedelta(minutes=11)
    await cache.get(lib)
    assert lib.loads == 2


async def test_corrupt_row_is_ignored(db):
    lib = CountingLibrary()
    await db.execute(
        "INSERT INTO snapshots (library_id, state_token, taken_at, data) VALUES (?, ?, ?, ?)",
        ("lib", "t1", "2026-01-01T00:00:00+00:00", b"garbage"),
    )
    cache = SnapshotCache(db)
    assert await cache.load("lib") is None
    await cache.get(lib)
    assert lib.loads == 1 and (await cache.load("lib")) is not None


async def test_folder_library_through_cache(db, tmp_path):
    from syncmusic.core.models import LibraryConfig
    from syncmusic.libraries.folder import FolderLibrary
    from tests.media import make_track

    make_track(tmp_path / "m" / "a.mp3", title="a")
    lib = FolderLibrary(
        LibraryConfig(id="f", type="folder", name="F", settings={"root": str(tmp_path / "m")}), None
    )
    cache = SnapshotCache(db)
    a = await cache.get(lib)
    assert (await cache.get(lib)) is a
    make_track(tmp_path / "m" / "b.mp3", title="b")
    assert len((await cache.get(lib)).tracks) == 2


# --- browsing: stale-while-revalidate ---------------------------------------------------------------


class SlowLibrary(CountingLibrary):
    """A library whose snapshot takes until `release` is set (an user's Navidrome took ~9 s)."""

    def __init__(self) -> None:
        super().__init__()
        import asyncio

        self.release = asyncio.Event()
        self.release.set()
        self.fail = False

    async def snapshot(self) -> LibrarySnapshot:
        await self.release.wait()
        if self.fail:
            raise OSError("server gone")
        return await super().snapshot()

    def failure_reason(self, e: BaseException) -> str:
        return str(e)


def _age(cache: SnapshotCache, lib_id: str, minutes: float) -> None:
    from syncmusic.core.models import utcnow

    cache._checked[lib_id] = utcnow() - timedelta(minutes=minutes)


async def test_browse_cold_waits_then_answers_from_memory(db):
    lib = SlowLibrary()
    cache = SnapshotCache(db)
    snap, checked = await cache.browse(lib)  # cold: loads
    assert lib.loads == 1 and len(snap.tracks) == 3
    lib.token = "t2"  # a fresh cache does not even ask for the token
    again, checked2 = await cache.browse(lib)
    assert again is snap and checked2 == checked and lib.loads == 1


async def test_expired_hit_answers_at_once_and_reloads_once_in_background(db):
    import asyncio

    lib = SlowLibrary()
    cache = SnapshotCache(db)
    updated: list[str] = []
    cache.on_updated = updated.append
    first, _ = await cache.browse(lib)
    _age(cache, lib.id, 31)
    lib.token = "t2"
    lib.release.clear()  # the reload hangs until released
    answers = [await asyncio.wait_for(cache.browse(lib), 1) for _ in range(3)]
    assert all(a[0] is first for a in answers)  # no waiting, the old data
    assert (
        len([t for t in cache._refreshing.values() if not t.done()]) == 1
    )  # one reload for three requests
    lib.release.set()
    await cache._refreshing[lib.id]
    assert lib.loads == 2 and updated == [lib.id]
    snap, checked = await cache.browse(lib)
    assert snap is not first and snap.state_token == "t2"


async def test_unchanged_library_after_the_refresh_interval_sends_no_update(db):
    lib = SlowLibrary()
    cache = SnapshotCache(db)
    updated: list[str] = []
    cache.on_updated = updated.append
    await cache.browse(lib)
    _age(cache, lib.id, 31)
    await cache.browse(lib)
    await cache._refreshing[lib.id]
    assert lib.loads == 1 and updated == []  # token checked, same snapshot
    assert cache.due() == []


async def test_due_and_failed_background_refresh_keeps_the_old_data(db):
    lib = SlowLibrary()
    cache = SnapshotCache(db)
    snap, _ = await cache.browse(lib)
    assert cache.due() == []
    _age(cache, lib.id, 30)
    assert cache.due() == [lib.id]
    lib.token, lib.fail = "t2", True
    await cache.refresh_soon(lib)
    assert (await cache.browse(lib))[0] is snap and cache.due() == []  # tried; next try in 30 min


async def test_reverse_sync_expiry_keeps_browsing_and_reloads_for_sync(db):
    lib = SlowLibrary()
    cache = SnapshotCache(db)
    snap, _ = await cache.browse(lib)
    await cache.expire(lib.id)
    lib.release.clear()
    import asyncio

    assert (await asyncio.wait_for(cache.browse(lib), 1))[0] is snap  # no waiting
    lib.release.set()
    await cache._refreshing[lib.id]
    assert lib.loads == 2  # the token did not change: reloaded anyway
    assert (await cache.get(lib)) is cache._memory[lib.id] and lib.loads == 2


async def test_refresh_forces_a_reload_and_invalidate_drops_everything(db):
    lib = SlowLibrary()
    cache = SnapshotCache(db)
    await cache.browse(lib)
    await cache.get(lib, force=True)
    assert lib.loads == 2
    await cache.invalidate(lib.id)
    assert cache.checked_at(lib.id) is None
    await cache.browse(lib)  # cold again: waits
    assert lib.loads == 3


async def test_service_loop_refreshes_due_libraries_and_announces_new_data(
    config, tmp_path, monkeypatch
):
    """The sync service looks for due snapshots every SNAPSHOT_LOOP_S and refreshes them in the background; a
    reload publishes `library.updated` (the open page redraws)."""
    import asyncio

    from syncmusic.app import Application
    from syncmusic.core import sync as sync_mod
    from syncmusic.core.models import LibraryConfig
    from tests.media import make_music

    monkeypatch.setattr(sync_mod, "SNAPSHOT_LOOP_S", 0.05)
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="A")
    app = Application(config)
    await app.start()
    try:
        await app.store.put_library(
            LibraryConfig(id="m", type="folder", name="M", settings={"root": str(music)})
        )
        events = app.bus.subscribe()
        lib = await app.sync.library("m")
        snap, _ = await app.sync.snapshots.browse(lib)
        assert len(snap.tracks) == 1
        make_music(music / "b.mp3", title="B")
        _age(app.sync.snapshots, "m", 30)
        ev = await asyncio.wait_for(events.get(), 10)
        while ev.type != "library.updated":
            ev = await asyncio.wait_for(events.get(), 10)
        assert ev.data == {"library_id": "m"}
        assert len((await app.sync.snapshots.browse(lib))[0].tracks) == 2
        # load() runs again after device changes: still one loop
        loop = app.sync._snapshot_loop
        await app.sync.load()
        assert app.sync._snapshot_loop is loop
        assert sum(t.get_name() == "snapshot-refresh" for t in asyncio.all_tasks()) == 1
    finally:
        await app.stop()


async def test_counts_per_kind_from_memory_only(db):
    """the Libraries list shows what a library holds - from the snapshot in memory, never loading one."""
    from syncmusic.core.models import MediaKind, Playlist

    cache = SnapshotCache(db)
    lib = CountingLibrary()
    assert cache.counts(lib.id) is None
    snap = await cache.get(lib)
    ref = lambda i: ItemRef(library_id=lib.id, item_id=i)  # noqa: E731
    snap.tracks.update(
        {
            "b1": Track(ref=ref("b1"), title="Ч. 1", kind=MediaKind.AUDIOBOOK, album_id="book-a"),
            "b2": Track(ref=ref("b2"), title="Ч. 2", kind=MediaKind.AUDIOBOOK, album_id="book-a"),
            "b3": Track(ref=ref("b3"), title="Інша", kind=MediaKind.AUDIOBOOK, album="Book B"),
            "p1": Track(ref=ref("p1"), title="Episode", kind=MediaKind.PODCAST),
            "v1": Track(ref=ref("v1"), title="Мультик", kind=MediaKind.VIDEO),
        }
    )
    snap.playlists["pl"] = Playlist(ref=ref("pl"), name="Mix")
    assert cache.counts(lib.id) == {
        "tracks": 3,
        "books": 2,
        "episodes": 1,
        "videos": 1,
        "playlists": 1,
    }
    await cache.invalidate(lib.id)
    assert cache.counts(lib.id) is None


async def test_refresh_policy(db):
    """Settings → Libraries: hourly trusts a snapshot checked within the hour, manual until Refresh
    (force) or an expiry by a reverse sync; neither has a background check more often than its interval."""
    from syncmusic.core import snapshots as mod

    lib = CountingLibrary()
    cache = SnapshotCache(db)
    cache.policy = "hourly"
    await cache.get(lib)
    lib.token = "t2"
    assert (await cache.get(lib)).state_token == "t1" and lib.loads == 1  # within the hour
    cache._checked[lib.id] -= timedelta(hours=2)
    assert (await cache.get(lib)).state_token == "t2" and lib.loads == 2
    assert cache.interval == mod.HOURLY

    cache.policy = "manual"
    lib.token = "t3"
    cache._checked[lib.id] -= timedelta(days=3)
    assert (await cache.get(lib)).state_token == "t2" and lib.loads == 2
    assert cache.due() == [] and cache.interval is None
    assert (await cache.get(lib, force=True)).state_token == "t3"  # Refresh
    lib.token = "t4"
    await cache.expire(lib.id)
    assert (await cache.get(lib)).state_token == "t4"  # a reverse sync changed the library

    cache.policy = "sync"
    lib.token = "t5"
    assert (await cache.get(lib)).state_token == "t5"


async def test_expired_reload_with_the_same_content_announces_nothing_and_waits_for_its_gap(db):
    """a reverse sync expired the snapshot, the background reload brought the same data → no
    `on_updated` (it marked every device for an update a minute later, forever); and the snapshot loop reloads an
    expired one at most every `expired_gap`."""
    lib = SlowLibrary()
    cache = SnapshotCache(db)
    updated: list[str] = []
    cache.on_updated = updated.append
    await cache.browse(lib)
    await cache.expire(lib.id)
    assert cache.due() == []  # just checked: waits for its gap
    _age(cache, lib.id, 15)
    assert cache.due() == [lib.id]
    await cache.refresh_soon(lib)
    assert lib.loads == 2 and updated == []  # reloaded, nothing new
    await cache.expire(lib.id)
    _age(cache, lib.id, 15)
    lib.token = "t2"
    await cache.refresh_soon(lib)
    assert updated == [lib.id]  # new data is announced


async def test_device_accounts_are_cached_apart_and_go_with_their_library(db):
    """a device's own account of a library has its own snapshot (`<id>@<device>`); its tracks keep the
    plain library id; invalidating the library drops them all, `forget` only the account's; `due` skips accounts."""
    main, kid = CountingLibrary(), CountingLibrary(account="kid-ipod")
    cache = SnapshotCache(db)
    a, b = await cache.get(main), await cache.get(kid)
    assert a is not b and main.loads == kid.loads == 1
    assert next(iter(b.tracks.values())).ref.library_id == "lib"
    assert await cache.load("lib@kid-ipod") is not None
    await cache.get(kid)
    assert kid.loads == 1
    cache.policy = "hourly"
    _age(cache, "lib", 120)
    _age(cache, "lib@kid-ipod", 120)
    assert cache.due() == ["lib"]
    await cache.patch(
        "star", ItemRef(library_id="lib", item_id="1"), {"starred": True}, "lib@kid-ipod"
    )
    assert b.tracks["1"].starred and not a.tracks["1"].starred
    await cache.forget("lib@kid-ipod")
    assert await cache.load("lib@kid-ipod") is None and await cache.cached("lib") is a
    await cache.get(kid)
    await cache.invalidate("lib")
    assert await cache.load("lib") is None and await cache.load("lib@kid-ipod") is None
    assert cache.checked_at("lib@kid-ipod") is None
