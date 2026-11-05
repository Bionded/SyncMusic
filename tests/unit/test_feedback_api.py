"""listening data that did not reach its library is listed per device (title from the library's
stored snapshot, library name, attempts, last error and times), dropped rows too; GET /devices has the counts."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

from datetime import UTC, datetime

from syncmusic.core.models import ItemRef, PlayEvent
from syncmusic.infra.store import PendingRow
from tests.unit.test_api_sync import env, music  # noqa: F401


async def test_pending_and_dropped_listed(env, tmp_path):
    app, c = env
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Stick",
        "type": "flashdrive",
        "mount_path": str(tmp_path / "stick"),
        "library_ids": ["music"],
    }
    dev = (await c.post("/api/v1/devices/register", json=body)).json()["id"]
    plan = (
        await c.post(f"/api/v1/devices/{dev}/plan", json={})
    ).json()  # loads the library's snapshot
    item = next(a for a in plan["actions"] if a["item"])
    ref = ItemRef.model_validate(item["ref"])
    play = PlayEvent(ref=ref, played_at=datetime(2026, 10, 24, 8, 0, tzinfo=UTC), source_device=dev)
    await app.store.capture_feedback(
        dev,
        [("play", play.model_dump(mode="json")), ("rating", {"ref": ref.key(), "rating": 4})],
        [],
        None,
    )
    rows = await app.store.pending_feedback(dev)
    await app.store.retry_failed(rows[0].id, "server said 500", count=False)
    await app.store.drop_pending(
        PendingRow(rows[1].id, "rating", rows[1].data, 9, "gone"), dev, "song not found"
    )

    data = (await c.get(f"/api/v1/devices/{dev}/feedback")).json()
    (p,) = data["pending"]
    assert p["kind"] == "play" and p["title"] == item["item"]["track"]["title"]
    assert p["library"] == "Music" and p["error"] == "server said 500" and p["attempts"] == 0
    assert p["tries"] == 1  # a failed try that was no refusal still counts as a try
    assert p["tried_at"] and p["captured_at"] and p["value"].startswith("2026-10-24")
    (d,) = data["dropped"]
    assert (
        d["kind"] == "rating"
        and d["value"] == 4
        and d["error"] == "song not found"
        and d["attempts"] == 10
    )
    listed = next(
        x for x in (await c.get("/api/v1/devices")).json()["registered"] if x["id"] == dev
    )
    assert (listed["feedback_pending"], listed["feedback_dropped"]) == (1, 1)

    assert (await c.delete(f"/api/v1/devices/{dev}/feedback/dropped")).status_code == 204
    assert (await c.get(f"/api/v1/devices/{dev}/feedback")).json()["dropped"] == []
    assert (await c.get("/api/v1/devices/nope/feedback")).status_code == 404
