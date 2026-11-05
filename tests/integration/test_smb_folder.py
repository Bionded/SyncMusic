"""Folder library over SMB against the compose Samba: the user share and the guest share scan like
the same folder on disk, processing gets whole files, a wrong password says why (and never shows it)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from syncmusic.core.models import LibraryConfig
from syncmusic.libraries.folder import FolderLibrary
from tests.integration import services as svc
from tests.media import make_music

pytestmark = pytest.mark.integration


def _view(snap):
    return sorted(
        (
            t.path,
            t.title,
            t.artist,
            t.album,
            t.format,
            t.size_bytes,
            round(t.duration_s or 0),
            t.has_lyrics,
        )
        for t in snap.tracks.values()
    )


def _smb(tmp_path, lib_id, **settings):
    s = {
        "source": "smb",
        "host": svc.ENV["SMB_HOST"],
        "port": int(svc.ENV["SMB_PORT"]),
        **settings,
    }
    ctx = SimpleNamespace(cache_dir=tmp_path / "cache")
    return FolderLibrary(LibraryConfig(id=lib_id, type="folder", name=lib_id, settings=s), ctx)


async def test_smb_user_and_guest_scan_like_local(services, tmp_path):
    src = tmp_path / "src"
    band = src / "Музика" / "Бумбокс"
    make_music(
        band / "01 Вахтерам.flac",
        seconds=40,
        title="Вахтерам",
        artist="Бумбокс",
        album="Family бізнес",
    )
    make_music(
        band / "02 Та4то.m4a", seconds=90, title="Та4то", artist="Бумбокс", album="Family бізнес"
    )
    make_music(band / "03 Наодинці.mp3", title="Наодинці", artist="Бумбокс", album="Family бізнес")
    (band / "03 Наодинці.lrc").write_text("[00:01.00]Наодинці\n", encoding="utf-8")
    svc.smb_files(src)
    local = FolderLibrary(
        LibraryConfig(id="loc", type="folder", name="L", settings={"root": str(src)}), None
    )
    want = _view(await local.snapshot())
    user = _smb(
        tmp_path,
        "user",
        username=svc.ENV["SMB_USER"],
        password=svc.ENV["SMB_PASSWORD"],
        remote_path="/music",
    )
    guest = _smb(tmp_path, "guest", remote_path="/public/Музика")
    assert (await user.check()).ok and (await guest.check()).ok
    snap = await user.snapshot()
    assert _view(snap) == want
    assert len((await guest.snapshot()).tracks) == 3
    flac = next(t for t in snap.tracks.values() if t.title == "Вахтерам")
    assert (await user.open_media(flac.ref.item_id)).read_bytes() == (src / flac.path).read_bytes()


async def test_smb_wrong_password_and_missing_share(services, tmp_path):
    wrong = _smb(
        tmp_path,
        "w",
        username=svc.ENV["SMB_USER"],
        password="not-the-password",
        remote_path="/music",
    )
    health = await wrong.check()
    assert not health.ok and "smb://" in health.message and "not-the-password" not in health.message
    gone = _smb(
        tmp_path,
        "g",
        username=svc.ENV["SMB_USER"],
        password=svc.ENV["SMB_PASSWORD"],
        remote_path="/nope",
    )
    assert not (await gone.check()).ok
