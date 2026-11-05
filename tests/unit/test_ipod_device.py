"""Stock iPod device on virtual iPod trees; the contract suite covers the basics."""

from __future__ import annotations

import datetime as dt

import pytest

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import DeviceConfig, DevicePlaylist, MediaKind
from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.devices.iPod.ipod import IPod
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.contracts.harness import _ipod_feedback, _ipod_item, ipod_tree
from tests.contracts.test_device_contract import _sync


def _dev(tmp_path, sysinfo: bytes | None = None, name="ipod1") -> IPod:
    mount = ipod_tree(tmp_path / name)
    if sysinfo is not None:
        (mount / "iPod_Control" / "Device" / "SysInfo").write_bytes(sysinfo)
    dev = IPod(DeviceConfig(id=name, type="ipod", name="iPod"), mount, None)

    class H:
        make_item = staticmethod(_ipod_item)

    dev._harness = H  # type: ignore[attr-defined]
    return dev


def _apple_disk(**kw) -> BlockDeviceInfo:
    fw = BlockDeviceInfo(name="sdb1", path="/dev/sdb1", type="part", size=40_000_000)
    data = BlockDeviceInfo(
        name="sdb2",
        path="/dev/sdb2",
        type="part",
        fstype="vfat",
        uuid="AB-CD",
        label="IPOD",
        size=200_000_000_000,
    )
    return BlockDeviceInfo(
        name="sdb",
        path="/dev/sdb",
        type="disk",
        tran="usb",
        serial="000A27",
        usb_vendor_id="05ac",
        children=[fw, data],
        **kw,
    )


def test_probe_beats_flashdrive(tmp_path):
    disk = _apple_disk()
    r = IPod.probe(disk, None)
    assert r is not None and r.data_partition == "/dev/sdb2" and r.identity.usb_serial == "000A27"
    assert FlashDrive.probe(disk, None) is None
    other = BlockDeviceInfo(
        name="sdc", path="/dev/sdc", type="disk", tran="usb", usb_vendor_id="0951"
    )
    assert IPod.probe(other, None) is None

    mount = ipod_tree(tmp_path / "tree", name="Домашній iPod")
    r = IPod.probe(None, mount)
    assert r is not None and r.confidence > 10 and r.suggested_name == "Домашній iPod"
    assert r.model == "classic_3"
    assert FlashDrive.probe(None, mount) is None
    (mount / ".rockbox").mkdir()
    # a Rockbox iPod: offered as stock too (dual boot), after the Rockbox class
    r = IPod.probe(None, mount)
    assert r is not None and r.confidence == 30 and r.details == {"rockbox": True}
    rb = tmp_path / "rb"
    (rb / ".rockbox").mkdir(parents=True)
    assert IPod.probe(None, rb) is None  # only /.rockbox, nothing says it is an iPod
    r = IPod.probe(_apple_disk(), rb)  # an Apple disk: the first stock sync creates iPod_Control
    assert r is not None and r.confidence == 30 and r.data_partition == "/dev/sdb2"
    from syncmusic.devices.Rockbox.rockbox import Rockbox
    from tests.contracts.harness import rockbox_tree

    assert Rockbox.probe(_apple_disk(), rockbox_tree(tmp_path / "rb2")).confidence > 30


def test_capabilities_per_model(tmp_path):
    classic = _dev(tmp_path).effective_capabilities()
    assert {"mp3", "aac", "alac"} <= classic.formats and "flac" not in classic.formats
    assert classic.has_rtc and classic.needs_device_db
    assert not classic.supports_time_sync  # off by default is verified on hardware
    assert classic.cover is not None and classic.cover.max_size >= 320
    assert classic.max_sample_rate == 48000 and classic.max_bit_depth == 16

    mini1 = _dev(tmp_path, b"ModelNumStr: M9160\n", name="mini").effective_capabilities()
    assert "alac" in mini1.formats and mini1.cover is None  # ALAC came with mini firmware 1.2

    second = _dev(tmp_path, b"ModelNumStr: M8737\n", name="2g").effective_capabilities()
    assert "alac" not in second.formats and second.formats == {"mp3", "aac", "wav", "aiff"}

    shuffle = _dev(tmp_path, b"ModelNumStr: MA546\n", name="shuffle").effective_capabilities()
    assert not shuffle.supports_playlists


