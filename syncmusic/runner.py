# SPDX-License-Identifier: GPL-3.0-or-later
"""Starts the application in the configured mode (daemon / web / desktop)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import uvicorn

from syncmusic.api.guard import machine_names
from syncmusic.api.server import ApiServer, create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from syncmusic.infra.config import BootstrapConfig

log = logging.getLogger(__name__)


async def run_app(cfg: BootstrapConfig) -> Application:
    """Runs until stopped; the returned app says whether an update asked for a restart (`app.restart`)."""
    app = Application(cfg)
    facade = AppFacade(app)
    ui: Any = None
    if cfg.mode != "daemon":
        from syncmusic.webui import web_ui

        ui = web_ui()
    api = create_api(
        facade,
        ui=ui,
        allowed_hosts=machine_names() | {h.lower() for h in cfg.allowed_hosts},
        local_ok=cfg.mode == "desktop",
    )
    server = ApiServer(
        uvicorn.Config(
            api,
            host=cfg.host,
            port=cfg.port,
            log_config=None,
            lifespan="on",
            timeout_graceful_shutdown=5,  # a last resort: ApiServer ends the event streams first
        )
    )

    # an installed update stops the server (after its response went out); the caller restarts
    def restart_soon() -> None:
        asyncio.get_running_loop().call_later(0.5, setattr, server, "should_exit", True)

    app.on_restart = restart_soon
    if cfg.mode != "desktop":
        await server.serve()
        return app
    await _run_desktop(server, cfg)
    return app


async def _run_desktop(server: uvicorn.Server, cfg: BootstrapConfig) -> None:
    """Server + desktop window: closing the window stops the server (→ Application.stop); a signal that stops the
    server closes the window."""
    serving = asyncio.ensure_future(server.serve())
    while not server.started and not serving.done():  # noqa: ASYNC110 - uvicorn has no started event
        await asyncio.sleep(0.05)
    if serving.done():  # failed to start (port in use, ...)
        await serving
        return
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
    stop = asyncio.Event()
    url = f"http://{host}:{cfg.port}"
    from syncmusic.webui.window import run_window

    window = asyncio.ensure_future(run_window(url, stop, cfg.data_dir / "desktop-window"))
    await asyncio.wait({serving, window}, return_when=asyncio.FIRST_COMPLETED)
    if not serving.done():
        log.info("window closed: stopping")
        server.should_exit = True
    stop.set()
    code = await window
    await serving
    if code:  # the browser failed (no display, ...): not a normal window close
        log.error("desktop window failed (exit code %s); see the output above", code)
        raise SystemExit(1)
