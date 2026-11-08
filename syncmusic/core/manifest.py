# SPDX-License-Identifier: GPL-3.0-or-later
"""On-device manifest `/.syncmusic/manifest.jsonl` and the inflight list.

Format (one JSON object per line): a header line first, then one `item` line per managed item, then one
`playlist` line per device playlist we manage. Unknown line types are skipped (forward compatible within a
manifest version). Written atomically (tmp + fsync + rename + fsync of the directory) **after** the files and
the device DB. A manifest that does not parse (e.g. a truncated last line) raises `ManifestError` - it is never
read as "fewer items", which would turn managed files into unknown ones.

Appended tail: a later commit that changes only items (checkpoints, update syncs) appends an `append` marker line
and then `item` lines (the last line for a key wins) and `delete` lines (`lib` + `item_id`) instead of rewriting the
whole file. A torn last line after an `append` marker is dropped (the commit it belonged to did not finish); before
the marker the rule above holds. The file is compacted (rewritten in full) when the tail grows past a quarter of the
items, when the header or the playlists change, and after a torn tail.

The inflight list `/.syncmusic/inflight` holds relative paths written during a sync but not yet committed;
the next sync removes those files before planning (they are not in the manifest).

Version 2 names libraries by device-local keys: the header's `libraries` table ("L1": source id, type, name, the
instance it belongs to, a few probe items) and every entry's `lib` + `item_id`. Reading maps a key to our library
(the same instance's library id, else the same source id, else a confirmed alias); a library we do not have becomes
`ext:<key>` - another instance's items, which are never planned, removed or swept. The mapping comes from the
`Context` the app sets (`set_context`); without one (tools, tests) keys map to the writer's library ids.

Plain blocking functions: devices call them via `asyncio.to_thread`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from syncmusic.core.models import (
    MANIFEST_VERSION,
    InstanceInfo,
    ItemRef,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    ManifestLibrary,
    utcnow,
)

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


# --- library keys ----------------------------------------------------------------------------

EXT_PREFIX = "ext:"
PROBE_SIZE = 5


@dataclass(frozen=True)
class Context:
    """What reading and writing a manifest needs to know about this instance."""

    instance_id: str = ""
    instance_name: str = ""
    app_version: str = ""
    # our libraries by id: what the header table says about them (source id, type, name, url hint)
    libraries: dict[str, ManifestLibrary] = field(default_factory=dict)
    # other instances' source ids the user paired with ours: source id -> our library id
    aliases: dict[str, str] = field(default_factory=dict)


_context = Context()
_generation = 0  # bumped by set_context: cached parses resolved with an older context are stale


def set_context(context: Context) -> None:
    global _context, _generation
    if context != _context:
        _context = context
        _generation += 1


def context() -> Context:
    return _context


def is_home(manifest: Manifest | None) -> bool:
    """This instance is the device's home: it set it up, was made home, or nobody is home yet. Without an
    instance id (tools, tests) every instance is home."""
    me = _context.instance_id
    home = manifest.header.home if manifest is not None else None
    return home is None or not me or home.id == me


def is_external(library_id: str) -> bool:
    """An item of a library this instance does not have (another instance put it there)."""
    return library_id.startswith(EXT_PREFIX)


def resolve(key: str, lib: ManifestLibrary, ctx: Context) -> str:
    """Our library id for the header library `key`, or `ext:<key>`."""
    # ours, even when the library was removed since (its items are then gone from it, as before)
    if lib.id and lib.instance and lib.instance == ctx.instance_id:
        return lib.id
    if lib.id and not lib.instance and (not ctx.libraries or lib.id in ctx.libraries):
        return lib.id  # written without an instance id (tools, tests)
    if lib.source_id:
        for lid, ours in ctx.libraries.items():
            if ours.source_id == lib.source_id:
                return lid
        alias = ctx.aliases.get(lib.source_id)
        if alias and (not ctx.libraries or alias in ctx.libraries):
            return alias
    return EXT_PREFIX + key


def _probe(entries: list[ManifestEntry]) -> list[dict[str, Any]]:
    live = [e for e in entries if not e.orphan] or entries
    if len(live) > PROBE_SIZE:
        step = len(live) / PROBE_SIZE
        live = [live[int(k * step)] for k in range(PROBE_SIZE)]
    return [
        {"item_id": e.ref.item_id, "title": e.meta.get("title", ""), "duration_s": e.duration_s}
        for e in live
    ]


def _library_table(
    manifest: Manifest, ctx: Context
) -> tuple[dict[str, ManifestLibrary], dict[str, str]]:
    """The header table for the libraries in use and library id -> key. Keys of libraries still in use keep their
    key; other instances' libraries keep their row as it was read."""
    used: dict[str, list[ManifestEntry]] = {}
    for e in manifest.items.values():
        used.setdefault(e.ref.library_id, []).append(e)
    refs = [*manifest.playlist_sources.values(), *manifest.playlist_created.values()]
    for listed in manifest.playlist_refs.values():
        refs += listed
    for k in refs:
        used.setdefault(k.partition("/")[0], [])
    old = manifest.header.libraries
    keys = {lid: k for lid, k in manifest.lib_keys.items() if lid in used}
    taken = set(keys.values()) | {lid[len(EXT_PREFIX) :] for lid in used if is_external(lid)}
    n = 0
    for lid in sorted(used):
        if is_external(lid) or lid in keys:
            continue
        while f"L{n + 1}" in taken:
            n += 1
        keys[lid] = f"L{n + 1}"
        taken.add(keys[lid])
    table: dict[str, ManifestLibrary] = {}
    for lid, entries in used.items():
        if is_external(lid):
            key = lid[len(EXT_PREFIX) :]
            if key in old:
                table[key] = old[key]
            continue
        key = keys[lid]
        base = ctx.libraries.get(lid) or old.get(key) or ManifestLibrary()
        table[key] = base.model_copy(
            update={
                "id": lid,
                "instance": ctx.instance_id,
                "probe": _probe(entries) if entries else (old[key].probe if key in old else []),
            }
        )
    return dict(sorted(table.items())), keys