async def test_metadata_update_and_playlists_persist(tmp_path):
    dev = _dev(tmp_path)
    manifest, refs = await _sync(dev, tmp_path, [("Група крові", "Скрябін"), ("Зозуля", "Скрябін")])
    (item_a, ref_a), (item_b, _) = refs

    s = await dev.begin()
    changed = item_a.track.model_copy(
        update={"title": "Група крові (2024)", "rating": 4, "year": 1988}
    )
    await dev.update_metadata(s, ref_a, changed)
    await dev.set_playlists(
        s, [DevicePlaylist(name="Найкраще", refs=[item_b.track.ref, item_a.track.ref])]
    )
    manifest.playlists = ["Найкраще"]
    await dev.commit(s, manifest)

    from pygpod import Database

    db = Database(str(dev.mount))
    t = next(t for t in db.tracks if str(t.dbid) == ref_a.device_ref)
    assert (t.title, t.rating, t.year) == ("Група крові (2024)", 80, 1988)
    assert [x.title for x in db.get_playlist("Найкраще").tracks] == ["Зозуля", "Група крові (2024)"]
    assert "Найкраще" in (await dev.read_contents()).playlists

    s = await dev.begin()
    await dev.set_playlists(s, [])
    manifest.playlists = []
    await dev.commit(s, manifest)
    assert Database(str(dev.mount)).get_playlist("Найкраще") is None


async def test_feedback_plays_ratings_and_no_double_count(tmp_path):
    dev = _dev(tmp_path)
    manifest, refs = await _sync(dev, tmp_path, [("One", "A"), ("Two", "B")])
    await _ipod_feedback(dev, manifest)
    fb = await dev.read_feedback(await dev.read_manifest())
    assert len(fb.plays) == 4
    for key in manifest.items:
        plays = sorted((p for p in fb.plays if p.ref.key() == key), key=lambda p: p.played_at)
        assert [p.played_at_estimated for p in plays] == [True, False]
        assert (plays[1].played_at - plays[0].played_at).total_seconds() == pytest.approx(3, abs=1)
        assert fb.ratings[key] == 4 and fb.play_counts[key] == 2
    # a forward sync before the reverse sync: the Play Counts file is merged, the plays stay visible
    s = await dev.begin()
    await dev.commit(s, manifest)
    assert not (dev.mount / "iPod_Control" / "iTunes" / "Play Counts").exists()
    again = await dev.read_feedback(await dev.read_manifest())
    assert len(again.plays) == 4
    # after the reverse sync the manifest knows the counts: nothing left to report
    for e in manifest.items.values():
        e.last_playcount, e.last_rating = 2, 4
    s = await dev.begin()
    await dev.commit(s, manifest)
    assert (await dev.read_feedback(await dev.read_manifest())).is_empty()


async def test_audiobook_position_feedback(tmp_path):
    dev = _dev(tmp_path)
    item = _ipod_item(tmp_path, "Розділ 1", "Автор")
    track = item.track.model_copy(update={"kind": MediaKind.AUDIOBOOK, "position_s": 12.5})
    item = item.model_copy(update={"plan": item.plan.model_copy(update={"track": track})})
    s = await dev.begin()
    ref = await dev.put_track(s, item)
    from syncmusic.core.models import Manifest, ManifestEntry, ManifestHeader

    manifest = Manifest(
        header=ManifestHeader(device_id=dev.id),
        items={
            track.ref.key(): ManifestEntry(
                ref=track.ref,
                device_ref=ref.device_ref,
                path=ref.path,
                size=ref.size,
                kind=MediaKind.AUDIOBOOK,
                last_position_s=12.5,
            )
        },
    )
    await dev.commit(s, manifest)
    from pygpod import Database

    db = Database(str(dev.mount))
    t = db.tracks[0]
    assert t.remember_position and t.bookmark_time == 12500
    t.bookmark_time = 61000  # listened on the iPod
    db.save()
    fb = await dev.read_feedback(manifest)
    assert fb.positions[track.ref.key()].position_s == 61.0


