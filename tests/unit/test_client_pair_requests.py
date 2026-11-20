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


async def ask(c: AsyncClient, card_uid: str = "", **kw) -> dict:
    info = client(**kw) | ({"card_uid": card_uid} if card_uid else {})
    r = await c.post(
        f"{C}/pair-requests", json={"client": info, "capabilities": {"formats": ["mp3"]}}
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


async def test_names_follow_the_server(setup):
    """The name given when approving wins; a rename on the device page, through the clients API or from
    the app keeps the pairing and the device in step, and the app learns it from its status poll."""
    app, c = setup
    await allow(c)
    req = await ask(c, name="HiBy R1 - Compás")
    r = await c.post(
        f"/api/v1/clients/requests/{req['request_id']}/approve", json={"name": "  Плеєр  "}
    )
    assert r.status_code == 200, r.text
    body = (await poll(c, req)).json()
    did, tok = body["device_id"], bearer(body["token"])
    assert (await c.get(f"{C}/me", headers=tok)).json()["name"] == "Плеєр"
    assert (await c.get(f"/api/v1/devices/{did}")).json()["config"]["name"] == "Плеєр"

    r = await c.patch(f"/api/v1/devices/{did}", json={"name": "Kitchen"})
    assert r.status_code == 200, r.text
    assert (await c.get(f"{C}/me", headers=tok)).json()["name"] == "Kitchen"
    assert (await c.get(f"{C}/status", headers=tok)).json()["name"] == "Kitchen"

    r = await c.patch(f"{C}/me", headers=tok, json={"name": "Garden"})
    assert r.json()["name"] == "Garden"
    assert (await c.get(f"/api/v1/devices/{did}")).json()["config"]["name"] == "Garden"

    r = await c.patch(f"/api/v1/clients/{did}", json={"name": "Hall"})
    assert r.status_code == 200
    assert (await c.get(f"/api/v1/devices/{did}")).json()["config"]["name"] == "Hall"
    assert (await c.get(f"{C}/status", headers=tok)).json()["name"] == "Hall"


async def test_a_new_player_waits_for_its_registration(setup):
    """A first pairing leaves the device 'registration pending': the client is told so and cannot sync until the
    user finishes the setup (or picks templates on the device page); a re-pair of the same install continues."""
    app, c = setup
    await allow(c)
    req = await ask(c, name="Player", uid="install-1")
    await c.post(f"/api/v1/clients/requests/{req['request_id']}/approve")
    body = (await poll(c, req)).json()
    did, tok = body["device_id"], bearer(body["token"])
    assert (await c.get(f"{C}/me", headers=tok)).json()["registration"] == "pending"
    assert (await c.get(f"{C}/status", headers=tok)).json()["registration"] == "pending"
    r = await c.post(f"{C}/sync", headers=tok, json={"capabilities": {"formats": ["mp3"]}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "registration_pending"
    listed = {d["id"]: d for d in (await c.get("/api/v1/devices")).json()["registered"]}
    assert listed[did]["registration_pending"] is True

    r = await c.patch(f"/api/v1/devices/{did}", json={"rule_set": {"template_ids": []}})
    assert r.status_code == 200, r.text
    assert (await c.get(f"{C}/status", headers=tok)).json()["registration"] == "done"

    again = await ask(c, name="Player", uid="install-1")  # the same install pairs again
    r = await c.post(f"/api/v1/clients/requests/{again['request_id']}/approve")
    assert r.json()["continued"] is True and r.json()["device_id"] == did
    assert (await c.get(f"/api/v1/devices/{did}")).json()["config"]["registration_pending"] is False


def test_card_uid_is_optional_and_checked():
    from pydantic import ValidationError

    from syncmusic.infra.clients import ClientInfo

    assert ClientInfo(uid="install-0001").card_uid == ""
    assert ClientInfo(uid="install-0001", card_uid="0f1e2d3c-aaaa").card_uid == "0f1e2d3c-aaaa"
    with pytest.raises(ValidationError):
        ClientInfo(uid="install-0001", card_uid="../etc")


def test_header_the_app_writes_is_a_valid_manifest_header(tmp_path):
    from syncmusic.core.manifest import read_header

    (tmp_path / ".syncmusic").mkdir()
    (tmp_path / ".syncmusic" / "manifest.jsonl").write_text(
        '{"app_version":"syncmusic-compas dev","created_at":"2026-01-02T03:04:05Z","device_id":"card-1",'
        '"device_uid":"card-1","manifest_version":2,"type":"header","updated_at":"2026-01-02T03:04:05Z"}\n'
    )
    header = read_header(tmp_path)
    assert header is not None and header.device_uid == "card-1"


async def test_request_offers_the_device_with_the_same_card(setup):
    from syncmusic.core.models import DeviceConfig, DeviceIdentity

    app, c = setup
    await allow(c)
    await app.sync.store.put_device(
        DeviceConfig(
            id="card-player",
            name="Card player",
            type="usb_drive",
            identity=DeviceIdentity(manifest_device_id="0f1e2d3c-bbbb"),
        )
    )
    await ask(c, uid="11111111-aaaa-4bbb-8ccc-000000000011", card_uid="0f1e2d3c-bbbb")
    await ask(c, uid="11111111-aaaa-4bbb-8ccc-000000000012", card_uid="0f1e2d3c-cccc")
    listed = {x["client"]["uid"]: x for x in (await c.get("/api/v1/clients/requests")).json()}
    assert listed["11111111-aaaa-4bbb-8ccc-000000000011"]["same_card"] == [
        {"id": "card-player", "name": "Card player", "type": "usb_drive"}
    ]
    assert listed["11111111-aaaa-4bbb-8ccc-000000000012"]["same_card"] == []


async def test_approve_adds_wifi_to_the_device_of_the_same_card(setup):
    from syncmusic.core.models import DeviceConfig, DeviceIdentity
    from syncmusic.devices.client import ClientDevice

    app, c = setup
    await allow(c)
    await app.sync.store.put_device(
        DeviceConfig(
            id="card-player",
            name="Card player",
            type="hiby_compas",
            identity=DeviceIdentity(manifest_device_id="0f1e2d3c-dddd"),
        )
    )
    other = await ask(c, uid="11111111-aaaa-4bbb-8ccc-000000000013", card_uid="0f1e2d3c-eeee")
    r = await c.post(
        f"/api/v1/clients/requests/{other['request_id']}/approve", json={"device_id": "card-player"}
    )
    assert r.status_code == 422  # not this player's card
    req = await ask(c, uid="11111111-aaaa-4bbb-8ccc-000000000014", card_uid="0f1e2d3c-dddd")
    r = await c.post(
        f"/api/v1/clients/requests/{req['request_id']}/approve", json={"device_id": "card-player"}
    )
    assert r.status_code == 200 and r.json()["device_id"] == "card-player", r.text
    body = (await poll(c, req)).json()
    me = await c.get(f"{C}/me", headers=bearer(body["token"]))
    assert me.status_code == 200 and me.json()["device_id"] == "card-player"
    # one record: stored as the card's device, reached as a Wi-Fi client while not on USB
    stored = await app.sync.store.device("card-player")
    assert stored.type == "hiby_compas" and len(await app.sync.store.devices()) == 1
    view = await app.sync.config("card-player")
    assert view.type == "client" and view.settings["player_database"] == "compas"
    assert isinstance(await app.client_sessions.device("card-player"), ClientDevice)
    await app.sync.store.put_device(view.model_copy(update={"name": "Renamed"}))
    stored = await app.sync.store.device("card-player")
    assert (stored.type, stored.name) == ("hiby_compas", "Renamed")
    listed = (await c.get("/api/v1/devices")).json()["registered"]
    dev = next(d for d in listed if d["id"] == "card-player")
    assert dev["connected"] and dev["connection"] == "wifi" and dev["kind"] == "network"
    assert dev["wifi_paired"] and not dev["can_format"]
    # the app has not talked for longer than the keep-alive window (or since a restart): offline,
    # still paired - never 'connected (Wi-Fi)' from the pairing alone
    app.clients._heard["card-player"] -= 3600
    dev = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == "card-player"
    )
    assert not dev["connected"] and dev["connection"] is None and dev["wifi_paired"]
    app.clients._heard.clear()  # a server restart
    dev = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == "card-player"
    )
    assert not dev["connected"]
    assert (await c.get(f"{C}/me", headers=bearer(body["token"]))).status_code == 200
    dev = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == "card-player"
    )
    assert dev["connected"] and dev["connection"] == "wifi"


