"""USB flash drive + file-device mixin."""

from __future__ import annotations

import os

import pytest

from syncmusic.core import manifest as mf
from syncmusic.core.models import (
    DeviceConfig,
    DevicePlaylist,
    DeviceTrackRef,
    ItemRef,
    Manifest,
    ManifestEntry,
    ManifestHeader,
)
from syncmusic.devices._filedevice import FileTooLarge, fstype_of
from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.contracts.harness import work_item
from tests.media import make_music


def _dev(tmp_path, **settings) -> FlashDrive:
    mount = tmp_path / "flash"
    mount.mkdir(exist_ok=True)
    return FlashDrive(
        DeviceConfig(id="f1", type="flashdrive", name="F", settings=settings), mount, None
    )


def _item(tmp_path, title, artist="Скрябін", album="Група крові", **sidecars):
    item = work_item(tmp_path, title, artist=artist)
    t = item.track.model_copy(update={"album": album})
    files = {}
    for key, data in sidecars.items():
        p = tmp_path / "work" / f"{title}-{key}"
        p.write_bytes(data)
        files[{"lrc": ".lrc", "cover": "cover.jpg"}[key]] = p
    return item.model_copy(
        update={"plan": item.plan.model_copy(update={"track": t}), "sidecars": files}
    )


def _manifest(dev, pairs) -> Manifest:
    return Manifest(
        header=ManifestHeader(device_id=dev.id),
        items={
            i.track.ref.key(): ManifestEntry(
                ref=i.track.ref,
                device_ref=r.device_ref,
                path=r.path,
                size=r.size,
                sidecars=r.sidecars,
            )
            for i, r in pairs
        },
    )


async def test_sidecars_and_folder_cover(tmp_path):
    dev = _dev(tmp_path)
    s = await dev.begin()
    a = _item(tmp_path, "Зірка", lrc=b"[00:01.00]x", cover=b"jpeg")
    b = _item(tmp_path, "Зозуля", cover=b"jpeg")
    ra = await dev.put_track(s, a)
    rb = await dev.put_track(s, b)
    assert ra.path == "Music/Скрябін/Група крові/Зірка.mp3"
    folder = dev.mount / "Music/Скрябін/Група крові"
    assert (folder / "Зірка.lrc").read_bytes() == b"[00:01.00]x"
    assert (folder / "cover.jpg").read_bytes() == b"jpeg"
    assert set(ra.sidecars) == {
        "Music/Скрябін/Група крові/Зірка.lrc",
        "Music/Скрябін/Група крові/cover.jpg",
    }
    await dev.commit(s, _manifest(dev, [(a, ra), (b, rb)]))
    assert not [p for p in dev.mount.rglob("*") if p.name.startswith(".sdtmp")]
    assert not (dev.mount / mf.INFLIGHT_PATH).exists()

    s = await dev.begin()
    await dev.remove_track(s, ra)
    assert not (folder / "Зірка.lrc").exists() and not (folder / "Зірка.mp3").exists()
    assert (folder / "cover.jpg").exists()  # still used by Зозуля
    await dev.remove_track(s, rb)
    assert not (dev.mount / "Music").exists()  # cover and empty folders gone


async def test_unmanaged_file_never_overwritten(tmp_path):
    dev = _dev(tmp_path)
    own = dev.mount / "Music/Скрябін/Група крові/Зірка.mp3"
    own.parent.mkdir(parents=True)
    own.write_bytes(b"user's file")
    s = await dev.begin()
    r = await dev.put_track(s, _item(tmp_path, "Зірка"))
    assert r.path == "Music/Скрябін/Група крові/Зірка (2).mp3"
    assert own.read_bytes() == b"user's file"