async def test_crash_between_db_and_manifest_self_heals(tmp_path, monkeypatch):
    dev = _dev(tmp_path)
    manifest, _ = await _sync(dev, tmp_path, [("One", "A")])
    s = await dev.begin()
    item = _ipod_item(tmp_path, "Two", "B")
    ref = await dev.put_track(s, item)

    def crash(*a, **kw):
        raise KeyboardInterrupt  # power loss after db.save(), before the manifest

    monkeypatch.setattr(manifest_io, "write", crash)
    with pytest.raises(KeyboardInterrupt):
        await dev.commit(s, manifest)
    monkeypatch.undo()
    assert len((await dev.read_contents()).items) == 2  # the DB has the new track

    s = await dev.begin()  # inflight file removed, its DB entry dropped
    assert not (dev.mount / ref.path).exists()
    await dev.commit(s, manifest)
    contents = await dev.read_contents()
    assert [i.device_ref for i in contents.items] == [e.device_ref for e in manifest.items.values()]


async def test_abort_removes_copied_files(tmp_path):
    dev = _dev(tmp_path)
    await _sync(dev, tmp_path, [("One", "A")])
    s = await dev.begin()
    ref = await dev.put_track(s, _ipod_item(tmp_path, "Two", "B"))
    assert (dev.mount / ref.path).is_file()
    await dev.abort(s)
    assert not (dev.mount / ref.path).exists()
    assert len((await dev.read_contents()).items) == 1


async def test_missing_reports_deleted_files(tmp_path):
    dev = _dev(tmp_path)
    manifest, refs = await _sync(dev, tmp_path, [("One", "A"), ("Two", "B")])
    (dev.mount / refs[0][1].path).unlink()
    assert await dev.missing(manifest) == {refs[0][0].track.ref.key()}


def test_commit_reserve_covers_staged_db(tmp_path):
    dev = _dev(tmp_path)
    assert dev.commit_reserve(11839) > 2 * 16_400_000  # a Classic: 16.4 MB iTunesDB


def test_data_partition_never_the_firmware_partition():
    """Real lsblk output (tests/unit/test_guard.py fixture): mini 2G sdc = firmware sdc1 + FAT32 sdc2."""
    from tests.unit.test_guard import DATA, parse_lsblk

    disks = {d.name: d for d in parse_lsblk(DATA.read_text())}
    assert IPod._data_partition(disks["sdc"]).path == "/dev/sdc2"
    assert IPod._data_partition(disks["sdb"]).path == "/dev/sdb1"
    # unreadable FAT: chosen by partition type, still not the type-0 firmware partition
    for p in disks["sdc"].partitions():
        p.fstype = None
    assert IPod._data_partition(disks["sdc"]).path == "/dev/sdc2"


def _kind_item(tmp_path, title, kind, **update):
    item = _ipod_item(tmp_path, title, "Автор")
    track = item.track.model_copy(update={"kind": kind, **update})
    return item.model_copy(update={"plan": item.plan.model_copy(update={"track": track})})


