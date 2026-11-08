"""infra/cache.py: processing cache with LRU eviction."""

from __future__ import annotations

import asyncio
import shutil

import pytest

from syncmusic.infra import cache as cache_mod
from syncmusic.infra.cache import ProcessingCache, make_key
from syncmusic.infra.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "sd.db")
    await database.open()
    yield database
    await database.close()


def blob(tmp_path, name: str, size: int):
    p = tmp_path / "src" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


async def test_put_get_round_trip(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache")
    src = blob(tmp_path, "Трек.m4a", 100)
    lrc = blob(tmp_path, "Трек.lrc", 10)
    key = make_key("lib", "item", "100:1", [("transcode", "abc", 1)])
    e = await c.put(key, {"track.m4a": src, "track.lrc": lrc}, {"format": "alac"})
    assert e.size == 110
    got = await c.get(key)
    assert got is not None
    assert got.meta == {"format": "alac"}
    assert got.files == ["track.lrc", "track.m4a"]
    assert got.file("track.m4a").read_bytes() == src.read_bytes()
    assert src.exists()  # copied, not moved
    assert await c.get("nope") is None


async def test_move_put(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache")
    src = blob(tmp_path, "a.mp3", 5)
    e = await c.put("k", {"a.mp3": src}, move=True)
    assert not src.exists()
    assert e.file("a.mp3").read_bytes() == b"xxxxx"


def test_key_changes_with_every_part():
    base = ("lib", "item", "100:1", [("transcode", "s1", 1)])
    k = make_key(*base)
    assert k == make_key(*base)
    assert make_key("lib", "item", "100:2", [("transcode", "s1", 1)]) != k  # source state
    assert make_key("lib", "item", "100:1", [("transcode", "s2", 1)]) != k  # settings
    assert make_key("lib", "item", "100:1", [("transcode", "s1", 2)]) != k  # processor version
    assert make_key("lib", "item", "100:1", []) != k  # chain


async def test_lru_eviction_by_last_access(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache", max_bytes=300)
    for k in ("a", "b", "c"):
        await c.put(k, {"f": blob(tmp_path, k, 100)})
    assert await c.get("a") is not None  # a is now the most recently used
    await c.put("d", {"f": blob(tmp_path, "d", 100)})  # 400 > 300: evict LRU = b
    assert await c.get("b") is None
    assert {k for k in "acd" if await c.get(k)} == set("acd")
    assert await c.total_size() == 300
    assert not (c.root / "b"[:2] / "b").exists()


async def test_eviction_skips_pinned_entries(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache", max_bytes=150)
    await c.put("a", {"f": blob(tmp_path, "a", 100)})
    async with c.pinned("a"):
        # a is pinned and b would push the total over the limit: b is not cached
        assert await c.put("b", {"f": blob(tmp_path, "b", 100)}) is None
        assert await c.get("a") is not None
    assert await c.put("b", {"f": blob(tmp_path, "b", 100)}) is not None  # unpinned: a goes
    assert await c.get("a") is None


async def test_evict_to_explicit_limit(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache")
    for k in ("a", "b"):
        await c.put(k, {"f": blob(tmp_path, k, 100)})
    assert await c.evict(max_bytes=0) == ["a", "b"]
    assert await c.total_size() == 0


async def test_atomic_put_leaves_nothing_on_failure(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache")
    good = blob(tmp_path, "good", 10)
    with pytest.raises(FileNotFoundError):
        await c.put("k", {"a": good, "b": tmp_path / "missing"})
    assert await c.get("k") is None
    assert not c.root.exists() or list(c.root.iterdir()) == []  # temp dir removed


async def test_put_replaces_existing_entry(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache")
    await c.put("k", {"old": blob(tmp_path, "o", 10)})
    e = await c.put("k", {"new": blob(tmp_path, "n", 20)})
    assert e.files == ["new"]
    assert await c.total_size() == 20


async def test_invalid_names_rejected(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache")
    for name in ("", "../x", "a/b", ".."):
        with pytest.raises(ValueError):
            await c.put("k", {name: blob(tmp_path, "s", 1)})


async def test_restart_keeps_index_and_cleanup(tmp_path):
    db = Database(tmp_path / "sd.db")
    await db.open()
    c = ProcessingCache(db, tmp_path / "cache")
    await c.put("keep", {"f": blob(tmp_path, "k", 10)})
    await c.put("gone", {"f": blob(tmp_path, "g", 10)})
    await db.close()

    # crash leftovers: temp dir, unindexed dir; and a row whose dir vanished
    (c.root / f"{cache_mod.TMP_PREFIX}dead").mkdir()
    (c.root / "zz" / "zzunknown").mkdir(parents=True)
    shutil.rmtree(c._dir("gone"))

    db2 = Database(tmp_path / "sd.db")
    await db2.open()
    c2 = ProcessingCache(db2, tmp_path / "cache")
    assert await c2.cleanup() == (2, 1)
    assert (await c2.get("keep")) is not None
    assert await c2.get("gone") is None
    assert not (c.root / "zz").exists()
    assert await c2.total_size() == 10
    await db2.close()


async def test_concurrent_puts_same_key(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache")
    srcs = [blob(tmp_path, f"s{i}", 10 + i) for i in range(5)]
    await asyncio.gather(*(c.put("k", {"f": s}) for s in srcs))
    e = await c.get("k")
    assert e is not None and e.files == ["f"]
    assert await c.total_size() == e.size
    assert [p.name for p in c.root.iterdir()] == ["k"]  # no temp leftovers


async def test_put_keeps_a_pinned_entry(db, tmp_path):
    """a pinned entry is being read; a second put of the key keeps it and drops the new files."""
    c = ProcessingCache(db, tmp_path / "cache")
    first = await c.put("k", {"old": blob(tmp_path, "o", 10)}, {"v": 1})
    async with c.pinned("k"):
        e = await c.put("k", {"new": blob(tmp_path, "n", 20)}, {"v": 2})
    assert e.dir == first.dir and e.files == ["old"] and e.meta == {"v": 1}
    assert await c.total_size() == 10
    assert [p.name for p in c.root.iterdir()] == [first.dir.parent.name]  # no temp dir left


async def test_put_evicts_before_storing_and_never_exceeds(db, tmp_path):
    """the total never passes the limit, also not for a moment after a put."""
    c = ProcessingCache(db, tmp_path / "cache", max_bytes=300)
    for k in "abc":
        await c.put(k, {"f": blob(tmp_path, k, 100)})
    async with c.pinned("a", "b"):
        e = await c.put("d", {"f": blob(tmp_path, "d", 100)})
        assert e is not None and await c.total_size() == 300
        assert await c.get("c") is None


async def test_put_passes_through_when_pinned_entries_fill_the_cache(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache", max_bytes=250)
    for k in "ab":
        await c.put(k, {"f": blob(tmp_path, k, 100)})
    src = blob(tmp_path, "big", 100)
    async with c.pinned("a", "b"):
        assert await c.put("big", {"f": src}, move=True) is None
    assert src.exists()  # left to the caller
    assert await c.total_size() == 200 and await c.get("big") is None
    assert (
        await c.put("huge", {"f": blob(tmp_path, "h", 300)}) is None
    )  # larger than the whole cache


async def test_running_total_matches_the_index(db, tmp_path):
    c = ProcessingCache(db, tmp_path / "cache")

    async def db_sum() -> int:
        row = await db.fetchone("SELECT COALESCE(SUM(size), 0) FROM cache_entries")
        return int(row[0])

    for i in range(5):
        await c.put(f"k{i}", {"a": blob(tmp_path, f"a{i}", 10 * (i + 1))})
    assert await c.total_size() == await db_sum() == 150
    await c.put("k0", {"a": blob(tmp_path, "big", 100)})  # replaced
    assert await c.total_size() == await db_sum() == 240
    await c.delete("k1")
    assert await c.total_size() == await db_sum() == 220
    shutil.rmtree((await c.get("k2")).dir)  # vanished files drop the row on the next read
    assert await c.get("k2") is None
    assert await c.total_size() == await db_sum() == 190
    await c.evict(max_bytes=150)
    assert await c.total_size() == await db_sum() <= 150
