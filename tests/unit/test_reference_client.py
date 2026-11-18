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
    await rc.pair("http://t/", (await new_code(http))["code"], "New")
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
    await app.store.put_device(cfg)
    monkeypatch.setattr(client_sessions, "MB", 1)  # one byte: one file per window
    dev = await app.client_sessions.device(device_id)

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