async def test_identical_unmanaged_file_taken_over(tmp_path):
    """a byte-identical file at the target path (moved there by hand) is taken over, not duplicated as
    ' (2)'; an existing sidecar is left alone, a missing one is written."""
    dev = _dev(tmp_path)
    item = _item(tmp_path, "Зірка", lrc=b"[00:01]x", cover=b"jpg")
    own = dev.mount / "Music/Скрябін/Група крові/Зірка.mp3"
    own.parent.mkdir(parents=True)
    own.write_bytes(item.path.read_bytes())
    (own.parent / "cover.jpg").write_bytes(b"user's cover")
    s = await dev.begin()
    r = await dev.put_track(s, item)
    assert r.path == "Music/Скрябін/Група крові/Зірка.mp3"
    assert sorted(p.name for p in own.parent.iterdir()) == ["cover.jpg", "Зірка.lrc", "Зірка.mp3"]
    assert (own.parent / "cover.jpg").read_bytes() == b"user's cover"
    assert r.sidecars == ["Music/Скрябін/Група крові/Зірка.lrc"]


async def test_replace_reuses_own_path(tmp_path):
    dev = _dev(tmp_path)
    s = await dev.begin()
    item = _item(tmp_path, "Зірка")
    r = await dev.put_track(s, item)
    await dev.commit(s, _manifest(dev, [(item, r)]))
    s = await dev.begin()
    r2 = await dev.put_track(s, item)
    assert r2.path == r.path


async def test_playlists_m3u8_relative(tmp_path):
    dev = _dev(tmp_path, filesystem="vfat")
    s = await dev.begin()
    a, b = _item(tmp_path, "Зірка"), _item(tmp_path, "Зозуля", artist="Кузьменко")
    ra, rb = await dev.put_track(s, a), await dev.put_track(s, b)
    missing = ItemRef(library_id="lib", item_id="nope")
    await dev.set_playlists(
        s, [DevicePlaylist(name="Мій: топ", refs=[b.track.ref, missing, a.track.ref])]
    )
    m = _manifest(dev, [(a, ra), (b, rb)])
    m.playlists = ["Мій: топ"]
    await dev.commit(s, m)
    text = (dev.mount / "Playlists" / "Мій - топ.m3u8").read_text(encoding="utf-8")
    assert text.splitlines() == ["#EXTM3U", f"../{rb.path}", f"../{ra.path}"]
    assert (dev.mount / "Playlists" / f"../{ra.path}").resolve().is_file()
    assert (await dev.read_contents()).playlists == ["Мій - топ"]
    s = await dev.begin()
    await dev.set_playlists(s, [])
    assert not (dev.mount / "Playlists" / "Мій - топ.m3u8").exists()


async def test_fat_file_size_limit(tmp_path):
    dev = _dev(tmp_path, filesystem="vfat")
    item = _item(tmp_path, "Big")
    os.truncate(item.path, 4 * 1024**3)  # sparse: 4 GiB > FAT32's 4 GiB - 1
    s = await dev.begin()
    with pytest.raises(FileTooLarge):
        await dev.put_track(s, item)
    assert not (dev.mount / "Music").exists()


async def test_vfat_rules_from_setting(tmp_path):
    dev = _dev(tmp_path, filesystem="vfat", formats=["mp3"])
    caps = dev.effective_capabilities()
    assert caps.path_rules.max_file_size == 4 * 1024**3 - 1 and caps.formats == {"mp3"}
    assert fstype_of(tmp_path) is not None


async def test_interrupted_sync_cleaned_on_next_begin(tmp_path):
    dev = _dev(tmp_path)
    s = await dev.begin()
    keep = _item(tmp_path, "Keep")
    rk = await dev.put_track(s, keep)
    await dev.commit(s, _manifest(dev, [(keep, rk)]))
    s = await dev.begin()
    r = await dev.put_track(s, _item(tmp_path, "Crash", lrc=b"x"))  # then the process dies
    (dev.mount / "Music/Скрябін/Група крові/.sdtmp-dead").write_bytes(b"half")
    again = _dev(tmp_path)
    s2 = await again.begin()
    assert set(s2.data["inflight_removed"]) == {r.path, *r.sidecars}
    assert not (dev.mount / r.path).exists() and (dev.mount / rk.path).exists()
    assert not (dev.mount / "Music/Скрябін/Група крові/.sdtmp-dead").exists()


