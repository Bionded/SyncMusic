"""Optional login: off = unchanged, on = 401 without a session or API token; login, wrong
password, tokens, sign out everywhere, the event stream, health open, desktop bypass, CLI recovery."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from syncmusic.api import auth as auth_api
from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.cli.main import cli
from syncmusic.facade import AppFacade
from syncmusic.infra import auth as auth_mod
from syncmusic.infra.auth import Auth, AuthState, hash_password, verify_password

PW = "correct horse"


@pytest.fixture
async def setup(config, monkeypatch):
    monkeypatch.setattr(auth_api, "MAX_DELAY_S", 0)
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            yield app, c
    finally:
        await app.stop()


def test_password_hash():
    h = hash_password(PW)
    assert h.startswith("scrypt$") and PW not in h
    assert verify_password(PW, h) and not verify_password("wrong", h)
    assert not verify_password(PW, "garbage")


def test_session_cookie_expires_and_needs_the_secret():
    state = AuthState(enabled=True, secret="s1")
    cookie = Auth.new_session(state)
    assert Auth.session_valid(state, cookie)
    assert not Auth.session_valid(AuthState(enabled=True, secret="s2"), cookie)
    issued = int(cookie.split(".")[0])
    assert not Auth.session_valid(state, cookie, now=issued + 31 * 86400)
    assert not Auth.session_valid(state, "nonsense")


async def test_off_by_default_changes_nothing(setup):
    _, c = setup
    assert (await c.get("/api/v1/settings")).status_code == 200
    r = await c.get("/api/v1/auth")
    assert r.json() == {
        "enabled": False,
        "has_password": False,
        "signed_in": True,
        "local_bypass": False,
        "tokens": [],
    }
    r = await c.post("/api/v1/auth/access", json={"enabled": True})
    assert r.status_code == 400  # no password yet
    r = await c.post("/api/v1/auth/access", json={"enabled": True, "password": "short"})
    assert r.status_code == 400


async def test_login_on(setup):
    app, c = setup
    r = await c.post("/api/v1/auth/access", json={"enabled": True, "password": PW})
    assert r.status_code == 200 and r.json()["signed_in"]
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert (await c.get("/api/v1/settings")).status_code == 200  # the cookie of the change
    c.cookies.clear()
    r = await c.get("/api/v1/settings")
    assert r.status_code == 401 and r.json()["error"] == "auth_required"
    assert (await c.get("/api/v1/health")).status_code == 200
    assert (await c.get("/api/v1/events")).status_code == 401
    assert (await c.get("/api/v1/version")).status_code == 401  # the UI asks after the login gate
    assert (await c.get("/openapi.json")).status_code == 401
    status = (await c.get("/api/v1/auth")).json()
    assert status["enabled"] and not status["signed_in"] and "tokens" not in status
    r = await c.post("/api/v1/auth/login", json={"password": "wrong"})
    assert r.status_code == 401 and app.auth.failures
    r = await c.post("/api/v1/auth/login", json={"password": PW})
    assert r.status_code == 200 and not app.auth.failures
    assert (await c.get("/api/v1/settings")).status_code == 200
    await c.post("/api/v1/auth/logout")
    assert (await c.get("/api/v1/settings")).status_code == 401


async def test_tokens_and_sign_out_everywhere(setup):
    app, c = setup
    await c.post("/api/v1/auth/access", json={"enabled": True, "password": PW})
    r = await c.post("/api/v1/auth/tokens", json={"name": "script"})
    token, tid = r.json()["token"], r.json()["id"]
    assert token.startswith("smt_") and token not in (await app.auth.state()).model_dump_json()
    listed = (await c.get("/api/v1/auth")).json()["tokens"]
    assert [t["name"] for t in listed] == ["script"] and "sha256" not in listed[0]
    other = AsyncClient(transport=c._transport, base_url="http://t")
    async with other:
        bearer = {"Authorization": f"Bearer {token}"}
        assert (await other.get("/api/v1/settings", headers=bearer)).status_code == 200
        assert (
            await other.get("/api/v1/settings", headers={"Authorization": "Bearer x"})
        ).status_code == 401
        # sign out everywhere ends sessions (also this one), tokens stay
        await c.post("/api/v1/auth/signout-everywhere")
        old = Auth.new_session(AuthState(enabled=True, secret="old"))
        c.cookies.set(auth_mod.COOKIE, old)
        assert (await c.get("/api/v1/settings")).status_code == 401
        assert (await other.get("/api/v1/settings", headers=bearer)).status_code == 200
        assert (await other.delete(f"/api/v1/auth/tokens/{tid}", headers=bearer)).status_code == 200
        assert (await other.get("/api/v1/settings", headers=bearer)).status_code == 401
    # turning it off again: open as before
    await c.post("/api/v1/auth/login", json={"password": PW})
    await c.post("/api/v1/auth/access", json={"enabled": False})
    c.cookies.clear()
    assert (await c.get("/api/v1/settings")).status_code == 200


async def test_desktop_bypass_for_local_requests(config):
    app = Application(config)
    await app.start()
    try:
        await app.auth.configure(True, PW)
        api = create_api(AppFacade(app), manage_lifecycle=False, local_ok=True)
        local = ASGITransport(app=api, client=("127.0.0.1", 1))
        remote = ASGITransport(app=api, client=("10.0.0.9", 1))
        async with AsyncClient(transport=local, base_url="http://t") as c:
            assert (await c.get("/api/v1/settings")).status_code == 200
            assert (await c.get("/api/v1/auth")).json()["local_bypass"]
        async with AsyncClient(transport=remote, base_url="http://t") as c:
            assert (await c.get("/api/v1/settings")).status_code == 401
    finally:
        await app.stop()


async def test_cli_set_password_recovers(config, monkeypatch):
    monkeypatch.setattr(auth_mod, "RELOAD_S", 0)
    app = Application(config)
    await app.start()
    try:
        await app.auth.configure(True, "forgotten-1")
        r = CliRunner().invoke(
            cli, ["set-password", "--data-dir", str(config.data_dir), "--password", "new-password"]
        )
        assert r.exit_code == 0, r.output
        state = await app.auth.state()  # the running daemon sees it
        assert state.enabled and verify_password("new-password", state.password_hash)
        r = CliRunner().invoke(
            cli, ["set-password", "--data-dir", str(config.data_dir), "--password", "short"]
        )
        assert r.exit_code == 1
    finally:
        await app.stop()


async def test_restore_keeps_the_login(setup):
    app, c = setup
    await c.post("/api/v1/auth/access", json={"enabled": True, "password": PW})
    s = await app.store.app_settings()
    await app.store.replace_config(s, [], [], [], [], [], [])
    assert (await app.auth.state()).enabled
    row = await app.db.fetchone("SELECT value FROM settings WHERE key = 'auth'")
    assert row is not None


async def test_gate_follows_a_restart_in_place(config, monkeypatch):
    """A restore restarts the application in place (new `Auth`, the old database closed): the gate and the routes
    use the current one."""
    monkeypatch.setattr(auth_mod, "RELOAD_S", 0)
    app = Application(config)
    facade = AppFacade(app)
    api = create_api(facade)
    async with (
        api.router.lifespan_context(api),
        AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c,
    ):
        await c.post("/api/v1/auth/access", json={"enabled": True, "password": PW})
        backup = (await c.get("/api/v1/settings/backup")).json()
        assert (await c.post("/api/v1/settings/backup/restore", json=backup)).status_code == 200
        assert facade.app is not app
        assert (await c.get("/api/v1/settings")).status_code == 200  # the session survives
        c.cookies.clear()
        assert (await c.get("/api/v1/settings")).status_code == 401
        assert (await c.post("/api/v1/auth/login", json={"password": PW})).status_code == 200
