# SPDX-License-Identifier: GPL-3.0-or-later
"""FastAPI app: REST API under /api/v1."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.exceptions import HTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from syncmusic import __version__
from syncmusic.api import auth as auth_routes
from syncmusic.api.events import event_stream
from syncmusic.api.guard import RequestGuard
from syncmusic.api.routes import manage as manage_routes
from syncmusic.api.routes import sync as sync_routes
from syncmusic.facade import AppFacade


def create_api(
    facade: AppFacade,
    manage_lifecycle: bool = True,
    ui: FastAPI | None = None,
    allowed_hosts: set[str] | None = None,
    local_ok: bool = False,
) -> FastAPI:
    """`ui`: the web UI (`syncmusic.webui.web_ui()`), served for every path outside `API_PATHS`; a UI with a
    lifespan of its own runs it inside the application's. `allowed_hosts`: extra Host names for the
    request guard (None: no Host check, unit tests). `local_ok`: with the optional login on, requests from this
    machine pass without one (desktop mode)."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            await facade.app.start()
        try:
            if ui is None or not hasattr(ui, "router"):
                yield
            else:
                async with ui.router.lifespan_context(ui):
                    yield
        finally:
            if manage_lifecycle:
                await facade.app.stop()

    api = FastAPI(title="SyncMusic", version=__version__, lifespan=lifespan)
    api.state.facade = facade
    api.state.stopping = asyncio.Event()  # set by ApiServer when shutting down: event streams end
    v1 = APIRouter(prefix="/api/v1")

    @v1.get("/health")
    async def health() -> dict[str, object]:
        return await facade.health()

    @v1.get("/version")
    async def version() -> dict[str, Any]:
        return facade.version()

    @v1.get("/events")
    async def events(request: Request, types: str = "") -> StreamingResponse:
        """Server-sent events; `types`: comma-separated type prefixes (`job.,sync.`), empty = every event."""
        prefixes = [t.strip() for t in types.split(",") if t.strip()] or None
        return StreamingResponse(
            event_stream(
                facade.app.bus, request.is_disconnected, types=prefixes, stop=api.state.stopping
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    v1.include_router(auth_routes.router(lambda: facade.app.auth, local_ok))
    v1.include_router(sync_routes.router(facade))
    v1.include_router(manage_routes.router(facade))
    sync_routes.install_error_handlers(api)
    api.include_router(v1)
    if ui is not None:
        api.add_middleware(_UiDispatch, ui=ui)
    # JSON (Player contents: megabytes on big devices), JS and CSS shrink 5-10x; event streams, audio and images
    # are excluded by the middleware itself, big bodies are compressed in a worker thread
    api.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    api.add_middleware(auth_routes.AuthGate, auth=lambda: facade.app.auth, local_ok=local_ok)
    api.add_middleware(RequestGuard, allowed_hosts=allowed_hosts)  # outermost: also guards the UI
    return api


class ApiServer(uvicorn.Server):
    """uvicorn server that ends the open event streams (`api.state.stopping`) before it waits for the
    connections to finish: a stop with an open web UI is quick and logs no 'timeout graceful shutdown exceeded' /
    CancelledError traceback."""

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        stopping = getattr(getattr(self.config.app, "state", None), "stopping", None)
        if isinstance(stopping, asyncio.Event):
            stopping.set()
        await super().shutdown(sockets)


# paths that stay with the API (its own 404/405 JSON errors); everything else is the UI
API_PATHS = ("/api", "/openapi.json", "/docs", "/redoc")


class _UiDispatch:
    """Sends HTTP/WebSocket requests outside the API paths to the web UI (a mount at `/` would also catch
    unknown API routes and wrong methods)."""

    def __init__(self, app: ASGIApp, ui: ASGIApp) -> None:
        self.app = app
        self.ui = ui

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not scope["path"].startswith(API_PATHS):
            try:
                await self.ui(scope, receive, send)
            except (
                HTTPException
            ) as e:  # a missing file: outside FastAPI's handlers it would be a 500
                await PlainTextResponse(e.detail, status_code=e.status_code)(scope, receive, send)
        else:
            await self.app(scope, receive, send)
