"""Played to the end on any device: `reverse.mark_finished` on the feedback of a device."""

from __future__ import annotations

from datetime import UTC, datetime

from syncmusic.core.models import (
    DeviceFeedback,
    ItemRef,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    MediaKind,
    PlayEvent,
    PositionInfo,
)
from syncmusic.core.reverse import mark_finished

AT = datetime(2026, 10, 26, 12, tzinfo=UTC)


def _manifest(**last: float | None) -> Manifest:
    items = {
        f"abs/{k}": ManifestEntry(
            ref=ItemRef.parse(f"abs/{k}"),
            device_ref=k,
            path=k,
            size=1,
            kind=MediaKind.MUSIC if k == "song" else MediaKind.PODCAST,
            duration_s=600.0,
            last_position_s=v,
        )
        for k, v in last.items()
    }
    return Manifest(header=ManifestHeader(device_id="d"), items=items)


def _play(k: str) -> PlayEvent:
    return PlayEvent(ref=ItemRef.parse(f"abs/{k}"), played_at=AT, source_device="d")


def test_cases():
    m = _manifest(reset=0, near=0, relisten=0, start=0, song=None, again=600.0)
    fb = DeviceFeedback(
        positions={
            "abs/reset": PositionInfo(position_s=6.0),  # played through, bookmark reset
            "abs/near": PositionInfo(position_s=590.0),  # no play counted yet
            "abs/relisten": PositionInfo(position_s=200.0),  # played, then 200 s again
            "abs/start": PositionInfo(position_s=120.0),  # just a position
        }
    )
    plays = [_play("reset"), _play("relisten"), _play("song"), _play("again")]
    # the device's own bookmarks of the marked ones: the manifest keeps them (the next sync sees no change)
    assert mark_finished(fb, plays, m) == {"abs/reset": 6.0, "abs/near": 590.0}
    p = fb.positions
    assert p["abs/reset"] == PositionInfo(position_s=600.0, finished=True, updated_at=AT)
    assert p["abs/near"].finished and p["abs/near"].position_s == 600.0
    assert not p["abs/relisten"].finished and p["abs/relisten"].position_s == 200.0
    assert not p["abs/start"].finished
    assert (
        "abs/song" not in p and "abs/again" not in p
    )  # music; finished last time, no bookmark: nothing new
    assert mark_finished(DeviceFeedback(), [], None) == {}
