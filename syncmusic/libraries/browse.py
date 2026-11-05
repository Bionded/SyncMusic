# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only folder picker: list one folder of a local path, an FTP server or an SMB share, with a
preview of what lies below it - how many folders and media files the first levels hold - so the user can pick a
library root or a device's target folder by looking instead of typing.

Nothing is ever written. Remote listings reuse the library sources (`libraries/remote.py`); an SMB path without a
share lists the server's shares (`infra/discovery.list_shares`). Hidden entries (dot names) are not shown.
"""

from __future__ import annotations

import os
import posixpath
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from syncmusic.libraries.folder import MEDIA_EXTS
from syncmusic.libraries.remote import Entry, FtpSource, MirrorSource, RemoteError, SmbSource

Source = Literal["local", "ftp", "smb", "mtp"]
PREVIEW_DEPTH = 2  # the folder itself and one level of subfolders
PREVIEW_MAX_DIRS = (
    60  # folder listings a preview may cost: a huge tree is counted partly ("at least")
)
MAX_FOLDERS = 1000  # subfolders shown


class Listing(BaseModel):
    path: str  # normalised: "/" or "/a/b"
    parent: str | None  # None at the top
    folders: list[str]  # the subfolder names, sorted
    files: int  # files directly in the folder
    media_files: int  # media files directly in the folder
    preview_folders: int  # folders in the first PREVIEW_DEPTH levels
    preview_media: int  # media files in the first PREVIEW_DEPTH levels
    partial: bool  # the preview stopped early: the counts are lower bounds
    shares: bool = (
        False  # SMB server level: `folders` are the shares (nothing can be counted there)
    )


def norm(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
    if ".." in parts:
        raise ValueError("path must not contain '..'")
    return "/" + "/".join(parts)


def _media(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in MEDIA_EXTS


def _visible(entries: list[Entry]) -> list[Entry]:
    return [e for e in entries if not e[0].startswith(".")]


def _listing(path: str, entries: list[Entry], list_dir: Any) -> Listing:
    """`list_dir(abs_path)` lists a subfolder for the preview (RemoteError/OSError: that folder is skipped)."""
    folders = sorted((e[0] for e in entries if e[1]), key=str.lower)
    files = [e[0] for e in entries if not e[1]]
    media = sum(1 for n in files if _media(n))
    p_folders, p_media, partial, budget = len(folders), media, False, PREVIEW_MAX_DIRS
    level = [posixpath.join(path, f) for f in folders]
    for _ in range(PREVIEW_DEPTH - 1):
        nxt: list[str] = []
        for d in level:
            if budget == 0:
                partial = True
                break
            budget -= 1
            try:
                sub = _visible(list_dir(d))
            except (OSError, RemoteError):
                continue
            nxt += [posixpath.join(d, e[0]) for e in sub if e[1]]
            p_media += sum(1 for e in sub if not e[1] and _media(e[0]))
        p_folders += len(nxt)
        level = nxt
    return Listing(
        path=path,
        parent=None if path == "/" else posixpath.dirname(path),
        folders=folders[:MAX_FOLDERS],
        files=len(files),
        media_files=media,
        preview_folders=p_folders,
        preview_media=p_media,
        partial=partial or len(folders) > MAX_FOLDERS,
    )


def browse_with(path: str, list_dir: Any) -> Listing:
    """A folder of any tree `list_dir(abs_path) -> [Entry]` can list (an MTP player's storage)."""
    path = norm(path)
    return _listing(path, _visible(list_dir(path)), list_dir)


def browse_local(path: str) -> Listing:
    """A folder on this machine (OSError with the reason when it cannot be listed)."""
    path = norm(path)

    def list_dir(p: str) -> list[Entry]:
        with os.scandir(p) as it:
            return [(e.name, e.is_dir(), 0, 0) for e in it]

    return _listing(path, _visible(list_dir(path)), list_dir)


def browse_remote(
    source: Literal["ftp", "smb"],
    *,
    host: str,
    port: int | None,
    username: str,
    password: str,
    path: str,
    tls: bool = False,
    timeout_s: float = 15,
) -> Listing:
    """A folder on a server; `path` is absolute on the server (SMB: `/<share>/<folder…>`). Blocking: run it in a
    thread. RemoteError with the reason (never the password)."""
    path = norm(path)
    mirror = Path(tempfile.gettempdir())  # never written: the sources only list here
    src: MirrorSource
    if source == "ftp":
        src = FtpSource(
            host=host,
            port=port or 21,
            username=username,
            password=password,
            path="/",
            tls=tls,
            mirror=mirror,
            timeout_s=timeout_s,
        )
    else:
        src = SmbSource(
            host=host,
            port=port or 445,
            username=username,
            password=password,
            path=path.split("/")[1],  # the share; folders below it are relative paths
            mirror=mirror,
            timeout_s=timeout_s,
        )

    def rel(p: str) -> str:
        if source == "ftp":
            return p.lstrip("/")
        return "/".join(p.split("/")[2:])

    with src.session() as h:
        return _listing(path, _visible(src.entries(h, rel(path))), lambda p: src.entries(h, rel(p)))


def shares_listing(names: list[str]) -> Listing:
    return Listing(
        path="/",
        parent=None,
        folders=sorted(names, key=str.lower),
        files=0,
        media_files=0,
        preview_folders=len(names),
        preview_media=0,
        partial=False,
        shares=True,
    )
