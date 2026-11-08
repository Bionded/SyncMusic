"""Stable source ids: the same source gives the same id whatever URL or mount path reaches it; the id is stored
when first read and a different one later is kept apart, never silently swapped."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import os
from pathlib import Path

from syncmusic.core.models import LibraryConfig
from syncmusic.libraries import folder as folder_mod
from syncmusic.libraries.folder import FolderLibrary
from tests.fake_abs import FakeABS
from tests.fake_jellyfin import FakeJellyfin
from tests.fake_navidrome import FakeNavidrome, song
from tests.fake_plex import FakePlex
from tests.unit.test_abs_library import make as make_abs
from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures
from tests.unit.test_jellyfin_library import _lib as make_jellyfin
from tests.unit.test_navidrome_library import make as make_nd
from tests.unit.test_plex_library import _lib as make_plex


def _navidrome() -> FakeNavidrome:
    nd = FakeNavidrome()
    for i, album in enumerate(["Beta", "Gamma", "Delta"]):
        nd.add(song(f"s{i}", f"Song {i}", album=album, albumId=f"al-{album}"))
    return nd


async def test_subsonic_same_server_under_two_urls_has_one_id():
    nd = _navidrome()
    a = await make_nd(nd).source_id()
    b = await make_nd(nd, url="http://192.0.2.10:4533/").source_id()
    assert a == b and a is not None and a.startswith("subsonic:")
    other = FakeNavidrome()
    other.add(song("x", "X", album="Beta", albumId="al-other"))
    assert await make_nd(other).source_id() != a


async def test_subsonic_fingerprint_drifts_with_a_new_first_album():
    nd = _navidrome()
    before = await make_nd(nd).source_id()
    nd.add(song("new", "New", album="Alpha", albumId="al-Alpha"))
    assert await make_nd(nd).source_id() != before  # matching falls back to probing


async def test_subsonic_empty_server_has_no_id():
    assert await make_nd(FakeNavidrome()).source_id() is None


async def test_abs_uses_library_uuids():
    srv = FakeABS()
    assert await make_abs(srv).source_id() == "abs:lb,lp"  # book + podcast libraries, not ebooks
    assert await make_abs(srv, library_ids=["lp"]).source_id() == "abs:lp"


async def test_jellyfin_and_plex_use_the_server_ids():
    assert (
        await make_jellyfin(FakeJellyfin(), password="pw").source_id()
        == "jellyfin:f00dfeedf00dfeed"
    )
    plex = await make_plex(FakePlex()).source_id()
    assert plex is not None and plex.startswith("plex:") and plex.endswith(":sec-uuid-1")


def _folder(**settings) -> FolderLibrary:
    return FolderLibrary(LibraryConfig(id="f", type="folder", name="F", settings=settings), None)


async def test_remote_folder_id_has_no_credentials():
    lib = _folder(
        source="smb",
        host="NAS.Example",
        username="u",
        password="secret",
        remote_path="/Share//Music/",
    )
    assert await lib.source_id() == "folder:smb://nas.example/Share/Music"
    ftp = _folder(source="ftp", host="ftp.example", port=2121, remote_path="/")
    assert await ftp.source_id() == "folder:ftp://ftp.example:2121/"


async def test_local_folder_same_fs_under_two_mount_points(tmp_path, monkeypatch):
    # the same filesystem (root /data) mounted twice; the library sits at the same place inside it
    for point in ("m1", "m2"):
        (tmp_path / point / "Music").mkdir(parents=True)
    dev = os.stat(tmp_path).st_dev
    mm = f"{os.major(dev)}:{os.minor(dev)}"
    info = tmp_path / "mountinfo"
    info.write_text(
        f"1 0 {mm} /data {tmp_path / 'm1'} rw - ext4 /dev/x rw\n"
        f"2 0 {mm} /data {tmp_path / 'm2'} rw - ext4 /dev/x rw\n"
        f"3 0 0:99 / {tmp_path / 'm2' / 'Music'} rw - tmpfs tmpfs rw\n"  # another device: ignored
    )
    monkeypatch.setattr(folder_mod, "MOUNTINFO", info)
    monkeypatch.setattr(folder_mod, "_fs_uuid", lambda d: "UUID-1" if d == dev else None)
    a = await _folder(root=str(tmp_path / "m1" / "Music")).source_id()
    b = await _folder(root=str(tmp_path / "m2" / "Music")).source_id()
    assert a == b == "folder:UUID-1:/data/Music"
    monkeypatch.setattr(folder_mod, "_fs_uuid", lambda d: None)
    assert await _folder(root=str(tmp_path / "m1" / "Music")).source_id() is None


def test_mount_point_escapes():
    assert folder_mod._unescape_mount(r"/media/My\040Disk") == "/media/My Disk"


async def test_stored_at_add_and_kept_when_the_source_changes(env, monkeypatch):
    app, c = env
    ids = iter(["folder:A:/m", "folder:B:/m", "folder:A:/m"])

    async def fake(self) -> str:
        return next(ids)

    monkeypatch.setattr(FolderLibrary, "source_id", fake)
    root = Path((await app.store.library("music")).settings["root"])  # type: ignore[union-attr]
    r = await c.post(
        "/api/v1/libraries",
        json={
            "id": "m2",
            "type": "folder",
            "name": "M2",
            "settings": {"root": str(root)},
            "source_id": "forged",
        },
    )
    assert r.status_code == 201 and r.json()["source_id"] == "folder:A:/m"
    lib = await app.sync.library_as("m2")
    assert await app.sync.refresh_source_id(lib) == "folder:B:/m"
    cfg = await app.store.library("m2")
    assert cfg is not None and (cfg.source_id, cfg.source_id_now) == ("folder:A:/m", "folder:B:/m")
    await app.sync.refresh_source_id(lib)  # back where it was
    cfg = await app.store.library("m2")
    assert cfg is not None and (cfg.source_id, cfg.source_id_now) == ("folder:A:/m", None)
    r = await c.patch("/api/v1/libraries/m2", json={"source_id": "x", "name": "M3"})
    assert r.status_code == 200, r.text
    assert r.json()["source_id"] == "folder:A:/m" and r.json()["name"] == "M3"


async def test_unreachable_source_leaves_the_config(env, monkeypatch):
    app, _ = env

    async def boom(self) -> str:
        raise OSError("down")

    monkeypatch.setattr(FolderLibrary, "source_id", boom)
    lib = await app.sync.library_as("music")
    before = await app.store.library("music")
    assert await app.sync.refresh_source_id(lib) == (before.source_id if before else None)
    assert await app.store.library("music") == before
