"""Wi-Fi client pairing: codes (valid, expired, reused, burnt, rate-limited), tokens scoped to the client API and
their own device, revoke and rotate, the mDNS record, and the client API off by default."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from syncmusic.infra import announce
from syncmusic.infra import clients as clients_mod
from syncmusic.infra.auth import hash_password

C = "/api/client/v1"


def client(uid: str = "11111111-aaaa-4bbb-8ccc-000000000001", name: str = "Test phone") -> dict:
    return {
        "uid": uid,
        "name": name,
        "model": "Model X",
        "os": "Android",
        "app": "demo",
        "app_version": "1.0",
    }


@pytest.fixture
async def setup(config):
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api), base_url="http://192.0.2.10:8765"
        ) as c:
            yield app, c
    finally:
        await app.stop()


async def allow(c: AsyncClient, mdns: bool = False) -> None:
    r = await c.patch("/api/v1/settings", json={"clients_allowed": True, "clients_mdns": mdns})
    assert r.status_code == 200, r.text


async def new_code(c: AsyncClient) -> dict:
    r = await c.post("/api/v1/clients/pairings")
    assert r.status_code == 200, r.text
    return r.json()


async def pair(c: AsyncClient, code: str, **kw) -> tuple[str, str]:
    r = await c.post(
        f"{C}/pair",
        json={"code": code, "client": client(**kw), "capabilities": {"formats": ["mp3"]}},
    )
    assert r.status_code == 201, r.text
    return r.json()["device_id"], r.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_client_api_is_off_by_default(setup):
    _, c = setup
    r = await c.get(f"{C}/hello")
    assert r.status_code == 403 and r.json()["error"]["code"] == "clients_disabled"
    assert (await c.post("/api/v1/clients/pairings")).status_code == 409


async def test_hello_and_pairing_with_a_valid_code(setup):
    app, c = setup
    await allow(c)
    hello = (await c.get(f"{C}/hello")).json()
    assert hello["protocol"] == {"min": 1, "max": 1} and not hello["pairing_open"]
    assert hello["instance"]["id"] == app.instance_id
    code = await new_code(c)
    assert len(code["code"]) == 8 and code["display"][4] == "-"
    assert code["qr"].startswith(
        "syncmusic://pair?u=http%3A%2F%2F192.0.2.10%3A8765%2F&c=" + code["code"]
    )
    assert code["qr_svg"].startswith("<svg") and "<path" in code["qr_svg"]
    assert (await c.get(f"{C}/hello")).json()["pairing_open"]
    device_id, token = await pair(c, code["display"].lower())  # case and dash do not matter
    assert token.startswith("smc_")
    me = await c.get(f"{C}/me", headers=bearer(token))
    assert me.status_code == 200 and me.json()["device_id"] == device_id
    assert me.json()["name"] == "Test phone" and me.json()["poll_s"] == 300
    listed = (await c.get("/api/v1/clients")).json()["clients"]
    assert [p["device_id"] for p in listed] == [device_id] and not listed[0]["revoked"]
    assert "token" not in str(listed[0]).lower().replace("revoked", "")
    # only the hash is stored
    con = sqlite3.connect(app.config.data_dir / "syncmusic.sqlite3")
    assert token not in str(con.execute("SELECT * FROM client_pairings").fetchall())
    con.close()


async def test_a_code_is_single_use_and_expires(setup):
    app, c = setup
    await allow(c)
    code = (await new_code(c))["code"]
    await pair(c, code)
    r = await c.post(
        f"{C}/pair", json={"code": code, "client": client("22222222-aaaa-4bbb-8ccc-0002")}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "code_invalid"
    late = (await new_code(c))["code"]
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await app.db.execute(
        "UPDATE client_pairing_codes SET expires_at = ? WHERE code = ?", (past, late)
    )
    r = await c.post(
        f"{C}/pair", json={"code": late, "client": client("33333333-aaaa-4bbb-8ccc-0003")}
    )
    assert r.status_code == 410 and r.json()["error"]["code"] == "code_expired"


async def test_wrong_codes_are_rate_limited_and_burn_the_code(setup, monkeypatch):
    app, c = setup
    await allow(c)
    code = (await new_code(c))["code"]
    for _ in range(clients_mod.WRONG_PER_MINUTE):
        r = await c.post(f"{C}/pair", json={"code": "AAAA2222", "client": client()})
        assert r.status_code == 400
    r = await c.post(f"{C}/pair", json={"code": code, "client": client()})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    app.clients._wrong.clear()
    await app.db.execute("UPDATE client_pairing_codes SET failures = ?", (clients_mod.BURN_AFTER,))
    r = await c.post(f"{C}/pair", json={"code": code, "client": client()})
    assert r.json()["error"]["code"] == "code_invalid"


async def test_at_most_three_open_codes(setup):
    _, c = setup
    await allow(c)
    codes = [(await new_code(c))["code"] for _ in range(4)]
    open_now = [x["code"] for x in (await c.get("/api/v1/clients/pairings")).json()]
    assert open_now == codes[1:]
    assert (await c.delete(f"/api/v1/clients/pairings/{codes[1]}")).json() == {"ok": True}
    assert len((await c.get("/api/v1/clients/pairings")).json()) == 2


async def test_token_reaches_only_the_client_api_and_its_own_device(setup):
    app, c = setup
    await allow(c)
    a, token_a = await pair(c, (await new_code(c))["code"])
    b, token_b = await pair(
        c, (await new_code(c))["code"], uid="44444444-aaaa-4bbb-8ccc-0004", name="Other"
    )
    assert a != b
    assert (await c.get(f"{C}/me", headers=bearer(token_a))).json()["device_id"] == a
    assert (await c.get(f"{C}/me", headers=bearer(token_b))).json()["device_id"] == b
    assert (await c.get(f"{C}/me")).json()["error"]["code"] == "auth_required"
    # with the login on, a client token is no API token
    await app.auth.save(
        (await app.auth.state()).model_copy(
            update={"enabled": True, "password_hash": hash_password("x" * 8), "secret": "s"}
        )
    )
    assert (await c.get("/api/v1/settings", headers=bearer(token_a))).status_code == 401
    assert (
        await c.get(f"{C}/me", headers=bearer(token_a))
    ).status_code == 200  # the gate leaves it alone


async def test_revoked_token_is_refused_and_the_device_stays(setup):
    _, c = setup
    await allow(c)
    device_id, token = await pair(c, (await new_code(c))["code"])
    assert (await c.post(f"/api/v1/clients/{device_id}/revoke")).json() == {"ok": True}
    r = await c.get(f"{C}/me", headers=bearer(token))
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_revoked"
    listed = (await c.get("/api/v1/clients")).json()["clients"]
    assert listed[0]["revoked"]
    assert (await c.post("/api/v1/clients/nope/revoke")).status_code == 404


async def test_pairing_again_continues_the_device(setup):
    _, c = setup
    await allow(c)
    first, old = await pair(c, (await new_code(c))["code"])
    r = await c.post(f"{C}/pair", json={"code": (await new_code(c))["code"], "client": client()})
    assert r.json()["device_id"] == first and r.json()["continued"]
    assert (await c.get(f"{C}/me", headers=bearer(old))).status_code == 401
    assert (await c.get(f"{C}/me", headers=bearer(r.json()["token"]))).status_code == 200


async def test_rotate_keeps_the_old_token_until_the_new_one_is_used(setup):
    _, c = setup
    await allow(c)
    _, old = await pair(c, (await new_code(c))["code"])
    new = (await c.post(f"{C}/me/token/rotate", headers=bearer(old))).json()["token"]
    assert (await c.get(f"{C}/me", headers=bearer(old))).status_code == 200  # grace period
    assert (await c.get(f"{C}/me", headers=bearer(new))).status_code == 200
    assert (await c.get(f"{C}/me", headers=bearer(old))).status_code == 401  # ended by the new one


async def test_rename_from_client_and_ui(setup):
    _, c = setup
    await allow(c)
    device_id, token = await pair(c, (await new_code(c))["code"])
    assert (await c.patch(f"{C}/me", json={"name": "Kitchen"}, headers=bearer(token))).json()[
        "name"
    ] == "Kitchen"
    assert (await c.patch(f"/api/v1/clients/{device_id}", json={"name": "Car"})).json()[
        "name"
    ] == "Car"


async def test_turning_clients_off_closes_the_client_api(setup):
    _, c = setup
    await allow(c)
    _, token = await pair(c, (await new_code(c))["code"])
    await c.patch("/api/v1/settings", json={"clients_allowed": False})
    assert (await c.get(f"{C}/me", headers=bearer(token))).json()["error"][
        "code"
    ] == "clients_disabled"


def test_mdns_txt_record():
    assert announce.txt_record("abc", "Home", True) == {
        "v": "1",
        "id": "abc",
        "name": "Home",
        "path": "/",
        "tls": "0",
        "pair": "1",
    }


class FakeZc:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def async_register_service(self, info, allow_name_change=False):
        self.calls.append(("register", dict(info.decoded_properties)))

    async def async_update_service(self, info):
        self.calls.append(("update", dict(info.decoded_properties)))

    async def async_unregister_service(self, info):
        self.calls.append(("unregister", {}))

    async def async_close(self):
        self.calls.append(("close", {}))


async def test_announcer_follows_settings_and_open_codes(setup):
    app, c = setup
    zc = FakeZc()
    app.announcer = announce.Announcer(8765, zeroconf_factory=lambda: zc)
    await allow(c, mdns=True)
    assert zc.calls[-1][0] == "register" and zc.calls[-1][1]["pair"] == "0"
    assert zc.calls[-1][1]["id"] == app.instance_id
    code = (await new_code(c))["code"]
    assert zc.calls[-1] == ("update", zc.calls[-1][1]) and zc.calls[-1][1]["pair"] == "1"
    await pair(c, code)
    assert zc.calls[-1][1]["pair"] == "0"
    await c.patch("/api/v1/settings", json={"clients_allowed": False})
    assert [k for k, _ in zc.calls[-2:]] == ["unregister", "close"] and not app.announcer.active


async def test_one_client_for_the_device_page(setup):
    app, c = setup
    await allow(c)
    device_id, _ = await pair(c, (await new_code(c))["code"])
    r = await c.get(f"/api/v1/clients/{device_id}")
    assert r.status_code == 200 and r.json()["device_id"] == device_id
    assert r.json()["session"] is None
    assert (await c.get("/api/v1/clients/pairings")).status_code == 200  # not shadowed
    assert (await c.get("/api/v1/clients/nope")).status_code == 404


def test_pairing_url_replaces_loopback_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.requests import Request

    from syncmusic.api.routes import clients as routes

    def req(host: str) -> Request:
        return Request(
            {
                "type": "http",
                "scheme": "http",
                "headers": [(b"host", host.encode())],
                "root_path": "",
                "path": "/",
                "query_string": b"",
            }
        )

    monkeypatch.setattr(routes, "_lan_ip", lambda: "192.0.2.44")
    assert routes.pairing_url(req("localhost:8099")) == "http://192.0.2.44:8099/"
    assert routes.pairing_url(req("127.0.0.1:8099")) == "http://192.0.2.44:8099/"
    assert routes.pairing_url(req("[::1]")) == "http://192.0.2.44/"
    assert routes.pairing_url(req("music.example:8099")) == "http://music.example:8099/"
    monkeypatch.setattr(routes, "_lan_ip", lambda: None)
    assert routes.pairing_url(req("localhost:8099")) == "http://localhost:8099/"
