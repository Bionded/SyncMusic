"""Automatic database backups: online copy, schedule, retention, before a factory reset."""

from __future__ import annotations

import asyncio
import gzip
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from syncmusic.app import Application
from syncmusic.core.models import LibraryConfig
from syncmusic.infra import dbbackup
from syncmusic.infra.db import Database


async def _app(config):
    app = Application(config)
    await app.start()
    return app


async def test_backup_is_a_readable_copy(config, tmp_path):
    app = await _app(config)
    try:
        await app.store.put_library(
            LibraryConfig(id="m", type="folder", name="Music", settings={"root": "/x"})
        )
        path = await app.backup_db("manual")
    finally:
        await app.stop()
    assert path.parent == config.data_dir / "backups" and path.name.endswith("-manual.sqlite3.gz")
    raw = tmp_path / "restored.sqlite3"
    raw.write_bytes(gzip.decompress(path.read_bytes()))
    with sqlite3.connect(raw) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert "Music" in str(c.execute("SELECT * FROM libraries").fetchall())


def test_due_and_prune(tmp_path):
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    assert dbbackup.due(tmp_path, "daily", now)  # none yet
    assert not dbbackup.due(tmp_path, "off", now)
    for h in range(10):
        at = now - timedelta(hours=h)
        (
            tmp_path / f"syncmusic-{at:%Y%m%d-%H%M%S}-{'auto' if h == 2 else 'update'}.sqlite3.gz"
        ).write_bytes(b"x")
    assert not dbbackup.due(tmp_path, "daily", now)  # the schedule's own one is 2 h old
    assert dbbackup.due(tmp_path, "6h", now + timedelta(hours=4))
    assert dbbackup.prune(tmp_path, 7) == 3
    left = dbbackup.backups(tmp_path)
    assert len(left) == 7 and left[0][1] == now  # the newest stay


async def test_scheduled_and_retention(config):
    app = await _app(config)
    try:
        s = await app.store.app_settings()
        await app.store.put_app_settings(s.model_copy(update={"db_backup_keep": 2}))
        await app.scheduled_backup()
        await app.scheduled_backup()  # not due again
        folder = config.data_dir / "backups"
        assert [r for _, _, r in dbbackup.backups(folder)] == ["auto"]
        for _ in range(3):
            await dbbackup.make(
                app.db, folder, "manual", datetime.now(UTC) + timedelta(seconds=_ + 1)
            )
        await app.backup_db("update")
        assert len(dbbackup.backups(folder)) == 2
        s = await app.store.app_settings()
        await app.store.put_app_settings(s.model_copy(update={"db_backup_every": "off"}))
        for p, _, _ in dbbackup.backups(folder):
            p.unlink()
        await app.scheduled_backup()
        assert dbbackup.backups(folder) == []
    finally:
        await app.stop()


async def test_api_list_download_restore_round_trip(config, tmp_path):
    from httpx import ASGITransport, AsyncClient

    from syncmusic.api.server import create_api
    from syncmusic.facade import AppFacade

    facade = AppFacade(await _app(config))
    api = create_api(facade, manage_lifecycle=False)
    lib = {
        "id": "m",
        "type": "folder",
        "name": "Music",
        "settings": {"root": str(tmp_path / "music")},
    }
    (tmp_path / "music").mkdir()
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            assert (await c.get("/api/v1/db-backups")).json() == []
            r = await c.post("/api/v1/libraries", json=lib)
            assert r.status_code == 201, r.text
            r = await c.post("/api/v1/db-backups")
            assert r.status_code == 201 and r.json()["reason"] == "manual", r.text
            name = r.json()["name"]
            r = await c.get(f"/api/v1/db-backups/{name}")
            assert r.status_code == 200 and gzip.decompress(r.content)[:15] == b"SQLite format 3"
            assert (await c.get("/api/v1/db-backups/..%2Fsyncmusic.sqlite3")).status_code == 404
            assert (await c.delete("/api/v1/libraries/m")).status_code == 204
            assert (await c.get("/api/v1/libraries")).json() == []
            old = facade.app
            r = await c.post(f"/api/v1/db-backups/{name}/restore")
            assert r.status_code == 200, r.text
            assert facade.app is not old and facade.app.started
            assert [x["name"] for x in (await c.get("/api/v1/libraries")).json()] == ["Music"]
            reasons = {b["reason"] for b in (await c.get("/api/v1/db-backups")).json()}
            assert reasons == {"manual", "before-restore"}
            broken = config.data_dir / "backups" / "syncmusic-20260101-000000-manual.sqlite3.gz"
            broken.write_bytes(gzip.compress(b"not a database"))
            r = await c.post(f"/api/v1/db-backups/{broken.name}/restore")
            assert r.status_code >= 400 and "cannot be read" in r.json()["message"]
            assert facade.app.started
    finally:
        await facade.app.stop()


async def test_cancelled_backup_finishes_before_the_target_closes(tmp_path, monkeypatch):
    """a backup cancelled by the app's shutdown closed its target under sqlite3_backup_step (segfault).
    The copy and both connections live in one worker thread, which ends the copy first."""
    events: list[str] = []
    started = threading.Event()

    class Conn(sqlite3.Connection):
        def backup(self, target, **kw) -> None:
            started.set()
            time.sleep(0.3)
            super().backup(target, **kw)
            events.append("copied")

        def close(self) -> None:
            events.append("closed")
            super().close()

    db = Database(tmp_path / "db.sqlite3")
    await db.open()
    monkeypatch.setattr(dbbackup.sqlite3, "connect", lambda *a, **kw: Conn(*a, **kw))
    try:
        task = asyncio.create_task(dbbackup.make(db, tmp_path / "backups", "auto"))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(50):
            if events.count("closed") == 2:
                break
            await asyncio.sleep(0.1)
        assert events == ["copied", "closed", "closed"]
    finally:
        await db.close()


async def test_backup_does_not_use_the_app_connection(tmp_path, monkeypatch):
    """aiosqlite's Connection.backup once never returned and every job waited behind it; the backup now
    copies with its own connection while the app keeps writing."""
    db = Database(tmp_path / "db.sqlite3")
    await db.open()
    try:
        await db.conn.execute("CREATE TABLE t (x)")
        await db.conn.commit()

        async def never(*a, **kw):
            await asyncio.Event().wait()

        monkeypatch.setattr(db.conn, "backup", never)
        writes = asyncio.create_task(_write_many(db))
        path = await asyncio.wait_for(dbbackup.make(db, tmp_path / "backups", "auto"), 10)
        await writes
        assert path.is_file()
    finally:
        await db.close()


async def _write_many(db: Database) -> None:
    for i in range(200):
        await db.conn.execute("INSERT INTO t VALUES (?)", (i,))
        await db.conn.commit()


async def test_a_stuck_backup_times_out(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite3")
    await db.open()
    monkeypatch.setattr(dbbackup, "_backup", lambda *a: time.sleep(1))
    try:
        with pytest.raises(TimeoutError, match="took over"):
            await dbbackup.make(db, tmp_path / "backups", "auto", timeout=0.1)
    finally:
        await db.close()
