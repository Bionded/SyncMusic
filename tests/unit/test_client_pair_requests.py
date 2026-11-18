"""Pairing requests (registration mode): a client asks without a code, the user approves or rejects it on the
server, the client polls with its secret and gets its token once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from tests.unit.test_clients import C, allow, bearer, client


@pytest.fixture
async def setup(config):
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            yield app, c
    finally:
        await app.stop()


async def ask(c: AsyncClient, **kw) -> dict:
    r = await c.post(
        f"{C}/pair-requests", json={"client": client(**kw), "capabilities": {"formats": ["mp3"]}}
    )
    assert r.status_code == 202, r.text
    return r.json()


async def poll(c: AsyncClient, req: dict, secret: str | None = None):
    return await c.get(
        f"{C}/pair-requests/{req['request_id']}",
        headers={"X-Pair-Secret": secret if secret is not None else req["secret"]},
    )


async def test_request_approved_gives_the_token_once(setup):
    app, c = setup
    await allow(c)
    assert "pair_requests" in (await c.get(f"{C}/hello")).json()["features"]
    req = await ask(c, name="Kitchen player")
    assert len(req["display_code"]) == 4 and req["display_code"].isdigit()
    r = await poll(c, req)
    assert r.status_code == 202 and r.json()["status"] == "pending"
    assert (await poll(c, req, "wrong")).status_code == 410
    listed = (await c.get("/api/v1/clients/requests")).json()
    assert [x["display_code"] for x in listed] == [req["display_code"]]
    assert listed[0]["client"]["name"] == "Kitchen player" and "secret" not in str(listed)
    r = await c.post(f"/api/v1/clients/requests/{req['request_id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert (await c.get("/api/v1/clients/requests")).json() == []
    r = await poll(c, req)
    assert r.status_code == 200, r.text
    body = r.json()
    me = await c.get(f"{C}/me", headers=bearer(body["token"]))
    assert me.status_code == 200 and me.json()["device_id"] == body["device_id"]
    assert (await poll(c, req)).status_code == 410  # handed out once
    clients = (await c.get("/api/v1/clients")).json()["clients"]
    assert [x["name"] for x in clients] == ["Kitchen player"]


async def test_rejected_request(setup):
    _, c = setup
    await allow(c)
    req = await ask(c)
    r = await c.post(f"/api/v1/clients/requests/{req['request_id']}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    r = await poll(c, req)
    assert r.status_code == 403 and r.json()["error"]["code"] == "request_rejected"
    assert (
        await c.post(f"/api/v1/clients/requests/{req['request_id']}/approve")
    ).status_code == 404
    assert (await c.get("/api/v1/clients")).json()["clients"] == []


async def test_expired_request(setup):
    app, c = setup
    await allow(c)
    req = await ask(c)
    app.clients._requests[req["request_id"]].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await poll(c, req)).status_code == 410
    assert (await c.get("/api/v1/clients/requests")).json() == []
    assert (
        await c.post(f"/api/v1/clients/requests/{req['request_id']}/approve")
    ).status_code == 404


async def test_refused_when_clients_or_requests_are_off(setup):
    _, c = setup
    r = await c.post(f"{C}/pair-requests", json={"client": client()})
    assert r.status_code == 403 and r.json()["error"]["code"] == "clients_disabled"
    await allow(c)
    assert (
        await c.patch("/api/v1/settings", json={"client_pair_requests": False})
    ).status_code == 200
    r = await c.post(f"{C}/pair-requests", json={"client": client()})
    assert r.status_code == 403 and r.json()["error"]["code"] == "pair_requests_disabled"
    assert (await c.get(f"{C}/hello")).json()["features"] == []


async def test_rate_limit_and_same_install_replaces_its_request(setup):
    _, c = setup
    await allow(c)
    first = await ask(c)
    await ask(c)  # the same uid asks again: one request
    assert len((await c.get("/api/v1/clients/requests")).json()) == 1
    assert (await poll(c, first)).status_code == 410
    await ask(c, uid="22222222-aaaa-4bbb-8ccc-000000000002")
    r = await c.post(
        f"{C}/pair-requests", json={"client": client(uid="33333333-aaaa-4bbb-8ccc-0003")}
    )
    assert r.status_code == 429 and r.headers["Retry-After"]


def test_at_most_two_pairing_attempts_per_second_per_address(monkeypatch):
    from syncmusic.infra import clients as mod

    monkeypatch.setattr(mod, "ATTEMPTS_PER_SECOND", 2)
    c = mod.Clients(db=None)  # type: ignore[arg-type]
    c.throttle("192.0.2.1")
    c.throttle("192.0.2.1")
    with pytest.raises(mod.ClientError) as e:
        c.throttle("192.0.2.1")
    assert e.value.status == 429 and e.value.code == "rate_limited" and e.value.retry_after_s == 1
    c.throttle("192.0.2.2")  # another address has its own budget
