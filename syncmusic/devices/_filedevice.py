# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared implementation for devices that are a plain file tree (Rockbox, USB flash drive).

`FileDeviceMixin` provides everything except `probe`, `format` and the capabilities: paths from the layout
templates (`devices/paths.py`) made legal for the mount's filesystem, copies with fsync + atomic rename,
sidecars, `.m3u8` playlists with relative paths, contents by walking the tree, free space via statvfs and the
manifest / inflight list under `/.syncmusic/` (`core/manifest.py`).

Rules:
- Unmanaged files are never overwritten: a new path that collides with any existing file gets ' (2)' - unless the
  file there is byte-identical to the one we would write: then it is taken over as it is (files the user moved by hand were written again next to themselves).
- Every path is listed in the inflight list *before* it is written; `begin()` deletes leftovers of an
  interrupted sync that never reached the manifest.
- A tags-only re-process patches only the changed byte range of the file on the device when its size
  is unchanged (the tags fit their padding). The original bytes are journalled first (`/.syncmusic/patch`),
  `begin()` rolls an interrupted patch back, so a crash leaves the file with its old tags, never a broken one.
- `WorkItem.sidecars` keys starting with '.' are per-track files (`<track stem><ext>`); other keys are files
  in the track's folder (e.g. `cover.jpg`), removed with the folder's last track.

Impl settings must have `layout: PathLayout`; the impl defines `model_capabilities()` (path rules from
`path_rules()`).
"""

from __future__ import annotations

import asyncio
import base64
import filecmp
import json
import logging
import os
import posixpath
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

import mutagen

from syncmusic.base.capabilities import PathRules
from syncmusic.base.device import Device, DeviceSession
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import (
    DeviceContents,
    DeviceItem,
    DevicePlaylist,
    DeviceTrackRef,
    Manifest,
    MediaKind,
    PreparedItem,
)
from syncmusic.core.mount_path import DEVICE_MARKERS
from syncmusic.devices import paths
from syncmusic.infra.blockdev.mount import syncfs, unescape_mount_field

VIDEO_EXTS = frozenset({".m4v", ".mov", ".mkv", ".avi", ".webm"})  # .mp4 is in MEDIA_EXTS
MEDIA_EXTS = frozenset(
    {
        ".mp3",
        ".flac",
        ".m4a",
        ".m4b",
        ".mp4",
        ".aac",
        ".ogg",
        ".oga",
        ".opus",
        ".wav",
        ".aif",
        ".aiff",
    }
    | {".wma", ".wv", ".ape"}
    | VIDEO_EXTS
)
TMP_PREFIX = ".sdtmp-"  # short hidden temp names: never longer than the limit, skipped by walks
COPY_CHUNK = 4 * 1024 * 1024
PATCH_PATH = manifest_io.STATE_DIR / "patch"  # journal of an in-place tag patch
PATCH_MAX = 8 * 1024 * 1024  # a larger changed range is written as a new file
KIND_DIRS = {
    "podcasts": MediaKind.PODCAST,
    "audiobooks": MediaKind.AUDIOBOOK,
    "videos": MediaKind.VIDEO,
}


class FileTooLarge(ValueError):
    """The file exceeds the filesystem's maximum file size (FAT32: 4 GiB − 1)."""


def fstype_of(path: Path) -> str | None:
    """Filesystem type of the mount holding `path` (from /proc/self/mounts), None if unknown."""
    try:
        lines = Path("/proc/self/mounts").read_text().splitlines()
    except OSError:
        return None
    target = os.path.realpath(path)
    best: tuple[int, str] | None = None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        mnt = unescape_mount_field(fields[1])
        inside = target == mnt or target.startswith(mnt.rstrip("/") + "/")
        if inside and (best is None or len(mnt) > best[0]):
            best = (len(mnt), fields[2])
    return best[1] if best else None