async def test_podcast_audiobook_and_artwork(tmp_path):
    """podcast flags + podcast list grouped by show, audiobook type + remember position,
    bookmark from the library position, artwork from the prepared cover (covers mode `device`)."""
    from pygpod import Database
    from pygpod.db.constants import MEDIATYPE_AUDIOBOOK, MEDIATYPE_PODCAST
    from pygpod.model.artwork_manager import ArtworkManager

    from tests.media import big_cover

    dev = _dev(tmp_path)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(big_cover(600, 600))
    published = dt.datetime(2024, 3, 5, 12, 0, tzinfo=dt.UTC)
    episode = _kind_item(
        tmp_path,
        "Випуск 1",
        MediaKind.PODCAST,
        series="Радіо-Т",
        album="ignored",
        published=published,
        comment="Про нове",
    )
    episode = episode.model_copy(update={"cover": cover})
    book = _kind_item(tmp_path, "Розділ 1", MediaKind.AUDIOBOOK, position_s=42.0, author="Кінг")
    music = _ipod_item(tmp_path, "Пісня", "Скрябін")
    s = await dev.begin()
    refs = [await dev.put_track(s, i) for i in (episode, book, music)]
    from syncmusic.core.models import Manifest, ManifestEntry, ManifestHeader

    manifest = Manifest(
        header=ManifestHeader(device_id=dev.id),
        items={
            i.track.ref.key(): ManifestEntry(
                ref=i.track.ref,
                device_ref=r.device_ref,
                path=r.path,
                size=r.size,
                kind=i.track.kind,
            )
            for i, r in zip((episode, book, music), refs, strict=True)
        },
    )
    await dev.commit(s, manifest)

    db = Database(str(dev.mount))
    by_title = {t.title: t for t in db.tracks}
    ep, ch, song = by_title["Випуск 1"], by_title["Розділ 1"], by_title["Пісня"]
    assert ep.media_type & MEDIATYPE_PODCAST and ep.album == "Радіо-Т"
    assert ep.remember_position and ep.skip_when_shuffling
    # the Podcasts view's release date and description; music has none
    assert ep.time_released.timestamp() == published.timestamp() and ep.description == "Про нове"
    assert song.time_released.year < 2000
    podcasts = [p for p in db.playlists if p.is_podcast]
    assert len(podcasts) == 1 and [t.title for t in podcasts[0].tracks] == ["Випуск 1"]
    assert ch.media_type & MEDIATYPE_AUDIOBOOK and ch.remember_position
    assert ch.bookmark_time == 42000 and ch.artist == "Кінг"
    assert not song.remember_position
    # artwork only for the item with a prepared cover (the generated MP3s carry none)
    assert ep.has_artwork and not ch.has_artwork and not song.has_artwork
    images = ArtworkManager(str(dev.mount))._get_or_create_mhli().children
    assert [i.fields.get("image_id") for i in images] == [ep.mhii_link]
    kinds = {i.title: i.kind for i in (await dev.read_contents()).items}
    assert kinds == {
        "Випуск 1": MediaKind.PODCAST,
        "Розділ 1": MediaKind.AUDIOBOOK,
        "Пісня": MediaKind.MUSIC,
    }


async def test_unplayable_file_refused_and_removed(tmp_path):
    from pygpod.exceptions import UnsupportedFormatError

    from tests.media import make_music, media_work_item

    dev = _dev(tmp_path)
    flac = make_music(tmp_path / "src" / "x.flac", title="x")
    item = media_work_item(flac, tmp_path / "work")
    item = item.model_copy(
        update={
            "plan": item.plan.model_copy(
                update={"file": item.plan.file.model_copy(update={"ext": "mp3"})}
            )
        }
    )  # a broken chain claims MP3
    s = await dev.begin()
    with pytest.raises(UnsupportedFormatError, match="FLAC"):
        await dev.put_track(s, item)
    music = dev.mount / "iPod_Control" / "Music"
    assert not [p for p in music.rglob("*") if p.is_file()]
    assert s.data["db"].tracks == []


def test_format_sysinfo_gets_the_guid_when_empty_or_missing():
    """a stock restore leaves SysInfo empty; the format must still write the FirewireGuid."""
    from syncmusic.devices.iPod.ipod import with_guid

    guid = "000A11223344556F"
    assert with_guid(b"", guid) == b"FirewireGuid: 0x000A11223344556F\n"
    assert with_guid(b"ModelNumStr: xC297", guid) == (
        b"ModelNumStr: xC297\nFirewireGuid: 0x000A11223344556F\n"
    )
    kept = b"ModelNumStr: xC297\nFirewireGuid: 0x000A27001301297E\n"
    assert with_guid(kept, guid) == kept
    assert with_guid(b"", "300000000000A1B2C3D4E5F60") == b""  # not a GUID-shaped serial
    assert with_guid(b"", None) == b""


