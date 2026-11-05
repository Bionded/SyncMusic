"""LibraryContract: what every Library implementation must do."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncmusic.base.library import Library
from syncmusic.core.models import Track


async def _read_all(media) -> int:
    if isinstance(media, Path):
        return media.stat().st_size
    total = 0
    async for chunk in media:
        total += len(chunk)
    return total


async def test_snapshot_valid_and_stable(library_case, tmp_path):
    name, h, expected = library_case
    lib = h.make(tmp_path)
    if expected is not None:
        assert isinstance(lib, expected)
    assert isinstance(lib, Library) and lib.type_name == name
    a = await lib.snapshot()
    b = await lib.snapshot()
    assert a.library_id == lib.id and len(a.tracks) >= 2
    for item_id, t in a.tracks.items():
        Track.model_validate(t.model_dump())
        assert t.ref.library_id == lib.id and t.ref.item_id == item_id
        assert t.kind in lib.capabilities.kinds
    assert set(a.tracks) == set(b.tracks)  # ids stable across snapshots
    assert a.state_token == b.state_token == await lib.state_token()
    await lib.close()


async def test_state_token_changes_after_content_change(library_case, tmp_path):
    _, h, _ = library_case
    lib = h.make(tmp_path)
    before = await lib.state_token()
    h.mutate(lib)
    assert await lib.state_token() != before
    await lib.close()


async def test_open_media_matches_size(library_case, tmp_path):
    _, h, _ = library_case
    lib = h.make(tmp_path)
    snap = await lib.snapshot()
    for item_id, t in list(snap.tracks.items())[:3]:
        assert await _read_all(await lib.open_media(item_id)) == t.size_bytes
    await lib.close()


async def test_check(library_case, tmp_path):
    _, h, _ = library_case
    lib = h.make(tmp_path)
    assert (await lib.check()).ok
    await lib.close()
    if h.bad_credentials is not None:
        bad = h.bad_credentials(tmp_path)
        status = await bad.check()
        assert not status.ok and status.message
        await bad.close()


async def test_open_media_unknown_id_raises(library_case, tmp_path):
    """An item that vanished between snapshot and download must fail loudly, never yield an error body
    as media bytes (it would be copied to the device as a track)."""
    _, h, _ = library_case
    lib = h.make(tmp_path)
    snap = await lib.snapshot()
    item_id = next(iter(snap.tracks))
    with pytest.raises(Exception):  # noqa: B017,PT011 - any error, as long as it is not silent
        await _read_all(await lib.open_media(item_id + "-gone"))
    await lib.close()
