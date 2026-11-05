"""HiBy R1 / SonixOS device: probe, reverse sync from the player's SQLite databases."""

from __future__ import annotations

import datetime as dt
import sqlite3

from syncmusic.core.models import (
    DeviceConfig,
    ItemRef,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    MediaKind,
)
from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.devices.HibyR1.sonixos import Hiby, device_rel
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.contracts.harness import hiby_sql, hiby_tree


def _blk(**kw) -> BlockDeviceInfo:
    # lsblk/udev of a R1: whole-disk exFAT, no partition table
    base = dict(
        name="sda",
        path="/dev/sda",
        type="disk",
        tran="usb",
        rm=True,
        size=512_711_000_000,
        fstype="exfat",
        uuid="92A3-B4C5",
        vendor="HiBy",
        model="R1 U-DISK",
        serial="HiBy_R1",
        usb_vendor_id="32bb",
        usb_product_id="0101",
    )
    return BlockDeviceInfo(**{**base, **kw})


def test_probe_beats_flash_drive():
    r = Hiby.probe(_blk(), None)
    assert r is not None and r.type_name == "hiby" and r.model == "R1"
    assert r.data_partition == "/dev/sda" and r.identity.fs_uuid == "92A3-B4C5"
    assert r.identity.usb_serial == "HiBy_R1" and r.suggested_name == "HiBy R1"
    flash = FlashDrive.probe(_blk(), None)
    assert flash is not None and r.confidence > flash.confidence
    assert Hiby.probe(_blk(usb_vendor_id="0951"), None) is None


def test_probe_by_database(tmp_path):
    assert Hiby.probe(None, tmp_path) is None
    assert Hiby.probe(None, hiby_tree(tmp_path)) is not None


def test_device_rel():
    assert device_rel("/mnt/sd_0/Music/a.flac") == "Music/a.flac"
    assert device_rel("/Music/a.flac") == "Music/a.flac"


def _dev(tmp_path) -> Hiby:
    mount = hiby_tree(tmp_path / "hiby")
    (mount / ".syncmusic").mkdir()
    return Hiby(DeviceConfig(id="h1", type="hiby", name="H"), mount, None)


def _manifest(**entries: dict) -> Manifest:
    items = {}
    for item_id, kw in entries.items():
        ref = ItemRef(library_id="lib", item_id=item_id)
        items[ref.key()] = ManifestEntry(
            ref=ref, device_ref=kw["path"], size=1, duration_s=200, **kw
        )
    return Manifest(header=ManifestHeader(device_id="h1"), items=items)


async def test_play_count_deltas(tmp_path):
    dev = _dev(tmp_path)
    m = _manifest(a={"path": "Music/A.flac", "last_playcount": 1}, b={"path": "Music/B.flac"})
    hiby_sql(
        dev.mount,
        "library.db",
        "INSERT INTO PLAY_COUNTS VALUES ('/mnt/sd_0/Music/A.flac', 3, 1793101600)",
    )
    hiby_sql(
        dev.mount,
        "library.db",
        "INSERT INTO PLAY_COUNTS VALUES ('/mnt/sd_0/Music/Gone.flac', 1, 1)",
    )
    # history logs skips too: not counted
    hiby_sql(
        dev.mount,
        "library.db",
        "INSERT INTO PLAY_HISTORY(path, played_at) VALUES ('/mnt/sd_0/Music/B.flac', 5)",
    )
    fb = await dev.read_feedback(m)
    assert fb.play_counts == {"lib/a": 3} and fb.unmapped == 1
    assert len(fb.plays) == 2 and {p.ref.item_id for p in fb.plays} == {"a"}
    newest = max(p.played_at for p in fb.plays)
    assert newest == dt.datetime.fromtimestamp(1793101600, dt.UTC)
    assert sorted(p.played_at_estimated for p in fb.plays) == [False, True]


async def test_counter_reset_counts_all(tmp_path):
    dev = _dev(tmp_path)
    m = _manifest(a={"path": "Music/A.flac", "last_playcount": 5})
    hiby_sql(
        dev.mount, "library.db", "INSERT INTO PLAY_COUNTS VALUES ('/mnt/sd_0/Music/A.flac', 2, 10)"
    )
    assert len((await dev.read_feedback(m)).plays) == 2


