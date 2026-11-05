"""Sync windows: boundaries, over midnight, weekdays, the next start."""

from __future__ import annotations

import datetime as dt

from syncmusic.core.models import SyncWindow
from syncmusic.core.windows import in_window, next_start

TZ = dt.UTC
MON = dt.date(2026, 11, 2)  # a Monday


def at(day: dt.date, hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), TZ)


def test_no_windows_is_any_time():
    assert in_window([], at(MON, "13:00"))
    assert next_start([], at(MON, "13:00")) is None


def test_boundaries():
    w = [SyncWindow(start="02:00", end="05:00")]
    assert not in_window(w, at(MON, "01:59"))
    assert in_window(w, at(MON, "02:00"))
    assert in_window(w, at(MON, "04:59"))
    assert not in_window(w, at(MON, "05:00"))
    assert next_start(w, at(MON, "05:00")) == at(MON + dt.timedelta(days=1), "02:00")
    assert next_start(w, at(MON, "01:00")) == at(MON, "02:00")


def test_over_midnight_belongs_to_its_start_day():
    w = [SyncWindow(start="23:00", end="06:00", days=[0])]  # Monday night only
    assert in_window(w, at(MON, "23:30"))
    assert in_window(w, at(MON + dt.timedelta(days=1), "05:59"))  # Tuesday morning, Monday's window
    assert not in_window(w, at(MON + dt.timedelta(days=1), "23:30"))
    assert not in_window(w, at(MON, "05:00"))  # Monday morning: Sunday's window, not set
    assert next_start(w, at(MON + dt.timedelta(days=1), "07:00")) == at(
        MON + dt.timedelta(days=7), "23:00"
    )


def test_several_windows_and_whole_day():
    w = [
        SyncWindow(start="12:00", end="13:00", days=[5]),
        SyncWindow(start="00:00", end="00:00", days=[6]),
    ]
    sat, sun = MON + dt.timedelta(days=5), MON + dt.timedelta(days=6)
    assert in_window(w, at(sat, "12:30")) and not in_window(w, at(sat, "13:30"))
    assert in_window(w, at(sun, "18:00"))
    assert next_start(w, at(MON, "10:00")) == at(sat, "12:00")
    assert next_start([SyncWindow(start="01:00", end="02:00", days=[])], at(MON, "00:00")) is None