async def test_abort_removes_session_files(tmp_path):
    dev = _dev(tmp_path)
    s = await dev.begin()
    r = await dev.put_track(s, _item(tmp_path, "Gone"))
    await dev.abort(s)
    assert not (dev.mount / r.path).exists()


async def test_read_contents_walk(tmp_path):
    dev = _dev(tmp_path)
    make_music(dev.mount / "Music" / "x.mp3", title="Пісня", artist="Скрябін", album="A")
    make_music(dev.mount / "Podcasts" / "Show" / "ep.mp3", title="Ep")
    (dev.mount / "Music" / "notes.txt").write_text("x")
    (dev.mount / "Music" / "broken.flac").write_bytes(b"garbage")
    (dev.mount / ".Trashes").mkdir()
    make_music(dev.mount / ".Trashes" / "old.mp3")
    (dev.mount / mf.STATE_DIR).mkdir()
    items = {i.path: i for i in (await dev.read_contents()).items}
    assert set(items) == {"Music/x.mp3", "Podcasts/Show/ep.mp3", "Music/broken.flac"}
    assert items["Music/x.mp3"].title == "Пісня" and items["Music/x.mp3"].artist == "Скрябін"
    assert items["Music/x.mp3"].duration_s and items["Podcasts/Show/ep.mp3"].kind == "podcast"
    assert items["Music/broken.flac"].title == "broken"


async def test_read_contents_tags_playlists_and_tag_cache(tmp_path, monkeypatch):
    """the extra tags, playlist members from Playlists/*.m3u8|m3u (relative, absolute, missing and
    outside entries), and mutagen runs again only for a changed file."""
    from syncmusic.devices import _filedevice

    dev = _dev(tmp_path)
    make_music(
        dev.mount / "Music" / "a.mp3",
        title="A",
        artist="Скрябін",
        albumartist="Андрій Кузьменко",
        album="Зірка",
        genre="Rock",
        date="1989-05-01",
        tracknumber="7/11",
        discnumber="2/2",
    )
    make_music(dev.mount / "Music" / "b.mp3", title="B")
    (dev.mount / "Playlists").mkdir()
    (dev.mount / "Playlists" / "Мій топ.m3u8").write_text(
        "\ufeff#EXTM3U\n../Music/b.mp3\n#EXTINF:1,x\n/Music/a.mp3\n../Music/gone.mp3\n../../etc/passwd\n",
        encoding="utf-8",
    )
    (dev.mount / "Playlists" / "old.m3u").write_text("..\\Music\\a.mp3\n", encoding="utf-8")
    calls: list[str] = []
    real = _filedevice.read_tags
    monkeypatch.setattr(_filedevice, "read_tags", lambda f: calls.append(f.name) or real(f))
    contents = await dev.read_contents()
    a = next(i for i in contents.items if i.path == "Music/a.mp3")
    assert (a.album_artist, a.genre, a.year, a.track_number, a.disc_number) == (
        "Андрій Кузьменко",
        "Rock",
        1989,
        7,
        2,
    )
    assert contents.playlists == ["old", "Мій топ"]
    assert contents.playlist_items == {
        "Мій топ": ["Music/b.mp3", "Music/a.mp3"],
        "old": ["Music/a.mp3"],
    }
    assert sorted(calls) == ["a.mp3", "b.mp3"]
    calls.clear()
    await dev.read_contents()
    assert calls == []
    make_music(dev.mount / "Music" / "b.mp3", title="B2", seconds=2.0)
    assert {i.title for i in (await dev.read_contents()).items} == {"A", "B2"} and calls == [
        "b.mp3"
    ]


async def test_free_space_and_capacity(tmp_path):
    dev = _dev(tmp_path)
    assert 0 < await dev.free_space() <= await dev.capacity()


def _blk(**kw) -> BlockDeviceInfo:
    part = BlockDeviceInfo(
        name="sda1", path="/dev/sda1", type="part", fstype="vfat", uuid="AB-CD", label="ZORVEX"
    )
    base = dict(
        name="sda",
        path="/dev/sda",
        type="disk",
        tran="usb",
        vendor="Zorvex",
        model="DT",
        serial="S1",
        children=[part],
    )
    return BlockDeviceInfo(**{**base, **kw})


