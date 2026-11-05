"""which devices take video, and how (iPod by model with TV-show fields, flash copies, Rockbox never)."""

from __future__ import annotations

from pygpod import Database
from pygpod.db.constants import MEDIATYPE_TVSHOW, MEDIATYPE_VIDEO

from syncmusic.core.models import DeviceConfig, Manifest, ManifestEntry, ManifestHeader, MediaKind
from syncmusic.devices.flashdrive import FlashDrive
from syncmusic.devices.paths import PathLayout
from syncmusic.devices.Rockbox.rockbox import Rockbox
from tests.media import make_video, video_work_item
from tests.unit.test_ipod_device import _dev


def _video(tmp_path, name, **extra):
    src = make_video(
        tmp_path / "src" / f"{name}.m4v",
        size="320x240",
        video=("-c:v", "libx264", "-profile:v", "baseline", "-preset", "ultrafast"),
        faststart=True,
    )
    item = video_work_item(src, tmp_path / "work")
    track = item.track.model_copy(update={"title": name, "extra": {**item.track.extra, **extra}})
    return item.model_copy(update={"plan": item.plan.model_copy(update={"track": track})})


def test_video_capabilities_per_device(tmp_path):
    classic = _dev(tmp_path).effective_capabilities()
    assert classic.supports_video and classic.video_profile == "ipod"
    mini = _dev(tmp_path, b"ModelNumStr: M9160\n", name="mini").effective_capabilities()
    assert not mini.supports_video and mini.video_profile == "ipod"  # the chain is the same
    flash = FlashDrive(DeviceConfig(id="f", type="flashdrive", name="F"), tmp_path, None)
    caps = flash.effective_capabilities()
    assert caps.supports_video and caps.video_profile is None
    assert caps.path_layout[MediaKind.VIDEO] == PathLayout().video
    rb = Rockbox(DeviceConfig(id="r", type="rockbox", name="R"), tmp_path, None)
    assert not rb.effective_capabilities().supports_video


async def test_ipod_files_videos_as_tv_shows_and_movies(tmp_path):
    dev = _dev(tmp_path)
    episode = _video(tmp_path, "Серія 1", show="Мультики", season=1, episode=3)
    movie = _video(tmp_path, "Фільм")
    s = await dev.begin()
    refs = [await dev.put_track(s, i) for i in (episode, movie)]
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
            for i, r in zip((episode, movie), refs, strict=True)
        },
    )
    await dev.commit(s, manifest)

    by_title = {t.title: t for t in Database(str(dev.mount)).tracks}
    ep, mv = by_title["Серія 1"], by_title["Фільм"]
    assert ep.media_type & MEDIATYPE_TVSHOW and ep.tvshow == "Мультики"
    assert (ep.season_number, ep.episode_number) == (1, 3)
    assert mv.media_type & MEDIATYPE_VIDEO and not mv.media_type & MEDIATYPE_TVSHOW
    assert not ep.remember_position
    kinds = {i.title: i.kind for i in (await dev.read_contents()).items}
    assert kinds == {"Серія 1": MediaKind.VIDEO, "Фільм": MediaKind.VIDEO}


async def test_flash_drive_copies_videos_into_videos(tmp_path):
    from tests.unit.test_flashdrive import _dev as _flash
    from tests.unit.test_flashdrive import _manifest

    dev = _flash(tmp_path)
    item = _video(tmp_path, "Серія 1")
    item = item.model_copy(
        update={
            "plan": item.plan.model_copy(
                update={"track": item.track.model_copy(update={"series": "Мультики"})}
            )
        }
    )
    s = await dev.begin()
    r = await dev.put_track(s, item)
    assert r.path == "Videos/Мультики/Серія 1.m4v"
    await dev.commit(s, _manifest(dev, [(item, r)]))
    kinds = {i.path: i.kind for i in (await dev.read_contents()).items}
    assert kinds == {"Videos/Мультики/Серія 1.m4v": MediaKind.VIDEO}


def test_ipod_chain_includes_convert_video():
    """the built-in iPod template and the suggested chain (no database read yet) have Convert video
    in the audio stage, before the tags."""
    from syncmusic.core.proc_templates import builtin_processor_templates
    from syncmusic.core.sync import suggested_processors
    from syncmusic.devices.iPod.ipod import IPod

    types = [p.type for p in suggested_processors(IPod.capabilities)]
    assert "video" in types and types.index("video") < types.index("retag")
    assert [p.type for p in builtin_processor_templates()["builtin-ipod"].processors] == types
    assert "video" not in [p.type for p in suggested_processors(Rockbox.capabilities)]
