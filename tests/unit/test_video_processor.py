"""`video` processor: the stock iPod's 320×240 profile - H.264 Baseline level 1.3, ≤ 768 kbit/s,
AAC stereo, faststart; files already in the profile are copied, a misplaced moov atom only remuxed."""

from __future__ import annotations

from fractions import Fraction

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core import registry
from syncmusic.infra.ffmpeg import probe_json
from syncmusic.processors.transcode import Transcode
from syncmusic.processors.video import Video, assess, fit_size, moov_first, target_fps
from tests.media import make_music, make_video, media_work_item, video_work_item

CAPS = DeviceCapabilities(formats={"mp3", "aac", "alac"}, supports_video=True, video_profile="ipod")
IN_PROFILE = (
    "-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "baseline", "-level", "1.3",
    "-b:v", "300k", "-maxrate", "400k", "-bufsize", "800k",
)  # fmt: skip


def _streams(path):
    info = probe_json(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    return info, v, a


def test_fit_size_and_frame_rate():
    assert fit_size(640, 480) == (320, 240)
    assert fit_size(1920, 1080) == (320, 180)
    assert fit_size(720, 576, Fraction(16, 15)) == (320, 240)  # PAL 4:3 with non-square pixels
    assert fit_size(720, 576, Fraction(64, 45)) == (320, 180)  # PAL anamorphic 16:9
    assert fit_size(176, 144) == (176, 144)  # never upscaled
    assert fit_size(320, 240) == (320, 240)
    assert [target_fps(f) for f in (25, 29.97, 30, 50, 59.94, 120, None)] == [
        None, None, None, 25, 29.97, 30, None,
    ]  # fmt: skip


async def test_a_big_video_is_encoded_into_the_ipod_profile(tmp_path):
    src = make_video(
        tmp_path / "src" / "Ну, постривай! S01E01.mkv", size="1280x720", seconds=2, rate=50
    )
    item = video_work_item(src, tmp_path / "work")
    proc = Video({}, None)
    planned = proc.plan(item.plan, CAPS)
    assert planned.file.ext == "m4v" and planned.steps == ["video"]
    out = await proc.run(item.model_copy(update={"plan": planned}), CAPS)
    assert out.path.suffix == ".m4v" and out.path.parent == tmp_path / "work"
    info, v, a = _streams(out.path)
    assert v["codec_name"] == "h264" and "Baseline" in v["profile"] and v["level"] <= 13
    assert (v["width"], v["height"]) == (320, 180) and v["pix_fmt"] == "yuv420p"
    assert Fraction(v["avg_frame_rate"]) == 25
    assert a["codec_name"] == "aac" and a["channels"] == 2
    assert int(info["format"]["bit_rate"]) <= (768 + 160) * 1000
    assert "mp4" in info["format"]["format_name"] and moov_first(out.path)
    assert assess(info, out.path) == ("copy", [])  # what it made is in the profile
    assert out.plan.file.size == out.path.stat().st_size and out.notes["video"].startswith("encode")


async def test_in_profile_files_are_copied_and_a_late_moov_only_remuxed(tmp_path):
    ok = make_video(tmp_path / "src" / "ok.m4v", size="320x240", video=IN_PROFILE, faststart=True)
    item = video_work_item(ok, tmp_path / "work")
    proc = Video({}, None)
    assert proc.plan(item.plan, CAPS) == item.plan  # predicted: copy
    assert await proc.run(item, CAPS) == item
    late = make_video(tmp_path / "src" / "late.mp4", size="320x240", video=IN_PROFILE)
    assert not moov_first(late)
    out = await proc.run(video_work_item(late, tmp_path / "work"), CAPS)
    assert out.notes["video"] == "remux: moov atom at the end" and moov_first(out.path)
    _, v, _ = _streams(out.path)
    _, before, _ = _streams(late)
    assert (
        v["nb_frames"] == before["nb_frames"] and v["bit_rate"] == before["bit_rate"]
    )  # streams copied
    # `always`: re-encoded even in profile
    again = await Video({"always": True}, None).run(item, CAPS)
    assert again.notes["video"] == "encode: always"


async def test_audio_items_pass_and_convert_audio_leaves_videos_alone(tmp_path):
    song = media_work_item(make_music(tmp_path / "src" / "a.flac"), tmp_path / "work")
    proc = Video({}, None)
    assert proc.plan(song.plan, CAPS) == song.plan
    assert await proc.run(song, CAPS) == song
    vid = video_work_item(make_video(tmp_path / "src" / "v.mkv"), tmp_path / "work")
    audio = Transcode({"rules": [{"to": "mp3", "when": "always"}]}, None)
    assert audio.plan(vid.plan, CAPS) == vid.plan
    assert await audio.run(vid, CAPS) == vid


def test_suggested_only_for_a_device_with_the_ipod_profile():
    assert registry.processor_class("video") is Video
    assert Video.suggest(CAPS) is not None
    assert Video.suggest(DeviceCapabilities(supports_video=True)) is None
    assert Video({"video_kbps": 400}, None).settings_hash() != Video({}, None).settings_hash()
