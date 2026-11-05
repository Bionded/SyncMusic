"""Library secrets are write-only in API responses."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures

SECRET = "PW_SECRET_X"
MASK = "********"
LIB = {
    "id": "nd",
    "name": "nd",
    "type": "navidrome",
    "settings": {"url": "http://127.0.0.1:1", "username": "u", "password": SECRET},
}


async def test_library_password_never_returned(env):
    app, c = env
    r = await c.post("/api/v1/libraries", json=LIB)
    assert r.status_code == 201, r.text
    for resp in (r, await c.get("/api/v1/libraries/nd"), await c.get("/api/v1/libraries")):
        assert SECRET not in resp.text
    assert r.json()["settings"]["password"] == MASK
    r = await c.patch("/api/v1/libraries/nd", json={"name": "Navidrome"})
    assert r.status_code == 200 and SECRET not in r.text


async def test_patch_keeps_secret_when_left_out_or_masked(env):
    app, c = env
    await c.post("/api/v1/libraries", json=LIB)
    await c.patch("/api/v1/libraries/nd", json={"settings": {"username": "v"}})
    r = await c.patch("/api/v1/libraries/nd", json={"settings": {"password": MASK}})
    assert r.status_code == 200, r.text
    stored = await app.store.library("nd")
    assert stored.settings["password"] == SECRET and stored.settings["username"] == "v"
    await c.patch("/api/v1/libraries/nd", json={"settings": {"password": "new"}})
    assert (await app.store.library("nd")).settings["password"] == "new"


async def test_abs_token_never_returned(env):
    app, c = env
    lib = {
        "id": "abs",
        "name": "abs",
        "type": "audiobookshelf",
        "settings": {"url": "http://127.0.0.1:1", "api_token": "TOKEN_SECRET_Y"},
    }
    r = await c.post("/api/v1/libraries", json=lib)
    assert r.status_code == 201, r.text
    assert "TOKEN_SECRET_Y" not in r.text
    assert "TOKEN_SECRET_Y" not in (await c.get("/api/v1/libraries")).text


async def test_settings_schema_marks_secrets(env):
    app, c = env
    types = {t["type"]: t for t in (await c.get("/api/v1/library-types")).json()}
    pw = types["subsonic"]["settings_schema"]["properties"]["password"]
    tok = types["audiobookshelf"]["settings_schema"]["properties"]["api_token"]
    assert pw["writeOnly"] and pw["format"] == "password"
    assert tok["writeOnly"] and tok["format"] == "password"


async def test_webhook_url_masked(env):
    app, c = env
    r = await c.patch("/api/v1/settings", json={"webhook_url": "https://ntfy.sh/secret-topic"})
    assert r.status_code == 200 and "secret-topic" not in r.text
    assert "secret-topic" not in (await c.get("/api/v1/settings")).text
    r = await c.patch("/api/v1/settings", json={"webhook_url": MASK, "cache_max_gb": 3})
    assert r.status_code == 200, r.text
    assert (await app.store.app_settings()).webhook_url == "https://ntfy.sh/secret-topic"
