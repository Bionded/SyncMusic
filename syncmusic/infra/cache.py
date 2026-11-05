# SPDX-License-Identifier: GPL-3.0-or-later
"""Processing cache with LRU eviction and a size limit.

An entry is a directory under `<cache_dir>/entries/<key[:2]>/<key>/` holding one or more files (the processed
track, its sidecars, a cover) plus caller-defined JSON `meta`. The index (size, last access) lives in SQLite
(`cache_entries`), so it survives restarts.

- `put` is atomic: files are copied into a temp dir under the cache root, then renamed into place, then indexed.
  A crash leaves at most a `.tmp-*` dir that `cleanup()` removes.
- `get` touches `last_access`; eviction removes the least recently accessed entries until the cache is under
  `max_bytes`. Entries currently `pinned` (being read by a sync) are never evicted.
- The limit holds during a sync too: `put` evicts room for the new entry *before* storing it
  and reserves its size; when the pinned entries alone leave no room, nothing is stored and `put` returns None -
  the caller uses its files as they are (pass-through, like the no-cache mode).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from syncmusic.infra.db import Database

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 20 * 1000**3
TMP_PREFIX = ".tmp-"


def make_key(*parts: Any) -> str:
    """sha256 over the JSON of `parts` (item ref, source state, chain of (type, settings hash, version))."""
    data = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def _now() -> str:
    # fixed-width UTC ISO timestamps sort correctly as text
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass
class CacheEntry:
    key: str
    dir: Path
    size: int
    meta: dict[str, Any] = field(default_factory=dict)

    def file(self, name: str) -> Path:
        return self.dir / name

    @property
    def files(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir())


class ProcessingCache:
    def __init__(self, db: Database, cache_dir: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.db = db
        self.root = cache_dir / "entries"
        self.max_bytes = max_bytes
        self._pinned: Counter[str] = Counter()
        self._reserved = 0  # bytes of puts between their room check and their index row
        self._lock = asyncio.Lock()  # serialises put/evict bookkeeping

    def _dir(self, key: str) -> Path:
        return self.root / key[:2] / key

    # --- read -------------------------------------------------------------------------------------

    async def get(self, key: str) -> CacheEntry | None:
        """The entry for `key` (touching its last access), or None. A row whose files vanished is dropped."""
        row = await self.db.fetchone(
            "SELECT path, size, meta FROM cache_entries WHERE key = ?", (key,)
        )
        if row is None:
            return None
        d = self.root / row["path"]
        if not d.is_dir():
            await self.db.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
            return None
        await self.db.execute(
            "UPDATE cache_entries SET last_access = ? WHERE key = ?", (_now(), key)
        )
        return CacheEntry(key, d, int(row["size"]), json.loads(row["meta"]))

    async def pinned_size(self) -> int:
        """Bytes of the entries a running sync holds (the only ones that may keep the total above the limit)."""
        keys = list(self._pinned)
        if not keys:
            return 0
        marks = ",".join("?" * len(keys))
        row = await self.db.fetchone(
            f"SELECT COALESCE(SUM(size), 0) FROM cache_entries WHERE key IN ({marks})", tuple(keys)
        )
        return int(row[0]) if row else 0

    async def total_size(self) -> int:
        row = await self.db.fetchone("SELECT COALESCE(SUM(size), 0) FROM cache_entries")
        return int(row[0]) if row else 0

    @asynccontextmanager
    async def pinned(self, *keys: str) -> AsyncIterator[None]:
        """Protect entries from eviction while their files are being read."""
        self._pinned.update(keys)
        try:
            yield
        finally:
            self._pinned.subtract(keys)
            self._pinned += Counter()  # drop zero counts

    # --- write ------------------------------------------------------------------------------------

    async def put(
        self,
        key: str,
        files: Mapping[str, Path],
        meta: Mapping[str, Any] | None = None,
        *,
        move: bool = False,
    ) -> CacheEntry | None:
        """Store `files` (name in the entry → source path) under `key`, replacing an existing entry.

        A `pinned` entry is being read and is kept instead (same key = same content by construction): the
        new files are discarded, so a concurrent reader never sees the entry vanish.
        With `move=True` the sources are moved instead of copied (cheap when on the same file system).
        Returns None when the entry does not fit next to the pinned entries; the sources are left untouched then.
        """
        for name in files:
            if not name or "/" in name or name in (".", ".."):
                raise ValueError(f"invalid cache file name {name!r}")
        need = sum(await asyncio.to_thread(lambda: [p.stat().st_size for p in files.values()]))
        async with self._lock:
            if self._pinned[key] and (kept := await self.get(key)) is not None:
                return kept
            await self._delete_locked(key)  # replaced anyway: its size does not count
            room = self.max_bytes - self._reserved - need
            await self._evict_locked(max_bytes=max(room, 0))
            if await self.total_size() > room:
                log.info(
                    "cache: %d bytes for %s do not fit next to pinned entries, not cached",
                    need,
                    key[:12],
                )
                return None
            self._reserved += need
        tmp = self.root / f"{TMP_PREFIX}{uuid.uuid4().hex}"
        try:
            try:
                size = await asyncio.to_thread(self._fill, tmp, files, move)
            except BaseException:
                await asyncio.to_thread(shutil.rmtree, tmp, True)
                raise
            final = self._dir(key)
            meta_json = json.dumps(dict(meta or {}), sort_keys=True, ensure_ascii=False)
            async with self._lock:
                if self._pinned[key] and (kept := await self.get(key)) is not None:
                    await asyncio.to_thread(shutil.rmtree, tmp, True)
                    return kept
                await asyncio.to_thread(self._swap_in, tmp, final)
                now = _now()
                await self.db.execute(
                    "INSERT OR REPLACE INTO cache_entries (key, path, size, created_at, last_access, meta)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (key, final.relative_to(self.root).as_posix(), size, now, now, meta_json),
                )
        finally:
            async with self._lock:
                self._reserved -= need
        return CacheEntry(key, final, size, dict(meta or {}))

    def _fill(self, tmp: Path, files: Mapping[str, Path], move: bool) -> int:
        tmp.mkdir(parents=True)
        size = 0
        for name, src in files.items():
            dst = tmp / name
            if move:
                shutil.move(src, dst)
            else:
                shutil.copyfile(src, dst)
            size += dst.stat().st_size
        return size

    @staticmethod
    def _swap_in(tmp: Path, final: Path) -> None:
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            old = final.with_name(f"{TMP_PREFIX}{uuid.uuid4().hex}")
            final.rename(old)
            tmp.rename(final)
            shutil.rmtree(old, ignore_errors=True)
        else:
            tmp.rename(final)

    async def delete(self, key: str) -> None:
        async with self._lock:
            await self._delete_locked(key)

    async def _delete_locked(self, key: str) -> None:
        row = await self.db.fetchone("SELECT path FROM cache_entries WHERE key = ?", (key,))
        await self.db.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
        if row is not None:
            await asyncio.to_thread(shutil.rmtree, self.root / row["path"], True)

    # --- eviction ---------------------------------------------------------------------------------

    async def evict(self, max_bytes: int | None = None) -> list[str]:
        """Remove least recently used entries until the total is <= max_bytes. Returns removed keys."""
        async with self._lock:
            return await self._evict_locked(max_bytes=max_bytes)

    async def _evict_locked(
        self, *, protect: Iterable[str] = (), max_bytes: int | None = None
    ) -> list[str]:
        limit = self.max_bytes if max_bytes is None else max_bytes
        total = await self.total_size()
        if total <= limit:
            return []
        keep = set(protect) | set(self._pinned)
        removed: list[str] = []
        for row in await self.db.fetchall(
            "SELECT key, size FROM cache_entries ORDER BY last_access, created_at"
        ):
            if total <= limit:
                break
            if row["key"] in keep:
                continue
            await self._delete_locked(row["key"])
            total -= int(row["size"])
            removed.append(row["key"])
        if removed:
            log.info("cache: evicted %d entries, %d bytes left", len(removed), total)
        return removed

    async def cleanup(self) -> tuple[int, int]:
        """Remove leftovers: temp dirs, directories without an index row, rows without a directory.

        Returns (orphan dirs removed, stale rows removed). Run at startup.
        """
        async with self._lock:
            rows = await self.db.fetchall("SELECT key, path FROM cache_entries")
            known = {r["path"]: r["key"] for r in rows}
            orphan_dirs = await asyncio.to_thread(self._remove_orphans, set(known))
            stale = [k for p, k in known.items() if not (self.root / p).is_dir()]
            for key in stale:
                await self.db.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
        return orphan_dirs, len(stale)

    def _remove_orphans(self, known: set[str]) -> int:
        if not self.root.is_dir():
            return 0
        removed = 0
        for top in self.root.iterdir():
            if top.name.startswith(TMP_PREFIX) or not top.is_dir():
                _remove(top)
                removed += 1
                continue
            for d in top.iterdir():
                if f"{top.name}/{d.name}" not in known:
                    _remove(d)
                    removed += 1
            if not any(top.iterdir()):
                top.rmdir()
        return removed


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with suppress(OSError):
            os.unlink(path)
