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

from syncmusic.core.models import DeviceContents, Manifest

CHUNK = 1024 * 1024


class StorageError(OSError):
    """The device's storage failed a read (EIO): the check stops."""


def _read_all(path: Path) -> None:
    with path.open("rb") as f:
        while f.read(CHUNK):
            pass


def check(
    mount: Path,
    manifest: Manifest | None,
    contents: DeviceContents,
    *,
    decode: Callable[[Path], str | None] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """The report: counts, missing, broken, foreign, orphans, the bytes they hold, and `io_error` when the storage
    failed (then the lists are what was found up to there). `decode(path)` returns an error text or None."""
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
    }
    total = len(entries) or 1
    for n, (key, e) in enumerate(sorted(entries.items(), key=lambda kv: kv[1].path)):
        if progress is not None and n % 10 == 0:
            progress(n / total, f"checking {n} of {len(entries)}")
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
        try:
            _read_all(path)
        except OSError as err:
            if err.errno == errno.EIO:
                report["io_error"] = f"{e.path}: {err.strerror or err}"
                return report
            broken.append(
                {"key": key, "path": e.path, "reason": f"cannot be read: {err.strerror or err}"}
            )
            continue
        report["bytes_read"] += size
        if decode is not None and (problem := decode(path)):
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
    for key, word in (
        ("missing", "missing"),
        ("broken", "broken"),
        ("foreign", "not from SyncMusic"),
        ("orphans", "orphans"),
    ):
        if report[key]:
            parts.append(f"{len(report[key])} {word}")
    return ", ".join(parts)
