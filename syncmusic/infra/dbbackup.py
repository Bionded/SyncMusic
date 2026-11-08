# SPDX-License-Identifier: GPL-3.0-or-later
"""Automatic database backups: SQLite's online backup (consistent while the app runs) into a
gzip file `syncmusic-<UTC time>-<reason>.sqlite3.gz`. Made on a schedule, before an update and before a factory
reset; the oldest beyond `keep` are deleted. Secrets stay in the copy as they are in the database."""

from __future__ import annotations

import asyncio
import gzip
import re
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from syncmusic.infra.db import Database

EVERY = {"6h": timedelta(hours=6), "daily": timedelta(days=1), "weekly": timedelta(days=7)}
NAME = re.compile(r"^syncmusic-(\d{8}-\d{6})-([a-z-]+)\.sqlite3\.gz$")


def backups(folder: Path) -> list[tuple[Path, datetime, str]]:
    """(path, when, reason) of the backups in `folder`, newest first."""
    out = []
    for p in folder.glob("syncmusic-*.sqlite3.gz") if folder.is_dir() else []:
        m = NAME.match(p.name)
        if m:
            at = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
            out.append((p, at, m.group(2)))
    return sorted(out, key=lambda b: b[1], reverse=True)


def due(folder: Path, every: str, now: datetime) -> bool:
    """A scheduled backup is due: none made by the schedule yet, or the newest one is `every` old."""
    if every not in EVERY:
        return False
    last = next((at for _, at, reason in backups(folder) if reason == "auto"), None)
    return last is None or now - last >= EVERY[every]


def prune(folder: Path, keep: int) -> int:
    """Delete all but the newest `keep` backups; returns how many went."""
    old = backups(folder)[keep:]
    for p, _, _ in old:
        p.unlink(missing_ok=True)
    return len(old)


def _compress(src: Path, dst: Path) -> None:
    tmp = dst.with_suffix(".part")
    with open(src, "rb") as f, gzip.open(tmp, "wb", compresslevel=6) as g:
        shutil.copyfileobj(f, g)
    tmp.chmod(0o600)  # the database holds passwords
    tmp.replace(dst)


TIMEOUT_S = 600.0


def copy(source: str, folder: Path, dst: Path) -> None:
    """Copy `source` with its own connection (WAL: consistent while the app writes) and compress it - all in this
    thread, so neither a cancel nor a timeout closes a connection under sqlite3_backup_step."""
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        raw = Path(tmp) / "db.sqlite3"
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(raw)
        try:
            src.backup(target)
        finally:
            target.close()
            src.close()
        _compress(raw, dst)


async def make(
    db: Database, folder: Path, reason: str, now: datetime | None = None, timeout: float = TIMEOUT_S
) -> Path:
    """Back the open database up into `folder` (created) and return the file. The copy never goes through the
    app's aiosqlite connection: a backup there once never returned and stalled every job."""
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    at = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    dst = folder / f"syncmusic-{at}-{reason}.sqlite3.gz"
    if db.path == ":memory:":  # tests: no file to open a second connection on
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            raw = Path(tmp) / "db.sqlite3"
            target = sqlite3.connect(raw, check_same_thread=False)
            try:
                await db.conn.backup(target)
            finally:
                target.close()
            await asyncio.to_thread(_compress, raw, dst)
        return dst
    work = asyncio.ensure_future(asyncio.to_thread(copy, db.path, folder, dst))
    try:
        await asyncio.wait_for(asyncio.shield(work), timeout)
    except TimeoutError:
        raise TimeoutError(f"database backup to {dst} took over {timeout:.0f} s") from None
    return dst


def find(folder: Path, name: str) -> Path | None:
    """The backup called `name` in `folder` (only names this module makes: no paths)."""
    path = folder / name
    return path if NAME.match(name) and path.is_file() else None


def unpack(src: Path, dst: Path) -> None:
    """Decompress a backup to `dst` and check it is an intact SyncMusic database (ValueError otherwise)."""
    try:
        with gzip.open(src, "rb") as g, open(dst, "wb") as f:
            shutil.copyfileobj(g, f)
        with sqlite3.connect(dst) as c:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.close()
    except (OSError, EOFError, sqlite3.DatabaseError) as e:
        dst.unlink(missing_ok=True)
        raise ValueError(f"the backup cannot be read: {e}") from e
    if not ok or not {"libraries", "devices"} <= tables:
        dst.unlink(missing_ok=True)
        raise ValueError("the backup is not an intact SyncMusic database")
