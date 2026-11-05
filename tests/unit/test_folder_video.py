"""Video in folder libraries: video files become `video` items with show / season / episode from
their tags or folders; an audio-only .mp4 stays audio; rules can use the video fields; no device takes video yet."""

from __future__ import annotations

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core.models import LibraryConfig, MediaKind
from syncmusic.core.rules.fields import FIELDS
from syncmusic.libraries.folder import FolderLibrary
from tests.media import make_music, make_video


async def test_videos_with_show_season_episode(tmp_path):
    root = tmp_path / "lib"
    make_video(root / "Мультики" / "Ну, постривай!" / "Сезон 1" / "S01E02 Випуск 2.mp4")
    make_video(
        root / "Мультики" / "Їжачок у тумані.mkv",
        size="320x240",
        title="Їжачок у тумані",
        date="1975",
    )
    make_video(
        root / "Серіал" / "Будинок.m4v",
        show="Свати",
        season_number="3",
        episode_sort="7",
        title="Новий рік",
    )
    make_music(root / "Музика" / "пісня.m4a", title="Пісня", artist="Гурт")
    lib = FolderLibrary(
        LibraryConfig(id="v", type="folder", name="V", settings={"root": str(root)}), None
    )
    snap = await lib.snapshot()
    by_title = {t.title: t for t in snap.tracks.values()}
    ep = by_title["Випуск 2"]
    assert ep.kind is MediaKind.VIDEO and ep.series == "Ну, постривай!"
    assert (ep.extra["show"], ep.extra["season"], ep.extra["episode"]) == ("Ну, постривай!", 1, 2)
    assert (ep.extra["width"], ep.extra["height"], ep.codec, ep.format) == (640, 480, "h264", "mp4")
    assert ep.duration_s and 0.5 < ep.duration_s < 2
    film = by_title["Їжачок у тумані"]
    assert film.kind is MediaKind.VIDEO and film.year == 1975 and "season" not in film.extra
    tagged = by_title["Новий рік"]
    assert (tagged.extra["show"], tagged.extra["season"], tagged.extra["episode"]) == (
        "Свати",
        3,
        7,
    )
    assert by_title["Пісня"].kind is MediaKind.MUSIC
    assert FIELDS["show"].get(ep) == "Ну, постривай!" and FIELDS["season"].get(film) == 0


async def test_audio_only_mp4_is_audio(tmp_path):
    root = tmp_path / "lib"
    src = make_music(tmp_path / "a.m4a", title="Тільки звук")
    (root / "x").mkdir(parents=True)
    (root / "x" / "a.mp4").write_bytes(src.read_bytes())
    lib = FolderLibrary(
        LibraryConfig(id="a", type="folder", name="A", settings={"root": str(root)}), None
    )
    (track,) = (await lib.snapshot()).tracks.values()
    assert track.kind is MediaKind.MUSIC and track.title == "Тільки звук"


def test_no_device_takes_video_by_default():
    caps = DeviceCapabilities()
    assert not caps.supports_kind(MediaKind.VIDEO)
    assert DeviceCapabilities(supports_video=True).supports_kind(MediaKind.VIDEO)
