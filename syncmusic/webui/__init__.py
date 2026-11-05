# SPDX-License-Identifier: GPL-3.0-or-later
"""Web UI: static HTML/CSS/vanilla JS served next to the REST API, no build step.

`static/index.html` is the shell (sidebar / bottom navigation, hash routing); `static/js/` holds the modules:
`api.js` (REST + server-sent events), `dom.js` (escaping HTML templates), `ui.js` (shared components), `i18n.js`
(strings), `router.js` and one module per screen in `views/`. Every value from the API is rendered through the
escaping `html` template tag.
"""

from __future__ import annotations

from pathlib import Path

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC_DIR = Path(__file__).parent / "static"


class _RevalidatedFiles(StaticFiles):
    """Static files the browser revalidates (ETag → 304) on every load: without a Cache-Control header it caches them
    heuristically and, after an update, runs new JS with an old stylesheet."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def web_ui() -> StaticFiles:
    """The ASGI app serving the UI (`/` → index.html); mounted by `create_api(ui=...)` outside the API paths."""
    return _RevalidatedFiles(directory=STATIC_DIR, html=True)