async def test_removals_batched_at_commit(tmp_path):
    """files go at once, database entries in one remove_tracks call at commit."""
    from pygpod import Database

    from tests.contracts.harness import _ipod_device, _ipod_item
    from tests.contracts.test_device_contract import _manifest

    dev = _ipod_device(tmp_path)
    s = await dev.begin()
    items = [_ipod_item(tmp_path, f"T{i}", "A") for i in range(3)]
    refs = [(it, await dev.put_track(s, it)) for it in items]
    manifest = _manifest(dev, refs)
    await dev.commit(s, manifest)

    s = await dev.begin()
    for _, r in refs[:2]:
        await dev.remove_track(s, r)
    assert not (dev.mount / refs[0][1].path).exists()
    assert len(s.data["db"].tracks) == 3  # still in memory until the commit
    for _, r in refs[:2]:
        manifest.items.pop(
            next(k for k, e in manifest.items.items() if e.device_ref == r.device_ref)
        )
    await dev.commit(s, manifest)
    db = Database(str(dev.mount), apply_play_counts=False)
    assert [str(t.dbid) for t in db.tracks] == [refs[2][1].device_ref]


async def test_database_backup_before_a_sync(tmp_path):
    """backup_device_db copies the DB before the sync writes it; backup_keep prunes."""
    from types import SimpleNamespace

    dev = _dev(tmp_path)
    await _sync(dev, tmp_path, [("One", "A")])  # backups off: nothing
    cache = tmp_path / "cache"
    assert not (cache / "device-backups").exists()
    dev.ctx = SimpleNamespace(cache_dir=cache, guard=None)
    dev.config = dev.config.model_copy(
        update={
            "sync": dev.config.sync.model_copy(update={"backup_device_db": True, "backup_keep": 2})
        }
    )
    db = dev.mount / "iPod_Control" / "iTunes" / "iTunesDB"
    before = db.read_bytes()
    await _sync(dev, tmp_path, [("Two", "A")])
    [first] = sorted((cache / "device-backups" / "ipod1").iterdir())
    assert (
        (first / "iPod_Control" / "iTunes" / "iTunesDB").read_bytes() == before != db.read_bytes()
    )
    assert (first / ".syncmusic" / "manifest.jsonl").is_file()
    for title in ("Three", "Four"):
        await _sync(dev, tmp_path, [(title, "A")])
    kept = sorted((cache / "device-backups" / "ipod1").iterdir())
    assert len(kept) == 2 and first not in kept

    # a backup that cannot be written fails the sync before the database is touched
    dev.ctx = SimpleNamespace(cache_dir=tmp_path / "file", guard=None)
    (tmp_path / "file").write_text("not a directory")
    current = db.read_bytes()
    with pytest.raises(OSError):
        await _sync(dev, tmp_path, [("Five", "A")])
    assert db.read_bytes() == current


async def test_contents_come_from_the_itunesdb(tmp_path):
    """names, tags, media types and playlists of every track come from the iTunesDB - also of a track
    another tool put there - never from the iPod_Control/Music/Fxx file name."""
    from pygpod import Database
    from pygpod.db.constants import MEDIATYPE_PODCAST

    from tests.media import make_music

    dev = _dev(tmp_path)
    src = make_music(tmp_path / "src" / "x.mp3", title="file tag title")
    db = Database(str(dev.mount))
    song = db.add_track(
        str(src),
        title="Зозуля",
        artist="Скрябін",
        albumartist="Андрій Кузьменко",
        album="Зірка на ймення Сонце",
        genre="Rock",
        year=1989,
        track_number=7,
        cd_number=1,
    )
    pod = db.add_track(str(src), title="Episode 1", album="Show", media_type=MEDIATYPE_PODCAST)
    db.add_track_to_playlist(db.create_playlist("Найкраще"), song)
    db.save()
    contents = await dev.read_contents()
    items = {i.device_ref: i for i in contents.items}
    s = items[str(song.dbid)]
    assert (
        s.title,
        s.artist,
        s.album_artist,
        s.album,
        s.genre,
        s.year,
        s.track_number,
        s.disc_number,
    ) == ("Зозуля", "Скрябін", "Андрій Кузьменко", "Зірка на ймення Сонце", "Rock", 1989, 7, 1)
    assert s.kind == MediaKind.MUSIC and s.path.startswith("iPod_Control/Music/F")
    assert s.title not in s.path
    assert (
        items[str(pod.dbid)].kind == MediaKind.PODCAST and items[str(pod.dbid)].title == "Episode 1"
    )
    assert contents.playlist_items["Найкраще"] == [str(song.dbid)]