def changed_range(a: Path, b: Path) -> tuple[int, int] | None:
    """[start, end) of the bytes that differ between two files of the same size; None when they are equal."""
    start = end = -1
    pos = 0
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while chunk := fa.read(COPY_CHUNK):
            other = fb.read(len(chunk))
            if chunk != other:
                first = next(i for i in range(len(chunk)) if chunk[i] != other[i])
                last = next(i for i in range(len(chunk) - 1, -1, -1) if chunk[i] != other[i])
                if start < 0:
                    start = pos + first
                end = pos + last + 1
            pos += len(chunk)
    return None if start < 0 else (start, end)


def patch_file(root: Path, rel: str, src: Path, start: int, end: int) -> None:
    """Write src[start:end] over the device file `rel`, journalling the original bytes first."""
    target = root / rel
    with open(target, "rb") as f:
        f.seek(start)
        original = f.read(end - start)
    journal = {"path": rel, "offset": start, "data": base64.b64encode(original).decode()}
    manifest_io.atomic_write(root / PATCH_PATH, json.dumps(journal).encode())
    with open(src, "rb") as f:
        f.seek(start)
        new = f.read(end - start)
    with open(target, "r+b") as f:
        f.seek(start)
        f.write(new)
        f.flush()
        os.fsync(f.fileno())
    (root / PATCH_PATH).unlink()


def roll_back_patch(root: Path) -> None:
    """Restore the original bytes of an interrupted in-place patch (no-op without a journal)."""
    try:
        journal = json.loads((root / PATCH_PATH).read_text())
    except FileNotFoundError:
        return
    except ValueError:  # the journal itself was cut short: the patch never started
        (root / PATCH_PATH).unlink(missing_ok=True)
        return
    target = (root / journal["path"]).resolve()
    if target.is_relative_to(root.resolve()) and target.is_file():
        with open(target, "r+b") as f:
            f.seek(journal["offset"])
            f.write(base64.b64decode(journal["data"]))
            f.flush()
            os.fsync(f.fileno())
        log.warning("rolled back an interrupted tag patch of %s", journal["path"])
    (root / PATCH_PATH).unlink(missing_ok=True)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def copy_atomic(src: Path, dest: Path) -> None:
    """Copy via a hidden temp file + fsync + rename (a crash never leaves a half file under the real name)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{TMP_PREFIX}{os.urandom(4).hex()}")
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, COPY_CHUNK)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(dest.parent)


log = logging.getLogger(__name__)


_TAG_CACHE: dict[str, tuple[int, int, dict[str, str], float | None]] = {}
TAG_CACHE_MAX = 200_000  # entries (all devices); cleared when full


def cached_tags(f: Path, st: os.stat_result) -> tuple[dict[str, str], float | None]:
    """`read_tags`, remembered per path while size and mtime stay the same: Player contents reads every file of
    the device, mutagen is the slow part."""
    key = os.fspath(f)
    hit = _TAG_CACHE.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2], hit[3]
    tags, duration = read_tags(f)
    if len(_TAG_CACHE) >= TAG_CACHE_MAX:
        _TAG_CACHE.clear()
    _TAG_CACHE[key] = (st.st_mtime_ns, st.st_size, tags, duration)
    return tags, duration


TAG_STORE_DIR = "device-tags"  # under the app's cache dir: <device id>.json


def load_tag_store(path: Path) -> dict[str, list[Any]]:
    """Tags of a device's files kept across restarts: device-relative path -> [mtime_ns, size, tags,
    duration]. A missing or broken file is an empty store."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_tag_store(path: Path, store: dict[str, list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _number(value: str | None) -> int | None:
    """'3', '3/12', '2001-05-01' → 3, 3, 2001; None when there is no leading number."""
    digits = ""
    for ch in (value or "").strip():
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits and int(digits) > 0 else None


