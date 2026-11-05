"""Update check against the GitHub releases: version order, channels, assets, ETag reuse, failures."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from syncmusic import _build
from syncmusic.infra import updates
from tests.unit.test_api_sync import env, music  # noqa: F401

DATA = json.loads((Path(__file__).parent / "data" / "github_releases.json").read_text())


def test_version_order():
    order = ["0.2.0", "v0.2.1-3", "0.2.1-10", "v0.2.1", "0.2.2-1", "0.10.0"]
    keys = [updates.version_key(v) for v in order]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert updates.version_key("nightly") is None and updates.version_key("v1.2-beta") is None
    assert updates.is_newer("0.2.1-3", "0.2.0") and not updates.is_newer("0.2.1-3", "0.2.1")
    assert not updates.is_newer("0.2.1", "0.2.1") and updates.is_newer("0.2.1", "garbage")


def test_channels_and_parsing():
    releases = updates.parse_releases(DATA)
    assert [r.version for r in releases] == [
        "0.2.2-4",
        "0.2.1",
        "0.2.1-10",
        "0.2.0",
    ]  # no draft, no "nightly"
    stable = updates.newest(releases, "stable")
    assert stable is not None and stable.version == "0.2.1" and stable.date == "2026-11-10"
    assert stable.notes == ["Covers on the Classic", "Rockbox playlists"]
    beta = updates.newest(releases, "beta")
    assert beta is not None and beta.version == "0.2.2-4" and beta.prerelease
    assert updates.newest([], "stable") is None and updates.parse_releases({"message": "x"}) == []


def test_asset_for_install_type():
    r = updates.newest(updates.parse_releases(DATA), "stable")
    assert r is not None
    assert updates.asset_for(r, "standalone", "aarch64")["url"] == "https://example.org/web-a"
    assert updates.asset_for(r, "standalone", "amd64")["url"] == "https://example.org/web-x"
    assert updates.asset_for(r, "desktop", "arm64")["url"] == "https://example.org/desk-a"
    assert updates.asset_for(r, "desktop", "x86_64") is None
    assert updates.asset_for(r, "service", "aarch64")["url"] == "https://example.org/web-a"
    assert updates.asset_for(r, "docker") is None and updates.asset_for(r, "source") is None


def test_running_version_and_install_type(monkeypatch, tmp_path):
    monkeypatch.setattr(_build, "BUILD_NUMBER", None)
    assert updates.running_version() == updates.__version__
    monkeypatch.setattr(_build, "BUILD_NUMBER", 7)
    assert updates.running_version() == f"{updates.__version__}-7"
    monkeypatch.setattr(_build, "INSTALL_TYPE", "desktop")
    assert updates.install_type() == "desktop"
    monkeypatch.setenv("SYNCMUSIC_INSTALL_TYPE", "service")  # the unit of `syncmusic --install`
    assert updates.install_type() == "service"
    monkeypatch.setenv("SYNCMUSIC_INSTALL_TYPE", "bogus")
    assert updates.install_type() == "desktop"
    monkeypatch.delenv("SYNCMUSIC_INSTALL_TYPE")
    monkeypatch.setattr(_build, "INSTALL_TYPE", None)
    monkeypatch.delenv("SYNCMUSIC_IN_DOCKER", raising=False)
    assert updates.install_type() == "source"
    monkeypatch.setenv("SYNCMUSIC_IN_DOCKER", "1")
    assert updates.install_type() == "docker"


class FakeGitHub(httpx.AsyncBaseTransport):
    def __init__(self, status=200, body=DATA):
        self.status, self.body, self.seen = status, body, []

    async def handle_async_request(self, request):
        self.seen.append(request)
        if request.headers.get("if-none-match") == '"e1"':
            return httpx.Response(304)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "rate limited"})
        return httpx.Response(200, json=self.body, headers={"ETag": '"e1"'})


async def test_check_reuses_the_etag(tmp_path, monkeypatch):
    monkeypatch.setattr(_build, "BUILD_NUMBER", None)
    monkeypatch.setattr(updates, "__version__", "0.2.0")
    monkeypatch.setattr(updates, "running_version", lambda: "0.2.0")
    gh = FakeGitHub()
    checker = updates.Checker(tmp_path / "cache" / "rel.json", transport=gh)
    s = await checker.check("example/SyncMusic", "stable")
    assert (
        s.available
        and s.latest == "0.2.1"
        and s.notes[0] == "Covers on the Classic"
        and not s.error
    )
    assert gh.seen[0].url.path == "/repos/example/SyncMusic/releases"
    assert "if-none-match" not in gh.seen[0].headers
    b = await checker.check("example/SyncMusic", "beta")  # answered from the cache: 304
    assert b.latest == "0.2.2-4" and gh.seen[1].headers["if-none-match"] == '"e1"'
    assert (
        await updates.Checker(tmp_path / "x.json", transport=gh).check("e/S", "nonsense")
    ).channel == "stable"


async def test_a_releases_url_instead_of_a_repository(tmp_path, monkeypatch):
    """a mirror (or a local fake release in tests): the URL as it is, same JSON shape."""
    monkeypatch.setattr(updates, "running_version", lambda: "0.2.0")
    gh = FakeGitHub()
    checker = updates.Checker(tmp_path / "rel.json", transport=gh)
    s = await checker.check("http://mirror.test:8080/releases.json", "stable")
    assert s.latest == "0.2.1" and not s.error
    assert str(gh.seen[0].url).startswith("http://mirror.test:8080/releases.json")
    assert updates.releases_url("a/b") == "https://api.github.com/repos/a/b/releases"


@pytest.mark.parametrize(
    "transport, error",
    [
        (FakeGitHub(status=403), "the releases cannot be read: HTTP 403"),
        (
            httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))),
            "ConnectError",
        ),
        (httpx.MockTransport(lambda r: httpx.Response(200, content=b"not json")), "cannot be read"),
    ],
)
async def test_failures_become_an_error_state(tmp_path, transport, error):
    s = await updates.Checker(tmp_path / "rel.json", transport=transport).check("e/S", "stable")
    assert error in (s.error or "") and not s.available and s.checked


async def test_api_check_and_notification(env, monkeypatch):
    app, c = env
    off = (await c.get("/api/v1/updates")).json()
    assert off["enabled"] is False and off["version"] and off["available"] is False
    r = (await c.post("/api/v1/updates/check")).json()
    assert r["enabled"] is False and r["checked"] is False  # update_repo = none: nothing asked
    monkeypatch.setattr(app.config, "update_repo", "example/SyncMusic")
    monkeypatch.setattr(updates, "running_version", lambda: "0.2.0")
    app.update_checker.transport = FakeGitHub()
    q = app.bus.subscribe()
    r = (await c.post("/api/v1/updates/check")).json()
    assert r["available"] and r["latest"] == "0.2.1" and r["channel"] == "stable" and r["jobs"] == 0
    seen = []
    while not q.empty():
        seen.append(q.get_nowait().type)
    assert "app.update_available" in seen
    await c.post("/api/v1/updates/check")  # announced once per new version
    again = []
    while not q.empty():
        again.append(q.get_nowait().type)
    assert "app.update_available" not in again
    await c.patch("/api/v1/settings", json={"update_channel": "beta"})
    r = (await c.post("/api/v1/updates/check")).json()
    assert r["latest"] == "0.2.2-4" and r["channel"] == "beta"
    assert (
        await c.patch("/api/v1/settings", json={"update_channel": "nightly"})
    ).status_code == 400