async def test_favourites_star_and_unstar(tmp_path):
    """hearts are favourites (starred), never ratings; added → star, removed → unstar, once."""
    dev = _dev(tmp_path)
    m = _manifest(a={"path": "Music/A.flac"}, b={"path": "Music/B.flac", "last_rating": 2})
    for p in ("A", "B"):
        hiby_sql(
            dev.mount,
            "library.db",
            f"INSERT INTO FAVOURITES VALUES ('/mnt/sd_0/Music/{p}.flac', '', '', 0)",
        )
    fb = await dev.read_feedback(m)
    assert fb.stars == {"lib/a": True, "lib/b": True} and fb.ratings == {}
    await dev.reset_feedback(None, fb)  # type: ignore[arg-type]
    assert (await dev.read_feedback(m)).stars == {}  # already sent
    await dev.reset_feedback(None, fb)  # type: ignore[arg-type]
    hiby_sql(dev.mount, "library.db", "DELETE FROM FAVOURITES WHERE path LIKE '%B.flac'")
    fb = await dev.read_feedback(m)
    assert fb.stars == {"lib/b": False} and fb.ratings == {}
    assert not Hiby.capabilities.supports_ratings_read


async def test_positions_audiobook_and_podcast(tmp_path):
    dev = _dev(tmp_path)
    m = _manifest(
        book={"path": "Audiobooks/X/002 - Two.mp3", "kind": MediaKind.AUDIOBOOK},
        done={"path": "Audiobooks/Y/001 - One.mp3", "kind": MediaKind.AUDIOBOOK},
        ep={"path": "Podcasts/S/ep.mp3", "kind": MediaKind.PODCAST, "last_position_s": 10.0},
        same={"path": "Podcasts/S/same.mp3", "kind": MediaKind.PODCAST, "last_position_s": 50.0},
    )
    hiby_sql(
        dev.mount,
        "audiobooks.db",
        "INSERT INTO AUDIOBOOK_TABLE(path, resume_file, resume_pos, last_played) VALUES (?, ?, 123.5, 1793101600)",
        "/mnt/sd_0/Audiobooks/X",
        "/mnt/sd_0/Audiobooks/X/002 - Two.mp3",
    )
    hiby_sql(
        dev.mount,
        "audiobooks.db",
        "INSERT INTO AUDIOBOOK_TABLE(path, resume_file, resume_pos) VALUES (?, ?, 0)",
        "/mnt/sd_0/Audiobooks/Y",
        "/mnt/sd_0/Audiobooks/Y/001 - One.mp3",
    )
    hiby_sql(
        dev.mount,
        "audiobooks.db",
        "INSERT INTO AUDIOBOOK_FINISHED VALUES ('/mnt/sd_0/Audiobooks/Y', 1)",
    )
    for key, path, pos in (("e", "ep", 300), ("s", "same", 50)):
        hiby_sql(
            dev.mount,
            "podcast.db",
            "INSERT INTO PODCAST_EPISODES(key, path, position, touched) VALUES (?, ?, ?, 1)",
            key,
            f"/mnt/sd_0/Podcasts/S/{path}.mp3",
            pos,
        )
    fb = await dev.read_feedback(m)
    assert set(fb.positions) == {"lib/book", "lib/done", "lib/ep"}
    assert fb.positions["lib/book"].position_s == 123.5 and not fb.positions["lib/book"].finished
    assert fb.positions["lib/done"].finished
    assert fb.positions["lib/ep"].position_s == 300


async def test_missing_or_broken_databases(tmp_path):
    dev = _dev(tmp_path)
    (dev.mount / ".local" / "podcast.db").write_bytes(b"not a database")
    (dev.mount / ".local" / "audiobooks.db").unlink()
    m = _manifest(a={"path": "Music/A.flac"})
    assert (await dev.read_feedback(m)).is_empty()


async def test_wipe_keeps_player_databases(tmp_path):
    dev = _dev(tmp_path)
    (dev.mount / "Music" / "x.flac").write_bytes(b"x")
    assert await dev.wipe() == 1
    assert (dev.mount / ".local" / "library.db").is_file() and not (dev.mount / "Music").exists()


