# SPDX-License-Identifier: GPL-3.0-or-later
"""Players without a time zone store wall-clock time (iPod time_played, Rockbox mktime); convert it to UTC."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


def wall_to_utc(
    wall: dt.datetime, zone: str | None = None, now: dt.datetime | None = None
) -> dt.datetime:
    """Wall-clock time in `zone` (None = the host's) → aware UTC. In the repeated hour after a fall-back change the
    later reading wins unless it lies in the future: a play is in the past, usually recent."""
    naive = wall.replace(tzinfo=None)
    tz = ZoneInfo(zone) if zone else None

    def utc(fold: int) -> dt.datetime:
        local = naive.replace(fold=fold)
        return (local.replace(tzinfo=tz) if tz else local).astimezone(dt.UTC)

    early, late = utc(0), utc(1)
    if late != early and late <= (now or dt.datetime.now(dt.UTC)):
        return late
    return early