def _to_device(key: str, keys: dict[str, str]) -> str:
    lid, _, item = key.partition("/")
    lib = lid[len(EXT_PREFIX) :] if is_external(lid) else keys.get(lid, lid)
    return f"{lib}/{item}"


def _from_device(key: str, libs: dict[str, str]) -> str:
    lib, _, item = key.partition("/")
    return f"{libs.get(lib, EXT_PREFIX + lib)}/{item}"


def _stamped_header(
    manifest: Manifest, table: dict[str, ManifestLibrary], ctx: Context
) -> ManifestHeader:
    h = manifest.header
    update: dict[str, Any] = {"manifest_version": MANIFEST_VERSION, "libraries": table}
    if ctx.instance_id:
        me = InstanceInfo(id=ctx.instance_id, name=ctx.instance_name, app_version=ctx.app_version)
        update["last_writer"] = me
        if h.home is None:
            update["home"] = me.model_copy(update={"app_version": "", "since": utcnow()})
    return h.model_copy(update=update)


# --- serialisation ---------------------------------------------------------------------------


_ITEM_HEAD = '{"type":"item",'


def _item_line(e: ManifestEntry, keys: dict[str, str]) -> str:
    rest = e.model_dump_json(exclude={"ref"})
    lib = _to_device(e.ref.key(), keys).partition("/")[0]
    return (
        f'{_ITEM_HEAD}"lib":{json.dumps(lib)},"item_id":{json.dumps(e.ref.item_id, ensure_ascii=False)},'
        + rest[len(_ITEM_HEAD) :]
    )


def _delete_line(key: str, keys: dict[str, str]) -> str:
    lib, _, item = _to_device(key, keys).partition("/")
    return json.dumps({"type": "delete", "lib": lib, "item_id": item}, ensure_ascii=False)


