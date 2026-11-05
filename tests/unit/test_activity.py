# ruff: noqa: F811 - fixtures imported from test_api_sync

"""The action log: entries per kind from a sync report, and GET /activity."""

from __future__ import annotations

from syncmusic.core.activity import report_activity
from syncmusic.core.models import ItemFailure, ReverseReport, SyncReport
from tests.unit.test_api_sync import env, music  # noqa: F401


def _report(**kw) -> SyncReport:
    return SyncReport(id="r1", device_id="dev", **kw)


def test_every_kind():
    r = _report(
        kind="update",
        counts={"add": 2, "remove": 1, "keep": 9},
        reverse=ReverseReport(
            plays_submitted=3,
            stars_pushed=1,
            positions_pushed=2,
            playlists_pushed={"Road": ["a", "b"], "Made": ["c"]},
            playlists_created=["Made"],
        ),
        playlists_written={"Pacific Drive OST": 58},
        playlists_dropped=["Old"],
        playlists_removed=["Gone"],
        failures=[ItemFailure(ref="x", error="boom")],
    )
    got = [(a.kind, a.name, a.counts) for a in report_activity(r)]
    assert got == [
        ("sent", "", {"plays": 3, "hearts": 1, "positions": 2}),
        ("playlist_to_library", "Road", {"songs": 2}),
        ("playlist_new_in_library", "Made", {}),
        ("files", "", {"add": 2, "remove": 1}),
        ("playlist_written", "Pacific Drive OST", {"songs": 58}),
        ("playlist_removed", "Old", {}),
        ("playlist_removed", "Gone", {}),
        ("failed", "", {"items": 1}),
    ]
    assert {a.sync_kind for a in report_activity(r)} == {"update"}


def test_nothing_done_is_no_entry():
    assert report_activity(_report(counts={"keep": 5}, reverse=ReverseReport(plays_found=0))) == []


async def test_api_activity(env):
    """GET /activity: entries of the stored reports, newest first, filtered by kind and device."""
    from datetime import UTC, datetime, timedelta

    app, c = env
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    old = _report(finished_at=t0, playlists_written={"A": 3})
    new = SyncReport(
        id="r2",
        device_id="dev",
        finished_at=t0 + timedelta(hours=1),
        reverse=ReverseReport(plays_submitted=2),
    )
    await app.store.put_report(old)
    await app.store.put_report(new)
    got = (await c.get("/api/v1/activity")).json()
    assert [(a["report_id"], a["kind"]) for a in got] == [
        ("r2", "sent"),
        ("r1", "playlist_written"),
    ]
    assert got[0]["counts"] == {"plays": 2}
    assert [a["kind"] for a in (await c.get("/api/v1/activity?kind=playlist_written")).json()] == [
        "playlist_written"
    ]
    assert (await c.get("/api/v1/activity?device=other")).json() == []


async def test_api_activity_sorted_by_finish(env):
    """A long sync that started first but finished last comes first (finished 14:42, after one finished at 13:09)."""
    from datetime import UTC, datetime, timedelta

    app, c = env
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    long = SyncReport(
        id="long",
        device_id="dev",
        started_at=t0,
        finished_at=t0 + timedelta(hours=3),
        playlists_written={"L": 1},
    )
    short = SyncReport(
        id="short",
        device_id="dev",
        started_at=t0 + timedelta(hours=1),
        finished_at=t0 + timedelta(hours=2),
        playlists_written={"S": 1},
    )
    await app.store.put_report(long)
    await app.store.put_report(short)
    got = (await c.get("/api/v1/activity")).json()
    assert [a["report_id"] for a in got] == ["long", "short"]
    assert [a["report_id"] for a in (await c.get("/api/v1/activity?limit=1")).json()] == ["long"]


def test_entries_name_their_target():
    """device entries carry the device's name, library entries the libraries'."""
    r = _report(reverse=ReverseReport(plays_submitted=1), playlists_written={"A": 1})
    got = {a.kind: a.target for a in report_activity(r, "My R1", "Navidrome")}
    assert got == {"sent": "Navidrome", "playlist_written": "My R1"}