async def test_itunesdb_entry_gets_the_files_own_tags(tmp_path):
    """the library knows only title / artist / album; the iTunesDB entry still gets album artist,
    composer, comment, totals, year, compilation, sort names and BPM from the file - the library's values on top."""
    from pygpod import Database

    from syncmusic.core.models import Manifest, ManifestHeader
    from tests.media import media_work_item
    from tests.unit.test_tagmap import _mp3

    dev = _dev(tmp_path)
    src = _mp3(tmp_path / "src" / "rich.mp3")
    item = media_work_item(src, tmp_path / "work")
    track = item.track.model_copy(
        update={
            "title": "Зозуля (library)",
            "artist": "Скрябін",
            "album": "Зірка",
            "album_artist": None,
            "genres": [],
            "year": None,
            "track_no": None,
            "disc_no": None,
            "comment": None,
            "compilation": False,
        }
    )
    item = item.model_copy(update={"plan": item.plan.model_copy(update={"track": track})})
    s = await dev.begin()
    await dev.put_track(s, item)
    await dev.commit(s, Manifest(header=ManifestHeader(device_id=dev.id)))
    t = Database(str(dev.mount)).tracks[0]
    assert (t.title, t.artist, t.album) == ("Зозуля (library)", "Скрябін", "Зірка")
    assert (t.albumartist, t.composer, t.comment, t.genre) == (
        "Скрябін",
        "Андрій Кузьменко",
        "remaster",
        "Rock",
    )
    assert (t.year, t.track_number, t.total_tracks, t.cd_number, t.total_cds) == (1989, 7, 11, 1, 2)
    assert t.compilation and t.bpm == 120
    assert (t.sort_title, t.sort_artist, t.sort_album, t.sort_albumartist) == (
        "Zozulya",
        "Skryabin",
        "Zirka",
        "Skryabin",
    )
    # a metadata update with the same gaps leaves those fields alone
    s = await dev.begin()
    from syncmusic.base.device import DeviceTrackRef

    ref = DeviceTrackRef(device_ref=str(t.dbid), path="x", size=1)
    await dev.update_metadata(s, ref, track.model_copy(update={"title": "Нове"}))
    await dev.commit(s, Manifest(header=ManifestHeader(device_id=dev.id)))
    t = Database(str(dev.mount)).tracks[0]
    assert (
        t.title == "Нове"
        and t.albumartist == "Скрябін"
        and t.comment == "remaster"
        and t.year == 1989
    )


async def test_a_checkpoint_commit_keeps_the_session_open(tmp_path):
    """the applier commits mid-session; tracks added and removed after it reach the next commit, and a
    session lost after the checkpoint would leave the checkpoint's tracks in the database."""
    from pygpod import Database

    from tests.contracts.harness import _ipod_device, _ipod_item
    from tests.contracts.test_device_contract import _manifest

    dev = _ipod_device(tmp_path)
    s = await dev.begin()
    first = [
        (it, await dev.put_track(s, it))
        for it in (_ipod_item(tmp_path, f"T{i}", "A") for i in range(2))
    ]
    await dev.commit(s, _manifest(dev, first))  # the checkpoint
    more = [
        (it, await dev.put_track(s, it))
        for it in (_ipod_item(tmp_path, f"U{i}", "A") for i in range(2))
    ]
    await dev.remove_track(s, first[0][1])

    def dbids():
        return sorted(str(t.dbid) for t in Database(str(dev.mount), apply_play_counts=False).tracks)

    assert dbids() == sorted(
        r.device_ref for _, r in first
    )  # not yet: only the checkpoint is on disk

    await dev.commit(s, _manifest(dev, first[1:] + more))
    assert dbids() == sorted(r.device_ref for _, r in first[1:] + more)
    assert all((dev.mount / r.path).is_file() for _, r in first[1:] + more)
    assert manifest_io.read_inflight(dev.mount) == []


