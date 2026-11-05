"""Rockbox device: probe, capabilities, playlists, wipe."""

from __future__ import annotations

import shutil

import pytest

from syncmusic.core.models import DeviceConfig, DevicePlaylist
from syncmusic.core.sync import suggested_processors
from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.devices.iPod.ipod import IPod
from syncmusic.devices.Rockbox.rockbox import Rockbox, installed_formats
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.contracts.harness import ipod_tree, rockbox_tree, work_item
from tests.contracts.test_device_contract import _sync


def _dev(mount, **settings):
    cfg = DeviceConfig(id="rb", type="rockbox", name="RB", settings=settings)
    return Rockbox(cfg, mount, None)


def test_probe_wins_on_rockbox_ipod(tmp_path):
    mount = rockbox_tree(ipod_tree(tmp_path / "mini"))  # Rockbox installed on an iPod
    apple = BlockDeviceInfo(
        name="sdz", path="/dev/sdz", tran="usb", type="disk", serial="S", usb_vendor_id="05ac"
    )
    rb = Rockbox.probe(apple, mount)
    assert rb is not None and rb.model == "ipodmini2g" and rb.details["version"] == "4.0"
    stock = IPod.probe(apple, mount)  # dual boot: offered after Rockbox
    assert (
        stock is not None
        and stock.confidence < rb.confidence
        and stock.details == {"rockbox": True}
    )
    assert FlashDrive.probe(apple, mount) is None
    # not mounted: stock iPod first, Rockbox offered below it (unknown until mounted)
    ipod = IPod.probe(apple, None)
    unmounted = Rockbox.probe(apple, None)
    assert ipod is not None and ipod.confidence < rb.confidence
    assert unmounted is not None and unmounted.confidence < ipod.confidence


def test_probe_unmounted_apple_disk_identity():
    fw = BlockDeviceInfo(name="sdz1", path="/dev/sdz1", type="part", size=40_000_000)
    data = BlockDeviceInfo(
        name="sdz2",
        path="/dev/sdz2",
        type="part",
        fstype="vfat",
        uuid="C5D6-E7F8",
        label="IPOD",
        size=255_000_000_000,
    )
    disk = BlockDeviceInfo(
        name="sdz",
        path="/dev/sdz",
        tran="usb",
        type="disk",
        serial="300000000000A1B2C3D4E5F60",
        usb_vendor_id="05ac",
        children=[fw, data],
    )
    r = Rockbox.probe(disk, None)
    assert r is not None and r.data_partition == "/dev/sdz2" and r.suggested_name == "IPOD"
    assert r.identity.usb_serial == disk.serial and r.identity.fs_uuid == "C5D6-E7F8"
    other = disk.model_copy(update={"usb_vendor_id": "0951"})
    assert Rockbox.probe(other, None) is None


def test_capabilities(tmp_path):
    dev = _dev(rockbox_tree(tmp_path / "rb"))
    caps = dev.effective_capabilities()
    assert caps.has_rtc is True  # the mini 2G's Features list `rtc`
    assert not caps.supports_podcasts and not caps.supports_audiobooks
    assert caps.supports_playlists and caps.lyrics == "lrc_sidecar"
    assert caps.supports_ratings_read and caps.supports_playcounts_read
    assert not caps.supports_ratings_write and not caps.supports_positions_read
    assert not caps.supports_time_sync
    assert caps.cover is not None and caps.cover.sidecar_name == "cover.jpg"
    assert not caps.cover.progressive and not caps.cover.embed
    assert {"mp3", "flac", "ogg", "opus", "aac", "alac"} <= caps.formats  # no codecs dir: default


def test_rtc_and_formats_from_the_device(tmp_path):
    mount = tmp_path / "rb"
    (mount / ".rockbox" / "codecs").mkdir(parents=True)
    (mount / ".rockbox" / "rockbox-info.txt").write_text("Target: sansae200\nFeatures: a:b\n")
    for codec in ("mpa", "vorbis", "flac", "spc", "wavpack"):
        (mount / ".rockbox" / "codecs" / f"{codec}.codec").write_bytes(b"")
    assert installed_formats(mount) == {"mp3", "ogg", "flac", "wv"}
    caps = _dev(mount).effective_capabilities()
    assert caps.has_rtc is False and caps.formats == {"mp3", "ogg", "flac", "wv"}
    assert _dev(mount, formats=["mp3"]).effective_capabilities().formats == {"mp3"}
    (mount / ".rockbox" / "rockbox-info.txt").unlink()
    assert _dev(mount).effective_capabilities().has_rtc is None


def test_suggested_processors(tmp_path):
    slots = {
        s.type: s.settings
        for s in suggested_processors(_dev(rockbox_tree(tmp_path / "rb")).effective_capabilities())
    }
    assert set(slots) == {"transcode", "retag", "covers", "lyrics"}
    rule = slots["transcode"]["rules"][0]
    assert rule["to"] == "mp3" and "flac" in rule["from"] and "mp3" not in rule["from"]
    assert slots["covers"] == {"mode": "sidecar", "sidecar_name": "cover.jpg"}


