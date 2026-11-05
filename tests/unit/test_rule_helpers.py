"""Direct tests of the evaluator's value helpers (mutation survivors)."""

from datetime import UTC, datetime

import pytest

from syncmusic.core.models import Rule
from syncmusic.core.rules.evaluator import (
    _between,
    _cmp,
    _date_key,
    _fold,
    _group_key,
    _to_bool,
    _to_num,
    _values,
    apply_limits,
)
from tests.factories import track


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        (0.0, False),
        (2.5, True),
        ("true", True),
        (" TRUE ", True),
        ("1", True),
        ("Yes", True),
        ("false", False),
        (" False", False),
        ("0", False),
        ("NO", False),
        ("", False),
        ("  ", False),
        ("maybe", None),
        (None, None),
        ([1], None),
    ],
)
def test_to_bool(value: object, expected: bool | None) -> None:
    assert _to_bool(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, 1.0),
        (False, 0.0),
        (3, 3.0),
        (2.5, 2.5),
        ("4.5", 4.5),
        ("x", None),
        (None, None),
        ([1], None),
    ],
)
def test_to_num(value: object, expected: float | None) -> None:
    assert _to_num(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "true"),
        (False, "false"),
        (3.0, "3"),
        (3.5, "3.5"),
        (7, "7"),
        ("RoCK", "rock"),
        ("Straße", "strasse"),
    ],
)
def test_fold(value: object, expected: str) -> None:
    assert _fold(value) == expected


def test_values() -> None:
    assert _values(None) == []
    assert _values("") == []
    assert _values(0) == [0]
    assert _values("a") == ["a"]
    assert _values(["a", None, "", "b"]) == ["a", "b"]
    assert _values(("x",)) == ["x"]


def test_date_key_years_and_dates() -> None:
    assert _date_key(1999) == datetime(1999, 1, 1, tzinfo=UTC).timestamp()
    assert _date_key(1000) == datetime(1000, 1, 1, tzinfo=UTC).timestamp()
    assert _date_key(9999) == datetime(9999, 1, 1, tzinfo=UTC).timestamp()
    assert _date_key(2001.0) == datetime(2001, 1, 1, tzinfo=UTC).timestamp()
    # outside the year range, booleans and None are not years
    assert _date_key(10000) != datetime(9999, 1, 1, tzinfo=UTC).timestamp()
    assert _date_key(True) is None
    assert _date_key(None) is None
    assert _date_key("2020-05-01") == datetime(2020, 5, 1, tzinfo=UTC).timestamp()


def test_cmp_and_between() -> None:
    assert (_cmp(1, 2), _cmp(2, 1), _cmp(2, 2)) == (-1, 1, 0)
    assert _cmp(None, 1) == 0 and _cmp(1, None) == 0
    assert _between(5, 1, 5) and _between(1, 1, 5)
    assert not _between(6, 1, 5) and not _between(0, 1, 5)
    assert not _between(None, 1, 5) and not _between(3, None, 5) and not _between(3, 1, None)


def test_group_key() -> None:
    by_id = track("1", album_id="al", artist_id="ar", album="A", artist="X", album_artist="Y")
    assert _group_key(by_id, "album") == "lib/al"
    assert _group_key(by_id, "artist") == "lib/ar"
    no_ids = track("2", library_id="L2", album="A", artist="X", album_artist="Y")
    assert _group_key(no_ids, "album") == "L2/Y|A"
    assert _group_key(no_ids, "artist") == "L2/X"
    only_artist = track("3", album="A", artist="X")
    assert _group_key(only_artist, "album") == "lib/X|A"
    bare = track("4")
    assert _group_key(bare, "album") == "lib/|"
    assert _group_key(bare, "artist") == "lib/"


def _ids(tracks: list) -> list[str]:
    return [t.ref.item_id for t in tracks]


def test_apply_limits_count_and_percent() -> None:
    ts = [track(str(i)) for i in range(10)]
    assert _ids(apply_limits(ts, Rule(id="r", limit=0))) == []
    assert _ids(apply_limits(ts, Rule(id="r", limit=3))) == ["0", "1", "2"]
    assert len(apply_limits(ts, Rule(id="r", limit_percent=100))) == 10
    assert len(apply_limits(ts, Rule(id="r", limit_percent=150))) == 10  # capped at 100 %
    assert (
        len(apply_limits(ts, Rule(id="r", limit_percent=0))) == 1
    )  # at least 1 %, at least one track
    assert len(apply_limits(ts * 10, Rule(id="r", limit_percent=1))) == 1
    assert len(apply_limits(ts * 10, Rule(id="r", limit_percent=0))) == 1  # 0 % → 1 % of 100
    assert len(apply_limits(ts * 20, Rule(id="r", limit_percent=0))) == 2  # 1 % of 200, not 2 %
    assert len(apply_limits(ts, Rule(id="r", limit_percent=35))) == 3


def test_apply_limits_size_exact_fit() -> None:
    ts = [track(str(i), size_bytes=1000) for i in range(5)]
    assert _ids(apply_limits(ts, Rule(id="r", limit_size="3000B"))) == [
        "0",
        "1",
        "2",
    ]  # exact fit kept
    assert _ids(apply_limits(ts, Rule(id="r", limit_size="2999B"))) == ["0", "1"]
    mixed = [track("a", size_bytes=1500), track("b", size_bytes=1500), track("c", size_bytes=10)]
    assert _ids(apply_limits(mixed, Rule(id="r", limit_size="3000B"))) == [
        "a",
        "b",
    ]  # stops at the first overflow


def test_apply_limits_duration_exact_fit() -> None:
    ts = [track(str(i), duration_s=60.0) for i in range(5)]
    assert _ids(apply_limits(ts, Rule(id="r", limit_duration="3m"))) == ["0", "1", "2"]
    no_dur = [track(str(i), duration_s=None) for i in range(3)] + [track("x", duration_s=60.0)]
    assert _ids(apply_limits(no_dur, Rule(id="r", limit_duration="1m"))) == [
        "0",
        "1",
        "2",
        "x",
    ]  # unknown = 0 s
