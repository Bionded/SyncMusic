"""Read-only folder picker against the compose Samba and pure-ftpd: an SMB server lists its shares
at `/`, a share's folder its subfolders with the preview counts (user and guest), an FTP folder the same; a wrong
password says why."""

from __future__ import annotations

import uuid

import pytest

from syncmusic.libraries import browse
from syncmusic.libraries.remote import RemoteError
from tests.integration import services as svc
from tests.integration.test_ftp_folder import _upload

pytestmark = pytest.mark.integration


def _tree(root):
    (root / "Музика" / "Бумбокс").mkdir(parents=True)
    (root / "Музика" / "Бумбокс" / "01.mp3").write_bytes(b"x")
    (root / "Музика" / "Бумбокс" / "deep").mkdir()
    (root / "Музика" / "top.flac").write_bytes(b"x")
    (root / "Книги").mkdir()
    (root / ".trash").mkdir()


def _smb(path, **login):
    return browse.browse_remote(
        "smb",
        host=svc.ENV["SMB_HOST"],
        port=int(svc.ENV["SMB_PORT"]),
        path=path,
        username=login.get("username", ""),
        password=login.get("password", ""),
    )


async def test_smb_share_folders_and_preview(services, tmp_path):
    _tree(tmp_path / "src")
    svc.smb_files(tmp_path / "src")
    user = {"username": svc.ENV["SMB_USER"], "password": svc.ENV["SMB_PASSWORD"]}
    top = _smb("/music", **user)
    assert top.folders == ["Книги", "Музика"] and top.parent == "/"
    assert (top.preview_folders, top.preview_media) == (3, 1)
    sub = _smb("/public/Музика")  # guest share
    assert sub.folders == ["Бумбокс"] and sub.media_files == 1 and sub.preview_media == 2
    assert sub.parent == "/public"
    with pytest.raises(RemoteError) as e:
        _smb("/music", username=svc.ENV["SMB_USER"], password="wrong-password")
    assert "wrong-password" not in str(e.value)


async def test_ftp_folder_and_preview(services, tmp_path):
    _tree(tmp_path / "src")
    base = f"/sd-browse-{uuid.uuid4().hex[:6]}"
    _upload(tmp_path / "src", base)
    lst = browse.browse_remote(
        "ftp",
        host=svc.ENV["FTP_HOST"],
        port=int(svc.ENV["FTP_PORT"]),
        path=base,
        username=svc.ENV["FTP_USER"],
        password=svc.ENV["FTP_PASSWORD"],
    )
    assert lst.folders == ["Книги", "Музика"] and (lst.preview_folders, lst.preview_media) == (3, 1)
    root = browse.browse_remote(
        "ftp",
        host=svc.ENV["FTP_HOST"],
        port=int(svc.ENV["FTP_PORT"]),
        path="/",
        username=svc.ENV["FTP_USER"],
        password=svc.ENV["FTP_PASSWORD"],
    )
    assert base.lstrip("/") in root.folders
