"""Wall-clock player times → UTC, also in the repeated hour after a fall-back change."""

import datetime as dt
import os
import time

import pytest

from syncmusic.devices.wallclock import wall_to_utc

UTC = dt.UTC
WALL = dt.datetime(2026, 10, 25, 1, 30)  # 01:00-02:00 happens twice in Europe/Lisbon that night


def test_repeated_hour_picks_the_later_reading_when_it_is_past():
    now = dt.datetime(2026, 10, 25, 1, 50, tzinfo=UTC)  # 01:50 WET
    assert wall_to_utc(WALL, "Europe/Lisbon", now) == dt.datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_repeated_hour_keeps_the_earlier_reading_when_the_later_is_future():
    now = dt.datetime(2026, 10, 25, 0, 50, tzinfo=UTC)  # 01:50 WEST, the first pass of the hour
    assert wall_to_utc(WALL, "Europe/Lisbon", now) == dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_unambiguous_times_unchanged():
    now = dt.datetime(2026, 12, 1, tzinfo=UTC)
    assert wall_to_utc(dt.datetime(2026, 7, 1, 12, 0), "Europe/Lisbon", now) == dt.datetime(
        2026, 7, 1, 11, 0, tzinfo=UTC
    )
    assert wall_to_utc(dt.datetime(2026, 1, 10, 20, 0), "Asia/Tokyo", now) == dt.datetime(
        2026, 1, 10, 11, 0, tzinfo=UTC
    )


@pytest.fixture
def lisbon_host():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Lisbon"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_host_zone_repeated_hour(lisbon_host):
    now = dt.datetime(2026, 10, 25, 1, 50, tzinfo=UTC)
    assert wall_to_utc(WALL, None, now) == dt.datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
