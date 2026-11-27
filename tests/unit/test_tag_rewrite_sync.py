"""A library file whose tags were rewritten (a tagger) but whose audio is the same is retagged on the device,
not downloaded and copied again; new audio is still a new file."""

from __future__ import annotations

import mutagen
import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core import manifest as manifest_io
from syncmusic.facade import AppFacade
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.fake_navidrome import FakeNavidrome, song
from tests.media import make_music
from tests.unit.test_api_sync import _apply, _register


@pytest.fixture
def server(tmp_path, monkeypatch):
    nd = FakeNavidrome(native=False)
    path = make_music(tmp_path / "src" / "s1.flac", seconds=5.0, title="Пісня", artist="Гурт")
    s = song("s1", "Пісня")
    s.update(_data=path.read_bytes(), size=path.stat().st_size, suffix="flac")
    s["contentType"] = "audio/flac"
    nd.add(s)
    nd.src = path
    monkeypatch.setattr(SubsonicLibrary, "transport", nd)
    return nd


@pytest.fixture
async def env(config, server, tmp_path):
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/libraries",
            json={
                "id": "nd",
                "type": "navidrome",
                "name": "Navidrome",
                "settings": {"url": "http://nd.test", "username": "admin", "password": "pw"},
            },
        )
        assert r.status_code == 201, r.text
        mount = tmp_path / "flash"
        dev = await _register(c, mount, library_ids=["nd"])
        yield app, c, mount, dev["id"]
    await app.stop()


async def _plan(app, c, dev_id):
    r = await c.post("/api/v1/libraries/nd/refresh")
    assert r.status_code < 300, r.text
    return (await c.post(f"/api/v1/devices/{dev_id}/plan", json={})).json()


def _rewrite(server, **tags) -> None:
    audio = mutagen.File(server.src, easy=True)
    for k, v in tags.items():
        audio[k] = v
    audio.save()
    s = server.songs["s1"]
    s.update(_data=server.src.read_bytes(), size=server.src.stat().st_size)
    server.last_scan = "2026-11-26T05:00:00Z"  # the server rescanned the changed file


async def test_tag_rewrite_is_a_tags_only_update(env, server):
    app, c, mount, dev_id = env
    plan = await _plan(app, c, dev_id)
    await _apply(app, c, plan["id"], first_sync="keep")
    entry = manifest_io.read(mount).items["nd/s1"]
    assert entry.audio_id and entry.audio_id.startswith("flac:")

    _rewrite(server, comment="x" * 40000)  # a tagger: bigger file (past the padding), same audio
    plan = await _plan(app, c, dev_id)
    (a,) = [a for a in plan["actions"] if a["ref"]["item_id"] == "s1"]
    assert a["kind"] == "replace" and a["reason"] == "tags changed in the library"
    assert a["tags_only"]
    server.download_bytes = 0
    report = await _apply(app, c, plan["id"])
    assert report["counts"]["replace"] == 1
    assert server.download_bytes < 64 * 1024  # never the whole file
    after = manifest_io.read(mount).items["nd/s1"]
    assert after.source_state.startswith(f"{server.songs['s1']['size']}:")
    assert after.audio_id == entry.audio_id
    plan = await _plan(app, c, dev_id)  # settled
    assert all(a["kind"] == "keep" for a in plan["actions"])


async def test_new_audio_is_still_a_new_file(env, server, tmp_path):
    app, c, mount, dev_id = env
    plan = await _plan(app, c, dev_id)
    await _apply(app, c, plan["id"], first_sync="keep")
    other = make_music(tmp_path / "o.flac", seconds=6.0, title="Пісня")
    server.src = other
    _rewrite(server)
    plan = await _plan(app, c, dev_id)
    (a,) = [a for a in plan["actions"] if a["ref"]["item_id"] == "s1"]
    assert a["kind"] == "replace" and a["reason"] == "source changed"


async def test_entries_of_an_older_version_learn_the_identity_then_retag(env, server):
    app, c, mount, dev_id = env
    plan = await _plan(app, c, dev_id)
    await _apply(app, c, plan["id"], first_sync="keep")
    m = manifest_io.read(mount)
    m.items["nd/s1"].audio_id = None  # written by a version without audio identities
    manifest_io.write(mount, m)

    plan = await _plan(app, c, dev_id)  # unchanged source: the identity is learnt on the way
    assert all(a["kind"] == "keep" for a in plan["actions"])
    await _apply(app, c, plan["id"])
    assert manifest_io.read(mount).items["nd/s1"].audio_id

    _rewrite(server, comment="y" * 40000)
    plan = await _plan(app, c, dev_id)
    (a,) = [a for a in plan["actions"] if a["ref"]["item_id"] == "s1"]
    assert a["reason"] == "tags changed in the library" and a["tags_only"]
