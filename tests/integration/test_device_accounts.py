"""a device's own library account against the compose Navidrome and Audiobookshelf. A second
user is created for the test (and deleted after); what the device's account sends - a star, a play, a position -
lands on that user, while the library's main account does not see it."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import httpx
import pytest

from syncmusic.app import Application
from syncmusic.core.models import (
    DeviceConfig,
    LibraryAccount,
    LibraryConfig,
    MediaKind,
    PlayEvent,
    utcnow,
)
from tests.integration.services import ENV

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "NAVIDROME_URL" not in ENV or "ABS_URL" not in ENV, reason="no local Navidrome/ABS"
    ),
]


@pytest.fixture
async def nd_kid(services) -> AsyncIterator[dict[str, str]]:
    """A second Navidrome user (native API, as the admin)."""
    name, password = f"kid{secrets.token_hex(3)}", secrets.token_urlsafe(12)
    async with httpx.AsyncClient(base_url=ENV["NAVIDROME_URL"], timeout=30) as c:
        r = await c.post(
            "/auth/login",
            json={"username": ENV["NAVIDROME_USER"], "password": ENV["NAVIDROME_PASSWORD"]},
        )
        r.raise_for_status()
        c.headers["x-nd-authorization"] = f"Bearer {r.json()['token']}"
        body = {"userName": name, "name": name, "password": password, "isAdmin": False}
        r = await c.post("/api/user", json=body)
        r.raise_for_status()
        user_id = r.json()["id"]
        # a new user sees no library until it is given one (Navidrome ≥ 0.58)
        libs = (await c.get("/api/library")).json()
        await c.put(f"/api/user/{user_id}/library", json={"libraryIds": [x["id"] for x in libs]})
        try:
            yield {"username": name, "password": password}
        finally:
            await c.delete(f"/api/user/{user_id}")


@pytest.fixture
async def abs_kid(services) -> AsyncIterator[dict[str, str]]:
    """A second Audiobookshelf user with access to every library, signed in by an API token."""
    name, password = f"kid{secrets.token_hex(3)}", secrets.token_urlsafe(12)
    async with httpx.AsyncClient(base_url=ENV["ABS_URL"], timeout=30) as c:
        c.headers["Authorization"] = f"Bearer {ENV['ABS_TOKEN']}"
        body = {
            "username": name,
            "password": password,
            "type": "user",
            "isActive": True,
            "permissions": {"accessAllLibraries": True, "update": True},
        }
        r = await c.post("/api/users", json=body)
        r.raise_for_status()
        user_id = r.json()["user"]["id"]
        try:
            r = await c.post("/login", json={"username": name, "password": password})
            r.raise_for_status()
            user = r.json()["user"]
            yield {"api_token": user.get("accessToken") or user["token"]}
        finally:
            await c.delete(f"/api/users/{user_id}")


async def _device(app, library_id: str, settings: dict[str, str]) -> DeviceConfig:
    """A device signed in as the library's account "kid"."""
    lib = await app.store.library(library_id)
    account = LibraryAccount(name="Kid", settings=settings)
    await app.store.put_library(lib.model_copy(update={"accounts": {"kid": account}}))
    return DeviceConfig(
        id="kid", type="flashdrive", name="Kid", library_accounts={library_id: "kid"}
    )


async def _sync_service(config):
    app = Application(config)
    await app.start()
    await app.store.put_library(
        LibraryConfig(
            id="nd",
            type="subsonic",
            name="Navidrome",
            settings={
                "url": ENV["NAVIDROME_URL"],
                "username": ENV["NAVIDROME_USER"],
                "password": ENV["NAVIDROME_PASSWORD"],
            },
        )
    )
    await app.store.put_library(
        LibraryConfig(
            id="abs",
            type="audiobookshelf",
            name="ABS",
            settings={"url": ENV["ABS_URL"], "api_token": ENV["ABS_TOKEN"]},
        )
    )
    return app


async def test_navidrome_stars_and_plays_land_on_the_devices_account(nd_kid, config):
    app = await _sync_service(config)
    try:
        main = await app.sync.library("nd")
        kid = await app.sync.library("nd", await _device(app, "nd", nd_kid))
        assert kid is not main and kid.cache_key == "nd@kid"
        assert (await kid.check()).ok
        snap = await app.sync.snapshots.get(kid)
        track = next(t for t in snap.tracks.values() if t.kind is MediaKind.MUSIC and not t.starred)
        before_main = await main.play_count(track.ref)
        await kid.set_starred(track.ref, True)
        await kid.submit_plays([PlayEvent(ref=track.ref, played_at=utcnow())])
        kid_now = (await app.sync.snapshots.get(kid, force=True)).tracks[track.ref.item_id]
        main_now = (await app.sync.snapshots.get(main, force=True)).tracks[track.ref.item_id]
        assert kid_now.starred and kid_now.play_count == 1
        assert not main_now.starred and await main.play_count(track.ref) == before_main
    finally:
        await app.stop()


async def test_abs_positions_land_on_the_devices_account(abs_kid, config):
    app = await _sync_service(config)
    try:
        main = await app.sync.library("abs")
        kid = await app.sync.library("abs", await _device(app, "abs", abs_kid))
        assert (await kid.check()).ok
        snap = await app.sync.snapshots.get(kid)
        episode = next(t for t in snap.tracks.values() if t.kind is MediaKind.PODCAST)
        main_before = await main.get_position(episode.ref)
        await kid.set_position(episode.ref, 21.0, False)
        kid_pos = await kid.get_position(episode.ref)
        assert kid_pos is not None and kid_pos.position_s == pytest.approx(21.0, abs=0.01)
        assert await main.get_position(episode.ref) == main_before
    finally:
        await app.stop()
