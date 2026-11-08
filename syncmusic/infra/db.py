# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite (WAL) via aiosqlite. A fresh database gets the baseline schema (`migrations/baseline.sql`, schema
version 1 = SyncMusic 0.2.0); later schema changes are numbered files after it (`002_*.sql`, ...) applied at startup, each in its own transaction, after a backup of the database (`backup_dir`). A failed migration
rolls back and stops the start. The daemon refuses a database it does not know rather than guess."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

BASELINE = 1


class SchemaError(RuntimeError):
    """The database's schema is not one this version can open."""


def _folder() -> Traversable:
    return resources.files("syncmusic.infra") / "migrations"


def _migrations(folder: Traversable | Path) -> list[tuple[int, str]]:
    """The numbered migrations after the baseline: (version, sql), in order."""
    out: list[tuple[int, str]] = []
    for entry in folder.iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:3].isdigit():
            version = int(name[:3])
            if version <= BASELINE:
                raise SchemaError(f"migration {name} is not after the baseline ({BASELINE})")
            out.append((version, entry.read_text(encoding="utf-8")))
    return sorted(out)


class Database:
    """Thin async wrapper. One connection, serialised access (SQLite is single-writer anyway)."""

    def __init__(
        self,
        path: Path | str,
        migrations: Traversable | Path | None = None,
        backup_dir: Path | None = None,
    ) -> None:
        self.path = str(path)
        self.backup_dir = backup_dir  # where a backup goes before migrations are applied
        self.migrations = migrations or _folder()  # tests pass their own folder
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database not open")
        return self._conn

    async def open(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def schema_version(self) -> int:
        """The schema's version (PRAGMA user_version; 0 = an empty database)."""
        row = await self.fetchone("PRAGMA user_version")
        return int(row[0]) if row else 0

    async def migrate(self) -> None:
        current = await self.schema_version()
        steps = _migrations(self.migrations)
        latest = steps[-1][0] if steps else BASELINE
        fresh = current == 0
        if fresh:
            if await self._has_tables():
                raise SchemaError(self._refuse("has tables but no schema version"))
            else:
                log.info("creating the database schema (baseline %d)", BASELINE)
                await self._apply((self.migrations / "baseline.sql").read_text("utf-8"), BASELINE)
                current = BASELINE
        if current > latest:
            raise SchemaError(
                self._refuse(f"is from a newer SyncMusic (schema {current} > {latest})")
            )
        pending = [(v, sql) for v, sql in steps if v > current]
        if not pending:
            return
        backup = None if fresh else await self._backup_before_migrating(current)
        for version, sql in pending:
            log.info("applying migration %03d", version)
            try:
                await self._apply(sql, version)
            except Exception as e:
                if self.conn.in_transaction:
                    await self.conn.rollback()
                where = f"; a backup from before the upgrade is {backup}" if backup else ""
                raise SchemaError(
                    f"upgrading the database {self.path} failed at migration {version:03d}: {e}. "
                    f"The database stays at schema {await self.schema_version()}{where}"
                ) from e

    async def _backup_before_migrating(self, current: int) -> Path | None:
        """A backup of a database about to be migrated (none for a fresh or in-memory one)."""
        if self.backup_dir is None or self.path == ":memory:":
            return None
        from datetime import UTC, datetime

        from syncmusic.infra import dbbackup  # imports this module

        at = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        dst = self.backup_dir / f"syncmusic-{at}-before-upgrade.sqlite3.gz"
        await asyncio.to_thread(self.backup_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(dbbackup.copy, self.path, self.backup_dir, dst)
        log.info("backed the database up to %s before upgrading from schema %d", dst, current)
        return dst

    async def _has_tables(self) -> bool:
        row = await self.fetchone(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return bool(row and row[0])

    async def _apply(self, sql: str, version: int) -> None:
        """`sql` and its version stamp in one transaction: a crash leaves the schema before or after it."""
        await self.conn.executescript(
            f"BEGIN;\n{sql}\nPRAGMA user_version = {int(version)};\nCOMMIT;"
        )

    def _refuse(self, why: str) -> str:
        return (
            f"the database {self.path} {why}: this SyncMusic cannot open it. Delete it for a fresh start, "
            "or restore a backup made by this version."
        )

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        await self.conn.execute(sql, tuple(params))
        await self.conn.commit()

    async def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        await self.conn.executemany(sql, [tuple(r) for r in rows])
        await self.conn.commit()

    async def execute_all(self, statements: Iterable[tuple[str, Iterable[Any]]]) -> None:
        """Several statements, committed together (all or nothing if the process dies in between)."""
        try:
            for sql, params in statements:
                await self.conn.execute(sql, tuple(params))
        except BaseException:
            await self.conn.rollback()
            raise
        await self.conn.commit()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()
