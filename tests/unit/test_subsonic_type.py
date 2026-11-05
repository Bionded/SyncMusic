"""the Navidrome library type is the Subsonic-compatible type `subsonic`. Old `navidrome` configs load and
are migrated (same ids, same settings); Navidrome extras (native API) are used only when the server is Navidrome."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core import registry
from syncmusic.core.models import LibraryConfig
from syncmusic.libraries.subsonic import SubsonicLibrary
from tests.fake_navidrome import FakeNavidrome, song
from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures
from tests.unit.test_navidrome_library import make


def test_old_type_name_loads_as_subsonic():
    cfg = LibraryConfig.model_validate(
        {"id": "navidrome", "type": "navidrome", "name": "ND", "settings": {"url": "http://x"}}
    )
    assert cfg.type == "subsonic" and cfg.id == "navidrome"
    assert (
        registry.library_class("navidrome") is SubsonicLibrary is registry.library_class("subsonic")
    )
    assert (
        SubsonicLibrary.display_name == "Subsonic-compatible"
        and "Gonic" in SubsonicLibrary.display_hint
    )
    assert [c.type_name for c in registry.library_classes()][
        0
    ] == "subsonic"  # the Add form's default


async def test_api_takes_the_old_name_and_lists_the_new_one(env):
    _, c = env
    r = await c.post(
        "/api/v1/libraries",
        json={
            "id": "nd",
            "type": "navidrome",
            "name": "ND",
            "settings": {"url": "http://127.0.0.1:9", "username": "u", "password": "p"},
        },
    )
    assert r.status_code == 201 and r.json()["type"] == "subsonic"
    types = (await c.get("/api/v1/library-types")).json()
    sub = next(t for t in types if t["type"] == "subsonic")
    assert sub["display_name"] == "Subsonic-compatible" and sub["display_hint"].startswith(
        "Navidrome, Gonic"
    )
    assert sub["capabilities"]["group_covers"] is True
    assert not any(t["type"] == "navidrome" for t in types)


async def test_native_extras_only_on_navidrome():
    for server_type, native_calls in (("navidrome", True), ("gonic", False)):
        srv = FakeNavidrome(server_type=server_type)
        srv.add(song("a", "A"))
        lib = make(srv)
        snap = await lib.snapshot()
        assert list(snap.tracks) == ["a"]
        used = any(c in ("login", "song", "album", "playlist") for c in srv.calls)
        assert used is native_calls, (server_type, srv.calls)
    srv = FakeNavidrome(server_type="navidrome")
    srv.add(song("a", "A"))
    off = make(srv, use_native_api=False)
    await off.snapshot()
    assert not any(c in ("login", "song", "album", "playlist") for c in srv.calls)
