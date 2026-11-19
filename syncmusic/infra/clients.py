# SPDX-License-Identifier: GPL-3.0-or-later
"""Wi-Fi clients: pairing codes and the per-client tokens.

A user opens a pairing in the UI and gets a short code (8 characters of an unambiguous alphabet, single use, a few
minutes valid, at most `MAX_OPEN_CODES` at once). A client app sends it with its own install uid and gets a device id
and a token; only the token's SHA-256 is stored. A token is valid only for the client API and only for its own device.
Pairing again with the same uid continues the device (same id, a new token). Wrong codes are rate-limited per client
address and a code that is guessed at too often is burnt. Revoking deletes the token hash, the device stays.

Without a code, a client in registration mode sends a pairing request; it waits (in memory, a few minutes) until the
user approves or rejects it in the UI. A short display code on both screens tells the user which player asks; the
client polls the request with the secret it got and receives its token once, after the approval.
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
from typing import Any, Literal

from pydantic import BaseModel, Field

from syncmusic.infra.db import Database

log = logging.getLogger(__name__)

ALPHABET = "0123456789"  # digits only: typed on players with a number pad (older codes had letters)
CODE_LEN = 8
MAX_OPEN_CODES = 3
ATTEMPTS_PER_SECOND = 2  # pairing attempts (codes, requests, polls of a request) per client address
WRONG_PER_MINUTE = 5  # wrong codes per client address and minute, then `rate_limited`
BURN_AFTER = 20  # wrong guesses while one code is open: that code is burnt
ROTATE_GRACE = timedelta(hours=24)
SEEN_EVERY_S = 60.0  # last_seen is written at most this often per client
MAX_OPEN_REQUESTS = 5  # pairing requests waiting for the user at once
REQUESTS_PER_MINUTE = 3  # pairing requests per client address and minute


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


class PairRequestState(BaseModel):
    """A client waiting in registration mode for the user to approve it on the server."""

    id: str
    client: ClientInfo
    capabilities: dict[str, Any]
    address: str
    display_code: (
        str  # shown on the client and in the UI: guards against approving the wrong player
    )
    created_at: datetime
    expires_at: datetime
    secret_sha256: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    device_id: str | None = None
    token: str | None = None
    continued: bool = False

    def view(self) -> dict[str, Any]:
        """What the web UI shows (no secret, no token)."""
        return {
            "id": self.id,
            "client": self.client.model_dump(mode="json"),
            "address": self.address,
            "display_code": self.display_code,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status,
        }


def normalize_code(code: str) -> str:
    """`1234-5678` → `12345678`, `abcd-2345` → `ABCD2345` (the dash and spaces are only for reading)."""
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
        self._attempts: dict[str, list[float]] = {}  # client address → monotonic times of attempts
        self._requests: dict[str, PairRequestState] = {}  # pairing requests live in memory only
        self._asks: dict[
            str, list[float]
        ] = {}  # client address → monotonic times of pairing requests

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

    def throttle(self, address: str) -> None:
        """At most ATTEMPTS_PER_SECOND pairing attempts per client address, whatever they are."""
        now = time.monotonic()
        recent = [t for t in self._attempts.get(address, []) if now - t < 1]
        if len(recent) >= ATTEMPTS_PER_SECOND:
            self._attempts[address] = recent
            raise ClientError("rate_limited", "too many pairing attempts, wait a second", 429, 1)
        recent.append(now)
        self._attempts[address] = recent
        if len(self._attempts) > 1000:  # forget idle addresses
            self._attempts = {a: ts for a, ts in self._attempts.items() if now - ts[-1] < 1}

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
        self._wrong.pop(address, None)
        return await self._issue(client, capabilities, address)

    async def _issue(
        self, client: ClientInfo, capabilities: dict[str, Any], address: str
    ) -> tuple[str, str, bool]:
        """A token for this install: `(device_id, token, continued)` (a known uid continues its device)."""
        now = _now()
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
        log.info(
            "client %s %s from %s (%s %s)",
            device_id,
            "paired again" if old is not None else "paired",
            address,
            client.app or "app",
            client.app_version,
        )
        return device_id, token, old is not None

    # --- pairing requests (registration mode: the user approves on the server) ---------------------

    def _drop_old_requests(self) -> None:
        now = _now()
        for rid, req in list(self._requests.items()):
            # finished requests stay until the client fetched the answer, at most until expiry
            if req.expires_at <= now:
                del self._requests[rid]

    def requests(self) -> list[PairRequestState]:
        """Pending pairing requests, oldest first."""
        self._drop_old_requests()
        return [r for r in self._requests.values() if r.status == "pending"]

    def new_request(
        self, client: ClientInfo, capabilities: dict[str, Any], address: str, minutes: int
    ) -> tuple[PairRequestState, str]:
        """A client asks to be paired: `(request, secret)`; the client polls with the secret."""
        self._drop_old_requests()
        now = time.monotonic()
        recent = [t for t in self._asks.get(address, []) if now - t < 60]
        if len(recent) >= REQUESTS_PER_MINUTE:
            retry = int(60 - (now - recent[0])) + 1
            raise ClientError(
                "rate_limited", "too many pairing requests, wait a minute", 429, retry
            )
        recent.append(now)
        self._asks[address] = recent
        pending = self.requests()
        # the same install asking again replaces its own request
        for r in pending:
            if r.client.uid == client.uid:
                del self._requests[r.id]
        pending = self.requests()
        if len(pending) >= MAX_OPEN_REQUESTS:
            raise ClientError("too_many_requests", "too many pairing requests are waiting", 429, 60)
        secret = secrets.token_urlsafe(24)
        req = PairRequestState(
            id=uuid.uuid4().hex[:16],
            client=client,
            capabilities=capabilities,
            address=address,
            display_code="".join(secrets.choice("0123456789") for _ in range(4)),
            created_at=_now(),
            expires_at=_now() + timedelta(minutes=minutes),
            secret_sha256=_sha(secret),
        )
        self._requests[req.id] = req
        log.info("pairing request %s from %s (%s)", req.id, address, client.name or client.model)
        return req, secret

    def request_for_client(self, request_id: str, secret: str) -> PairRequestState:
        """The client's own request (its secret proves it); expired or unknown → `request_expired`."""
        self._drop_old_requests()
        req = self._requests.get(request_id)
        if req is None or not hmac.compare_digest(req.secret_sha256, _sha(secret)):
            raise ClientError("request_expired", "no such pairing request (expired?)", 410)
        if req.status != "pending":
            del self._requests[request_id]  # the answer is handed out once
        return req

    async def answer_request(self, request_id: str, approve: bool) -> PairRequestState | None:
        """Approve (issues the token, kept for the client's next poll) or reject a pending request."""
        self._drop_old_requests()
        req = self._requests.get(request_id)
        if req is None or req.status != "pending":
            return None
        if approve:
            req.device_id, req.token, req.continued = await self._issue(
                req.client, req.capabilities, req.address
            )
            req.status = "approved"
        else:
            req.status = "rejected"
            log.info("pairing request %s rejected", request_id)
        return req

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

    async def update_client(self, device_id: str, changes: dict[str, Any]) -> None:
        """Merge what the client reports later (its running app version, an update offer) into its info."""
        r = await self.db.fetchone(
            "SELECT client FROM client_pairings WHERE device_id = ?", (device_id,)
        )
        if r is None:
            return
        info = json.loads(r["client"]) | changes
        await self.db.execute(
            "UPDATE client_pairings SET client = ? WHERE device_id = ?",
            (json.dumps(info), device_id),
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
