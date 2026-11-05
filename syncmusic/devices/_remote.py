# SPDX-License-Identifier: GPL-3.0-or-later
"""What the server devices (FTP, SMB) share: the parts of reading and writing their file tree that do not depend
on the connection (- they were copied between `ftp.py` and `smb.py`). Plain functions over a file
listing (`device-relative path -> size`); each device keeps its own I/O."""

from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath

from syncmusic.base.capabilities import PathRules
from syncmusic.base.device import DeviceSession
from syncmusic.core.models import (
    DeviceContents,
    DeviceItem,
    DevicePlaylist,
    Manifest,
    PreparedItem,
)
from syncmusic.devices import paths
from syncmusic.devices._filedevice import MEDIA_EXTS
from syncmusic.devices.paths import PathLayout


def contents(files: dict[str, int]) -> DeviceContents:
    """Player contents of a listing: the media files and the playlists in the playlist folder."""
    items = [
        DeviceItem(device_ref=rel, path=rel, size=size, title=PurePosixPath(rel).stem)
        for rel, size in sorted(files.items())
        if PurePosixPath(rel).suffix.lower() in MEDIA_EXTS
    ]
    lists = sorted(
        PurePosixPath(rel).stem
        for rel in files
        if posixpath.dirname(rel) == paths.PLAYLIST_DIR and rel.lower().endswith((".m3u8", ".m3u"))
    )
    return DeviceContents(items=items, playlists=lists)


def fill_session(
    s: DeviceSession, manifest: Manifest | None, files: dict[str, int], rules: PathRules
) -> None:
    """The session's path bookkeeping (`taken`, `paths`) from the listing after begin's clean-up."""
    s.data["taken"] = {paths.path_key(r, rules) for r in files}
    s.data["paths"] = {k: e.path for k, e in (manifest.items if manifest else {}).items()}


def track_targets(
    s: DeviceSession, item: PreparedItem, rules: PathRules, layout: PathLayout
) -> tuple[str, list[tuple[Path, str]]]:
    """(the track's path, [(local file, device path)] for it and its sidecars): its rendered path, made unique
    unless it keeps the path it had."""
    rel = paths.render(item.track, item.plan.file.ext, rules, layout)
    old = s.data["paths"].get(item.track.ref.key())
    if old is None or paths.path_key(old, rules) != paths.path_key(rel, rules):
        rel = paths.unique(rel, s.data["taken"], rules)
    targets: list[tuple[Path, str]] = [(item.path, rel)]
    stem, folder = PurePosixPath(rel).stem, posixpath.dirname(rel)
    for key, src in sorted(item.sidecars.items()):
        name = stem + key if key.startswith(".") else key
        targets.append(
            (src, posixpath.join(folder, paths.sanitize_component(name, rules, has_ext=True)))
        )
    return rel, targets


def track_written(
    s: DeviceSession, item: PreparedItem, rel: str, written: list[str], rules: PathRules
) -> None:
    taken: set[str] = s.data["taken"]
    for w in written:
        taken.add(paths.path_key(w, rules))
    s.data["paths"][item.track.ref.key()] = rel


def playlist_files(
    s: DeviceSession, playlists: list[DevicePlaylist], rules: PathRules
) -> tuple[list[str], dict[str, bytes]]:
    """(stale playlist files to delete, device path -> .m3u8 text) for `set_playlists`; records the new paths."""
    by_ref: dict[str, str] = s.data["paths"]
    new = {p.name: paths.playlist_path(p.name, rules) for p in playlists}
    old = s.manifest.playlist_paths if s.manifest else {}
    stale = [
        old.get(n) or paths.playlist_path(n, rules)
        for n in (s.manifest.playlists if s.manifest else [])
        if n not in new
    ]
    s.data["playlist_paths"] = new
    texts = {}
    for pl in playlists:
        lines = ["#EXTM3U"] + [
            posixpath.relpath(by_ref[r.key()], paths.PLAYLIST_DIR)
            for r in pl.refs
            if r.key() in by_ref
        ]
        texts[new[pl.name]] = ("\n".join(lines) + "\n").encode("utf-8")
    return stale, texts