async def test_merge_a_wifi_record_into_the_usb_device_of_the_same_card(setup):
    """A player approved as a new Wi-Fi device while its card was registered over USB: both records offer
    'Merge'; merging keeps the USB record (its settings, the chosen name) with the Wi-Fi record's history,
    plays already sent, pairing and files; the next USB sync merges the two manifests."""
    from types import SimpleNamespace

    from syncmusic.base.capabilities import DeviceCapabilities
    from syncmusic.core import manifest as manifest_io
    from syncmusic.core.models import (
        DeviceConfig,
        DeviceIdentity,
        Manifest,
        ManifestEntry,
        ManifestHeader,
    )
    from syncmusic.core.models import ItemRef as Ref

    app, c = setup
    await allow(c)
    await app.sync.store.put_device(
        DeviceConfig(
            id="card-player",
            name="Card player",
            type="hiby_compas",
            identity=DeviceIdentity(manifest_device_id="0f1e2d3c-ffff"),
        )
    )
    req = await ask(c, uid="11111111-aaaa-4bbb-8ccc-000000000015", card_uid="0f1e2d3c-ffff")
    r = await c.post(f"/api/v1/clients/requests/{req['request_id']}/approve")
    wifi_id = r.json()["device_id"]
    token = (await poll(c, req)).json()["token"]
    wifi = await app.sync.store.device(wifi_id)
    assert wifi is not None and wifi.type == "client"
    db = app.sync.store.db
    await db.execute(
        "INSERT INTO submitted_plays (sink, library_id, item_id, played_at, device_id, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("nd", "music", "t1", "2026-01-01T00:00:00+00:00", wifi_id, "2026-01-01T00:00:00+00:00"),
    )

    def entry(key: str) -> ManifestEntry:
        return ManifestEntry(
            ref=Ref(library_id="music", item_id=key),
            device_ref=f"Music/{key}.mp3",
            path=f"Music/{key}.mp3",
            size=1,
        )

    outbox = app.sync._network_dir(wifi)
    manifest_io.write(
        outbox,
        Manifest(
            header=ManifestHeader(device_id=wifi_id, device_uid=wifi.identity.manifest_device_id),
            items={"music/a": entry("a")},
        ),
    )
    listed = {d["id"]: d for d in (await c.get("/api/v1/devices")).json()["registered"]}
    assert [m["id"] for m in listed["card-player"]["merge_with"]] == [wifi_id]
    assert listed[wifi_id]["merge_with"][0]["keep"] == "card-player"

    r = await c.post(
        "/api/v1/devices/card-player/merge", json={"device_id": "card-player", "name": "R1"}
    )
    assert r.status_code == 400  # only a Wi-Fi record goes into a USB one
    r = await c.post("/api/v1/devices/card-player/merge", json={"device_id": wifi_id, "name": "R1"})
    assert r.status_code == 200, r.text
    stored = await app.sync.store.devices()
    assert [(d.id, d.type, d.name) for d in stored] == [("card-player", "hiby_compas", "R1")]
    me = await c.get(f"{C}/me", headers=bearer(token))
    assert me.json()["device_id"] == "card-player"
    rows = await db.fetchall("SELECT device_id FROM submitted_plays")
    assert [r["device_id"] for r in rows] == ["card-player"]
    moved = app.sync._network_dir(stored[0])
    m = manifest_io.read(moved)
    assert m is not None and m.header.device_uid == "0f1e2d3c-ffff" and list(m.items) == ["music/a"]
    view = await app.sync.config("card-player")
    assert view.type == "client"
    dev = await app.client_sessions.device("card-player")
    assert dev.card_manifest() is None  # not before the card's own list is merged in
    listed = {d["id"]: d for d in (await c.get("/api/v1/devices")).json()["registered"]}
    assert listed["card-player"]["merge_with"] == []

    # the next USB sync: the card's manifest gains the Wi-Fi items, the Wi-Fi side takes the result
    card = moved.parent / "card"
    manifest_io.write(
        card,
        Manifest(
            header=ManifestHeader(device_id="card-player", device_uid="0f1e2d3c-ffff"),
            items={"music/b": entry("b")},
        ),
    )

    async def write_manifest(m: Manifest) -> None:
        manifest_io.write(card, m)

    usb = SimpleNamespace(
        network=False,
        mount=card,
        write_manifest=write_manifest,
        effective_capabilities=DeviceCapabilities,
    )
    await app.sync._mirror_card_manifest(stored[0], usb)
    for root in (card, moved):
        manifest_io.forget(root)
        assert sorted(manifest_io.read(root).items) == ["music/a", "music/b"]
    assert not (moved / manifest_io.MERGED_MARK).exists()
    assert dev.card_manifest() is not None