def test_probe_block_device():
    r = FlashDrive.probe(_blk(), None)
    assert r is not None and r.confidence == 10 and r.data_partition == "/dev/sda1"
    assert (
        r.identity.usb_serial == "S1"
        and r.identity.fs_uuid == "AB-CD"
        and r.suggested_name == "ZORVEX"
    )
    assert FlashDrive.probe(_blk(tran="sata"), None) is None
    assert FlashDrive.probe(_blk(type="part"), None) is None


def test_probe_refuses_ipods():
    """Apple disks (Classic 05ac:1261, Rockbox mini 05ac:1205) are never offered as flash drives."""
    for pid in ("1261", "1205"):
        assert FlashDrive.probe(_blk(usb_vendor_id="05ac", usb_product_id=pid), None) is None
    assert FlashDrive.probe(_blk(usb_vendor_id="05AC"), None) is None
    assert FlashDrive.probe(_blk(usb_vendor_id="0951", usb_product_id="1666"), None) is not None


def test_probe_skips_other_device_trees(tmp_path):
    (tmp_path / ".rockbox").mkdir()
    (tmp_path / ".syncmusic").mkdir()
    assert FlashDrive.probe(None, tmp_path) is None
    assert FlashDrive.probe(_blk(), tmp_path) is None


def test_remove_outside_root_ignored(tmp_path):
    import asyncio

    dev = _dev(tmp_path)
    victim = tmp_path / "victim.mp3"
    victim.write_bytes(b"x")

    async def go():
        s = await dev.begin()
        await dev.remove_track(
            s,
            DeviceTrackRef(
                device_ref="x", path="../victim.mp3", size=1, sidecars=["../victim.mp3"]
            ),
        )

    asyncio.run(go())
    assert victim.exists()


async def test_car_mode_ascii_short_paths(tmp_path):
    """car stereo / old player mode - ASCII names (Cyrillic transliterated, emoji replaced), each
    name within max_name_len and the whole path within max_path_len; off: the names stay as they are."""
    title = "Хочу змін 🎸 " + "дуже довга назва " * 8
    plain = _dev(tmp_path, filesystem="vfat").path_rules()
    assert not plain.ascii_only and plain.max_path_len == 240
    dev = _dev(tmp_path, filesystem="vfat", car_mode=True, max_path_len=100, max_name_len=40)
    rules = dev.effective_capabilities().path_rules
    assert rules.ascii_only and rules.max_path_len == 100 and rules.max_name_len == 40
    assert rules.max_file_size == 4 * 1024**3 - 1  # the file system's own limits stay
    s = await dev.begin()
    res = await dev.put_track(s, _item(tmp_path, title))
    assert res.path.isascii() and "🎸" not in res.path, res.path
    assert len(res.path) <= 100 and all(len(p) <= 40 for p in res.path.split("/")), res.path
    assert "Skryabin" in res.path and "Khochu" in res.path, res.path
    assert (dev.mount / res.path).is_file()
    # the defaults when only the switch is on: 128 / 64
    rules = _dev(tmp_path, car_mode=True).path_rules()
    assert (rules.max_path_len, rules.max_name_len) == (128, 64)


async def test_car_mode_folder_limit(tmp_path):
    """in car mode a folder takes at most `max_files_per_folder` entries (sidecars count); the
    next files go to "<album> (2)"."""
    dev = _dev(tmp_path, filesystem="vfat", car_mode=True, max_files_per_folder=10)
    s = await dev.begin()
    out = [await dev.put_track(s, _item(tmp_path, f"Track {n:02}")) for n in range(14)]
    folders = {}
    for r in out:
        folder = r.path.rsplit("/", 1)[0]
        folders[folder] = folders.get(folder, 0) + 1
    assert sorted(folders.values()) == [4, 10], folders
    assert any(f.endswith(" (2)") for f in folders), folders
    for f in folders:
        assert len(list((dev.mount / f).iterdir())) <= 10


