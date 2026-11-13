"""HiBy R1 - Compás: probe, device.json, plugins, Podcasts marker, Wipe keeps the player's files."""

import asyncio
import sqlite3

from syncmusic.core.models import DeviceConfig
from syncmusic.devices.HibyR1.compas import HibyCompas
from syncmusic.devices.HibyR1.sonixos import Hiby


def test_probe_needs_the_compas_folder(tmp_path):
    assert HibyCompas.probe(None, tmp_path) is None
    (tmp_path / ".compas").mkdir()
    assert HibyCompas.probe(None, tmp_path).type_name == "hiby_compas"


def test_compas_outranks_sonixos_on_a_card_with_both(tmp_path):
    (tmp_path / ".compas").mkdir()
    (tmp_path / ".local").mkdir()
    sqlite3.connect(tmp_path / ".local" / "library.db").close()
    assert HibyCompas.probe(None, tmp_path).confidence > Hiby.probe(None, tmp_path).confidence


def _dev(tmp_path) -> HibyCompas:
    (tmp_path / ".compas").mkdir(exist_ok=True)
    return HibyCompas(DeviceConfig(id="c", type="hiby_compas", name="C"), tmp_path, None)


def test_begin_marks_podcasts_ignored(tmp_path):
    dev = _dev(tmp_path)
    asyncio.run(dev.begin())
    assert (tmp_path / "Podcasts" / "database.ignore").is_file()


