"""The database schema: a fresh install gets the baseline (schema 1), numbered migrations after
it are applied in order and end where a fresh install of the new schema would, anything else is refused with a clear message."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path

import pytest

from syncmusic.infra import db as dbmod
from syncmusic.infra.db import BASELINE, Database, SchemaError

SHIPPED = Path(dbmod.__file__).parent / "migrations"


def _open(path: Path, migrations: Path | None = None) -> None:
    async def run() -> None:
        db = Database(path, migrations)
        try:
            await db.open()
        finally:
            await db.close()

    asyncio.run(run())


def _schema(path: Path) -> set[tuple[str, str, str]]:
    """Every table, index and trigger with its columns (PRAGMA, so whitespace in the DDL does not matter)."""
    c = sqlite3.connect(path)
    out: set[tuple[str, str, str]] = set()
    for kind, name in c.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND type != 'view'"
    ):
        if kind == "table":
            cols = c.execute(f"PRAGMA table_xinfo('{name}')").fetchall()
            out.add((kind, name, repr([tuple(x)[1:] for x in cols])))
            fks = c.execute(f"PRAGMA foreign_key_list('{name}')").fetchall()
            out.add(("fk", name, repr(sorted(tuple(x)[2:] for x in fks))))
        else:
            cols = c.execute(f"PRAGMA index_xinfo('{name}')").fetchall() if kind == "index" else []
            out.add((kind, name, repr([tuple(x) for x in cols])))
    c.close()
    return out


def _version(path: Path) -> int:
    c = sqlite3.connect(path)
    (v,) = c.execute("PRAGMA user_version").fetchone()
    c.close()
    return int(v)


def test_fresh_install_is_the_baseline(tmp_path):
    _open(tmp_path / "a.db")
    assert _version(tmp_path / "a.db") == BASELINE
    _open(tmp_path / "a.db")  # opening again changes nothing
    assert _version(tmp_path / "a.db") == BASELINE
    assert not [f for f in SHIPPED.glob("[0-9]*.sql")]  # 0.2.0 ships no migrations


def test_a_newer_database_is_refused(tmp_path):
    _open(tmp_path / "a.db")
    c = sqlite3.connect(tmp_path / "a.db")
    c.execute("PRAGMA user_version = 2")
    c.close()
    with pytest.raises(SchemaError, match="newer SyncMusic"):
        _open(tmp_path / "a.db")


def test_tables_without_a_version_are_refused(tmp_path):
    c = sqlite3.connect(tmp_path / "a.db")
    c.execute("CREATE TABLE libraries (id TEXT)")
    c.close()
    with pytest.raises(SchemaError, match="no schema version"):
        _open(tmp_path / "a.db")


def test_baseline_then_migrations_equals_a_fresh_install_of_the_new_schema(tmp_path):
    """The harness for the first real migration: install the baseline, apply 002, compare with a fresh install
    whose baseline already has the change."""
    old = tmp_path / "old"
    shutil.copytree(SHIPPED, old)
    _open(tmp_path / "upgraded.db", old)
    (old / "002_note.sql").write_text("ALTER TABLE libraries ADD COLUMN note TEXT;\n")
    _open(tmp_path / "upgraded.db", old)
    assert _version(tmp_path / "upgraded.db") == 2

    new = tmp_path / "new"
    new.mkdir()
    baseline = (SHIPPED / "baseline.sql").read_text()
    (new / "baseline.sql").write_text(baseline + "\nALTER TABLE libraries ADD COLUMN note TEXT;\n")
    _open(tmp_path / "fresh.db", new)
    assert _schema(tmp_path / "upgraded.db") == _schema(tmp_path / "fresh.db")


def test_a_migration_at_or_before_the_baseline_is_an_error(tmp_path):
    folder = tmp_path / "m"
    shutil.copytree(SHIPPED, folder)
    (folder / "001_x.sql").write_text("SELECT 1;")
    with pytest.raises(SchemaError, match="not after the baseline"):
        _open(tmp_path / "a.db", folder)
