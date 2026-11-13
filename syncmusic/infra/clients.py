# SPDX-License-Identifier: GPL-3.0-or-later
"""Wi-Fi clients: pairing codes and the per-client tokens.

A user opens a pairing in the UI and gets a short code (8 characters of an unambiguous alphabet, single use, a few
minutes valid, at most `MAX_OPEN_CODES` at once). A client app sends it with its own install uid and gets a device id
and a token; only the token's SHA-256 is stored. A token is valid only for the client API and only for its own device.
Pairing again with the same uid continues the device (same id, a new token). Wrong codes are rate-limited per client
address and a code that is guessed at too often is burnt. Revoking deletes the token hash, the device stays.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from syncmusic.infra.db import Database

log = logging.getLogger(__name__)

ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 8
MAX_OPEN_CODES = 3
WRONG_PER_MINUTE = 5  # wrong codes per client address and minute, then `rate_limited`
BURN_AFTER = 20  # wrong guesses while one code is open: that code is burnt
ROTATE_GRACE = timedelta(hours=24)
SEEN_EVERY_S = 60.0  # last_seen is written at most this often per client


class ClientError(Exception):
    """A refused client request: `code` is the protocol's error code, `status` the HTTP status."""

    def __init__(self, code: str, message: str, status: int, retry_after_s: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after_s = retry_after_s


class ClientInfo(BaseModel):
    """What a client says about itself when it pairs (shown in the UI, never trusted)."""

    uid: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    name: str = Field("", max_length=64)
    model: str = Field("", max_length=64)
    manufacturer: str = Field("", max_length=64)
    os: str = Field("", max_length=32)
    os_version: str = Field("", max_length=32)
    app: str = Field("", max_length=64)
    app_version: str = Field("", max_length=32)


class PairingCode(BaseModel):
    code: str
    created_at: datetime
    expires_at: datetime


class Pairing(BaseModel):
    """A paired client, as the UI lists it (no token data)."""

    device_id: str
    client_uid: str
    name: str
    client: dict[str, Any]
    capabilities: dict[str, Any]
    created_at: datetime
    last_seen: datetime | None = None
    revoked: bool = False


def normalize_code(code: str) -> str:
    """`abcd-2345` → `ABCD2345` (the dash and spaces are only for reading)."""
    return "".join(c for c in code.upper() if c.isalnum())


def show_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime) -> str:
    return t.isoformat()


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


