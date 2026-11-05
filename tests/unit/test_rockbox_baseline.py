"""Rockbox feedback baseline: the manifest's last-known play count / rating start from what the
tagcache holds for the file, not from the library."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core.models import DeviceConfig
from syncmusic.devices.Rockbox import rockbox_feedback as rf
from syncmusic.devices.Rockbox.rockbox import Rockbox
from tests.contracts.harness import rockbox_tree
from tests.rockbox_tagcache import Row, write_tagcache
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401


async def test_put_track_baseline_from_tagcache(tmp_path):
    from tests.contracts.harness import work_item

    mount = rockbox_tree(tmp_path / "rb")
    cfg = DeviceConfig(id="rb", type="rockbox", name="RB", settings={"filesystem": "vfat"})
    dev = Rockbox(cfg, mount, None)
    s = await dev.begin()
    first = await dev.put_track(s, work_item(tmp_path, "One", artist="A"))
    assert first.stats is not None and first.stats.playcount == 0 and first.stats.rating is None
    # Rockbox kept (resurrected) stats for the path; the item is written there again (replace)
    write_tagcache(mount / ".rockbox", [Row(f"/{first.path.upper()}", playcount=4, rating=7)])
    s = await dev.begin()
    ref = await dev.put_track(s, work_item(tmp_path, "One", artist="A"))
    assert ref.path == first.path  # ours (in the manifest session paths) → same path
    assert ref.stats is not None and (ref.stats.playcount, ref.stats.rating) == (4, 4)
    contents = await dev.read_contents()
    assert [i.stats for i in contents.items] == [ref.stats]  # adopt baseline


def test_counter_reset_counts_everything_since():
    from tests.unit.test_rockbox_tagcache import FAT, NOW, _manifest

    m = _manifest(**{"Music/A/a.mp3": {"last_playcount": 50}})
    entries = [rf.TagcacheEntry("/Music/A/a.mp3", 3, 0, 0, 1, 100_000)]
    fb = rf.tagcache_feedback(entries, m, FAT, now=NOW, device_id="rb")
    assert len(fb.plays) == 3


async def test_end_to_end_plays_and_ratings_after_sync(env, tmp_path, monkeypatch):
    """Library rates a song 4 stars and has played it 7 times; Rockbox's tagcache knows nothing about the new
    files. After the device counts 2 plays, exactly 2 are reported and the library rating is not removed."""
    from syncmusic.libraries.folder import FolderLibrary

    real = FolderLibrary._read_track

    def rated(self, *a, **kw):
        t = real(self, *a, **kw)
        return t and t.model_copy(update={"rating": 4, "play_count": 7})

    monkeypatch.setattr(FolderLibrary, "_read_track", rated)
    app, c = env
    mount = rockbox_tree(tmp_path / "rb")
    cfg = await _register(c, mount, name="Mini", type="rockbox", processors=[])
    plan = (await c.post(f"/api/v1/devices/{cfg['id']}/plan")).json()
    await _apply(app, c, plan["id"], confirm=True, first_sync="keep")
    dev = await app.sync.open_device(cfg["id"])
    manifest = await dev.read_manifest()
    assert manifest is not None and len(manifest.items) == 3
    for e in manifest.items.values():
        assert e.last_playcount == 0 and e.last_rating is None  # from the (empty) tagcache
    paths = sorted(e.path for e in manifest.items.values())
    rows = [Row(f"/{paths[0]}", playcount=2, lastplayed=5), *[Row(f"/{p}") for p in paths[1:]]]
    write_tagcache(mount / ".rockbox", rows)
    fb = await dev.read_feedback(manifest)
    assert len(fb.plays) == 2 and fb.ratings == {}