@pytest.mark.parametrize("desc", ["replaygain_track_gain", "REPLAYGAIN_TRACK_GAIN"])
async def test_sound_check_from_the_written_gain(tmp_path, desc):
    """the gain the retag processor wrote into the copy becomes the track's Sound Check."""
    from mutagen.id3 import ID3, TXXX
    from pygpod import Database
    from pygpod.tags import soundcheck_from_gain

    from syncmusic.core.models import Manifest, ManifestHeader
    from tests.media import media_work_item
    from tests.unit.test_tagmap import _mp3

    dev = _dev(tmp_path)
    src = _mp3(tmp_path / "src" / "rg.mp3")
    tags = ID3(src)
    for key in (
        "TXXX:REPLAYGAIN_TRACK_GAIN",
        "TXXX:replaygain_track_gain",
    ):  # the fixture has -7.20 dB
        tags.delall(key)
    tags.add(TXXX(encoding=3, desc=desc, text=["-6.18 dB"]))
    tags.save(src)
    s = await dev.begin()
    await dev.put_track(s, media_work_item(src, tmp_path / "work"))
    await dev.commit(s, Manifest(header=ManifestHeader(device_id=dev.id)))
    assert Database(str(dev.mount)).tracks[0].soundcheck == soundcheck_from_gain(-6.18) > 1000


async def test_an_episode_on_the_device_gets_its_release_date_with_an_update(tmp_path):
    """an episode synced before has no release date; its metadata update writes it."""
    from pygpod import Database

    from syncmusic.core.models import Manifest, ManifestEntry, ManifestHeader

    dev = _dev(tmp_path)
    episode = _kind_item(tmp_path, "Випуск 1", MediaKind.PODCAST, series="Радіо-Т")
    s = await dev.begin()
    ref = await dev.put_track(s, episode)
    key = episode.track.ref.key()
    entry = ManifestEntry(
        ref=episode.track.ref, device_ref=ref.device_ref, path=ref.path, size=ref.size
    )
    manifest = Manifest(header=ManifestHeader(device_id=dev.id), items={key: entry})
    await dev.commit(s, manifest)
    published = dt.datetime(2024, 3, 5, 12, 0, tzinfo=dt.UTC)
    s = await dev.begin()
    await dev.update_metadata(s, ref, episode.track.model_copy(update={"published": published}))
    await dev.commit(s, manifest)
    t = next(t for t in Database(str(dev.mount)).tracks if str(t.dbid) == ref.device_ref)
    assert t.time_released.timestamp() == published.timestamp()


async def test_fetch_track_copies_the_file_off_the_ipod(tmp_path):
    """a tags-only re-process starts from the iPod's own file."""
    dev = _dev(tmp_path)
    _, refs = await _sync(dev, tmp_path, [("One", "A")])
    ref = refs[0][1]
    dest = tmp_path / "fetched.mp3"
    assert await dev.fetch_track(ref, dest)
    assert dest.read_bytes() == (dev.mount / ref.path).read_bytes()
    assert not await dev.fetch_track(
        ref.model_copy(update={"path": "iPod_Control/Music/F00/X.mp3"}), dest
    )


