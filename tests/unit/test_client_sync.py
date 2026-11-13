"""Wi-Fi client sync sessions through the client API with a fake client: pairing makes the device, a sync plans
into the outbox, the plan pages, status polls say when to sync."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core.models import RuleSet
from syncmusic.core.rules.templates import own_template_id
from syncmusic.facade import AppFacade
from tests.media import make_music
from tests.unit.test_clients import C, allow, bearer, new_code, pair

ALL_RULES = [{"id": "all", "name": "Everything", "criteria": {"all": []}}]
CAPS = {"formats": ["mp3"], "storage": {"total": 10**9, "free": 10**9}}


@pytest.fixture
def music(tmp_path):
    root = tmp_path / "music"
    make_music(root / "A" / "01 One.mp3", title="One", artist="Кобза", album="Перший")
    make_music(root / "A" / "02 Two.mp3", title="Two", artist="Кобза", album="Перший")
    return root


@pytest.fixture
async def phone(config, music):
    """(app, http client, device id, client headers): a paired client whose device syncs everything."""
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/libraries",
            json={
                "id": "music",
                "type": "folder",
                "name": "Music",
                "settings": {"root": str(music)},
            },
        )
        assert r.status_code == 201, r.text
        await allow(c)
        device_id, token = await pair(c, (await new_code(c))["code"])
        r = await c.post(
            "/api/v1/templates",
            json={"name": "Phone rules", "library_ids": ["music"], "rules": ALL_RULES},
        )
        cfg = await app.store.device(device_id)
        assert cfg is not None and cfg.type == "client"
        cfg.rule_set = RuleSet(template_ids=[r.json()["id"]])
        await app.store.put_device(cfg)
        yield app, c, device_id, bearer(token)
    await app.stop()


async def sync(c, h, body=None, *, tries=200):
    """Start a sync and poll until the session is no longer planning."""
    r = await c.post(f"{C}/sync", json=body or {"capabilities": CAPS}, headers=h)
    assert r.status_code in (200, 202), r.text
    view = r.json()
    for _ in range(tries):
        if view["status"] != "planning":
            return view
        await asyncio.sleep(0.05)
        r = await c.get(f"{C}/sync/{view['session_id']}", headers=h)
        view = r.json()
    raise AssertionError(f"still planning: {view}")


async def test_pairing_creates_the_client_device(phone):
    app, c, device_id, h = phone
    devices = (await c.get("/api/v1/devices")).json()["registered"]
    assert [d["id"] for d in devices] == [device_id]
    dev = await app.client_sessions.device(device_id)
    assert dev.state.capabilities == {"formats": ["mp3"]}


async def test_first_sync_plans_into_the_outbox(phone):
    app, c, device_id, h = phone
    view = await sync(c, h)
    assert view["status"] == "ready", view
    plan = view["plan"]
    tracks = [f for f in plan["fetch"] if f["role"] == "track"]
    assert len(tracks) == 2 and all(f["op"] == "add" and len(f["fid"]) == 64 for f in tracks)
    assert plan["totals"]["fetch_count"] == len(plan["fetch"]) and plan["delete"] == []
    assert all(f["key"] for f in tracks) and tracks[0]["path"].endswith(".mp3")
    r = await c.get(f"{C}/sync/{view['session_id']}/plan?section=fetch&limit=1", headers=h)
    assert r.json()["items"] == plan["fetch"][:1] and r.json()["next"] == "1"
    status = (await c.get(f"{C}/status", headers=h)).json()
    assert status["sync_wanted"] == "full" and status["plan_ready"]


async def test_a_session_is_the_clients_own(phone):
    app, c, device_id, h = phone
    view = await sync(c, h)
    other_id, other_token = await pair(
        c, (await new_code(c))["code"], uid="22222222-aaaa-4bbb-8ccc-000000000002"
    )
    r = await c.get(f"{C}/sync/{view['session_id']}", headers=bearer(other_token))
    assert r.status_code == 410 and r.json()["error"]["code"] == "session_expired"


async def test_delta_inventory_needs_the_current_cursor(phone):
    _, c, _, h = phone
    body = {"inventory": {"kind": "delta", "since": "999", "items": []}}
    r = await c.post(f"{C}/sync", json=body, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "inventory_required"


async def test_a_client_that_holds_files_is_reviewed_on_the_server(phone):
    app, c, device_id, h = phone
    body = {"capabilities": CAPS, "inventory": {"items": [{"path": "Music/x.mp3", "size": 3}]}}
    view = await sync(c, h, body)
    assert view["status"] == "needs_review" and view["retry_after_s"]
    assert app.sync._waiting[device_id]["plan_id"]


async def test_status_is_quiet_without_work(phone):
    app, c, device_id, h = phone
    status = (await c.get(f"{C}/status", headers=h)).json()
    assert status["sync_wanted"] == "none" and not status["plan_ready"]
    app.sync.request_client_sync(device_id)
    assert (await c.get(f"{C}/status", headers=h)).json()["sync_wanted"] == "full"


async def download(c, h, item, start: int = 0) -> bytes:
    headers = dict(h)
    if start:
        headers |= {"Range": f"bytes={start}-", "If-Range": f'"{item["fid"]}"'}
    r = await c.get(f"{C}/files/{item['fid']}", headers=headers)
    assert r.status_code == (206 if start else 200), r.text
    assert r.headers["etag"] == f'"{item["fid"]}"'
    return r.content


async def deliver(c, h, view) -> dict:
    """Fetch, verify and ack everything, ack the deletes, finish: a well-behaved client."""
    import hashlib

    sid = view["session_id"]
    plan = view["plan"]
    for item in plan["fetch"]:
        data = await download(c, h, item)
        assert hashlib.sha256(data).hexdigest() == item["fid"] and len(data) == item["size"]
        r = await c.post(f"{C}/sync/{sid}/ack", json={"done": [item]}, headers=h)
        assert r.status_code == 200, r.text
    if plan["delete"]:
        body = {"deleted": [d["path"] for d in plan["delete"]]}
        assert (await c.post(f"{C}/sync/{sid}/ack", json=body, headers=h)).status_code == 200
    r = await c.post(f"{C}/sync/{sid}/finish", json={"status": "complete"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def held(plan_fetch) -> list[dict]:
    return [{"path": f["path"], "size": f["size"]} for f in plan_fetch]


async def test_interrupted_download_resumes_and_acks_drain_the_outbox(phone):
    app, c, device_id, h = phone
    view = await sync(c, h)
    sid, item = view["session_id"], view["plan"]["fetch"][0]
    whole = await download(c, h, item)
    assert await download(c, h, item, start=1000) == whole[1000:]
    stale = dict(h) | {"Range": "bytes=1000-", "If-Range": '"other"'}
    r = await c.get(f"{C}/files/{item['fid']}", headers=stale)
    assert r.status_code == 200 and r.content == whole  # another version: the whole file again
    assert (await c.get(f"{C}/files/{'0' * 64}", headers=h)).status_code == 404

    r = await c.post(f"{C}/sync/{sid}/ack", json={"done": [item]}, headers=h)
    assert r.json()["progress"]["done"] == 1 and r.json()["cancel"] is False
    again = await c.post(f"{C}/sync/{sid}/ack", json={"done": [item]}, headers=h)
    assert again.json()["progress"] == r.json()["progress"]  # idempotent
    dev = await app.client_sessions.device(device_id)
    assert item["path"] in dev.state.inventory and item["path"] not in dev.outbox()
    bad = await c.post(
        f"{C}/sync/{sid}/ack", json={"done": [{"path": "Music/nope.mp3"}]}, headers=h
    )
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "not_in_plan"
    # the session ends half done: the rest waits in the outbox for the next one
    r = await c.post(f"{C}/sync/{sid}/finish", json={"status": "partial"}, headers=h)
    cursor = r.json()["inventory_cursor"]
    body = {"capabilities": CAPS, "inventory": {"kind": "delta", "since": cursor, "items": []}}
    nxt = await sync(c, h, body)
    assert nxt["status"] == "ready", nxt
    assert {f["path"] for f in nxt["plan"]["fetch"]} == {
        f["path"] for f in view["plan"]["fetch"][1:]
    }


async def test_update_without_changes_fetches_nothing(phone):
    _, c, _, h = phone
    view = await sync(c, h)
    done = await deliver(c, h, view)
    body = {
        "capabilities": CAPS,
        "inventory": {"kind": "full", "items": held(view["plan"]["fetch"])},
    }
    for _ in range(2):
        nxt = await sync(c, h, body)
        assert nxt["status"] == "ready", nxt
        assert nxt["plan"]["fetch"] == [] and nxt["plan"]["delete"] == []
    assert done["progress"]["done"] == len(view["plan"]["fetch"])


async def test_file_deleted_on_the_client_is_sent_again(phone):
    _, c, _, h = phone
    view = await sync(c, h)
    await deliver(c, h, view)
    tracks = [f for f in view["plan"]["fetch"] if f["role"] == "track"]
    kept = [f for f in view["plan"]["fetch"] if f["path"] != tracks[0]["path"]]
    nxt = await sync(c, h, {"capabilities": CAPS, "inventory": {"items": held(kept)}})
    assert [f["path"] for f in nxt["plan"]["fetch"] if f["role"] == "track"] == [tracks[0]["path"]]


async def test_track_gone_from_the_library_is_deleted_on_the_client(phone, music):
    app, c, device_id, h = phone
    cfg = await app.store.device(device_id)
    cfg.delete_threshold_pct = 100  # 1 of 2 tracks would ask the user first
    await app.store.put_device(cfg)
    view = await sync(c, h)
    await deliver(c, h, view)
    (music / "A" / "02 Two.mp3").unlink()
    await c.post("/api/v1/libraries/music/refresh")
    body = {"capabilities": CAPS, "inventory": {"items": held(view["plan"]["fetch"])}}
    nxt = await sync(c, h, body)
    assert nxt["status"] == "ready", nxt
    gone = [d["path"] for d in nxt["plan"]["delete"]]
    assert any(p.endswith("Two.mp3") for p in gone), nxt["plan"]
    await deliver(c, h, nxt)
    left = held([f for f in view["plan"]["fetch"] if f["path"] not in gone])
    last = await sync(c, h, {"capabilities": CAPS, "inventory": {"items": left}})
    assert last["plan"]["delete"] == [] and last["plan"]["fetch"] == []


async def test_flac_is_converted_for_a_client_that_cannot_play_it(config, tmp_path):
    flac = tmp_path / "flac"
    make_music(flac / "Пісня.flac", title="Пісня", artist="Кобза", album="Перший")
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            body = {"id": "m", "type": "folder", "name": "M", "settings": {"root": str(flac)}}
            assert (await c.post("/api/v1/libraries", json=body)).status_code == 201
            await allow(c)
            device_id, token = await pair(c, (await new_code(c))["code"])  # plays mp3 only
            r = await c.post(
                "/api/v1/templates", json={"name": "T", "library_ids": ["m"], "rules": ALL_RULES}
            )
            cfg = await app.store.device(device_id)
            cfg.library_ids, cfg.rule_set = ["m"], RuleSet(template_ids=[r.json()["id"]])
            await app.store.put_device(cfg)
            view = await sync(c, bearer(token))
            assert view["status"] == "ready", view
            tracks = [f for f in view["plan"]["fetch"] if f["role"] == "track"]
            assert [f["path"].rsplit(".", 1)[1] for f in tracks] == ["mp3"]
            assert tracks[0]["mime"] == "audio/mpeg"
    finally:
        await app.stop()


async def test_events_are_deduplicated_and_reach_the_reverse_step(phone):
    app, c, device_id, h = phone
    view = await sync(c, h)
    await deliver(c, h, view)
    key = next(f["key"] for f in view["plan"]["fetch"] if f["role"] == "track")
    batch = {
        "events": [
            {
                "eid": "e1",
                "seq": 1,
                "type": "play",
                "completed": True,
                "key": key,
                "at": "2026-01-02T10:00:00Z",
                "clock": "ntp",
            },
            {"eid": "e2", "seq": 2, "type": "favourite", "on": True, "key": key},
        ]
    }
    r = await c.post(f"{C}/events", json=batch, headers=h)
    assert r.json() == {"acked_seq": 2, "accepted": 2, "duplicates": 0}
    r = await c.post(f"{C}/events", json=batch, headers=h)  # the answer was lost: sent again
    assert r.json() == {"acked_seq": 2, "accepted": 0, "duplicates": 2}
    dev = await app.client_sessions.device(device_id)
    fb = await dev.read_feedback(await dev.read_manifest())
    assert len(fb.plays) == 1 and fb.stars == {key: True}


async def test_events_need_the_client_token(phone):
    _, c, _, _ = phone
    r = await c.post(f"{C}/events", json={"events": []})
    assert r.status_code == 401
    r = await c.post(
        f"{C}/events",
        json={"events": [{"eid": "x", "type": "rating", "value": 9}]},
        headers={"Authorization": "Bearer nope"},
    )
    assert r.status_code == 401


async def _pending(app, device_id) -> int:
    dev = await app.client_sessions.device(device_id)
    return len(dev.load_events().pending)


async def test_events_reach_the_libraries_without_a_sync(phone):
    app, c, device_id, h = phone
    view = await sync(c, h)
    await deliver(c, h, view)
    key = next(f["key"] for f in view["plan"]["fetch"] if f["role"] == "track")
    app.client_sessions.events_debounce_s = 0.2
    for n in (1, 2):  # a burst: handed on once, after the last one
        ev = {"eid": f"f{n}", "seq": n, "type": "favourite", "on": True, "key": key}
        await c.post(f"{C}/events", json={"events": [ev]}, headers=h)
    assert len(app.client_sessions._deliver) == 1
    for _ in range(200):
        if not await _pending(app, device_id) and not app.jobs.device_busy(device_id):
            break
        await asyncio.sleep(0.05)
    assert await _pending(app, device_id) == 0  # consumed by the reverse step of an update


async def test_events_wait_while_the_client_syncs(phone):
    app, c, device_id, h = phone
    view = await sync(c, h)
    await deliver(c, h, view)
    key = next(f["key"] for f in view["plan"]["fetch"] if f["role"] == "track")
    open_view = await sync(c, h)  # the client is syncing: its own reverse step takes them
    assert open_view["status"] == "ready"
    app.client_sessions.events_debounce_s = 0.05
    calls = []
    real = app.sync.update_sync
    app.sync.update_sync = lambda *a, **k: calls.append(a) or real(*a, **k)
    ev = {"eid": "w1", "seq": 1, "type": "favourite", "on": True, "key": key}
    await c.post(f"{C}/events", json={"events": [ev]}, headers=h)
    await asyncio.sleep(0.3)
    assert calls == [] and await _pending(app, device_id) == 1


async def test_deleted_by_user_stays_off_the_client(phone):
    app, c, device_id, h = phone
    view = await sync(c, h)
    await deliver(c, h, view)
    track = next(f for f in view["plan"]["fetch"] if f["role"] == "track")
    cfg = await app.store.device(device_id)
    cfg.delete_threshold_pct = 100  # one of two tracks leaves: no review for this test
    await app.store.put_device(cfg)
    ev = {"eid": "d1", "seq": 1, "type": "deleted_by_user", "key": track["key"]}
    r = await c.post(f"{C}/events", json={"events": [ev]}, headers=h)
    assert r.status_code == 200, r.text
    # the client no longer holds it: the next sync neither fetches it again nor keeps it in the plan
    view = await sync(
        c,
        h,
        {
            "capabilities": CAPS,
            "inventory": {
                "kind": "full",
                "items": [
                    {"path": f["path"], "size": f["size"]}
                    for f in view["plan"]["fetch"]
                    if f["path"] != track["path"]
                ],
            },
        },
    )
    assert view["status"] == "ready", view
    assert track["key"] not in {f["key"] for f in view["plan"]["fetch"]}
    own = (await app.store.templates())[own_template_id(device_id)]
    assert [r.id for r in own.excludes] == [
        "removed-from-device"
    ]  # shown on the device's Templates tab


async def test_plan_carries_metadata_and_listening_state(phone):
    app, c, device_id, h = phone
    view = await sync(c, h)
    tracks = [f for f in view["plan"]["fetch"] if f["role"] == "track"]
    assert {f["meta"]["title"] for f in tracks} == {"One", "Two"}
    assert all(f["meta"]["kind"] == "music" and "position_s" not in f["meta"] for f in tracks)
    state = {s["key"]: s for s in view["plan"]["state"]}
    assert set(state) == {f["key"] for f in tracks}
    assert all(s["favourite"] is False and s["play_count"] == 0 for s in state.values())
    r = await c.get(
        f"{C}/sync/{view['session_id']}/plan", params={"section": "state", "limit": 1}, headers=h
    )
    assert len(r.json()["items"]) == 1 and r.json()["next"] == "1"


async def test_playlist_edits_shape_the_clients_playlists(phone):
    from syncmusic.devices.client import ClientEvent

    app, c, device_id, h = phone
    view = await sync(c, h)
    await deliver(c, h, view)
    dev = await app.client_sessions.device(device_id)
    m = await dev.read_manifest()
    one, two = sorted(m.items, key=lambda k: m.items[k].path)
    m.playlist_refs = {"Mix": [one, two]}  # as last synced
    edits = [
        {"name": "Mix", "ops": [{"op": "move", "key": two, "index": 0}]},
        {"name": "Mix", "ops": [{"op": "remove", "key": one}, {"op": "add", "key": "nd/none"}]},
        {"name": "Новий", "ops": [{"op": "add", "key": one}]},  # made on the client
    ]
    dev.add_events(
        [
            ClientEvent(eid=f"p{i}", seq=i, type="playlist_edit", clock="ntp", **e)
            for i, e in enumerate(edits, 1)
        ]
    )
    lists = await dev.read_playlists(m)
    assert lists == {"Mix": [m.items[two].path], "Новий": [m.items[one].path]}
    await dev.read_feedback(m)  # the reverse step consumes them once it pushed
    assert dev._read_events == {"p1", "p2", "p3"}


def test_an_empty_object_counts_as_an_empty_list() -> None:
    import pydantic

    from syncmusic.core.client_sessions import AckRequest, SyncRequest

    req = SyncRequest.model_validate({"inventory": {"kind": "full", "items": {}, "removed": {}}})
    assert req.inventory.items == [] and req.inventory.removed == []
    ack = AckRequest.model_validate({"done": {}, "deleted": {}, "failed": {}})
    assert ack.done == [] and ack.deleted == [] and ack.failed == []
    with pytest.raises(pydantic.ValidationError):
        SyncRequest.model_validate({"inventory": {"items": {"path": "Music/a.mp3"}}})


async def test_a_download_ticket_works_without_the_token_for_its_session_only(phone):
    app, c, device_id, h = phone
    view = await sync(c, h)
    sid, item = view["session_id"], view["plan"]["fetch"][0]
    assert item["url"].startswith(f"files/{item['fid']}?ticket=")
    whole = await download(c, h, item)
    r = await c.get(f"{C}/{item['url']}")
    assert r.status_code == 200 and r.content == whole
    r = await c.get(f"{C}/{item['url']}", headers={"Range": "bytes=10-"})
    assert r.status_code == 206 and r.content == whole[10:]
    ticket = item["url"].split("ticket=")[1]
    assert (await c.get(f"{C}/files/{'0' * 64}?ticket={ticket}")).status_code == 404
    assert (await c.get(f"{C}/files/{item['fid']}?ticket=wrong")).status_code == 404
    await c.post(f"{C}/sync/{sid}/finish", json={"status": "partial"}, headers=h)
    assert (await c.get(f"{C}/{item['url']}")).status_code == 404  # the session is closed