def playlist_members(playlist: Path, root: Path) -> list[str]:
    """Device-relative paths of an .m3u8/.m3u playlist's entries (relative to the playlist, or absolute from the
    device root), in order; entries outside the device are skipped."""
    out = []
    base = PurePosixPath(playlist.relative_to(root).parent.as_posix())
    try:
        text = playlist.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff").replace("\\", "/")
        if not line or line.startswith("#"):
            continue
        rel = posixpath.normpath(line.lstrip("/") if line.startswith("/") else str(base / line))
        if rel != ".." and not rel.startswith("../"):
            out.append(rel)
    return out


def read_tags(f: Path) -> tuple[dict[str, str], float | None]:
    """First value of each easy tag and the duration; empty for unreadable files."""
    try:
        audio = mutagen.File(f, easy=True)
    except Exception:  # noqa: BLE001 - an unreadable file is still listed
        return {}, None
    if audio is None:
        return {}, None
    tags = {str(k): str(v[0]) for k, v in (audio.tags or {}).items() if v}
    return tags, getattr(audio.info, "length", None)


class FileDeviceMixin:
    """Mixin for `Device` subclasses backed by a mounted file tree."""

    mount: Path
    settings: Any
    # top-level names `wipe()` never deletes (impls may add their own, e.g. Rockbox's root logs)
    WIPE_KEEP: tuple[str, ...] = DEVICE_MARKERS

    # --- helpers ------------------------------------------------------------------------------

    def path_rules(self) -> PathRules:
        """Rules for the mounted filesystem (setting `filesystem` overrides detection); cached."""
        cached: PathRules | None = getattr(self, "_path_rules", None)
        if cached is None:
            fs = getattr(self.settings, "filesystem", None) or fstype_of(self.mount)
            cached = PathRules.for_filesystem(fs)
            if getattr(self.settings, "ascii_only", False):
                cached = cached.model_copy(update={"ascii_only": True})
            self._path_rules = cached
        return cached

    def _rules(self) -> PathRules:
        assert isinstance(self, Device)
        return self.effective_capabilities().path_rules

    def _session_rules(self, s: DeviceSession) -> PathRules:
        """The path rules resolved when the session opened: per-item calls must not recompute the capabilities."""
        rules = s.data.get("rules")
        if rules is None:
            rules = s.data["rules"] = self._rules()
        return rules

    def _playlist_dir(self) -> str:
        """Folder of the playlists we write: the setting `playlist_dir` where a device has it (Rockbox)."""
        return getattr(self.settings, "playlist_dir", None) or paths.PLAYLIST_DIR

    def _playlist_format(self) -> tuple[str, str]:
        """(extension, line end) of the playlists we write; UTF-8 `.m3u8` unless a device says otherwise."""
        return ".m3u8", "\n"

    def _tag_store_path(self) -> Path | None:
        """Where Player contents keeps the tags it read from this device, so a restart does not read every
        file again; None without an app context (tests, tools)."""
        ctx = getattr(self, "ctx", None)
        config = getattr(self, "config", None)
        if ctx is None or config is None:
            return None
        return Path(ctx.cache_dir) / TAG_STORE_DIR / f"{config.id}.json"

    def _walk(self) -> list[Path]:
        """All files on the device except our state dir and hidden/system folders."""
        out: list[Path] = []
        for root, dirs, files in os.walk(self.mount):
            dirs[:] = [
                d for d in dirs if not d.startswith(".") and d != "System Volume Information"
            ]
            out += [Path(root) / f for f in files if not f.startswith(".")]
        return out

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.mount).as_posix()

    # --- space and state ----------------------------------------------------------------------

    async def playlist_files(self, manifest: Manifest) -> dict[str, list[str]]:
        """Every .m3u8/.m3u in the playlist folder (and where the manifest says ours are) as name -> entries
        (device-relative paths); ours keep their name, others are named by their file (players that edit playlists)."""
        rules = self._rules()
        ours = {
            manifest.playlist_paths.get(n) or paths.playlist_path(n, rules): n
            for n in manifest.playlists
        }

        def work() -> dict[str, list[str]]:
            files = {rel for rel in ours if (self.mount / rel).is_file()}
            folder = self.mount / self._playlist_dir()
            if folder.is_dir():
                files |= {
                    self._rel(p)
                    for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in (".m3u8", ".m3u")
                }
            return {
                ours.get(rel) or PurePosixPath(rel).stem: playlist_members(
                    self.mount / rel, self.mount
                )
                for rel in sorted(files)
            }

        return await asyncio.to_thread(work)

    async def free_space(self) -> int:
        st = await asyncio.to_thread(os.statvfs, self.mount)
        return st.f_bavail * st.f_frsize

    async def capacity(self) -> int:
        st = await asyncio.to_thread(os.statvfs, self.mount)
        return st.f_blocks * st.f_frsize

    async def read_manifest(self) -> Manifest | None:
        return await asyncio.to_thread(manifest_io.read, self.mount)

    async def missing(self, manifest: Manifest) -> set[str]:
        """Manifest entries whose track file no longer exists (one stat per entry)."""

        def work() -> set[str]:
            root, isfile, join = str(self.mount), os.path.isfile, os.path.join  # no Path per entry
            return {k for k, e in manifest.items.items() if not isfile(join(root, e.path))}

        return await asyncio.to_thread(work)

    async def read_contents(self) -> DeviceContents:
        """Every media file on the device with its tags (also files other tools put there) and the
        playlists in the playlist folder (`Playlists/`) with their members."""

        store_path = self._tag_store_path()
        if store_path is not None and getattr(self, "_tag_store", None) is None:
            self._tag_store = await asyncio.to_thread(load_tag_store, store_path)
        store: dict[str, list[Any]] = getattr(self, "_tag_store", None) or {}

        def tags_of(f: Path, rel: str, st: os.stat_result) -> tuple[dict[str, str], float | None]:
            hit = store.get(rel)
            if (
                isinstance(hit, list)
                and len(hit) == 4
                and hit[0] == st.st_mtime_ns
                and hit[1] == st.st_size
            ):
                return hit[2], hit[3]
            tags, duration = cached_tags(f, st)
            store[rel] = [st.st_mtime_ns, st.st_size, tags, duration]
            return tags, duration

        def work() -> DeviceContents:
            fresh: set[str] = set()
            new = 0
            items = []
            for f in sorted(self._walk()):
                if f.suffix.lower() not in MEDIA_EXTS:
                    continue
                try:
                    st = f.stat()
                except OSError as e:  # vanished or unreadable since the walk
                    log.debug("skipping %s: %s", f, e)
                    continue
                rel = self._rel(f)
                hit = store.get(rel)
                tags, duration = tags_of(f, rel, st)
                new += store.get(rel) is not hit
                fresh.add(rel)

                top = PurePosixPath(rel).parts[0].casefold()
                items.append(
                    DeviceItem(
                        device_ref=rel,
                        path=rel,
                        size=st.st_size,
                        title=tags.get("title") or f.stem,
                        artist=tags.get("artist"),
                        album=tags.get("album"),
                        album_artist=tags.get("albumartist"),
                        genre=tags.get("genre"),
                        year=_number(tags.get("date") or tags.get("originaldate")),
                        track_number=_number(tags.get("tracknumber")),
                        disc_number=_number(tags.get("discnumber")),
                        duration_s=duration,
                        kind=MediaKind.VIDEO
                        if f.suffix.lower() in VIDEO_EXTS
                        else KIND_DIRS.get(top, MediaKind.MUSIC),
                    )
                )
            files = sorted(
                p
                for p in (self.mount / self._playlist_dir()).glob("*")
                if p.suffix.lower() in (".m3u8", ".m3u") and p.is_file()
            )
            gone = set(store) - fresh
            for rel in gone:
                del store[rel]
            if store_path is not None and (new or gone or not store_path.exists()):
                try:
                    save_tag_store(store_path, store)
                except OSError as e:
                    log.debug("tag cache of %s not saved: %s", self.mount, e)
            known = {i.device_ref for i in items}
            return DeviceContents(
                items=items,
                playlists=[p.stem for p in files],
                playlist_items={
                    p.stem: [r for r in playlist_members(p, self.mount) if r in known]
                    for p in files
                },
            )

        return await asyncio.to_thread(work)

    # --- session ------------------------------------------------------------------------------

    async def begin(self) -> DeviceSession:
        def work() -> DeviceSession:
            roll_back_patch(self.mount)
            manifest = manifest_io.read(self.mount)
            s = DeviceSession(manifest)
            s.data["inflight_removed"] = manifest_io.clean_inflight(self.mount, manifest)
            for f in self.mount.rglob(f"{TMP_PREFIX}*"):
                f.unlink(missing_ok=True)
            rules = s.data["rules"] = self._rules()
            s.data["taken"] = {paths.path_key(self._rel(f), rules) for f in self._walk()}
            # ItemRef key -> relative path of tracks on the device (manifest + this session)
            s.data["paths"] = {k: e.path for k, e in (manifest.items if manifest else {}).items()}
            return s

        return await asyncio.to_thread(work)

    async def put_track(self, s: DeviceSession, item: PreparedItem) -> DeviceTrackRef:
        rules = self._session_rules(s)
        size = item.path.stat().st_size
        if rules.max_file_size is not None and size > rules.max_file_size:
            raise FileTooLarge(
                f"{item.track.title}: {size} bytes exceeds the filesystem limit {rules.max_file_size}"
            )
        layout = paths.device_layout(self.settings)
        rel = paths.render(item.track, item.plan.file.ext, rules, layout)
        taken: set[str] = s.data["taken"]
        old = s.data["paths"].get(item.track.ref.key())
        adopt = False
        if old is None or paths.path_key(old, rules) != paths.path_key(rel, rules):
            ours = self._ours(s, rules)
            key = paths.path_key(rel, rules)
            adopt = (
                key in taken
                and key not in ours
                and await asyncio.to_thread(_same_file, item.path, self.mount / rel)
            )
            if not adopt:
                if rules.max_files_per_folder:  # car stereos
                    rel = paths.spill(
                        rel, self._folder_counts(s, rules), 1 + len(item.sidecars), rules
                    )
                rel = paths.unique(rel, taken, rules)
        dest = self.mount / rel
        targets = [(item.path, dest), *self._sidecar_targets(dest, item, rules)]
        if adopt:  # the file stays as it is; sidecars already there are not ours to replace
            targets = [(src, d) for src, d in targets[1:] if not d.exists()]
            log.info("%s: %s is already on the device: taken over", self.id, rel)
        written = [self._rel(d) for _, d in targets]

        def work() -> None:
            manifest_io.add_inflight(self.mount, written)
            for src, d in targets:
                copy_atomic(src, d)

        s.inflight += written
        await asyncio.to_thread(work)
        for w in written:
            if paths.path_key(w, rules) not in taken and rules.max_files_per_folder:
                counts = self._folder_counts(s, rules)
                parent = paths.path_key(str(PurePosixPath(w).parent), rules)
                counts[parent] = counts.get(parent, 0) + 1
            taken.add(paths.path_key(w, rules))
        s.data["paths"][item.track.ref.key()] = rel
        if (known := s.data.get("ours")) is not None:
            # its old path is no longer ours (as when the set is built from `paths`)
            if old is not None:
                known.discard(paths.path_key(old, rules))
            known.add(paths.path_key(rel, rules))
        return DeviceTrackRef(
            device_ref=rel,
            path=rel,
            size=dest.stat().st_size,
            sidecars=written if adopt else written[1:],
        )

    def _ours(self, s: DeviceSession, rules: PathRules) -> set[str]:
        """Path keys of the session's own tracks (manifest + written so far), built once per session: rebuilding it
        per written track made a first sync quadratic in the number of tracks."""
        ours: set[str] | None = s.data.get("ours")
        if ours is None:
            ours = {paths.path_key(p, rules) for p in s.data["paths"].values()}
            s.data["ours"] = ours
        return ours

    def _folder_counts(self, s: DeviceSession, rules: PathRules) -> dict[str, int]:
        """Entries per folder (path key of the parent) of the session's taken paths, counted once."""
        counts: dict[str, int] | None = s.data.get("folders")
        if counts is None:
            counts = {}
            for k in s.data["taken"]:
                parent = paths.path_key(str(PurePosixPath(k).parent), rules)
                counts[parent] = counts.get(parent, 0) + 1
            s.data["folders"] = counts
        return counts

    def _sidecar_targets(
        self, dest: Path, item: PreparedItem, rules: PathRules
    ) -> list[tuple[Path, Path]]:
        targets = []
        for key, src in sorted(item.sidecars.items()):
            if key.startswith("."):
                side = dest.with_name(
                    paths.sanitize_component(dest.stem + key, rules, has_ext=True)
                )
            else:
                side = dest.parent / paths.sanitize_component(key, rules, has_ext=True)
            targets.append((src, side))
        return targets

    async def rewrite_tags(
        self, s: DeviceSession, ref: DeviceTrackRef, item: PreparedItem
    ) -> DeviceTrackRef | None:
        """Tags-only re-process: patch the changed bytes of the file in place and rewrite its
        sidecars; None (a normal replace) when the size changed or the changed range is large."""
        rules = self._session_rules(s)
        dest = self.mount / ref.path
        if not ref.path or not dest.is_file() or dest.stat().st_size != item.path.stat().st_size:
            return None
        span = await asyncio.to_thread(changed_range, item.path, dest)
        if span is not None and span[1] - span[0] > PATCH_MAX:
            return None
        targets = self._sidecar_targets(dest, item, rules)
        new = [self._rel(d) for _, d in targets]
        fresh = [r for r in new if r not in ref.sidecars]

        def work() -> None:
            if span is not None:
                patch_file(self.mount, ref.path, item.path, *span)
            manifest_io.add_inflight(self.mount, fresh)
            for src, d in targets:
                copy_atomic(src, d)
            for rel in ref.sidecars:  # per-track sidecars the chain no longer makes
                p = self.mount / rel
                if rel not in new and p.parent == dest.parent and p.stem == dest.stem:
                    p.unlink(missing_ok=True)

        s.inflight += fresh
        await asyncio.to_thread(work)
        taken: set[str] = s.data["taken"]
        for w in fresh:
            taken.add(paths.path_key(w, rules))
        kept = [r for r in ref.sidecars if r not in new and (self.mount / r).exists()]
        return DeviceTrackRef(
            device_ref=ref.device_ref, path=ref.path, size=ref.size, sidecars=new + kept
        )

    async def remove_track(self, s: DeviceSession, ref: DeviceTrackRef) -> None:
        rules = self._session_rules(s)
        base = self.mount.resolve()

        def inside(rel: str) -> Path | None:
            p = (self.mount / rel).resolve()
            return p if p.is_relative_to(base) and p != base else None

        def work() -> None:
            track = inside(ref.path)
            if track is None:
                return
            track.unlink(missing_ok=True)
            folder_files = []
            for rel in ref.sidecars:
                p = inside(rel)
                if p is None:
                    continue
                if p.parent == track.parent and p.stem == track.stem:
                    p.unlink(missing_ok=True)
                else:
                    folder_files.append(p)
            for p in folder_files:  # folder files go with the folder's last track
                if p.parent.is_dir() and not any(
                    f.suffix.lower() in MEDIA_EXTS for f in p.parent.iterdir()
                ):
                    p.unlink(missing_ok=True)
            d = track.parent
            while d != base and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent

        await asyncio.to_thread(work)
        s.data["taken"].discard(paths.path_key(ref.path, rules))
        for key, rel in list(s.data["paths"].items()):
            if rel == ref.path:
                del s.data["paths"][key]
                s.data.pop("ours", None)  # rebuilt on the next use

    async def remove_playlists(self, s: DeviceSession, names: list[str]) -> None:
        """The player's own playlist files of these names (`playlist_files` names them by their file) go."""
        folder = self.mount / self._playlist_dir()
        gone = set(names)

        def work() -> None:
            if not folder.is_dir():
                return
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() in (".m3u8", ".m3u") and p.stem in gone:
                    p.unlink()
                    log.info("%s: playlist file %s removed", self.id, self._rel(p))

        await asyncio.to_thread(work)

    async def set_playlists(self, s: DeviceSession, playlists: list[DevicePlaylist]) -> None:
        rules = self._rules()
        by_ref: dict[str, str] = s.data["paths"]
        folder = self._playlist_dir()
        ext, eol = self._playlist_format()
        new = {p.name: paths.playlist_path(p.name, rules, folder, ext) for p in playlists}
        old_paths = s.manifest.playlist_paths if s.manifest else {}
        old = {
            n: old_paths.get(n) or paths.playlist_path(n, rules)
            for n in (s.manifest.playlists if s.manifest else [])
        }
        s.data["playlist_paths"] = new

        def work() -> None:
            # gone, or moved to another folder (the setting changed): the old file goes, then its empty folder
            for name, rel in old.items():
                if new.get(name) != rel:
                    (self.mount / rel).unlink(missing_ok=True)
                    d = (self.mount / rel).parent
                    while d != self.mount and d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
                        d = d.parent
            for pl in playlists:
                rel = new[pl.name]
                lines = ["#EXTM3U"]
                lines += [
                    posixpath.relpath(by_ref[r.key()], folder) for r in pl.refs if r.key() in by_ref
                ]
                data = (eol.join(lines) + eol).encode("utf-8")
                if not (self.mount / rel).exists():  # a new playlist is ours only once committed
                    manifest_io.add_inflight(self.mount, [rel])
                manifest_io.atomic_write(self.mount / rel, data)

        await asyncio.to_thread(work)

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        if "playlist_paths" in s.data:  # set_playlists ran: where this sync put them
            manifest.playlist_paths = {
                n: p for n, p in s.data["playlist_paths"].items() if n in manifest.playlists
            }

        def work() -> None:
            syncfs(
                self.mount
            )  # files are already fsynced; this also flushes directory updates on vfat
            manifest_io.write(self.mount, manifest)
            manifest_io.clear_inflight(self.mount)

        await asyncio.to_thread(work)
        s.inflight.clear()
        s.manifest = manifest

    def wipe_keeps(self, name: str) -> bool:
        """A card-root entry Wipe leaves alone."""
        return name in self.WIPE_KEEP

    async def wipe(self) -> int:
        """Delete every file and folder on the device except our state dir (manifest goes too) and
        `WIPE_KEEP`: iPod_Control / .rockbox (never destroy an iPod or Rockbox database, even if such a tree ended up behind a directory device). Returns the number of files deleted outside our state
        dir."""

        def files(p: Path) -> int:
            return sum(1 for f in p.rglob("*") if not f.is_dir()) if p.is_dir() else 1

        def work() -> int:
            n = 0
            for child in self.mount.iterdir():
                if self.wipe_keeps(child.name):
                    log.warning("wipe: keeping %s", child)
                elif child.name == manifest_io.STATE_DIR.name:
                    for f in child.iterdir():
                        shutil.rmtree(f) if f.is_dir() else f.unlink()
                elif child.is_dir() and not child.is_symlink():
                    n += files(child)
                    shutil.rmtree(child)
                else:
                    n += 1
                    child.unlink()
            return n

        return await asyncio.to_thread(work)

    async def abort(self, s: DeviceSession) -> None:
        """Delete what this session wrote (it never reached the manifest)."""
        await asyncio.to_thread(manifest_io.clean_inflight, self.mount, s.manifest)
        s.inflight.clear()


def _same_file(a: Path, b: Path) -> bool:
    """`b` is a file with exactly `a`'s bytes."""
    try:
        return (
            b.is_file()
            and a.stat().st_size == b.stat().st_size
            and filecmp.cmp(a, b, shallow=False)
        )
    except OSError:
        return False
