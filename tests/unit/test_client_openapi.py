"""docs/dev/client-api.openapi.json is the client API exactly as the server serves it. After changing the client
API, regenerate it with `SYNCMUSIC_WRITE_OPENAPI=1 uv run pytest tests/unit/test_client_openapi.py`."""

from __future__ import annotations

import json
import os
from pathlib import Path

from syncmusic.api.routes.clients import PREFIX
from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade

DOC = Path(__file__).parents[2] / "docs" / "dev" / "client-api.openapi.json"


def _refs(node, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.add(ref.rsplit("/", 1)[-1])
        for v in node.values():
            _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)


def client_spec(full: dict) -> dict:
    """Only the client routes, and the schemas they use (transitively)."""
    paths = {p: v for p, v in full["paths"].items() if p.startswith(PREFIX + "/")}
    schemas = full.get("components", {}).get("schemas", {})
    used: set[str] = set()
    _refs(paths, used)
    todo = list(used)
    while todo:
        found: set[str] = set()
        _refs(schemas.get(todo.pop(), {}), found)
        for name in found - used:
            used.add(name)
            todo.append(name)
    return {
        "openapi": full["openapi"],
        "info": {
            "title": "SyncMusic client API",
            "version": "1",
            "description": "Wi-Fi clients pair with a SyncMusic server, sync files and send listening events."
            " Guide: docs/dev/client-protocol.md.",
        },
        "paths": paths,
        "components": {"schemas": {k: schemas[k] for k in sorted(used)}},
    }


async def test_client_openapi_matches_the_server(config):
    app = Application(config)
    spec = client_spec(create_api(AppFacade(app), manage_lifecycle=False).openapi())
    text = json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("SYNCMUSIC_WRITE_OPENAPI"):
        DOC.write_text(text, "utf-8")
    assert DOC.exists() and DOC.read_text("utf-8") == text, (
        "the client API changed: regenerate docs/dev/client-api.openapi.json (see this file's docstring)"
    )
    assert f"{PREFIX}/pair" in spec["paths"] and f"{PREFIX}/files/{{fid}}" in spec["paths"]