def test_wipe_keeps_the_players_files(tmp_path):
    dev = _dev(tmp_path)
    for name in (".plugins/p.lua", "themes/t.json", "radio.txt", "r1.upt", "Music/a.mp3", "x.txt"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")
    (tmp_path / ".compas" / "device.json").write_text("{}")
    assert asyncio.run(dev.wipe()) == 2
    left = {p.name for p in tmp_path.iterdir()}
    assert left == {".compas", ".plugins", "themes", "radio.txt", "r1.upt"}


def test_registered():
    from syncmusic.core import registry

    assert registry.DEVICES["hiby_compas"].endswith(":HibyCompas")


def test_probe_reads_device_json_and_plugins(tmp_path):
    from tests.contracts.harness import compas_tree

    compas_tree(tmp_path, plugins=("Podcasts",))
    r = HibyCompas.probe(None, tmp_path)
    assert r.suggested_name == "HiBy R1 Compás"
    assert r.details["player_version"] == "v9.9.9"
    assert r.details["database_format"] == 8
    assert r.details["plugins"] == ["Podcasts"]


def test_damaged_device_json_still_probes(tmp_path):
    (tmp_path / ".compas").mkdir()
    (tmp_path / ".compas" / "device.json").write_text("{not json")
    r = HibyCompas.probe(None, tmp_path)
    assert r.type_name == "hiby_compas" and r.details["player_version"] is None


def test_capabilities_follow_the_plugins(tmp_path):
    from tests.contracts.harness import compas_tree

    compas_tree(tmp_path, plugins=("Audiobooks",))
    caps = _dev(tmp_path).effective_capabilities()
    assert caps.supports_audiobooks and not caps.supports_podcasts
    (tmp_path / ".plugin-assets" / "Podcasts").mkdir()
    caps = _dev(tmp_path).effective_capabilities()
    assert caps.supports_audiobooks and caps.supports_podcasts


def test_wipe_keeps_plugin_state_and_downloads(tmp_path):
    dev = _dev(tmp_path)
    files = {
        "Podcasts/.catalog/feed.xml": True,
        "Podcasts/.progress.tsv": True,
        "Podcasts/database.ignore": True,
        "Podcasts/subscriptions.opml": True,
        "Podcasts/Show A/2026-01-01 Ep [abcd1234].mp3": True,  # the plugin's download
        "Podcasts/Show B/ours.mp3": False,
        "Audiobooks/database.ignore": True,
        "Audiobooks/Author/Book/01.mp3": False,
        "Themes/x.json": True,
        "Radio.txt": True,
        ".plugins/Audiobooks/main.lua": True,
        ".plugins/.audiobooks_state_v3": True,
        ".plugin-assets/a.png": True,
        ".compas/device.json": True,
        ".local/x": True,
        "r1.upt": True,
        "r1.upt.parked": True,
        "Music/A/01.mp3": False,
        "Playlists/Mix.m3u": False,
    }
    for name in files:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")
    (tmp_path / "Podcasts" / ".progress.tsv").write_text(
        "P\tk1\t10\t100\t0\t0\t%2Fdata%2Fmnt%2Fsd_0%2FPodcasts%2FShow%20A%2F2026-01-01%20Ep%20"
        "%5Babcd1234%5D.mp3\t0\tEp\t\t0\n"
        # an episode SyncMusic listed: not the plugin's, so it is wiped and its record goes too
        "P\tf00%7Csyncmusic%3APodcasts%2FShow%20B%2Fours.mp3\t0\t0\t0\t1\t"
        "%2Fdata%2Fmnt%2Fsd_0%2FPodcasts%2FShow%20B%2Fours.mp3\t0\tOurs\t\t0\n"
    )
    assert asyncio.run(dev.wipe()) == 4
    assert "ours.mp3" not in (tmp_path / "Podcasts" / ".progress.tsv").read_text()
    for name, kept in files.items():
        assert (tmp_path / name).exists() == kept, name
    assert not (tmp_path / "Podcasts" / "Show B").exists()
    assert not (tmp_path / "Audiobooks" / "Author").exists()


def test_unmounted_compas_gadget_is_offered_as_compas():
    """Before any mount only the USB ids are known: the Compás gadget (product 0004) is Compás, 0101 SonixOS."""
    from syncmusic.core.detection import probe_disk
    from tests.unit.test_hiby_device import _blk

    best = probe_disk(_blk(model="File-Stor Gadget", usb_product_id="0004"))[0]
    assert best.type_name == "hiby_compas"
    assert best.identity.fs_uuid == "92A3-B4C5"
    assert probe_disk(_blk())[0].type_name == "hiby"
    assert HibyCompas.probe(_blk(), None) is None


def test_player_info(tmp_path):
    from tests.contracts.harness import compas_tree

    compas_tree(tmp_path)
    assert _dev(tmp_path).player_info() == {
        "firmware": "Compás",
        "version": "v9.9.9",
        "plugins": ["Audiobooks", "Podcasts"],
    }


def test_firmware_switch_follows_the_usb_gadget(tmp_path):
    """A registered R1 whose player was reflashed: the sibling type is offered, the same firmware is not."""
    from syncmusic.core.detection import firmware_switch
    from tests.unit.test_hiby_device import _blk

    compas, sonix = _blk(usb_product_id="0004"), _blk()

    def switch(type_name, blk, mount=None):
        own = (HibyCompas if type_name == "hiby_compas" else Hiby).probe(blk, mount)
        return firmware_switch(type_name, own, blk, mount)

    assert switch("hiby", compas) == "hiby_compas"
    assert switch("hiby_compas", sonix) == "hiby"
    assert switch("hiby", sonix) is None
    assert switch("hiby_compas", compas) is None
    # mounted, a card with both players' files: the gadget decides
    (tmp_path / ".compas").mkdir()
    (tmp_path / ".local").mkdir()
    sqlite3.connect(tmp_path / ".local" / "library.db").close()
    assert switch("hiby_compas", sonix, tmp_path) == "hiby"
    assert switch("hiby", compas, tmp_path) == "hiby_compas"
    assert switch("hiby", sonix, tmp_path) is None


def test_firmware_switch_only_for_types_with_siblings():
    from syncmusic.core.detection import firmware_switch
    from tests.unit.test_hiby_device import _blk

    assert firmware_switch("flashdrive", None, _blk(usb_product_id="0004"), None) is None
    assert firmware_switch("no-such-type", None, _blk(), None) is None


def test_detector_flags_a_reflashed_player():
    from syncmusic.core.detection import Detector
    from syncmusic.core.models import DeviceIdentity
    from syncmusic.infra.events import EventBus
    from tests.unit.test_hiby_device import _blk

    blk = _blk(usb_product_id="0004")
    cfg = DeviceConfig(id="r1", type="hiby", name="R1", identity=DeviceIdentity(fs_uuid=blk.uuid))
    cd = asyncio.run(Detector(EventBus(), lambda: [cfg]).disk_added(blk))
    assert cd.device_id == "r1" and cd.firmware_switch == "hiby_compas"


def test_positions_go_to_the_plugin_state_at_commit(tmp_path, monkeypatch):
    from syncmusic.core.models import DeviceTrackRef
    from syncmusic.devices._filedevice import FileDeviceMixin
    from syncmusic.devices.HibyR1 import compas_feedback as cf

    async def no_manifest(self, s, manifest):
        pass

    monkeypatch.setattr(FileDeviceMixin, "commit", no_manifest)

    dev = _dev(tmp_path)

    async def run():
        s = await dev.begin()
        ok = await dev.set_position(
            s,
            DeviceTrackRef(
                device_ref="Audiobooks/Book/01.mp3", path="Audiobooks/Book/01.mp3", size=1
            ),
            61.0,
        )
        music = await dev.set_position(
            s, DeviceTrackRef(device_ref="Music/A/01.mp3", path="Music/A/01.mp3", size=1), 5.0
        )
        assert not (tmp_path / cf.AUDIOBOOK_STATE).exists()  # written at commit
        await dev.commit(s, None)
        return ok, music

    assert asyncio.run(run()) == (True, False)
    assert cf.audiobook_positions(tmp_path)["Audiobooks/Book/01.mp3"][0] == 61.0


def test_commit_lists_our_episodes_when_the_podcasts_plugin_is_there(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from syncmusic.core.models import ItemRef, ManifestEntry, MediaKind
    from syncmusic.devices._filedevice import FileDeviceMixin
    from syncmusic.devices.HibyR1 import compas_feedback as cf

    async def no_manifest(self, s, manifest):
        pass

    monkeypatch.setattr(FileDeviceMixin, "commit", no_manifest)
    dev = _dev(tmp_path)
    entry = ManifestEntry(
        ref=ItemRef(library_id="abs", item_id="e1"),
        device_ref="Podcasts/Show/ep.mp3",
        path="Podcasts/Show/ep.mp3",
        size=1,
        kind=MediaKind.PODCAST,
        meta={"title": "Episode", "series": "Show", "published": "2026-02-03T00:00:00+00:00"},
    )
    manifest = SimpleNamespace(items={"k": entry})

    async def run():
        await dev.commit(await dev.begin(), manifest)

    asyncio.run(run())
    assert not (tmp_path / cf.PODCAST_PROGRESS).exists()  # no plugin: nothing listed
    (tmp_path / ".plugin-assets" / "Podcasts").mkdir(parents=True)
    asyncio.run(run())
    [line] = (tmp_path / cf.PODCAST_PROGRESS).read_text().splitlines()
    assert line.split("\t")[8:10] == ["Episode", "2026-02-03"]


def test_formats_the_plugins_cannot_play_are_converted_per_kind():
    from syncmusic.core.models import MediaKind
    from syncmusic.processors.transcode import Transcode

    caps = HibyCompas.capabilities
    assert caps.formats_for(MediaKind.MUSIC) >= {"wv", "dsd"}
    assert "wv" not in caps.formats_for(MediaKind.AUDIOBOOK)
    assert caps.formats_for(MediaKind.PODCAST) == {"mp3", "aac", "alac", "ogg", "opus", "flac"}
    tc = Transcode(Transcode.Settings.model_validate({}), None)
    for kind, fmt, converted in (
        (MediaKind.MUSIC, "wv", False),
        (MediaKind.AUDIOBOOK, "wv", True),
        (MediaKind.PODCAST, "wav", True),
        (MediaKind.PODCAST, "mp3", False),
    ):
        d = tc.decide(fmt, 44100, 16, caps.for_kind(kind))
        assert (d is not None) == converted, (kind, fmt)


def test_a_new_audiobook_starts_at_the_library_position(tmp_path, monkeypatch):
    from syncmusic.core.models import MediaKind
    from syncmusic.devices._filedevice import FileDeviceMixin
    from syncmusic.devices.HibyR1 import compas_feedback as cf
    from tests.contracts.harness import work_item

    async def no_manifest(self, s, manifest):
        pass

    monkeypatch.setattr(FileDeviceMixin, "commit", no_manifest)
    card = tmp_path / "card"
    card.mkdir()
    dev = _dev(card)

    def kind(title, k, pos):
        item = work_item(tmp_path, title)
        t = item.track.model_copy(update={"kind": k, "position_s": pos, "album": "Book"})
        return item.model_copy(update={"plan": item.plan.model_copy(update={"track": t})})

    async def run():
        s = await dev.begin()
        book = await dev.put_track(s, kind("Chapter", MediaKind.AUDIOBOOK, 42.0))
        await dev.put_track(s, kind("Song", MediaKind.MUSIC, 10.0))
        await dev.commit(s, None)
        return book

    book = asyncio.run(run())
    assert cf.audiobook_positions(card) == {
        book.path: (42.0, False, cf.audiobook_positions(card)[book.path][2])
    }


def test_playlists_are_m3u_like_the_players_own(tmp_path):
    assert _dev(tmp_path)._playlist_format() == (".m3u", "\n")


def test_reload_hint_only_after_spoken_word_changes(tmp_path, monkeypatch):
    from syncmusic.core.models import DeviceTrackRef
    from syncmusic.devices._filedevice import FileDeviceMixin
    from syncmusic.devices.HibyR1.compas import RELOAD_HINT

    async def nothing(self, *a):
        pass

    monkeypatch.setattr(FileDeviceMixin, "commit", nothing)
    monkeypatch.setattr(FileDeviceMixin, "remove_track", nothing)
    dev = _dev(tmp_path)

    def ref(p):
        return DeviceTrackRef(device_ref=p, path=p, size=1)

    async def run(path):
        s = await dev.begin()
        await dev.remove_track(s, ref(path))
        await dev.commit(s, None)
        return s.data.get("commit_warnings", [])

    assert RELOAD_HINT not in asyncio.run(run("Music/A/01.mp3"))
    assert asyncio.run(run("Podcasts/Show/01.mp3")) == [RELOAD_HINT]


def test_positions_a_stale_plugin_overwrote_are_written_again(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from syncmusic.core.models import DeviceFeedback, DeviceTrackRef, MediaKind
    from syncmusic.devices._filedevice import FileDeviceMixin
    from syncmusic.devices.HibyR1 import compas as cmod
    from syncmusic.devices.HibyR1 import compas_feedback as cf

    async def nothing(self, *a):
        pass

    monkeypatch.setattr(FileDeviceMixin, "commit", nothing)
    seen: dict = {}

    def feedback(data, *a, **k):
        seen.update(data.positions)
        return DeviceFeedback()

    monkeypatch.setattr(cmod, "hiby_feedback", feedback)
    dev = _dev(tmp_path)
    (tmp_path / ".plugins").mkdir()
    (tmp_path / cf.AUDIOBOOK_STATE).write_text("")
    path = "Audiobooks/Book/01.mp3"
    entry = SimpleNamespace(
        path=path, duration_s=600.0, kind=MediaKind.AUDIOBOOK, meta={}, orphan=False
    )
    manifest = SimpleNamespace(items={"k": entry})

    async def sync(pos=None):
        s = await dev.begin()
        if pos is not None:
            await dev.set_position(s, DeviceTrackRef(device_ref=path, path=path, size=1), pos)
        await dev.commit(s, manifest)
        return s.data

    asyncio.run(sync(300.0))
    assert cf.audiobook_positions(tmp_path)[path][0] == 300.0
    # the plugin, never reloaded, saves its old position over ours
    cf.write_positions(tmp_path, {path: cf.Position(20.0, False, 600.0)}, 1)
    asyncio.run(dev.read_feedback(manifest))
    assert path not in seen  # not sent to the library as the listener's
    data = asyncio.run(sync())
    assert cf.audiobook_positions(tmp_path)[path][0] == 300.0
    assert data["player_notice"] == cmod.RELOAD_HINT
    # real progress past ours is the listener's and stays
    cf.write_positions(tmp_path, {path: cf.Position(400.0, False, 600.0)}, 2)
    asyncio.run(dev.read_feedback(manifest))
    assert seen[path][0] == 400.0
    assert "player_notice" not in asyncio.run(sync())


def _manifest_with(*paths: str):
    from syncmusic.core.models import ItemRef, Manifest, ManifestEntry, ManifestHeader

    m = Manifest(header=ManifestHeader(device_id="c"))
    for i, p in enumerate(paths):
        r = ItemRef(library_id="l", item_id=str(i))
        m.items[r.key()] = ManifestEntry(ref=r, device_ref=p, path=p, size=1)
    m.playlists = ["Mix"]
    return m


def test_playlists_edited_on_the_player_are_read_back(tmp_path):
    dev = _dev(tmp_path)
    music = tmp_path / "Music"
    music.mkdir()
    for n in "abc":
        (music / f"{n}.mp3").write_text("x")
    pl = tmp_path / "Playlists"
    pl.mkdir()
    # ours, reordered and appended to by the player (relative and absolute player paths)
    (pl / "Mix.m3u").write_text(
        "#EXTM3U\n../Music/b.mp3\n../Music/a.mp3\n/data/mnt/sd_0/Music/c.mp3\n"
    )
    (pl / "New on player.m3u").write_text("/data/mnt/sd_0/Music/a.mp3\n")
    m = _manifest_with("Music/a.mp3", "Music/b.mp3", "Music/c.mp3")
    m.playlist_paths = {"Mix": "Playlists/Mix.m3u"}
    got = asyncio.run(dev.read_playlists(m))
    assert got == {
        "Mix": ["Music/b.mp3", "Music/a.mp3", "Music/c.mp3"],
        "New on player": ["Music/a.mp3"],
    }


def test_no_playlists_read_back_when_playlists_are_off(tmp_path):
    dev = HibyCompas(
        DeviceConfig(id="c", type="hiby_compas", name="C", settings={"playlists": False}),
        tmp_path,
        None,
    )
    assert asyncio.run(dev.read_playlists(_manifest_with())) is None
