"""The reference Wi-Fi client end to end against an in-process server: pairing, first sync into a folder, update,
interrupted download resumed, deletions, events up (sent twice, counted once)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core.models import RuleSet
from syncmusic.facade import AppFacade
from syncmusic.reference_client import ReferenceClient
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