async def test_transcode_suggestion_converts_lossless_only(tmp_path):
    from syncmusic.processors.transcode import Transcode

    dev = _dev(rockbox_tree(tmp_path / "rb"))
    caps = dev.effective_capabilities()
    slot = Transcode.suggest(caps)
    assert slot is not None
    t = Transcode(slot.settings, None)
    flac = t.decide("flac", 44100, 16, caps)
    assert flac is not None and flac.to == "mp3"
    assert t.decide("mp3", 44100, None, caps) is None
    assert t.decide("ogg", 44100, None, caps) is None


async def test_m3u8_playlists(tmp_path):
    dev = _dev(rockbox_tree(tmp_path / "rb"))
    manifest, refs = await _sync_items(dev, tmp_path)
    s = await dev.begin()
    await dev.set_playlists(
        s, [DevicePlaylist(name="Улюблене", refs=[r[0].track.ref for r in refs])]
    )
    await dev.commit(s, manifest.model_copy(update={"playlists": ["Улюблене"]}))
    text = (dev.mount / "Playlists" / "Улюблене.m3u8").read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U\n") and f"../{refs[0][1].path}" in text


async def _sync_items(dev, tmp_path):
    dev._harness = type(
        "H", (), {"make_item": staticmethod(lambda t, a, b: work_item(t, a, artist=b))}
    )
    return await _sync(dev, tmp_path, [("Група крові", "Скрябін"), ("Орбіт", "Тартак")])


async def test_wipe_keeps_rockbox_and_logs(tmp_path):
    dev = _dev(rockbox_tree(tmp_path / "rb"))
    await _sync_items(dev, tmp_path)
    (dev.mount / ".scrobbler.log").write_text("#AUDIOSCROBBLER/1.1\n")
    (dev.mount / "Other").mkdir()
    await dev.wipe()
    names = sorted(p.name for p in dev.mount.iterdir())
    assert names == [".rockbox", ".scrobbler.log", ".syncmusic"]
    assert (dev.mount / ".rockbox" / "rockbox-info.txt").is_file()
    assert await dev.read_manifest() is None


def test_format_backup_drops_database_and_play_logs(tmp_path):
    """Format erases Rockbox's tag database and play logs too; firmware, codecs and settings stay."""

    from syncmusic.devices.Rockbox.rockbox import FORMAT_DROP

    rb = tmp_path / "src" / ".rockbox"
    (rb / "codecs").mkdir(parents=True)
    for name in (
        "rockbox.ipod",
        "config.cfg",
        "codecs/mpa.codec",
        "database_idx.tcd",
        "database_0.tcd",
        "playback.log",
        "playback_0001.log",
        "playback.log.syncmusic-20261022T100000",
    ):
        (rb / name).write_text("x")
    shutil.copytree(rb, tmp_path / "bak", ignore=shutil.ignore_patterns(*FORMAT_DROP))
    kept = sorted(
        str(p.relative_to(tmp_path / "bak")) for p in (tmp_path / "bak").rglob("*") if p.is_file()
    )
    assert kept == ["codecs/mpa.codec", "config.cfg", "rockbox.ipod"]


async def test_playlist_folder_setting_moves_the_playlists(tmp_path):
    """the playlists go to the folder of the setting `playlist_dir`, with entries relative to it; when
    the setting changes, the next sync writes them there and removes the old files and their empty folder."""
    from syncmusic.core import manifest as manifest_io

    dev = _dev(rockbox_tree(tmp_path / "rb"))
    manifest, refs = await _sync_items(dev, tmp_path)
    pl = [DevicePlaylist(name="Улюблене", refs=[r[0].track.ref for r in refs])]
    for folder in ("Playlists", "Lists/Mine", "Playlists"):
        dev.settings = dev.settings.model_copy(update={"playlist_dir": folder})
        s = await dev.begin()
        await dev.set_playlists(s, pl)
        await dev.commit(s, s.manifest.model_copy(update={"playlists": ["Улюблене"]}))
        f = dev.mount / folder / "Улюблене.m3u8"
        lines = f.read_text(encoding="utf-8").splitlines()[1:]
        assert [(f.parent / x).resolve() for x in lines] == [
            (dev.mount / r[1].path).resolve() for r in refs
        ]
        assert manifest_io.read(dev.mount).playlist_paths == {"Улюблене": f"{folder}/Улюблене.m3u8"}
        playlists = sorted(p.relative_to(dev.mount).as_posix() for p in dev.mount.rglob("*.m3u8"))
        assert playlists == [f"{folder}/Улюблене.m3u8"]
        assert (await dev.read_contents()).playlists == ["Улюблене"]
    assert not (dev.mount / "Lists").exists()  # the empty old folder went too


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("Playlists", "Playlists"),
        ("/Music/Lists/", "Music/Lists"),
        ("..", None),
        ("a/../b", None),
        (".rockbox", None),
        ("", None),
        ("  ", None),
    ],
)
def test_playlist_folder_validation(value, ok):
    from syncmusic.devices.Rockbox.rockbox import RockboxSettings

    if ok is None:
        with pytest.raises(ValueError):
            RockboxSettings(playlist_dir=value)
    else:
        assert RockboxSettings(playlist_dir=value).playlist_dir == ok
