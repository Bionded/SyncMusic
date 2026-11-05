# SPDX-License-Identifier: GPL-3.0-or-later
"""Sync windows: may an automatic sync start now, and when does the next window open."""

from __future__ import annotations

import datetime as dt

from syncmusic.core.models import SyncWindow

_LOOKAHEAD_DAYS = 8  # every weekday once, plus a window that started yesterday


def _spans(
    w: SyncWindow, day: dt.date, tz: dt.tzinfo | None
) -> tuple[dt.datetime, dt.datetime] | None:
    """The window as it opens on `day` (None: not on that weekday). A window over midnight ends the next day; one
    with start == end lasts the whole day."""
    if day.weekday() not in w.days:
        return None
    sh, sm = (int(x) for x in w.start.split(":"))
    eh, em = (int(x) for x in w.end.split(":"))
    start = dt.datetime.combine(day, dt.time(sh, sm), tz)
    end = dt.datetime.combine(day, dt.time(eh, em), tz)
    if end <= start:
        end += dt.timedelta(days=1)
    return start, end


def in_window(windows: list[SyncWindow], now: dt.datetime) -> bool:
    """True when no windows are set or `now` lies in one of them (yesterday's one may run past midnight)."""
    if not windows:
        return True
    for w in windows:
        for day in (now.date() - dt.timedelta(days=1), now.date()):
            span = _spans(w, day, now.tzinfo)
            if span and span[0] <= now < span[1]:
                return True
    return False


def next_start(windows: list[SyncWindow], now: dt.datetime) -> dt.datetime | None:
    """When the next window opens after `now` (None: no windows, or none on any weekday)."""
    starts = [
        span[0]
        for w in windows
        for i in range(_LOOKAHEAD_DAYS)
        if (span := _spans(w, now.date() + dt.timedelta(days=i), now.tzinfo)) and span[0] > now
    ]
    return min(starts, default=None)