async def test_car_mode_playlists_m3u_crlf(tmp_path):
    """car mode writes plain .m3u with CRLF and ASCII names; switched off, the next sync replaces it
    with the .m3u8."""
    dev = _dev(tmp_path, filesystem="vfat", car_mode=True)
    s = await dev.begin()
    a = _item(tmp_path, "Зірка")
    ra = await dev.put_track(s, a)
    await dev.set_playlists(s, [DevicePlaylist(name="Мій топ", refs=[a.track.ref])])
    m = _manifest(dev, [(a, ra)])
    m.playlists = ["Мій топ"]
    await dev.commit(s, m)
    (pl,) = (dev.mount / "Playlists").iterdir()
    assert pl.suffix == ".m3u" and pl.name.isascii(), pl.name
    assert pl.read_bytes() == f"#EXTM3U\r\n../{ra.path}\r\n".encode()
    assert (await dev.read_contents()).playlists == [pl.stem]
    plain = _dev(tmp_path, filesystem="vfat")
    s = await plain.begin()
    s.data["paths"][a.track.ref.key()] = ra.path
    await plain.set_playlists(s, [DevicePlaylist(name="Мій топ", refs=[a.track.ref])])
    assert [p.name for p in (dev.mount / "Playlists").iterdir()] == ["Мій топ.m3u8"]


async def test_put_track_cost_does_not_grow_with_written_tracks(tmp_path, monkeypatch):
    """the session's own path keys are built once, not once per written track (a first sync of a big
    library was quadratic): path_key calls stay linear in the tracks written."""
    from syncmusic.devices import paths

    dev = _dev(tmp_path)
    s = await dev.begin()
    items = [_item(tmp_path, f"Пісня {i}") for i in range(60)]
    calls = 0
    real = paths.path_key

    def counting(rel, rules):
        nonlocal calls
        calls += 1
        return real(rel, rules)

    monkeypatch.setattr(paths, "path_key", counting)
    for item in items:
        await dev.put_track(s, item)
    assert calls < 10 * len(items)  # quadratic: ~len(items)**2 / 2 = 1800


async def test_moved_own_track_frees_its_old_path(tmp_path):
    """a track written again under another path no longer owns its old one: an identical foreign file
    there is taken over, as before the own-key set was kept per session."""
    dev = _dev(tmp_path)
    a = _item(tmp_path, "Зірка")
    s = await dev.begin()
    ra = await dev.put_track(s, a)
    moved = a.model_copy(
        update={
            "plan": a.plan.model_copy(
                update={"track": a.track.model_copy(update={"album": "Інший"})}
            )
        }
    )
    rb = await dev.put_track(s, moved)
    assert rb.path != ra.path and s.data["ours"] == {k for k in s.data["ours"] if "Інший" in k}


async def test_capabilities_resolved_once_per_session(tmp_path, monkeypatch):
    """writing tracks does not recompute the device capabilities per track; a new session sees changed settings."""
    dev = _dev(tmp_path)
    s = await dev.begin()
    calls = 0
    real = dev.effective_capabilities

    def counting():
        nonlocal calls
        calls += 1
        return real()

    monkeypatch.setattr(dev, "effective_capabilities", counting)
    for i in range(5):
        await dev.put_track(s, _item(tmp_path, f"Пісня {i}"))
    assert calls == 0
    await dev.abort(s)
    monkeypatch.undo()
    car = _dev(tmp_path, car_mode=True)
    s2 = await car.begin()
    ref = await car.put_track(s2, _item(tmp_path, "Ґанок"))
    assert ref.path.isascii()


def test_a_new_disk_has_no_format_limit(tmp_path):
    assert _dev(tmp_path).effective_capabilities().formats == set()
    assert _dev(tmp_path, purpose="backup").effective_capabilities().formats == set()


def test_stored_formats_still_limit(tmp_path):
    caps = _dev(tmp_path, formats=["mp3", "aac"]).effective_capabilities()
    assert caps.formats == {"mp3", "aac"}


def test_purpose_is_player_or_backup(tmp_path):
    assert _dev(tmp_path).s.purpose == "player"
    with pytest.raises(ValueError):
        _dev(tmp_path, purpose="radio")
