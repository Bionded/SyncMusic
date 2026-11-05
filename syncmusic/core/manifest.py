# SPDX-License-Identifier: GPL-3.0-or-later
"""On-device manifest `/.syncmusic/manifest.jsonl` and the inflight list.

Format (one JSON object per line): a header line first, then one `item` line per managed item, then one
`playlist` line per device playlist we manage. Unknown line types are skipped (forward compatible within a
manifest version). Written atomically (tmp + fsync + rename + fsync of the directory) **after** the files and
the device DB. A manifest that does not parse (e.g. a truncated last line) raises `ManifestError` - it is never
read as "fewer items", which would turn managed files into unknown ones.

The inflight list `/.syncmusic/inflight` holds relative paths written during a sync but not yet committed;
the next sync removes those files before planning (they are not in the manifest).

Plain blocking functions: devices call them via `asyncio.to_thread`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from syncmusic.core.models import MANIFEST_VERSION, Manifest, ManifestEntry, ManifestHeader

log = logging.getLogger(__name__)

STATE_DIR = Path(".syncmusic")
MANIFEST_PATH = STATE_DIR / "manifest.jsonl"
INFLIGHT_PATH = STATE_DIR / "inflight"


class ManifestError(Exception):
    """The manifest exists but cannot be read (corrupt, truncated, not a manifest)."""


class ManifestTooNew(ManifestError):
    """The manifest was written by a newer SyncMusic; the user decides."""

    choices: tuple[Literal["wipe", "try_anyway"], ...] = ("wipe", "try_anyway")

    def __init__(self, found: int, supported: int = MANIFEST_VERSION) -> None:
        super().__init__(
            f"device manifest version {found} is newer than supported ({supported}): "
            "wipe the device and start fresh, or try anyway (may not work)"
        )
        self.found = found
        self.supported = supported


# --- serialisation ---------------------------------------------------------------------------


def dumps(manifest: Manifest) -> str:
    lines = [manifest.header.model_dump_json()]
    lines += [e.model_dump_json() for e in manifest.items.values()]
    for n in manifest.playlists:
        row: dict[str, Any] = {"type": "playlist", "name": n}
        if n in manifest.playlist_paths:
            row["path"] = manifest.playlist_paths[n]
        if n in manifest.playlist_hashes:
            row["hash"] = manifest.playlist_hashes[n]
        if n in manifest.playlist_refs:
            row["refs"] = manifest.playlist_refs[n]
        if n in manifest.playlist_sources:
            row["source"] = manifest.playlist_sources[n]
        if n in manifest.playlist_smart:
            row["smart"] = True
        lines.append(json.dumps(row, ensure_ascii=False))
    for n, source in manifest.playlist_created.items():
        created = {"type": "device_playlist", "name": n, "source": source}
        lines.append(
            json.dumps({**created, "refs": manifest.playlist_refs.get(n, [])}, ensure_ascii=False)
        )
    return "\n".join(lines) + "\n"


def loads(text: str, *, allow_newer: bool = False) -> Manifest:
    """Parse a manifest. `allow_newer` = the user chose "try anyway" for a newer manifest version."""
    rows: list[dict[str, Any]] = []
    for no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise ManifestError(
                f"manifest line {no} is not valid JSON (truncated write?): {e}"
            ) from e
        if not isinstance(row, dict):
            raise ManifestError(f"manifest line {no} is not an object")
        rows.append(row)
    if not rows or rows[0].get("type") != "header":
        raise ManifestError("manifest has no header line")
    version = rows[0].get("manifest_version")
    if not isinstance(version, int):
        raise ManifestError("manifest header has no manifest_version")
    if version > MANIFEST_VERSION and not allow_newer:
        raise ManifestTooNew(version)
    try:
        header = ManifestHeader.model_validate(rows[0])
        items: dict[str, ManifestEntry] = {}
        playlists: list[str] = []
        playlist_paths: dict[str, str] = {}
        playlist_hashes: dict[str, str] = {}
        playlist_refs: dict[str, list[str]] = {}
        playlist_sources: dict[str, str] = {}
        playlist_smart: set[str] = set()
        playlist_created: dict[str, str] = {}
        for row in rows[1:]:
            kind = row.get("type")
            if kind == "item":
                entry = ManifestEntry.model_validate(row)
                items[entry.ref.key()] = entry
            elif kind == "playlist":
                playlists.append(str(row["name"]))
                if row.get("path"):
                    playlist_paths[str(row["name"])] = str(row["path"])
                if row.get("hash"):
                    playlist_hashes[str(row["name"])] = str(row["hash"])
                if isinstance(row.get("refs"), list):
                    playlist_refs[str(row["name"])] = [str(r) for r in row["refs"]]
                if row.get("source"):
                    playlist_sources[str(row["name"])] = str(row["source"])
                if row.get("smart"):
                    playlist_smart.add(str(row["name"]))
            elif kind == "device_playlist":
                playlist_created[str(row["name"])] = str(row["source"])
                playlist_refs[str(row["name"])] = [str(r) for r in row.get("refs") or []]
            else:
                log.warning("manifest: skipping unknown line type %r", kind)
    except (ValidationError, KeyError) as e:
        if version > MANIFEST_VERSION:
            raise ManifestTooNew(version) from e
        raise ManifestError(f"invalid manifest: {e}") from e
    return Manifest(
        header=header,
        items=items,
        playlists=playlists,
        playlist_paths=playlist_paths,
        playlist_hashes=playlist_hashes,
        playlist_refs=playlist_refs,
        playlist_sources=playlist_sources,
        playlist_smart=playlist_smart,
        playlist_created=playlist_created,
    )


# --- files on the device ---------------------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError:  # some filesystems (vfat via some drivers) refuse fsync on directories
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` via a tmp file + fsync + rename; on failure the old file stays."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def read(root: Path, *, allow_newer: bool = False) -> Manifest | None:
    """The manifest under the device root, None if there is none (first sync / foreign device)."""
    path = root / MANIFEST_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as e:
        raise ManifestError(f"manifest is not UTF-8: {e}") from e
    return loads(text, allow_newer=allow_newer)


def write(root: Path, manifest: Manifest) -> None:
    atomic_write(root / MANIFEST_PATH, dumps(manifest).encode("utf-8"))


def delete(root: Path) -> None:
    """Remove the manifest (and the state dir when nothing else is in it): the device is unmanaged again."""
    (root / MANIFEST_PATH).unlink(missing_ok=True)
    with contextlib.suppress(OSError):  # not empty or not there
        (root / STATE_DIR).rmdir()


# --- inflight list ---------------------------------------------------------------------------


def read_inflight(root: Path) -> list[str]:
    """Relative paths written by an interrupted sync (a partial last line is kept as written)."""
    try:
        text = (root / INFLIGHT_PATH).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def add_inflight(root: Path, paths: list[str]) -> None:
    """Append paths (before the files are written) and fsync, so a crash still leaves them listed."""
    if not paths:
        return
    path = root / INFLIGHT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(p + "\n" for p in paths))
        f.flush()
        os.fsync(f.fileno())


def clear_inflight(root: Path) -> None:
    (root / INFLIGHT_PATH).unlink(missing_ok=True)


def clean_inflight(root: Path, manifest: Manifest | None) -> list[str]:
    """Delete inflight files not in the manifest (a crashed sync's leftovers); returns what was removed."""
    managed: set[str] = set()
    for e in manifest.items.values() if manifest else []:
        managed.add(e.path)
        managed.update(e.sidecars)
    removed: list[str] = []
    base = root.resolve()
    for rel in read_inflight(root):
        target = (root / rel).resolve()
        if rel in managed or not target.is_relative_to(base) or target == base:
            continue
        if target.is_file():
            target.unlink()
            removed.append(rel)
    clear_inflight(root)
    return removed
