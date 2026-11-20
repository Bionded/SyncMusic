# SPDX-License-Identifier: GPL-3.0-or-later
"""Device integrity check: compare what is on a device with its manifest and read every file back.

Read-only: it finds manifest entries without their file (missing - the next sync writes them again), files whose
size differs from the manifest or that cannot be read (broken; `deep` also decodes them with ffmpeg), media files that
are not in the manifest (put there by other tools) and items whose library no longer has them (orphans); the last two
are the space that could be freed. An I/O error stops the check at once with the storage hint - reading
on can hang for minutes per file on failing storage."""

from __future__ import annotations

import errno
from collections.abc import Callable
from pathlib import Path
from typing import Any

from syncmusic.core.models import DeviceContents, DeviceItem, Manifest

CHUNK = 1024 * 1024


class StorageError(OSError):
    """The device's storage failed a read (EIO): the check stops."""


MODES = ("quick", "full", "deep")


class Cancelled(Exception):
    """The user cancelled the check: the report holds what was found up to there."""


def _read_all(path: Path, cancelled: Callable[[], bool] | None = None) -> None:
    with path.open("rb") as f:
        while f.read(CHUNK):
            if cancelled is not None and cancelled():
                raise Cancelled


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def list_files(
    mount: Path,
    walk: Callable[[], list[Path]],
    media_exts: frozenset[str],
    *,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[float | None, str], None] | None = None,
) -> DeviceContents:
    """The media files of a folder-like device by path and size only (no tags: reading the tags of
    thousands of files over USB is what made a check sit at 0 for many minutes)."""
    items: list[DeviceItem] = []
    files = walk()
    for n, f in enumerate(files):
        if cancelled is not None and n % 50 == 0 and cancelled():
            raise Cancelled
        if progress is not None and n % 500 == 0:
            progress(None, f"listing the player's files: {n} of {len(files)}")
        if f.suffix.lower() not in media_exts:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        rel = f.relative_to(mount).as_posix()
        items.append(DeviceItem(device_ref=rel, path=rel, size=size, title=f.stem))
    return DeviceContents(items=items)


def check(
    mount: Path,
    manifest: Manifest | None,
    contents: DeviceContents,
    *,
    read_back: bool = True,
    decode: Callable[[Path], str | None] | None = None,
    progress: Callable[[float, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """The report: counts, missing, broken, foreign, orphans, the bytes they hold, and `io_error` when the storage
    failed or `cancelled` when the user stopped it (then the lists are what was found up to there).
    `read_back=False` (the quick check) only compares sizes; `decode(path)` returns an error text or None."""
    entries = manifest.items if manifest else {}
    # ours / not ours as Player contents tells them apart (core/contents.py)
    ours = manifest.by_device_ref() if manifest else {}
    missing: list[dict[str, Any]] = []
    broken: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = [
        {"key": k, "path": e.path, "size": e.size} for k, e in entries.items() if e.orphan
    ]
    foreign: list[dict[str, Any]] = [
        {"path": i.path, "size": i.size, "title": i.title}
        for i in contents.items
        if i.device_ref not in ours
    ]
    wasted = sum(int(x["size"]) for x in foreign) + sum(int(x["size"]) for x in orphans)
    report: dict[str, Any] = {
        "checked": 0,
        "bytes_read": 0,
        "missing": missing,
        "broken": broken,
        "foreign": foreign,
        "orphans": orphans,
        "wasted_bytes": wasted,
        "io_error": None,
        "deep": decode is not None,
        "mode": "deep" if decode is not None else "full" if read_back else "quick",
        "cancelled": False,
    }
    total_bytes = sum(e.size or 0 for e in entries.values()) or 1
    done_bytes = 0
    for n, (key, e) in enumerate(sorted(entries.items(), key=lambda kv: kv[1].path)):
        if cancelled is not None and cancelled():
            report["cancelled"] = True
            return report
        if progress is not None and (read_back or n % 50 == 0):
            frac = done_bytes / total_bytes if read_back else n / max(len(entries), 1)
            extra = f" ({_gb(done_bytes)} of {_gb(total_bytes)})" if read_back else ""
            progress(frac, f"checking {n} of {len(entries)}{extra}")
        done_bytes += e.size or 0
        path = mount / e.path
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            missing.append({"key": key, "path": e.path})
            continue
        except OSError as err:
            if err.errno == errno.EIO:
                report["io_error"] = f"{e.path}: {err.strerror or err}"
                return report
            broken.append(
                {"key": key, "path": e.path, "reason": f"cannot be read: {err.strerror or err}"}
            )
            continue
        report["checked"] += 1
        if e.size and size != e.size:
            broken.append(
                {"key": key, "path": e.path, "reason": f"size {size} bytes, written {e.size}"}
            )
            continue
        if not read_back:
            continue
        try:
            _read_all(path, cancelled)
        except Cancelled:
            report["cancelled"] = True
            return report
        except OSError as err:
            if err.errno == errno.EIO:
                report["io_error"] = f"{e.path}: {err.strerror or err}"
                return report
            broken.append(
                {"key": key, "path": e.path, "reason": f"cannot be read: {err.strerror or err}"}
            )
            continue
        report["bytes_read"] += size
        problem = decode(path) if decode is not None else None
        if cancelled is not None and cancelled():
            report["cancelled"] = True
            return report
        if problem:
            broken.append({"key": key, "path": e.path, "reason": problem})
    if progress is not None:
        progress(1.0, f"{len(entries)} items checked")
    return report


def after_fix(report: dict[str, Any], deleted: set[str]) -> dict[str, Any]:
    """The report once Delete broken files removed `deleted`: those are missing now (the next sync writes them
    again), `fixed` counts them."""
    out = dict(report)
    gone = [b for b in report["broken"] if b["path"] in deleted]
    if not gone:
        return report
    out["broken"] = [b for b in report["broken"] if b["path"] not in deleted]
    out["missing"] = report["missing"] + [
        {"key": b.get("key", ""), "path": b["path"]} for b in gone
    ]
    out["fixed"] = len(gone)
    out["summary"] = summary(out)
    return out


def summary(report: dict[str, Any]) -> str:
    """One line for the job message and the Overview."""
    if report.get("io_error"):
        return f"stopped: the device's storage failed ({report['io_error']})"
    parts = [f"{report['checked']} files checked"]
    if report.get("cancelled"):
        parts[0] = f"cancelled after {report['checked']} files"
    for key, word in (
        ("missing", "missing"),
        ("broken", "broken"),
        ("foreign", "not from SyncMusic"),
        ("orphans", "orphans"),
    ):
        if report[key]:
            parts.append(f"{len(report[key])} {word}")
    return ", ".join(parts)
