"""Save and restore the listening data of the test Navidrome around reverse-sync tests.

Subsonic can add plays but never remove them, so tests that scrobble snapshot the `annotation` (play count, play
date, rating, star - per song, album and artist) and `scrobbles` tables first and put them back exactly afterwards,
through the `sqlite3` inside the Navidrome container of the compose stack (tests/integration/services.py; SQLite
locking keeps the running server consistent). Only these two tables are touched, and in them only the rows of
one user: by default this process's (services.worker_account - every xdist worker is a user of its own, and
tests/conftest.py restores the worker's rows after every test on the stack). The test media is never involved.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from tests.integration import services

DB = "/data/navidrome.db"
TABLES = ("annotation", "scrobbles")


def _sql(script: str, *, as_json: bool = False) -> str:
    args = ["exec", "-T", "navidrome", "sqlite3", *(["-json"] if as_json else []), DB]
    # wait for the lock: Navidrome and the other workers' restores write the same database
    return services.compose(*args, input=".timeout 30000\n" + script).stdout


def available() -> bool:
    if not services.running():
        return False
    try:
        _sql("select 1;")
    except (subprocess.CalledProcessError, OSError):
        return False
    return True


def _mine(user: str | None) -> str:
    """SQL condition: rows of one Navidrome user (default: the compose tests' user of .env.test)."""
    name = (user or services.ENV["NAVIDROME_USER"]).replace("'", "''")
    return f"user_id = (select id from user where user_name = '{name}')"


def save(user: str | None = None) -> dict[str, list[dict[str, Any]]]:
    return {
        t: json.loads(_sql(f"select * from {t} where {_mine(user)};", as_json=True) or "[]")
        for t in TABLES
    }


def _literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, int | float):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def restore(state: dict[str, list[dict[str, Any]]], user: str | None = None) -> None:
    lines = ["BEGIN IMMEDIATE;"]
    for table in TABLES:
        lines.append(f"DELETE FROM {table} WHERE {_mine(user)};")
        for row in state[table]:
            cols = ", ".join(row)
            vals = ", ".join(_literal(v) for v in row.values())
            lines.append(f"INSERT INTO {table} ({cols}) VALUES ({vals});")
    lines.append("COMMIT;")
    _sql("\n".join(lines))
