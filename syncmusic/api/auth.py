# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional login: the gate in front of the API and its `/api/v1/auth` routes.

Off (the default): every request passes as before. On: the API paths need the session cookie of a login or an
`Authorization: Bearer <API token>`; `/api/v1/health` and the login routes stay open. The web UI's static files
stay open too (they hold no data): the UI asks `GET /api/v1/auth` and shows its login screen. In desktop mode a
request from this machine passes without a login.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from syncmusic.infra.auth import COOKIE, SESSION_DAYS, Auth, AuthState, verify_password

GATED = ("/api", "/openapi.json", "/docs", "/redoc")
# the client API authenticates its own requests (client tokens, `api/routes/clients.py`)
OWN_AUTH = ("/api/client/",)
OPEN = frozenset({"/api/v1/health", "/api/v1/auth", "/api/v1/auth/login", "/api/v1/auth/logout"})
MAX_DELAY_S = 30


def _local(client: Any) -> bool:
    try:
        return client is not None and ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


def _cookie(headers: dict[str, str]) -> str:
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return ""


async def signed_in(auth: Auth, state: AuthState, scope: Scope, local_ok: bool) -> bool:
    """True when the request may use the API: login off, a valid session or token, or local in desktop mode."""
    if not state.enabled or (local_ok and _local(scope.get("client"))):
        return True
    headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token and await auth.token_valid(state, token.strip()):
        return True
    cookie = _cookie(headers)
    return bool(cookie) and auth.session_valid(state, cookie)


class AuthGate:
    """ASGI middleware (inside the request guard): 401 for an API request without a login."""

    def __init__(self, app: ASGIApp, auth: Callable[[], Auth], local_ok: bool = False) -> None:
        """`auth`: the current application's (a restore restarts the application in place: a new `Auth`)."""
        self.app = app
        self.auth = auth
        self.local_ok = local_ok

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] in ("http", "websocket")
            and scope["path"].startswith(GATED)
            and scope["path"].rstrip("/") not in OPEN
            and not scope["path"].startswith(OWN_AUTH)
        ):
            auth = self.auth()
            state = await auth.state()
            if not await signed_in(auth, state, scope, self.local_ok):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                response = JSONResponse(
                    {"error": "auth_required", "message": "sign in first"}, status_code=401
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class LoginRequest(BaseModel):
    password: str


class AccessRequest(BaseModel):
    enabled: bool
    password: str | None = None  # None: keep the current one


class TokenRequest(BaseModel):
    name: str = ""


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def router(current: Callable[[], Auth], local_ok: bool = False) -> APIRouter:
    """`current`: the current application's `Auth` (see `AuthGate`)."""
    r = APIRouter(prefix="/auth")

    def secure(request: Request) -> bool:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        return proto.split(",")[0].strip() == "https"

    def set_session(request: Request, response: Response, state: AuthState) -> None:
        response.set_cookie(
            COOKIE,
            Auth.new_session(state),
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            samesite="strict",
            secure=secure(request),
            path="/",
        )

    async def view(request: Request, state: AuthState) -> dict[str, Any]:
        ok = await signed_in(current(), state, request.scope, local_ok)
        out: dict[str, Any] = {
            "enabled": state.enabled,
            "has_password": bool(state.password_hash),
            "signed_in": ok,
            "local_bypass": local_ok and _local(request.scope.get("client")),
        }
        if ok:
            out["tokens"] = [t.model_dump(mode="json", exclude={"sha256"}) for t in state.tokens]
        return out

    @r.get("")
    async def status(request: Request) -> dict[str, Any]:
        return await view(request, await current().state())

    @r.post("/login")
    async def login(body: LoginRequest, request: Request) -> Response:
        state = await current().state()
        client = request.client.host if request.client else "?"
        if not state.enabled:
            return _error(409, "conflict", "the login is off")
        if not await asyncio.to_thread(verify_password, body.password, state.password_hash):
            fails = current().failures.get(client, 0) + 1
            current().failures[client] = fails
            await asyncio.sleep(min(2 ** (fails - 1), MAX_DELAY_S))  # brute-force delay
            return _error(401, "wrong_password", "wrong password")
        current().failures.pop(client, None)
        response = JSONResponse(await view(request, state) | {"signed_in": True})
        set_session(request, response, state)
        return response

    @r.post("/logout")
    async def logout() -> Response:
        response = JSONResponse({"signed_in": False})
        response.delete_cookie(COOKIE, path="/")
        return response

    @r.post("/access")
    async def access(body: AccessRequest, request: Request) -> Response:
        """Turn the login on/off or set a new password (when on, only a signed-in user gets here: the gate)."""
        try:
            state = await current().configure(body.enabled, body.password)
        except ValueError as e:
            return _error(400, "invalid", str(e))
        response = JSONResponse(await view(request, state) | {"signed_in": True})
        if state.enabled:  # the caller stays signed in (a new password made a new secret)
            set_session(request, response, state)
        return response

    @r.post("/signout-everywhere")
    async def signout_everywhere() -> Response:
        await current().sign_out_everywhere()
        response = JSONResponse({"signed_in": False})
        response.delete_cookie(COOKIE, path="/")
        return response

    @r.post("/tokens")
    async def add_token(body: TokenRequest) -> dict[str, Any]:
        entry, token = await current().add_token(body.name)
        return entry.model_dump(mode="json", exclude={"sha256"}) | {"token": token}

    @r.delete("/tokens/{token_id}", response_model=None)
    async def revoke_token(token_id: str) -> Response | dict[str, bool]:
        if not await current().revoke_token(token_id):
            return _error(404, "not_found", "no such token")
        return {"ok": True}

    return r
