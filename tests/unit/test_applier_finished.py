# SPDX-License-Identifier: GPL-3.0-or-later
"""A library that only marks a spoken-word item finished (same position) moves the device's bookmark once."""

from syncmusic.core.applier import Applier
from syncmusic.core.models import (
    ActionKind,
    ItemRef,
    ManifestEntry,
    MediaKind,
    PlanAction,
    PlanItem,
    PlannedFile,
    SyncReport,
    Track,
)


class _Dev:
    def __init__(self) -> None:
        self.calls: list[tuple[float, bool]] = []

    async def set_position(self, s, ref, pos, finished):
        self.calls.append((pos, finished))
        return True


def _applier(dev: _Dev) -> Applier:
    a = Applier.__new__(Applier)
    a.device = dev  # type: ignore[assignment]
    a.device_positions = set()

    async def ok() -> None:
        return None

    a.check_device = ok  # type: ignore[method-assign]
    return a


async def test_finished_with_unchanged_position_is_written_once():
    ref = ItemRef(library_id="abs", item_id="ep")
    track = Track(ref=ref, title="Ep", kind=MediaKind.PODCAST, position_s=30.0, finished=True)
    entry = ManifestEntry(
        ref=ref,
        device_ref="",
        path="Podcasts/Ep.mp3",
        size=1,
        kind=MediaKind.PODCAST,
        last_position_s=30.0,
    )
    action = PlanAction(
        kind=ActionKind.KEEP,
        ref=ref,
        entry=entry,
        item=PlanItem(track=track, file=PlannedFile(format="mp3", ext=".mp3", size=1)),
    )
    dev = _Dev()
    ap = _applier(dev)
    assert await ap._position(None, action, entry, SyncReport.__new__(SyncReport))
    assert dev.calls == [(30.0, True)] and entry.last_finished is True
    assert not await ap._position(None, action, entry, SyncReport.__new__(SyncReport))
    assert dev.calls == [(30.0, True)]


async def test_old_manifest_entries_still_read():
    e = ManifestEntry.model_validate(
        {"ref": {"library_id": "a", "item_id": "b"}, "device_ref": "", "path": "p", "size": 1}
    )
    assert e.last_finished is None