async def test_put_track_baseline_from_player_not_library(tmp_path):
    """the manifest's play count starts from what SonixOS holds (0), not from the library's count, so the
    player's first play is a delta."""
    from tests.contracts.harness import work_item

    dev = _dev(tmp_path)
    s = await dev.begin()
    ref = await dev.put_track(s, work_item(tmp_path, "Why’s This Dealer?", artist="Niko B"))
    assert ref.stats is not None and ref.stats.playcount == 0 and ref.stats.position_s is None
    hiby_sql(
        dev.mount,
        "library.db",
        "INSERT INTO PLAY_COUNTS VALUES (?, 1, 1793189439)",
        f"/mnt/sd_0/{ref.path}",
    )
    s = await dev.begin()
    again = await dev.put_track(s, work_item(tmp_path, "Why’s This Dealer?", artist="Niko B"))
    assert again.path == ref.path and again.stats is not None and again.stats.playcount == 1
    contents = await dev.read_contents()
    assert [i.stats.playcount for i in contents.items if i.stats] == [1]  # adopt baseline
    fb = await dev.read_feedback(_manifest(a={"path": ref.path, "last_playcount": 0}))
    assert [p.played_at for p in fb.plays] == [dt.datetime.fromtimestamp(1793189439, dt.UTC)]


async def test_podcast_position_relative_path_no_time(tmp_path):
    """Podcast paths are relative and `touched` is a counter, not a time - the
    position maps and carries no update time (1970 would lose every conflict to the library)."""
    dev = _dev(tmp_path)
    path = "Podcast/Повна хронологія всесвіту Warhammer 40000/2026-08-14 - Створення Космодесантника.m4a"
    m = _manifest(ep={"path": path, "kind": MediaKind.PODCAST, "last_position_s": 0.0})
    hiby_sql(
        dev.mount,
        "podcast.db",
        "INSERT INTO PODCAST_EPISODES(key, path, position, duration, played, touched)"
        " VALUES (?, ?, 959.655034013605, 4885.3, 0, 1)",
        f"f:{path}",
        path,
    )
    fb = await dev.read_feedback(m)
    assert fb.positions["lib/ep"].position_s == 959.655034013605
    assert fb.positions["lib/ep"].updated_at is None and not fb.positions["lib/ep"].finished


def test_layout_uses_sonixos_folders(tmp_path):
    """podcasts go into SonixOS's `Podcast/` folder, audiobooks into `Audiobooks/`."""
    dev = _dev(tmp_path)
    layout = dev.s.layout
    assert layout.podcast.startswith("Podcast/{series}/")
    assert layout.audiobook.startswith("Audiobooks/") and layout.music.startswith("Music/")


async def test_put_track_tags_spare_the_index_card_reads(tmp_path, monkeypatch):
    """the tags of a written file come from the local copy at put_track and are kept in the cache file,
    so the commit's index rebuild (also of a fresh device object) reads nothing from the card."""
    from syncmusic.devices.HibyR1 import hiby_index
    from syncmusic.devices.HibyR1 import sonixos as hiby
    from tests.contracts.harness import work_item
    from tests.media import make_track

    dev = _dev(tmp_path)
    (dev.mount / ".local" / "library.db").unlink()
    hiby.ensure_databases(dev.mount, tmp_path / "bak")  # the full SonixOS schema
    s = await dev.begin()
    item = work_item(tmp_path, "Song", artist="Artist")
    item.path.unlink()
    make_track(item.path, title="Tagged", artist="Artist")
    ref = await dev.put_track(s, item)
    card_reads: list = []
    real = hiby_index.read_tags
    monkeypatch.setattr(
        hiby_index,
        "read_tags",
        lambda p: (card_reads.append(p) if dev.mount in p.parents else None) or real(p),
    )
    hiby_sql(dev.mount, "library.db", "DELETE FROM MEDIA_TABLE")
    assert await dev._rebuild_index() == 1
    assert card_reads == []
    assert (dev._backup_dir().parent / hiby.TAG_CACHE).is_file()
    again = Hiby(dev.config, dev.mount, None)  # a new job: the cache comes from the file
    hiby_sql(again.mount, "library.db", "DELETE FROM MEDIA_TABLE")
    assert await again._rebuild_index() == 1
    assert card_reads == []
    con = sqlite3.connect(again.mount / ".local" / "library.db")
    rows = con.execute("SELECT path, name, artist FROM MEDIA_TABLE").fetchall()
    con.close()
    assert rows == [(f"/mnt/sd_0/{ref.path}", "Tagged", "Artist")]
