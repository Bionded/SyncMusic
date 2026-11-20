"""The reference Wi-Fi client end to end against an in-process server: pairing, first sync into a folder, update,
interrupted download resumed, deletions, events up (sent twice, counted once)."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core.models import RuleSet
from syncmusic.facade import AppFacade
from syncmusic.reference_client import ClientProtocolError, ReferenceClient
from tests.media import make_music
from tests.unit.test_client_sync import ALL_RULES
from tests.unit.test_clients import allow, new_code


@pytest.fixture
async def server(config, tmp_path):
    music = tmp_path / "music"
    make_music(music / "A" / "01 One.mp3", title="One", artist="Кобза", album="Перший")
    make_music(music / "A" / "02 Two.mp3", title="Two", artist="Кобза", album="Перший")
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
        r = await c.post(
            "/api/v1/templates",
            json={"name": "Phone rules", "library_ids": ["music"], "rules": ALL_RULES},
        )
        template = r.json()["id"]

        async def paired(folder) -> ReferenceClient:
            rc = ReferenceClient(folder, http=c)
            rc.formats = ["mp3"]
            device_id = await rc.pair("http://t/", (await new_code(c))["code"], "Ref")
            cfg = await app.store.device(device_id)
            cfg.library_ids = ["music"]
            cfg.rule_set = RuleSet(template_ids=[template])
            cfg.delete_threshold_pct = 100
            cfg.registration_pending = False  # registered on the server
            await app.store.put_device(cfg)
            return rc

        yield app, paired, music
    await app.stop()


def tracks(folder) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.mp3"))


async def test_first_sync_update_and_deletion(server, tmp_path):
    app, paired, music = server
    rc = await paired(tmp_path / "phone")
    first = await rc.sync()
    assert first["fetched"] >= 2 and first["failed"] == 0
    assert len(tracks(rc.folder)) == 2
    assert (await rc.sync())["fetched"] == 0  # nothing changed
    # reopened from its state file: same pairing
    again = ReferenceClient(rc.folder, http=rc.http)
    again.formats = rc.formats
    assert again.state["device_id"] == rc.state["device_id"]
    (music / "A" / "02 Two.mp3").unlink()
    out = await again.sync()
    assert out["deleted"] >= 1 and len(tracks(rc.folder)) == 1


async def test_interrupted_download_resumes(server, tmp_path):
    _, paired, _ = server
    rc = await paired(tmp_path / "phone")
    view = await rc.start()
    item = next(f for f in view["plan"]["fetch"] if f["role"] == "track")
    assert not await rc.download(item, stop_after=1)  # the Wi-Fi dropped
    part = rc.folder / (item["path"] + ".part")
    assert part.exists() and 0 < part.stat().st_size < item["size"]
    await rc.http.post(
        rc._url(f"sync/{view['session_id']}/finish"),
        json={"status": "partial"},
        headers=rc._headers(),
    )
    out = await rc.sync()  # the next sync continues the .part
    assert out["failed"] == 0 and not part.exists() and len(tracks(rc.folder)) == 2


async def test_events_recorded_offline_are_sent_once(server, tmp_path):
    app, paired, _ = server
    rc = await paired(tmp_path / "phone")
    view = await rc.start()
    key = next(f["key"] for f in view["plan"]["fetch"] if f["role"] == "track")
    await rc.http.post(
        rc._url(f"sync/{view['session_id']}/finish"),
        json={"status": "cancelled"},
        headers=rc._headers(),
    )
    rc.record("play", key, completed=True)
    rc.record("favourite", key, on=True)
    lost = list(rc.state["events"])
    assert await rc.send_events() == 2 and rc.state["events"] == []
    rc.state["events"] = lost  # the answer was lost: the client sends them again
    await rc.send_events()
    dev = await app.client_sessions.device(rc.state["device_id"])
    assert len(dev.load_events().pending) == 2


async def test_client_without_templates_gets_a_reason_and_no_traceback(server, tmp_path, caplog):
    import logging

    from tests.unit.test_clients import new_code

    app, paired, _ = server
    http = (await paired(tmp_path / "other")).http
    rc = ReferenceClient(tmp_path / "phone", http=http)
    rc.formats = ["mp3"]
    device_id = await rc.pair("http://t/", (await new_code(http))["code"], "New")
    with pytest.raises(ClientProtocolError, match="registration_pending"):
        await rc.start()  # paired, not registered yet: the app waits instead of syncing nothing
    cfg = await app.store.device(device_id)
    cfg.registration_pending = False  # registered without a template
    await app.store.put_device(cfg)
    caplog.set_level(logging.WARNING)
    with pytest.raises(ClientProtocolError, match="no templates"):
        await rc.start()
    s = next(iter(app.client_sessions.sessions.values()))
    view = app.client_sessions.view(s)
    assert view["status"] == "failed" and view["code"] == "no_templates"
    hits = [r for r in caplog.records if "no templates" in r.getMessage()]
    assert hits and all(r.levelname == "WARNING" and not r.exc_info for r in hits)


async def test_generated_set_is_installed_once_with_its_commit_file(server, tmp_path, monkeypatch):
    from syncmusic.devices.client import ClientDevice, GeneratedFile, GeneratedSet

    app, paired, music = server
    built = tmp_path / "built"
    built.mkdir()
    (built / "a.idx").write_bytes(b"first part")
    (built / "b.idx").write_bytes(b"second part")
    set_id = ["v1"]

    def generated(self, manifest):
        return GeneratedSet(
            set_id=set_id[0],
            commit="Index/commit",
            files=[
                GeneratedFile(path="Index/a.idx", source=built / "a.idx"),
                GeneratedFile(path="Index/b.idx", source=built / "b.idx"),
            ],
        )

    monkeypatch.setattr(ClientDevice, "generated_files", generated)
    rc = await paired(tmp_path / "phone")
    first = await rc.sync()
    assert first["generated"] == 1 and first["failed"] == 0
    idx = rc.folder / "Index"
    assert (idx / "a.idx").read_bytes() == b"first part" and (idx / "commit").read_text() == "v1"
    assert not list(idx.glob("*.new*"))
    one = next(rc.folder.rglob("*.mp3"))
    assert one.stat().st_mtime > 0
    # the same set again is skipped; the generated files are not reported as synced files
    assert (await rc.sync())["generated"] == 0
    assert all(not p.startswith("Index/") for p in rc._files())
    set_id[0] = "v2"
    (built / "b.idx").write_bytes(b"second part, rebuilt")
    assert (await rc.sync())["generated"] == 1
    assert (idx / "b.idx").read_bytes() == b"second part, rebuilt"
    assert (idx / "commit").read_text() == "v2"


async def test_pairing_request_approved_on_the_server(server, tmp_path):
    app, paired, _ = server
    http = (await paired(tmp_path / "other")).http
    rc = ReferenceClient(tmp_path / "phone", http=http)
    codes: list[str] = []

    async def approve() -> None:
        for _ in range(600):
            if reqs := (await http.get("/api/v1/clients/requests")).json():
                break
            await asyncio.sleep(0.05)
        assert reqs[0]["display_code"] == codes[0]
        await http.post(f"/api/v1/clients/requests/{reqs[0]['id']}/approve")

    task = asyncio.create_task(approve())
    device_id = await asyncio.wait_for(rc.request_pairing("http://t/", "Asked", codes.append), 30)
    await task
    assert (await app.clients.get(device_id)).name == "Asked"
    assert ReferenceClient(tmp_path / "phone", http=http).state["token"].startswith("smc_")


async def test_windowed_delivery_keeps_the_outbox_small_and_resumes(server, tmp_path, monkeypatch):
    """Delivery under a tiny outbox budget, end to end: one file per window, the outbox never holds more than
    the window, plain files are linked from the library (no copy), acked files leave the outbox, a download
    dropped in the middle of a window continues in the next session and the outbox ends empty."""
    from syncmusic.core import client_sessions

    app, paired, music = server
    rc = await paired(tmp_path / "phone")
    device_id = rc.state["device_id"]
    cfg = await app.store.device(device_id)
    cfg.settings = {**cfg.settings, "outbox_budget_mb": 1}
    cfg.registration_pending = False  # registered on the server
    await app.store.put_device(cfg)
    monkeypatch.setattr(client_sessions, "MB", 1)  # one byte: one file per window
    dev = await app.client_sessions.device(device_id)
    rc.stream = False  # an older client: outbox windows

    view = await rc.start()
    item = next(f for f in view["plan"]["fetch"] if f["role"] == "track")
    assert view["plan"]["more"] == 1
    held = dev.outbox()
    assert [p for p in held if p.endswith(".mp3")] == [item["path"]]
    assert (dev.mount / item["path"]).stat().st_nlink >= 2  # linked, not a second copy
    assert not await rc.download(item, stop_after=1)  # the Wi-Fi dropped mid-window
    await rc.http.post(
        rc._url(f"sync/{view['session_id']}/finish"),
        json={"status": "partial"},
        headers=rc._headers(),
    )
    assert item["path"] in dev.outbox()  # still waiting for the client

    out = await rc.sync()  # resumes the .part, then fetches the next window
    assert out["failed"] == 0 and len(tracks(rc.folder)) == 2
    assert not list(rc.folder.rglob("*.part"))
    assert not [p for p in dev.outbox() if p.endswith(".mp3")]


async def test_streaming_delivery_in_one_session_resumes_a_dropped_download(
    server, tmp_path, monkeypatch
):
    """A streaming client: one session lists every file as the server prepares it (here one ahead of the
    acks), a download dropped in the middle resumes in the next sync and the outbox ends empty."""
    from syncmusic.core import client_sessions

    app, paired, music = server
    rc = await paired(tmp_path / "phone")
    monkeypatch.setattr(client_sessions, "STREAM_AHEAD_FILES", 1)
    dev = await app.client_sessions.device(rc.state["device_id"])
    view = await rc.start()
    assert view["plan"]["stream"] and view["plan"]["more"] == 0
    first = await anext(f async for f in rc._pages(view, "fetch") if f["role"] == "track")
    assert not await rc.download(first, stop_after=1)  # the Wi-Fi dropped
    await rc.http.post(
        rc._url(f"sync/{view['session_id']}/finish"),
        json={"status": "partial"},
        headers=rc._headers(),
    )
    out = await rc.sync()
    assert out["failed"] == 0 and len(tracks(rc.folder)) == 2
    assert not list(rc.folder.rglob("*.part"))
    assert not [p for p in dev.outbox() if p.endswith(".mp3")]
    assert (await rc.sync())["fetched"] == 0


async def test_wifi_added_to_a_registered_device_syncs_into_that_device(server, tmp_path):
    """A player whose card is already a registered (USB) device pairs onto that device: its Wi-Fi syncs plan for
    the same record (its rules, its items) and the record stays the USB device."""
    from syncmusic.core.models import DeviceConfig, DeviceIdentity

    app, paired, _ = server
    http = (await paired(tmp_path / "other")).http
    ref_cfg = (await app.store.devices())[0]
    await app.store.put_device(
        DeviceConfig(
            id="card-drive",
            name="Card drive",
            type="usb_drive",
            identity=DeviceIdentity(manifest_device_id="0f1e2d3c-ffff"),
            library_ids=["music"],
            rule_set=ref_cfg.rule_set,
            delete_threshold_pct=100,
        )
    )
    rc = ReferenceClient(tmp_path / "card", http=http)
    rc.formats, rc.card_uid = ["mp3"], "0f1e2d3c-ffff"

    async def approve() -> None:
        for _ in range(600):
            if reqs := (await http.get("/api/v1/clients/requests")).json():
                break
            await asyncio.sleep(0.05)
        assert [d["id"] for d in reqs[0]["same_card"]] == ["card-drive"]
        r = await http.post(
            f"/api/v1/clients/requests/{reqs[0]['id']}/approve", json={"device_id": "card-drive"}
        )
        assert r.status_code == 200, r.text

    task = asyncio.create_task(approve())
    assert await asyncio.wait_for(rc.request_pairing("http://t/", "R1"), 30) == "card-drive"
    await task
    out = await rc.sync()
    assert out["failed"] == 0 and len(tracks(rc.folder)) == 2
    assert (await rc.sync())["fetched"] == 0
    stored = await app.store.device("card-drive")
    assert stored.type == "usb_drive" and len(await app.store.devices()) == 2


async def test_wifi_sync_writes_the_card_manifest_a_usb_sync_reads(server, tmp_path):
    """A player paired onto a registered device gets that device's manifest onto its card after a complete
    sync: the same uid and every file it took, so a USB sync of the card sees them as its own. A USB sync
    later hands its manifest back to the Wi-Fi side."""
    from types import SimpleNamespace

    from syncmusic.core import manifest as manifest_io
    from syncmusic.core.models import DeviceConfig, DeviceIdentity
    from syncmusic.core.sync import foreign_manifest

    app, paired, _ = server
    http = (await paired(tmp_path / "other")).http
    ref_cfg = (await app.store.devices())[0]
    await app.store.put_device(
        DeviceConfig(
            id="card-drive",
            name="Card drive",
            type="usb_drive",
            identity=DeviceIdentity(manifest_device_id="0f1e2d3c-eeee"),
            library_ids=["music"],
            rule_set=ref_cfg.rule_set,
            delete_threshold_pct=100,
        )
    )
    rc = ReferenceClient(tmp_path / "card", http=http)
    rc.formats, rc.card_uid = ["mp3"], "0f1e2d3c-eeee"

    async def approve() -> None:
        for _ in range(600):
            if reqs := (await http.get("/api/v1/clients/requests")).json():
                break
            await asyncio.sleep(0.05)
        await http.post(
            f"/api/v1/clients/requests/{reqs[0]['id']}/approve", json={"device_id": "card-drive"}
        )

    task = asyncio.create_task(approve())
    await asyncio.wait_for(rc.request_pairing("http://t/", "R1"), 30)
    await task
    assert (await rc.sync())["failed"] == 0
    card = manifest_io.read(rc.folder)
    assert card is not None
    assert not foreign_manifest(card, await app.store.device("card-drive"))
    assert sorted(e.path for e in card.items.values()) == sorted(tracks(rc.folder))
    assert (rc.folder / ".syncmusic/manifest.jsonl.bak").read_bytes() == (
        rc.folder / ".syncmusic/manifest.jsonl"
    ).read_bytes()
    assert not any(p.startswith(".syncmusic/") for p in rc._files())

    # a USB sync of the card changed its manifest: the Wi-Fi side takes the card's copy
    usb = tmp_path / "usb"
    (usb / ".syncmusic").mkdir(parents=True)
    card.header.app_version = "0.0.1-usb"
    changed = manifest_io.dumps(card)
    assert changed.encode() != (rc.folder / ".syncmusic/manifest.jsonl").read_bytes()
    (usb / ".syncmusic/manifest.jsonl").write_text(changed, encoding="utf-8")
    cfg = await app.store.device("card-drive")
    caps = (await app.client_sessions.device("card-drive")).effective_capabilities()
    usb_dev = SimpleNamespace(network=False, mount=usb, effective_capabilities=lambda: caps)
    await app.sync._mirror_card_manifest(cfg, usb_dev)
    outbox = app.sync._network_dir(cfg) / ".syncmusic/manifest.jsonl"
    assert outbox.read_text(encoding="utf-8") == changed
    assert (await rc.sync())["fetched"] == 0


async def test_reset_install_on_the_same_card_comes_back_configured(server, tmp_path):
    app, paired, _ = server
    rc = await paired(tmp_path / "card" / "app1")
    http = rc.http
    rc.card_uid = "card-ref-0001"
    device_id = await rc.pair("http://t/", (await new_code(http))["code"], "Ref")
    await rc.set_settings({"after_sync": "poweroff", "dim_after_s": 60})

    # the app's data is gone: a new install on the same card asks to pair
    fresh = ReferenceClient(tmp_path / "card" / "app2", http=http)
    fresh.card_uid = "card-ref-0001"
    assert fresh.state["uid"] != rc.state["uid"]

    async def approve() -> None:
        for _ in range(600):
            if reqs := (await http.get("/api/v1/clients/requests")).json():
                break
            await asyncio.sleep(0.05)
        await http.post(f"/api/v1/clients/requests/{reqs[0]['id']}/approve")

    task = asyncio.create_task(approve())
    again = await asyncio.wait_for(fresh.request_pairing("http://t/", "Ref"), 30)
    await task
    assert again == device_id and len(await app.clients.list()) == 1
    assert fresh.state["settings"] == {"after_sync": "poweroff", "dim_after_s": 60}
    assert [t["name"] for t in fresh.templates if t["on"]] == ["Phone rules"]
    assert (await app.store.device(device_id)).registration_pending is False
    with pytest.raises(ClientProtocolError):
        await rc._call("GET", "me")  # the old install's token is revoked
