"""Folder library over FTP against the compose pure-ftpd: it scans like the same folder on disk
(pure-ftpd refuses REST in ASCII mode, which pyftpdlib in the unit tests allows), processing gets whole files."""

from __future__ import annotations

import ftplib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncmusic.core.models import LibraryConfig
from syncmusic.libraries import remote
from syncmusic.libraries.folder import FolderLibrary
from tests.integration import services as svc
from tests.integration.test_smb_folder import _view
from tests.media import make_music

pytestmark = pytest.mark.integration


def _upload(src: Path, base: str) -> None:
    with ftplib.FTP() as ftp:
        ftp.encoding = "utf-8"
        ftp.connect(svc.ENV["FTP_HOST"], int(svc.ENV["FTP_PORT"]))
        ftp.login(svc.ENV["FTP_USER"], svc.ENV["FTP_PASSWORD"])
        ftp.mkd(base)
        for p in sorted(src.rglob("*")):
            remote = f"{base}/{p.relative_to(src).as_posix()}"
            if p.is_dir():
                ftp.mkd(remote)
            else:
                with p.open("rb") as f:
                    ftp.storbinary(f"STOR {remote}", f)


async def test_ftp_folder_scans_like_local(services, tmp_path, monkeypatch):
    # every media file read sparse (REST), as a big FLACs are: a whole download would switch to binary
    monkeypatch.setattr(remote, "WHOLE_BELOW", 0)
    src = tmp_path / "src"
    album = src / "Луперкаль" / "Зимородок (2016)"
    for n, title in enumerate(("Ау", "Зимородок", "Сни", "Мрія"), 1):
        make_music(
            album / f"0{n} - {title}.flac",
            seconds=20,
            title=title,
            artist="Луперкаль",
            album="Зимородок",
        )
    make_music(src / "Інше" / "Пісня.mp3", title="Пісня", artist="Хтось", album="Інше")
    base = f"/sd-lib-{uuid.uuid4().hex[:6]}"
    _upload(src, base)
    local = FolderLibrary(
        LibraryConfig(id="loc", type="folder", name="L", settings={"root": str(src)}), None
    )
    want = _view(await local.snapshot())
    settings = {
        "source": "ftp",
        "host": svc.ENV["FTP_HOST"],
        "port": int(svc.ENV["FTP_PORT"]),
        "username": svc.ENV["FTP_USER"],
        "password": svc.ENV["FTP_PASSWORD"],
        "remote_path": base,
    }
    ctx = SimpleNamespace(cache_dir=tmp_path / "cache")
    lib = FolderLibrary(LibraryConfig(id="ftp", type="folder", name="FTP", settings=settings), ctx)
    assert (await lib.check()).ok
    snap = await lib.snapshot()
    assert _view(snap) == want  # all 5, none dropped by "503 ASCII resume is unsafe"
    flac = next(t for t in snap.tracks.values() if t.title == "Зимородок")
    assert (await lib.open_media(flac.ref.item_id)).read_bytes() == (src / flac.path).read_bytes()