class Clients:
    """Pairing codes and client tokens in the database; the checks the client API makes."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._wrong: dict[str, list[float]] = {}  # client address → monotonic times of wrong codes
        self._seen: dict[str, float] = {}  # device id → monotonic time last_seen was written

    # --- pairing codes (UI side) -------------------------------------------------------------------

    async def open_codes(self) -> list[PairingCode]:
        await self.db.execute(
            "DELETE FROM client_pairing_codes WHERE expires_at <= ? OR used_at IS NOT NULL "
            "OR failures >= ?",
            (_iso(_now()), BURN_AFTER),
        )
        rows = await self.db.fetchall(
            "SELECT code, created_at, expires_at FROM client_pairing_codes ORDER BY created_at"
        )
        return [
            PairingCode(
                code=r["code"],
                created_at=datetime.fromisoformat(r["created_at"]),
                expires_at=datetime.fromisoformat(r["expires_at"]),
            )
            for r in rows
        ]

    async def new_code(self, minutes: int) -> PairingCode:
        """A new pairing code; the oldest open one is dropped when `MAX_OPEN_CODES` are open."""
        codes = await self.open_codes()
        for old in codes[: max(0, len(codes) - MAX_OPEN_CODES + 1)]:
            await self.cancel_code(old.code)
        now = _now()
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        entry = PairingCode(code=code, created_at=now, expires_at=now + timedelta(minutes=minutes))
        await self.db.execute(
            "INSERT INTO client_pairing_codes (code, created_at, expires_at) VALUES (?, ?, ?)",
            (code, _iso(entry.created_at), _iso(entry.expires_at)),
        )
        return entry

    async def cancel_code(self, code: str) -> bool:
        code = normalize_code(code)
        row = await self.db.fetchone(
            "SELECT code FROM client_pairing_codes WHERE code = ?", (code,)
        )
        await self.db.execute("DELETE FROM client_pairing_codes WHERE code = ?", (code,))
        return row is not None

    async def pairing_open(self) -> bool:
        return bool(await self.open_codes())

    # --- pairing (client side) ---------------------------------------------------------------------

    def _check_rate(self, address: str) -> None:
        now = time.monotonic()
        recent = [t for t in self._wrong.get(address, []) if now - t < 60]
        self._wrong[address] = recent
        if len(recent) >= WRONG_PER_MINUTE:
            retry = int(60 - (now - recent[0])) + 1
            raise ClientError("rate_limited", "too many wrong codes, wait a minute", 429, retry)

    async def _wrong_code(self, address: str) -> None:
        self._wrong.setdefault(address, []).append(time.monotonic())
        # every open code counts the guess: a code someone guesses at is burnt after BURN_AFTER
        await self.db.execute("UPDATE client_pairing_codes SET failures = failures + 1")

    async def pair(
        self, code: str, client: ClientInfo, capabilities: dict[str, Any], address: str
    ) -> tuple[str, str, bool]:
        """Use a pairing code: `(device_id, token, continued)`; `continued` when this install paired before."""
        self._check_rate(address)
        row = await self.db.fetchone(
            "SELECT * FROM client_pairing_codes WHERE code = ?", (normalize_code(code),)
        )
        if row is None or row["used_at"] or row["failures"] >= BURN_AFTER:
            await self._wrong_code(address)
            log.warning("client pairing from %s: wrong code", address)
            raise ClientError("code_invalid", "unknown or used pairing code", 400)
        now = _now()
        if datetime.fromisoformat(row["expires_at"]) <= now:
            raise ClientError("code_expired", "the pairing code has expired", 410)
        await self.db.execute(
            "UPDATE client_pairing_codes SET used_at = ? WHERE code = ? AND used_at IS NULL",
            (_iso(now), row["code"]),
        )
        token = "smc_" + secrets.token_urlsafe(32)
        name = client.name.strip() or client.model.strip() or "Wi-Fi client"
        info = client.model_dump_json()
        caps = json.dumps(capabilities)
        old = await self.db.fetchone(
            "SELECT device_id FROM client_pairings WHERE client_uid = ?", (client.uid,)
        )
        if old is not None:
            device_id = str(old["device_id"])
            await self.db.execute(
                "UPDATE client_pairings SET client = ?, capabilities = ?, token_sha256 = ?, "
                "old_token_sha256 = NULL, old_token_until = NULL, revoked_at = NULL, last_seen = ? "
                "WHERE device_id = ?",
                (info, caps, _sha(token), _iso(now), device_id),
            )
        else:
            device_id = uuid.uuid4().hex[:12]
            await self.db.execute(
                "INSERT INTO client_pairings (device_id, client_uid, name, client, capabilities, "
                "token_sha256, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (device_id, client.uid, name, info, caps, _sha(token), _iso(now), _iso(now)),
            )
        self._wrong.pop(address, None)
        log.info(
            "client %s %s from %s (%s %s)",
            device_id,
            "paired again" if old is not None else "paired",
            address,
            client.app or "app",
            client.app_version,
        )
        return device_id, token, old is not None

    # --- tokens ------------------------------------------------------------------------------------

    async def authenticate(self, token: str) -> str:
        """The device id a client token belongs to; `ClientError` (401) otherwise. Using the new token of a
        rotation ends the old one's grace period."""
        want = _sha(token)
        now = _now()
        row = await self.db.fetchone(
            "SELECT device_id, token_sha256, old_token_sha256, old_token_until FROM client_pairings "
            "WHERE token_sha256 = ? OR old_token_sha256 = ?",
            (want, want),
        )
        if row is None:
            raise ClientError("token_revoked", "this client is not paired (any more)", 401)
        device_id = str(row["device_id"])
        if row["token_sha256"] and hmac.compare_digest(row["token_sha256"], want):
            if row["old_token_sha256"]:
                await self.db.execute(
                    "UPDATE client_pairings SET old_token_sha256 = NULL, old_token_until = NULL "
                    "WHERE device_id = ?",
                    (device_id,),
                )
        else:
            until = _dt(row["old_token_until"])
            if until is None or until <= now:
                raise ClientError("token_revoked", "this token was replaced", 401)
        await self._touch(device_id, now)
        return device_id

    async def _touch(self, device_id: str, now: datetime) -> None:
        mono = time.monotonic()
        if mono - self._seen.get(device_id, -SEEN_EVERY_S) >= SEEN_EVERY_S:
            self._seen[device_id] = mono
            await self.db.execute(
                "UPDATE client_pairings SET last_seen = ? WHERE device_id = ?",
                (_iso(now), device_id),
            )

    async def rotate(self, device_id: str) -> str:
        """A new token; the current one stays valid for `ROTATE_GRACE` or until the new one is used."""
        token = "smc_" + secrets.token_urlsafe(32)
        await self.db.execute(
            "UPDATE client_pairings SET old_token_sha256 = token_sha256, old_token_until = ?, "
            "token_sha256 = ? WHERE device_id = ?",
            (_iso(_now() + ROTATE_GRACE), _sha(token), device_id),
        )
        log.info("client %s rotated its token", device_id)
        return token

    async def revoke(self, device_id: str) -> bool:
        """The client's token stops working at once; the device and its history stay."""
        if await self.get(device_id) is None:
            return False
        await self.db.execute(
            "UPDATE client_pairings SET token_sha256 = NULL, old_token_sha256 = NULL, "
            "old_token_until = NULL, revoked_at = ? WHERE device_id = ?",
            (_iso(_now()), device_id),
        )
        log.info("client %s revoked", device_id)
        return True

    async def rename(self, device_id: str, name: str) -> bool:
        name = name.strip()[:64]
        if not name or await self.get(device_id) is None:
            return False
        await self.db.execute(
            "UPDATE client_pairings SET name = ? WHERE device_id = ?", (name, device_id)
        )
        return True

    async def update_capabilities(self, device_id: str, capabilities: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE client_pairings SET capabilities = ? WHERE device_id = ?",
            (json.dumps(capabilities), device_id),
        )

    # --- listing -----------------------------------------------------------------------------------

    @staticmethod
    def _pairing(r: Any) -> Pairing:
        return Pairing(
            device_id=r["device_id"],
            client_uid=r["client_uid"],
            name=r["name"],
            client=json.loads(r["client"]),
            capabilities=json.loads(r["capabilities"]),
            created_at=datetime.fromisoformat(r["created_at"]),
            last_seen=_dt(r["last_seen"]),
            revoked=r["token_sha256"] is None,
        )

    async def get(self, device_id: str) -> Pairing | None:
        r = await self.db.fetchone(
            "SELECT * FROM client_pairings WHERE device_id = ?", (device_id,)
        )
        return self._pairing(r) if r else None

    async def list(self) -> list[Pairing]:
        rows = await self.db.fetchall("SELECT * FROM client_pairings ORDER BY created_at")
        return [self._pairing(r) for r in rows]
