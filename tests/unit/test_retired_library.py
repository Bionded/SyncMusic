"""A library type that is not offered any more (Plex): not in the type lists, not creatable; a stored one loads as
unavailable with a clear message and a sync from it removes nothing on the device."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import pytest

from syncmusic.core import registry
from syncmusic.core.models import ActionKind
from syncmusic.core.sync import SyncError
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401


def test_plex_is_not_offered():
    assert "plex" not in registry.LIBRARIES
    assert "plex" not in {c.type_name for c in registry.library_classes()}
    assert registry.library_class("plex").type_name == "unsupported"


async def test_stored_plex_library_is_unavailable_and_removes_nothing(env, tmp_path):
    app, c = env
    types = (await c.get("/api/v1/library-types")).json()
    assert "plex" not in {t["type"] for t in types}
    r = await c.post(
        "/api/v1/libraries",
        json={"id": "px", "type": "plex", "name": "Plex", "settings": {"url": "http://p"}},
    )
    assert r.status_code == 400 and "Plex is not supported in this version" in r.json()["message"]
    await _register(c, tmp_path / "flash")
    pc = await app.sync.plan("my-flash")
    await _apply(app, c, pc.plan.id, first_sync="keep")
    on_device = sorted(p.name for p in (tmp_path / "flash").rglob("*.mp3"))
    assert on_device
    # the library becomes a Plex one, as a config from an older version would have it
    music = await app.store.library("music")
    await app.store.put_library(
        music.model_copy(update={"type": "plex", "settings": {"url": "http://p", "token": "t"}})
    )
    app.sync.forget_library("music")
    libs = {x["id"]: x for x in (await c.get("/api/v1/libraries")).json()}
    assert libs["music"]["type"] == "plex"
    health = (await c.post("/api/v1/libraries/music/check")).json()
    assert health["ok"] is False and "Plex is not supported in this version" in health["message"]
    try:
        pc = await app.sync.plan("my-flash")
    except SyncError as e:
        assert "not supported" in str(e) or "unavailable" in str(e).lower()
    else:
        assert not [a for a in pc.plan.actions if a.kind == ActionKind.DELETE]
    assert sorted(p.name for p in (tmp_path / "flash").rglob("*.mp3")) == on_device


@pytest.mark.parametrize("name", ["subsonic", "navidrome"])
def test_offered_types_still_load(name):
    assert registry.library_class(name).type_name == "subsonic"
