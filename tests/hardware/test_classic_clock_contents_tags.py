"""On the real iPod Classic.

- Time sync: a new device has time sync on; on connect the clock is set before anything mounts the data partition, and
  never while it is mounted; the file system stays clean.
- Tags: FLAC → ALAC from the local Navidrome keeps every source tag (MusicBrainz ids, totals, label, ...) and the
  cover in the device file.
- Player contents: names and tags come from the iTunesDB (equal to pygpod's read of it); a track another tool
  put on the iPod is listed as foreign with its database title.
- Removal: with no library attached, the foreign track and a synced one are removed from the iPod and its database.

The Classic's data partition is formatted (partition table unchanged) and left unmounted, not ejected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from syncmusic.app import Application
from syncmusic.core.models import LibraryConfig
from syncmusic.core.sync import RegisterRequest
from tests.factories import own_rules
from tests.hardware.conftest import quiesce
from tests.hardware.test_classic_acceptance import (
    _check_db,
    _data_listing,
    _partition_table,
)
from tests.hardware.test_player_contents import FIVE
from tests.hardware.test_ui_click_through import _fsck
from tests.integration.services import media_root
from tests.integration.test_navidrome_local import ENV

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]

NAME = "Clock Classic"
# FLAC (Vorbis comment) keys that must reach the ALAC file, by their MP4 atom
CARRIED = {
    "musicbrainz_trackid": "----:com.apple.iTunes:MusicBrainz Track Id",
    "musicbrainz_albumid": "----:com.apple.iTunes:MusicBrainz Album Id",
    "musicbrainz_artistid": "----:com.apple.iTunes:MusicBrainz Artist Id",
    "label": "----:com.apple.iTunes:LABEL",
    "title": "©nam",
    "album": "©alb",
    "artist": "©ART",
}


def _mounted(disk: str) -> bool:
    """Any partition of the disk (or the disk itself) is mounted."""
    return any(
        line.split()[0].startswith(disk) for line in Path("/proc/mounts").read_text().splitlines()
    )


def _source(mount: Path) -> str:
    for line in Path("/proc/mounts").read_text().splitlines():
        src, target = line.split()[:2]
        if target == str(mount):
            return src
    raise AssertionError(f"{mount} is not mounted")


def _owner_sources() -> dict:
    import mutagen

    out: dict = {}
    for p in (media_root() / "Music").rglob("*"):
        if p.suffix.lower() in (".flac", ".mp3"):
            f = mutagen.File(p, easy=False)
            title = f.tags.get("title") or f.tags.get("TIT2")
            if title:
                out.setdefault(str(title[0]), f)
    return out


@pytest.fixture
async def app(config, classic_disk):
    a = Application(config)
    await a.start()
    await a.store.put_library(
        LibraryConfig(
            id="nd",
            type="subsonic",
            name="Navidrome",
            settings={
                "url": ENV["NAVIDROME_URL"],
                "username": ENV["NAVIDROME_USER"],
                "password": ENV["NAVIDROME_PASSWORD"],
            },
        )
    )
    classic = classic_disk
    await a._disk_added(classic)
    yield a, classic
    await a.stop()


async def test_classic_clock_on_connect_contents_tags_and_foreign_removal(app):
    import mutagen
    from pygpod import Database

    a, classic = app
    before = _data_listing()
    table = _partition_table(classic.path)
    cfg = await a.sync.register(
        RegisterRequest(
            name=NAME,
            disk=classic.path,
            rule_set=await own_rules(a, NAME, FIVE, ["nd"]),
            on_connect="manual",
        )
    )
    assert cfg.settings.get("time_sync") is True  # time sync: on by default for a new device
    cfg = await a.sync.format(cfg.id, confirm=NAME)
    await a.sync._unmount(cfg.id)
    data_part = _source((await a.sync.open_device(cfg.id)).mount)  # the FAT32 partition
    await a.sync._unmount(cfg.id)
    assert data_part.startswith(classic.path) and not _mounted(classic.path)

    # time sync: a reconnect sets the clock first, while nothing has the data partition mounted
    calls: list[bool] = []
    real = a.sync._sync_time

    async def spy(c, device):
        calls.append(_mounted(classic.path) or c.id in a.sync._mounts)
        return await real(c, device)

    a.sync._sync_time = spy
    a.sync._clock_set.pop(cfg.id, None)
    await a._disk_added(classic)
    task = a.sync._clock_tasks.get(cfg.id)
    assert task is not None, "no clock task on connect"
    dev = await a.sync.open_device(cfg.id)  # waits for the clock (and its settle time)
    assert task.done() and task.result() is True
    assert calls == [False], calls
    assert dev.mount and _mounted(data_part)

    # tags and contents: sync five FLACs as ALAC, tags and cover kept; contents = pygpod's read of the iTunesDB
    pc = await a.sync.plan(cfg.id)
    job = await a.jobs.wait((await a.sync.apply(pc.plan.id, first_sync="keep")).id)
    assert job.status == "succeeded", job.error
    assert all(c is False for c in calls), calls
    dev, m, db = await _check_db(a, cfg.id)
    rows = await a.sync.contents(cfg.id)
    assert len(rows) == 5 and not any(r["foreign"] or r["orphan"] for r in rows)
    by_path = {"/".join(p for p in t.ipod_path.split(":") if p): t for t in db.tracks}
    # the local Navidrome serves the generated test media (read only here)
    sources = await asyncio.to_thread(_owner_sources)
    checked = 0
    for r in rows:
        t = by_path[r["path"]]
        assert (r["title"], r["artist"], r["album"]) == (t.title, t.artist, t.album)
        f = mutagen.File(dev.mount / r["path"])
        src = sources.get(r["title"])
        if r["path"].endswith(".m4a"):  # FLAC → ALAC
            assert f.info.codec == "alac", (r["path"], f.info.codec)
            assert f.tags.get("covr"), f"cover lost: {r['title']}"
            assert f.tags["trkn"][0][1] > 0, f"track total lost: {r['title']}"
            if src is not None and src.mime[0] == "audio/flac":
                for key, atom in CARRIED.items():
                    if key in src.tags:
                        assert f.tags.get(atom), f"{key} lost on {r['title']}"
                checked += 1
        elif src is not None:  # plain copy: every source frame is still there
            # the lyrics processor writes the library's lyrics with the library's language ("USLT::eng" in the
            # file can come back as "USLT::xxx"): lyrics frames count by kind
            def kinds(keys):
                return {k.split("::")[0] if k.startswith(("USLT", "SYLT")) else k for k in keys}

            missing = kinds(src.tags.keys()) - kinds(f.tags.keys())
            assert not missing, (r["title"], missing)
            checked += 1
    assert checked, "no synced track matched a source file by title"

    # contents and removal: a foreign track, then no library attached: remove it and one synced track
    src_file = dev.mount / rows[0]["path"]
    db = Database(str(dev.mount))
    foreign = db.add_track(str(src_file), title="Чужа пісня", artist="Сторонній")
    db.save()
    foreign_file = dev.mount / "/".join(p for p in foreign.ipod_path.split(":") if p)
    cfg = await a.sync.config(cfg.id)
    cfg.library_ids = []
    cfg = await a.sync.update_device(cfg)
    rows = {r["title"]: r for r in await a.sync.contents(cfg.id)}
    assert rows["Чужа пісня"]["foreign"] and rows["Чужа пісня"]["artist"] == "Сторонній"
    managed = next(r for r in rows.values() if not r["foreign"])
    job = await a.jobs.wait(
        (await a.sync.remove_from_device(cfg.id, [rows["Чужа пісня"]["key"], managed["key"]])).id
    )
    assert job.status == "succeeded", job.error
    dev = await a.sync.open_device(cfg.id)
    titles = [t.title for t in Database(str(dev.mount)).tracks]
    assert "Чужа пісня" not in titles and managed["title"] not in titles and len(titles) == 4
    assert not foreign_file.exists() and not (dev.mount / managed["path"]).exists()
    assert len(await a.sync.contents(cfg.id)) == 4

    await quiesce(a, cfg.id)
    await a.sync._unmount(cfg.id)
    await asyncio.to_thread(_fsck, data_part)
    assert _partition_table(classic.path) == table
    assert _data_listing() == before, "the test media changed"
