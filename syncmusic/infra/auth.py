# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional login for the web UI and API (off by default, decision D12).

The state is one row of the `settings` table (key `auth`, not part of `AppSettings`: never in a settings backup):
whether a password is required, its scrypt hash, the secret that signs session cookies and the API tokens (only
their SHA-256 is stored, a token is shown once). A session cookie is `<issued unix time>.<HMAC>`; "sign out
everywhere" makes a new secret, which ends every session. `syncmusic set-password` writes the same row directly
into the database (it also works while the daemon runs: `Auth` re-reads the row every few seconds).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from syncmusic.infra.db import Database

KEY = "auth"
COOKIE = "syncmusic_session"
SESSION_DAYS = 30
RELOAD_S = (
    3.0  # how stale the cached state may be (a password set with the CLI while the daemon runs)
)
MIN_PASSWORD = 8
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}  # ~16 MB, ~50 ms on the Pi 5


class ApiToken(BaseModel):
    id: str
    name: str
    sha256: str
    created: datetime
    last_used: datetime | None = None


class AuthState(BaseModel):
    enabled: bool = False
    password_hash: str = ""
    secret: str = ""
    tokens: list[ApiToken] = []


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    b64 = base64.b64encode
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p)
        )
    except ValueError:
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    """The cached auth state and the checks the request middleware (`api.auth.AuthGate`) makes."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._state = AuthState()
        self._loaded = -RELOAD_S
        self.failures: dict[
            str, int
        ] = {}  # client address → wrong passwords in a row (login delay)

    async def state(self) -> AuthState:
        if time.monotonic() - self._loaded >= RELOAD_S:
            row = await self.db.fetchone("SELECT value FROM settings WHERE key = ?", (KEY,))
            self._state = AuthState.model_validate(json.loads(row["value"])) if row else AuthState()
            self._loaded = time.monotonic()
        return self._state

    async def save(self, state: AuthState) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (KEY, state.model_dump_json()),
        )
        self._state = state
        self._loaded = time.monotonic()

    # --- checks ------------------------------------------------------------------------------------

    @staticmethod
    def session_valid(state: AuthState, cookie: str, now: float | None = None) -> bool:
        issued, _, mac = cookie.partition(".")
        if not issued.isdigit() or not state.secret:
            return False
        now = time.time() if now is None else now
        if not 0 <= now - int(issued) <= SESSION_DAYS * 86400:
            return False
        want = hmac.new(state.secret.encode(), issued.encode(), "sha256").hexdigest()
        return hmac.compare_digest(want, mac)

    @staticmethod
    def new_session(state: AuthState) -> str:
        issued = str(int(time.time()))
        return f"{issued}.{hmac.new(state.secret.encode(), issued.encode(), 'sha256').hexdigest()}"

    async def token_valid(self, state: AuthState, token: str) -> bool:
        want = _token_hash(token)
        for t in state.tokens:
            if hmac.compare_digest(t.sha256, want):
                now = datetime.now(UTC)
                if t.last_used is None or (now - t.last_used).total_seconds() > 3600:
                    t.last_used = now  # coarse: not a database write per request
                    await self.save(state)
                return True
        return False

    # --- changes (the API routes; the callers check the user may make them) -----------------------

    async def configure(self, enabled: bool, password: str | None) -> AuthState:
        state = (await self.state()).model_copy(deep=True)
        if password is not None:
            if len(password) < MIN_PASSWORD:
                raise ValueError(f"the password needs at least {MIN_PASSWORD} characters")
            state.password_hash = hash_password(password)
            state.secret = secrets.token_hex(32)  # a new password ends the other sessions
        if enabled and not state.password_hash:
            raise ValueError("set a password first")
        if not state.secret:
            state.secret = secrets.token_hex(32)
        state.enabled = enabled
        await self.save(state)
        return state

    async def sign_out_everywhere(self) -> None:
        state = (await self.state()).model_copy(deep=True)
        state.secret = secrets.token_hex(32)
        await self.save(state)

    async def add_token(self, name: str) -> tuple[ApiToken, str]:
        state = (await self.state()).model_copy(deep=True)
        token = "smt_" + secrets.token_urlsafe(32)
        entry = ApiToken(
            id=secrets.token_hex(6),
            name=name.strip() or "token",
            sha256=_token_hash(token),
            created=datetime.now(UTC),
        )
        state.tokens.append(entry)
        await self.save(state)
        return entry, token

    async def revoke_token(self, token_id: str) -> bool:
        state = (await self.state()).model_copy(deep=True)
        kept = [t for t in state.tokens if t.id != token_id]
        if len(kept) == len(state.tokens):
            return False
        state.tokens = kept
        await self.save(state)
        return True


def set_password_offline(db_path: Path, password: str, enable: bool = True) -> None:
    """`syncmusic set-password`: write a new password (and turn the login on) straight into the database, e.g. to
    recover a lost one on the host. Ends every session; the API tokens stay."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"the password needs at least {MIN_PASSWORD} characters")
    con = sqlite3.connect(db_path, timeout=10)
    try:
        row = con.execute("SELECT value FROM settings WHERE key = ?", (KEY,)).fetchone()
        data: dict[str, Any] = json.loads(row[0]) if row else {}
        state = AuthState.model_validate(data)
        state.password_hash = hash_password(password)
        state.secret = secrets.token_hex(32)
        state.enabled = enable or state.enabled
        con.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (KEY, state.model_dump_json()),
        )
        con.commit()
    finally:
        con.close()
