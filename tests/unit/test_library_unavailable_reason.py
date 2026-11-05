"""browsing / refreshing a Navidrome or ABS library whose server times out or refuses names the reason
(503 library_unavailable, job error), never 'is not available: ' with nothing after it."""
# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import pytest

from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures
from tests.unit.test_library_unreachable import closed_url, silent_url  # noqa: F401 - fixtures


def _settings(kind: str, url: str) -> dict[str, object]:
    if kind == "navidrome":
        return {"url": url, "username": "u", "password": "p", "timeout_s": 0.3}
    return {"url": url, "api_token": "t", "timeout_s": 0.3}


async def _reasons(app, c, kind: str, url: str) -> list[str]:
    r = await c.post(
        "/api/v1/libraries",
        json={"id": "dead", "type": kind, "name": "Dead", "settings": _settings(kind, url)},
    )
    assert r.status_code == 201, r.text
    out = []
    for path in ("/libraries/dead/items?query=abc", "/libraries/dead/playlists"):
        r = await c.get("/api/v1" + path)
        assert r.status_code == 503, r.text
        assert r.json()["error"] == "library_unavailable"
        out.append(r.json()["message"])
    r = await c.post("/api/v1/libraries/dead/refresh")
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "failed"
    out.append(job.error)
    for m in out:
        assert "is not available: " in m and not m.rstrip().endswith(":"), m
    return out


@pytest.mark.parametrize("kind", ["navidrome", "audiobookshelf"])
async def test_timeout_reason(env, silent_url, kind):
    app, c = env
    for m in await _reasons(app, c, kind, silent_url):
        assert "timed out after 0.3 s" in m, m


@pytest.mark.parametrize("kind", ["navidrome", "audiobookshelf"])
async def test_refused_reason(env, closed_url, kind):
    app, c = env
    for m in await _reasons(app, c, kind, closed_url):
        assert "refused" in m.lower(), m