async def test_merge_keeps_the_live_pairing_over_a_revoked_one(setup):
    """The kept record has an old revoked pairing: merging must move the other record's live pairing onto
    it (not drop it), so the app keeps working."""
    from syncmusic.core.models import DeviceConfig, DeviceIdentity

    app, c = setup
    await allow(c)
    await app.sync.store.put_device(
        DeviceConfig(
            id="card-player",
            name="Card player",
            type="hiby_compas",
            identity=DeviceIdentity(manifest_device_id="0f1e2d3c-eeee"),
        )
    )
    await app.sync.store.db.execute(
        "INSERT INTO client_pairings (device_id, client_uid, name, client, capabilities, token_sha256, "
        "created_at, revoked_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
        (
            "card-player",
            "11111111-aaaa-4bbb-8ccc-0000000000aa",
            "Card player",
            "{}",
            "{}",
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
        ),
    )
    req = await ask(c, uid="11111111-aaaa-4bbb-8ccc-000000000016", card_uid="0f1e2d3c-eeee")
    wifi_id = (await c.post(f"/api/v1/clients/requests/{req['request_id']}/approve")).json()[
        "device_id"
    ]
    token = (await poll(c, req)).json()["token"]
    await app.sync.store.merge_device("card-player", wifi_id)
    me = await c.get(f"{C}/me", headers=bearer(token))
    assert me.status_code == 200 and me.json()["device_id"] == "card-player", me.text
