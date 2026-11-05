# SPDX-License-Identifier: GPL-3.0-or-later
"""Storage health of a device: write speed per full sync and I/O errors, and when to warn.

The sync service records a `write` sample (bytes, seconds spent writing) after every full sync that wrote at least
MIN_SAMPLE_BYTES, and an `io_error` whenever a sync or an integrity check stopped on the device's storage. The
warning says the storage may be failing when an I/O error happened in the last 30 days, or when the last sync wrote
at less than half the median speed of the syncs before it (at least 3 of them)."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

MIN_SAMPLE_BYTES = 5 * 1024 * 1024  # smaller syncs are dominated by the database and the flush
WINDOW = timedelta(days=30)
TREND = 10  # samples shown
DROP = 0.5


def summary(records: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """{io_errors, last_io_error, speeds [{at, mb_s}], last_mb_s, usual_mb_s, warning} from the records of the last
    30 days (oldest first)."""
    cut = (now - WINDOW).isoformat()
    recent = [r for r in records if r["at"] >= cut]
    errors = [r for r in recent if r["kind"] == "io_error"]
    speeds = [
        {"at": r["at"], "mb_s": round(r["bytes"] / r["seconds"] / 1e6, 2)}
        for r in recent
        if r["kind"] == "write" and r["seconds"] > 0 and r["bytes"] >= MIN_SAMPLE_BYTES
    ][-TREND:]
    last = speeds[-1]["mb_s"] if speeds else None
    before = [x["mb_s"] for x in speeds[:-1]]
    usual = round(statistics.median(before), 2) if len(before) >= 3 else None
    warning = None
    if errors:
        n = len(errors)
        warning = f"{n} I/O error{'s' if n != 1 else ''} in the last 30 days"
    elif last is not None and usual and last < usual * DROP:
        warning = f"the last sync wrote at {last} MB/s, usually {usual} MB/s"
    return {
        "io_errors": len(errors),
        "last_io_error": errors[-1] if errors else None,
        "speeds": speeds,
        "last_mb_s": last,
        "usual_mb_s": usual,
        "warning": warning,
    }
