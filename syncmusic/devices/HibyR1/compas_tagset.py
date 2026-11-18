# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: ASYNC240  (build runs in its own event loop inside a worker thread: blocking file work is fine)
"""The Compás player's library database, built on the server for a Wi-Fi client (the SyncMusic app on the player).

The app cannot run the player's indexer itself, so the server builds the database over exactly the files the card
holds: a mirror tree of hard links to them (compas-sdtool skips symlinks; a copy where a link cannot be made), each
file's mtime rounded down to an even second (FAT keeps 2 s steps, so the mtime the app gives the file on the card is
the same), the Podcasts/Audiobooks folders kept out of the music library like on the card (database.ignore), then
`compas-sdtool scan`. The new generation's files (generation 1: the app installs the set as a whole new database)
are the generated set; before it is offered, the set is opened with `compas-sdtool info` on a scratch card holding
only it, as the player would load it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from syncmusic.devices.HibyR1 import compas_feedback as cf
from syncmusic.devices.HibyR1 import compas_sdtool as sd

GEN_FILE = re.compile(r"^database_(?:idx|\d+)\.tcd\.g(\d+)(?:\..+)?$")
IGNORE_FILE = "database.ignore"
IGNORED_DIRS = ("Podcasts", "Audiobooks")
MANIFEST = "manifest.json"


class TagsetError(Exception):
    """The set could not be built or does not load like the player would load it."""


def even_mtime(p: Path) -> int:
    """Round the file's mtime down to an even second (in place) and return it."""
    st = p.stat()
    mt = int(st.st_mtime) // 2 * 2
    if st.st_mtime_ns != mt * 1_000_000_000:
        os.utime(p, (mt, mt))
    return mt


def set_id(files: dict[str, Path]) -> str:
    """The same card contents (path, size, even mtime) give the same id, so a set is built once."""
    rows = [[rel, p.stat().st_size, even_mtime(p)] for rel, p in sorted(files.items())]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:32]


def _link(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


async def build(files: dict[str, Path], out: Path, tool: Path) -> dict[str, object]:
    """Build the set for `files` (card path -> file on the server) into `out`; its manifest."""
    out.mkdir(parents=True, exist_ok=True)
    info = await sd.info(tool, out)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".mirror-") as tmp:
        mirror = Path(tmp)
        for rel, src in files.items():
            even_mtime(src)
            _link(src, mirror / rel)
        for folder in IGNORED_DIRS:
            (mirror / folder).mkdir(parents=True, exist_ok=True)
            (mirror / folder / IGNORE_FILE).touch()
        done = await sd.scan(tool, mirror)
        gen = cf.generation(mirror / cf.COMPAS_DIR)
        if gen != 1:
            raise TagsetError(f"compas-sdtool built generation {gen}, not a new database")
        names = []
        for p in sorted((mirror / cf.COMPAS_DIR).iterdir()):
            m = GEN_FILE.match(p.name)
            if m and int(m.group(1)) == gen:
                shutil.copyfile(p, out / p.name)
                names.append(p.name)
    await _check(tool, out, names, done.get("songs"))
    manifest = {
        "database_format": info.database_format,
        "sdtool": f"{info.version} {info.build}",
        "songs": done.get("songs"),
        "files": names,
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1))
    return manifest


async def _check(tool: Path, out: Path, names: list[str], songs: object) -> None:
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".check-") as tmp:
        db = Path(tmp) / cf.COMPAS_DIR
        db.mkdir()
        for n in names:
            shutil.copyfile(out / n, db / n)
        (db / "tagcache.gen").write_text("1")
        events = await sd._run(tool, ["info", tmp], sd.INFO_TIMEOUT_S)
    got = next((e for e in reversed(events) if "generation" in e), {})
    if got.get("generation") != 1 or got.get("songs") != songs:
        raise TagsetError(f"the built database does not load: {got!r}, expected 1 / {songs} songs")


def build_cached(files: dict[str, Path], cache: Path, tool: Path) -> tuple[str, Path, list[str]]:
    """The set for `files` from `cache/<set id>/`, built when missing (older sets are removed). Blocking: run it in
    a worker thread."""
    sid = set_id(files)
    out = cache / sid
    if not (out / MANIFEST).is_file():
        cache.mkdir(parents=True, exist_ok=True)
        tmp = cache / f".{sid}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            asyncio.run(build(files, tmp, tool))
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        tmp.rename(out)
        for old in cache.iterdir():
            if old != out and old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
    names = json.loads((out / MANIFEST).read_text())["files"]
    return sid, out, list(names)