async def test_tags_only_reprocess_patches_the_ipod_file_in_place(tmp_path):
    """same-size retag -> the file is patched in place, the DB entry kept and updated."""
    dev = _dev(tmp_path)
    manifest, refs = await _sync(dev, tmp_path, [("One", "A")])
    item, ref = refs[0]
    on_device = dev.mount / ref.path
    before = on_device.read_bytes()
    retagged = tmp_path / "retagged.mp3"
    data = bytearray(before)
    data[-8:] = b"LYRICS!!"  # a same-size tag change near the end
    retagged.write_bytes(bytes(data))
    track = item.track.model_copy(update={"title": "One (remaster)"})
    work = item.model_copy(
        update={"path": retagged, "plan": item.plan.model_copy(update={"track": track})}
    )

    s = await dev.begin()
    patched = await dev.rewrite_tags(s, ref, work)
    assert patched is not None and patched.device_ref == ref.device_ref
    assert patched.path == ref.path and on_device.read_bytes() == bytes(data)
    await dev.commit(s, manifest)
    from pygpod import Database

    t = next(t for t in Database(str(dev.mount)).tracks if str(t.dbid) == ref.device_ref)
    assert t.title == "One (remaster)"
    assert not list((dev.mount / "iPod_Control" / "Music").rglob(".sdtmp-*"))

    # a size change, or a cover for the ArtworkDB: the applier replaces the file instead
    s = await dev.begin()
    retagged.write_bytes(bytes(data) + b"x")
    assert await dev.rewrite_tags(s, ref, work) is None
    retagged.write_bytes(bytes(data))
    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"\xff\xd8jpeg")  # undecodable: the old cover stays, the applier replaces
    assert await dev.rewrite_tags(s, ref, work.model_copy(update={"cover": cover})) is None
    await dev.abort(s)


async def test_tags_only_reprocess_replaces_the_cover_in_place(tmp_path):
    """with a cover the file is still patched in place, the thumbnails replaced (no orphan)."""
    from pygpod import Database
    from pygpod.model.artwork_manager import ArtworkManager

    from tests.media import big_cover

    dev = _dev(tmp_path)
    manifest, refs = await _sync(dev, tmp_path, [("One", "A")])
    item, ref = refs[0]
    on_device = dev.mount / ref.path
    data = bytearray(on_device.read_bytes())
    data[-8:] = b"LYRICS!!"
    retagged = tmp_path / "retagged.mp3"
    retagged.write_bytes(bytes(data))
    for size in (600, 300):  # twice: the second replaces the first
        cover = tmp_path / f"cover{size}.jpg"
        cover.write_bytes(big_cover(size, size))
        work = item.model_copy(update={"path": retagged, "cover": cover})
        s = await dev.begin()
        patched = await dev.rewrite_tags(s, ref, work)
        assert patched is not None and patched.device_ref == ref.device_ref
        await dev.commit(s, manifest)
    assert on_device.read_bytes() == bytes(data)
    t = next(t for t in Database(str(dev.mount)).tracks if str(t.dbid) == ref.device_ref)
    assert t.has_artwork
    images = ArtworkManager(str(dev.mount))._get_or_create_mhli().children
    assert [i.fields.get("image_id") for i in images] == [t.mhii_link]


async def test_interrupted_ipod_patch_is_rolled_back_at_begin(tmp_path):
    from syncmusic.devices._filedevice import PATCH_PATH, patch_file

    dev = _dev(tmp_path)
    _, refs = await _sync(dev, tmp_path, [("One", "A")])
    ref = refs[0][1]
    on_device = dev.mount / ref.path
    before = on_device.read_bytes()
    new = tmp_path / "new.mp3"
    new.write_bytes(before[:-4] + b"ZZZZ")
    patch_file(dev.mount, ref.path, new, len(before) - 4, len(before))
    # simulate a crash after the write: put the journal back
    import base64
    import json

    (dev.mount / PATCH_PATH).write_text(
        json.dumps(
            {
                "path": ref.path,
                "offset": len(before) - 4,
                "data": base64.b64encode(before[-4:]).decode(),
            }
        )
    )
    s = await dev.begin()
    assert on_device.read_bytes() == before and not (dev.mount / PATCH_PATH).exists()
    await dev.abort(s)
