"""a library URL with credentials (user:pass@) or a query (?token=) is refused, and the refusal does not
repeat the secret."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import pytest

from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures


@pytest.mark.parametrize(
    "url",
    [
        "http://zzuser:PW_SECRET_Q9@127.0.0.1:4599/",
        "http://127.0.0.1:4599/?token=TOK_SECRET_Q9",
        "http://127.0.0.1:4599/#TOK_SECRET_Q9",
        "ftp://127.0.0.1/",
    ],
)
async def test_refused_without_echoing_the_secret(env, url):
    _, c = env
    for body in (
        {
            "id": "nd",
            "type": "navidrome",
            "name": "ND",
            "settings": {"url": url, "username": "u", "password": "p"},
        },
        {
            "id": "abs",
            "type": "audiobookshelf",
            "name": "ABS",
            "settings": {"url": url, "api_token": "t"},
        },
    ):
        r = await c.post("/api/v1/libraries", json=body)
        assert r.status_code == 400, r.text
        assert "SECRET" not in r.text and "url" in r.text
    assert [lib["id"] for lib in (await c.get("/api/v1/libraries")).json()] == ["music"]


async def test_a_plain_url_is_accepted(env):
    _, c = env
    r = await c.post(
        "/api/v1/libraries",
        json={
            "id": "nd",
            "type": "navidrome",
            "name": "ND",
            "settings": {"url": "http://127.0.0.1:1/navidrome/", "username": "u", "password": "p"},
        },
    )
    assert r.status_code == 201, r.text


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("http://olduser:PW_SECRET_Q9@127.0.0.1:4599", "http://********@127.0.0.1:4599"),
        ("http://127.0.0.1:4599/nd/?token=TOK_SECRET_Q9", "http://127.0.0.1:4599/nd/?********"),
    ],
)
async def test_a_stored_url_with_a_secret_is_never_shown(env, stored, shown, caplog):
    """a library saved with credentials in its url: masked in every answer, the check says
    what to fix, a PATCH with a clean url repairs it."""
    app, c = env
    body = {
        "id": "nd",
        "type": "navidrome",
        "name": "ND",
        "settings": {"url": "http://127.0.0.1:4599", "username": "u", "password": "p"},
    }
    assert (await c.post("/api/v1/libraries", json=body)).status_code == 201
    cfg = await app.store.library("nd")
    await app.store.put_library(
        cfg.model_copy(update={"settings": {**cfg.settings, "url": stored}})
    )
    listed = (await c.get("/api/v1/libraries")).json()
    one = (await c.get("/api/v1/libraries/nd")).json()
    assert next(lib for lib in listed if lib["id"] == "nd")["settings"]["url"] == shown
    assert one["settings"]["url"] == shown
    r = await c.post("/api/v1/libraries/nd/check")
    assert "SECRET" not in r.text and "url" in r.text
    r = await c.patch(
        "/api/v1/libraries/nd", json={"settings": {"url": shown}}
    )  # the mask sent back
    assert r.status_code == 400 and "SECRET" not in r.text
    r = await c.patch("/api/v1/libraries/nd", json={"settings": {"url": "http://127.0.0.1:4599"}})
    assert r.status_code == 200 and r.json()["settings"]["url"] == "http://127.0.0.1:4599"
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("10.0.0.10:4533", "http://10.0.0.10:4533"),
        ("10.0.0.5", "http://10.0.0.5"),
        ("nas.local:13378", "http://nas.local:13378"),
        ("[::1]:4533", "http://[::1]:4533"),
        ("  127.0.0.1:4533/navidrome/ ", "http://127.0.0.1:4533/navidrome/"),
        ("https://music.example:443", "https://music.example:443"),
        ("http://127.0.0.1:4533", "http://127.0.0.1:4533"),
    ],
)
async def test_a_url_without_scheme_gets_http(env, given, stored):
    """never https guessed; a scheme that is there stays; TLS checking stays on by default."""
    _, c = env
    body = {
        "id": "nd",
        "type": "navidrome",
        "name": "ND",
        "settings": {"url": given, "username": "u", "password": "p"},
    }
    r = await c.post("/api/v1/libraries", json=body)
    assert r.status_code == 201, r.text
    assert (
        r.json()["settings"]["url"] == stored
        and r.json()["settings"].get("verify_tls", True) is True
    )


@pytest.mark.parametrize("url", ["host:notaport", "zzuser:PW_SECRET_Q9@127.0.0.1:4599"])
async def test_a_bad_url_without_scheme_is_refused(env, url):
    _, c = env
    body = {
        "id": "abs",
        "type": "audiobookshelf",
        "name": "A",
        "settings": {"url": url, "api_token": "t"},
    }
    r = await c.post("/api/v1/libraries", json=body)
    assert r.status_code == 400 and "SECRET" not in r.text, r.text