def dumps(manifest: Manifest, ctx: Context | None = None) -> str:
    """Version 2 text. The manifest takes the header written (library table, writer) and the keys given to
    new libraries (`lib_keys`), so it equals what reading the text back gives."""
    ctx = ctx or _context
    table, keys = _library_table(manifest, ctx)
    manifest.lib_keys = keys
    manifest.header = _stamped_header(manifest, table, ctx)
    lines = [manifest.header.model_dump_json()]
    lines += [_item_line(e, keys) for e in manifest.items.values()]

    def dev(k: str) -> str:
        return _to_device(k, keys)

    for n in manifest.playlists:
        row: dict[str, Any] = {"type": "playlist", "name": n}
        if n in manifest.playlist_paths:
            row["path"] = manifest.playlist_paths[n]
        if n in manifest.playlist_hashes:
            row["hash"] = manifest.playlist_hashes[n]
        if n in manifest.playlist_refs:
            row["refs"] = [dev(k) for k in manifest.playlist_refs[n]]
        if n in manifest.playlist_sources:
            row["source"] = dev(manifest.playlist_sources[n])
        if n in manifest.playlist_smart:
            row["smart"] = True
        lines.append(json.dumps(row, ensure_ascii=False))
    for n, source in manifest.playlist_created.items():
        created = {"type": "device_playlist", "name": n, "source": dev(source)}
        refs = [dev(k) for k in manifest.playlist_refs.get(n, [])]
        lines.append(json.dumps({**created, "refs": refs}, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def loads(text: str, *, allow_newer: bool = False, ctx: Context | None = None) -> Manifest:
    """Parse a manifest. `allow_newer` = the user chose "try anyway" for a newer manifest version."""
    ctx = ctx or _context
    rows: list[dict[str, Any]] = []
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    in_tail = False
    for no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            if in_tail and no == len(lines):
                log.warning("manifest: dropping a torn appended last line")
                break
            raise ManifestError(
                f"manifest line {no} is not valid JSON (truncated write?): {e}"
            ) from e
        if not isinstance(row, dict):
            raise ManifestError(f"manifest line {no} is not an object")
        in_tail = in_tail or row.get("type") == "append"
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
        v1 = version < 2
        libs = {k: resolve(k, lib, ctx) for k, lib in header.libraries.items()}
        lib_keys = {lid: k for k, lid in libs.items() if not is_external(lid)}

        def ours(k: str) -> str:
            return k if v1 else _from_device(k, libs)

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
                if not v1:
                    lib = str(row.pop("lib"))
                    ref = ItemRef(
                        library_id=libs.get(lib, EXT_PREFIX + lib), item_id=str(row.pop("item_id"))
                    )
                    row["ref"] = ref
                entry = ManifestEntry.model_validate(row)
                items[entry.ref.key()] = entry
            elif kind == "delete":
                lib = str(row["lib"])
                items.pop(f"{libs.get(lib, EXT_PREFIX + lib)}/{row['item_id']}", None)
            elif kind == "append":
                pass
            elif kind == "playlist":
                playlists.append(str(row["name"]))
                if row.get("path"):
                    playlist_paths[str(row["name"])] = str(row["path"])
                if row.get("hash"):
                    playlist_hashes[str(row["name"])] = str(row["hash"])
                if isinstance(row.get("refs"), list):
                    playlist_refs[str(row["name"])] = [ours(str(r)) for r in row["refs"]]
                if row.get("source"):
                    playlist_sources[str(row["name"])] = ours(str(row["source"]))
                if row.get("smart"):
                    playlist_smart.add(str(row["name"]))
            elif kind == "device_playlist":
                playlist_created[str(row["name"])] = ours(str(row["source"]))
                playlist_refs[str(row["name"])] = [ours(str(r)) for r in row.get("refs") or []]
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
        lib_keys=lib_keys,
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


# parsed manifests by device root: (size, mtime, header line) -> Manifest. Parsing is the cost of a read (~35 us per
# entry on a Pi, 0.5 s at 15 000 entries) and every view of a device reads it; a hit returns a copy, as callers change
# the manifest they get. Our own writes drop the entry; the header line (its `updated_at` changes on every write)
# catches a change made elsewhere within the file system's mtime resolution.
_CACHE: dict[str, tuple[tuple[int, int, bytes, int], Manifest]] = {}
_CACHE_SIZE = 8


def copy(m: Manifest) -> Manifest:
    """A copy whose containers and entries can be changed without touching the cached one (the entries' own
    fields are only ever replaced, never changed in place)."""
    return m.model_copy(
        update={
            "header": m.header.model_copy(),
            "items": {k: e.model_copy() for k, e in m.items.items()},
            "playlists": list(m.playlists),
            "playlist_paths": dict(m.playlist_paths),
            "playlist_hashes": dict(m.playlist_hashes),
            "playlist_refs": {k: list(v) for k, v in m.playlist_refs.items()},
            "playlist_sources": dict(m.playlist_sources),
            "playlist_smart": set(m.playlist_smart),
            "playlist_created": dict(m.playlist_created),
            "lib_keys": dict(m.lib_keys),
        }
    )


# lines appended since the last full write, by device root; -1 = the file must be compacted before the next append
# (it ends in a torn line). Only roots in _CACHE count.
_TAIL: dict[str, int] = {}
APPEND_MIN = (
    100  # a tail of up to this many lines is always fine; beyond, up to a quarter of the items
)


def forget(root: Path) -> None:
    """Drop the cached manifest of this device root (written, deleted or unmounted)."""
    _CACHE.pop(str(root), None)
    _TAIL.pop(str(root), None)


def _key(f: Any) -> tuple[int, int, bytes, int]:
    st = os.fstat(f.fileno())
    return (st.st_size, st.st_mtime_ns, f.readline(), _generation)


def _remember(root: Path, key: tuple[int, int, bytes, int], m: Manifest, tail: int) -> None:
    _CACHE.pop(str(root), None)
    while len(_CACHE) >= _CACHE_SIZE:
        old = next(iter(_CACHE))
        del _CACHE[old]
        _TAIL.pop(old, None)
    _CACHE[str(root)] = (key, copy(m))
    _TAIL[str(root)] = tail


def drop_cache() -> None:
    """Drop every cached manifest (Free memory now); the next read parses the file again."""
    _CACHE.clear()
    _TAIL.clear()


def cached_counts() -> tuple[int, int]:
    """(cached manifests, their entries) for the memory overview."""
    return len(_CACHE), sum(len(m.items) for _, m in _CACHE.values())


def read(root: Path, *, allow_newer: bool = False) -> Manifest | None:
    """The manifest under the device root, None if there is none (first sync / foreign device)."""
    path = root / MANIFEST_PATH
    try:
        with path.open("rb") as f:
            key = _key(f)
            hit = _CACHE.get(str(root))
            if hit is not None and hit[0] == key and not allow_newer:
                return copy(hit[1])
            raw = key[2] + f.read()
    except FileNotFoundError:
        forget(root)
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ManifestError(f"manifest is not UTF-8: {e}") from e
    manifest = loads(text, allow_newer=allow_newer)
    if not allow_newer:
        at = text.find('\n{"type":"append"')
        tail = -1 if not raw.endswith(b"\n") else 0 if at < 0 else text.count("\n", at + 1)
        _remember(root, key, manifest, tail)
    return manifest


def read_header(root: Path) -> ManifestHeader | None:
    """Only the header (the first line) of the manifest under the device root - for matching a device,
    without parsing its entries; None if there is none."""
    path = root / MANIFEST_PATH
    try:
        with path.open("rb") as f:
            first = f.readline()
    except FileNotFoundError:
        return None
    try:
        row = json.loads(first.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ManifestError(f"manifest header is not valid JSON: {e}") from e
    if not isinstance(row, dict) or row.get("type") != "header":
        raise ManifestError("manifest has no header line")
    try:
        return ManifestHeader.model_validate(row)
    except ValueError as e:
        raise ManifestError(f"manifest header is invalid: {e}") from e


def unchanged(old: bytes, manifest: Manifest) -> bool:
    """True when the manifest written as `old` says the same as `manifest` apart from the header's
    `updated_at`: an update sync with nothing to do leaves the file (and every cache keyed on its mtime) alone."""
    first, _, rest = old.partition(b"\n")
    new_first, _, new_rest = dumps(manifest).encode("utf-8").partition(b"\n")
    if new_rest != rest:
        return False
    try:
        a, b = json.loads(first.decode("utf-8")), json.loads(new_first.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    a.pop("updated_at", None)
    b.pop("updated_at", None)
    return a == b


def _base(root: Path) -> tuple[Manifest, int] | None:
    """The cached parse of the manifest file as it is now, with its tail length; None when not cached."""
    hit = _CACHE.get(str(root))
    if hit is None or _TAIL.get(str(root), -1) < 0:
        return None
    try:
        with (root / MANIFEST_PATH).open("rb") as f:
            key = _key(f)
    except OSError:
        return None
    return (hit[1], _TAIL[str(root)]) if hit[0] == key else None


def _tail_lines(base: Manifest, tail: int, manifest: Manifest, ctx: Context) -> list[str] | None:
    """The lines to append to turn `base` into `manifest`, None when it needs a full write."""
    table, keys = _library_table(manifest, ctx)
    header = _stamped_header(manifest, table, ctx)

    def same(h: ManifestHeader) -> dict[str, Any]:
        d = h.model_dump(exclude={"updated_at"})
        for lib in d["libraries"].values():
            lib.pop("probe", None)  # matching hints; refreshed when the file is compacted
        return d

    if same(header) != same(base.header) or keys != base.lib_keys:
        return None
    for f in (
        "playlists",
        "playlist_paths",
        "playlist_hashes",
        "playlist_refs",
        "playlist_sources",
        "playlist_smart",
        "playlist_created",
    ):
        if getattr(manifest, f) != getattr(base, f):
            return None
    changed = [e for k, e in manifest.items.items() if base.items.get(k) != e]
    deleted = [k for k in base.items if k not in manifest.items]
    if not changed and not deleted:
        return []
    if tail + len(changed) + len(deleted) > max(APPEND_MIN, len(manifest.items) // 4):
        return None
    manifest.lib_keys = keys
    manifest.header = base.header.model_copy()
    return [
        '{"type":"append"}',
        *(_item_line(e, keys) for e in changed),
        *(_delete_line(k, keys) for k in deleted),
    ]


def write(root: Path, manifest: Manifest, *, compact: bool = False) -> None:
    """Write the manifest: appended to the file when only items changed since it was last read or written here
    (see the module docstring), else in full; `compact` forces the full write."""
    path = root / MANIFEST_PATH
    base = None if compact else _base(root)
    if base is not None:
        lines = _tail_lines(base[0], base[1], manifest, _context)
        if lines == []:
            return
        if lines is not None:
            with open(path, "ab") as f:
                f.write(("\n".join(lines) + "\n").encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            with path.open("rb") as f:
                _remember(root, _key(f), manifest, base[1] + len(lines))
            return
    try:
        old = path.read_bytes()
    except OSError:
        old = None
    if old is not None and unchanged(old, manifest):
        return
    forget(root)
    atomic_write(path, dumps(manifest).encode("utf-8"))
    with contextlib.suppress(OSError), path.open("rb") as f:
        _remember(root, _key(f), manifest, 0)


def delete(root: Path) -> None:
    """Remove the manifest (and the state dir when nothing else is in it): the device is unmanaged again."""
    forget(root)
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
