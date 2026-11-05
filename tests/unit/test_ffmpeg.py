"""infra/ffmpeg.py against the real ffmpeg binary on synthetic media."""

from __future__ import annotations

import asyncio
import subprocess
import uuid

import pytest

from syncmusic.infra import ffmpeg
from syncmusic.infra.ffmpeg import FfmpegError, parse_probe, probe, run_ffmpeg
from tests.media import make_track


@pytest.mark.parametrize(
    ("ext", "codec", "container"),
    [(".mp3", "mp3", "mp3"), (".flac", "flac", "flac"), (".m4a", "aac", "mov,mp4,m4a,3gp,3g2,mj2")],
)
async def test_probe_cyrillic_paths(tmp_path, ext, codec, container):
    path = make_track(
        tmp_path / "Скрябін" / f"-Група крові: 1{ext}",
        seconds=2,
        title="Група крові",
        artist="Скрябін",
    )
    r = await probe(path)
    assert r.codec == codec
    assert r.container == container
    assert r.duration_s == pytest.approx(2.0, abs=0.1)
    assert r.sample_rate == 44100
    assert r.channels == 1
    assert r.size_bytes == path.stat().st_size
    assert r.tags["title"] == "Група крові"
    assert r.tags["artist"] == "Скрябін"
    assert r.bit_depth == (16 if ext == ".flac" else None)
    assert not r.has_cover


async def test_probe_sees_cover_and_stream_tags(tmp_path):
    r = await probe(make_track(tmp_path / "a.mp3", cover=True))
    assert r.has_cover
    r = await probe(make_track(tmp_path / "b.opus", title="Зірка"))
    assert r.codec == "opus"
    assert r.tags["title"] == "Зірка"  # Ogg keeps tags on the stream


async def test_run_ffmpeg_converts(tmp_path):
    src = make_track(tmp_path / "Пісня.flac", seconds=1)
    out = tmp_path / "вихід.m4a"
    await run_ffmpeg(["-i", ffmpeg.fpath(src), "-c:a", "alac", ffmpeg.fpath(out)])
    r = await probe(out)
    assert r.codec == "alac"
    assert r.bit_depth == 16


async def test_failure_raises_with_stderr(tmp_path):
    bad = tmp_path / "garbage.mp3"
    bad.write_bytes(b"not audio at all" * 10)
    with pytest.raises(FfmpegError) as e:
        await run_ffmpeg(["-i", ffmpeg.fpath(bad), "-f", "null", "-"])
    assert e.value.returncode not in (0, None)
    assert "garbage.mp3" in str(e.value) or e.value.stderr
    with pytest.raises(FfmpegError):
        await probe(tmp_path / "missing.flac")


async def test_missing_binary_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg, "FFMPEG", str(tmp_path / "no-ffmpeg"))
    with pytest.raises(FfmpegError, match="exit None"):
        await run_ffmpeg(["-version"])


def _running(marker: str) -> bool:
    return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0


async def test_cancellation_kills_process(tmp_path):
    marker = f"sd-cancel-{uuid.uuid4().hex}"
    out = tmp_path / f"{marker}.wav"
    # -re: read the 10-minute input in real time, so the run is still going when we cancel
    task = asyncio.create_task(
        run_ffmpeg(["-re", "-f", "lavfi", "-i", "sine=duration=600", ffmpeg.fpath(out)])
    )
    for _ in range(50):
        await asyncio.sleep(0.1)
        if _running(marker):
            break
    assert _running(marker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _running(marker)


async def test_timeout_kills_process(tmp_path):
    marker = f"sd-timeout-{uuid.uuid4().hex}"
    with pytest.raises(TimeoutError):
        await run_ffmpeg(
            [
                "-re",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=600",
                ffmpeg.fpath(tmp_path / f"{marker}.wav"),
            ],
            timeout_s=0.5,
        )
    assert not _running(marker)


def test_encoder_detection():
    enc = ffmpeg.encoders()
    assert {"alac", "aac", "flac", "libmp3lame"} <= enc
    assert ffmpeg.aac_encoder() in ("libfdk_aac", "aac")
    assert ffmpeg.aac_encoder() == ("libfdk_aac" if "libfdk_aac" in enc else "aac")


def test_parse_probe_hi_res_flac():
    r = parse_probe(
        {
            "format": {"format_name": "flac", "duration": "3.5", "size": "100", "bit_rate": "9"},
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "flac",
                    "sample_rate": "96000",
                    "channels": 2,
                    "sample_fmt": "s32",
                    "bits_per_raw_sample": "24",
                },
                {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            ],
        }
    )
    assert (r.sample_rate, r.bit_depth, r.channels, r.has_cover) == (96000, 24, 2, True)
    assert parse_probe({}).codec is None


def test_configured_path_nice_and_check(tmp_path, monkeypatch):
    """Settings → Conversion: the configured ffmpeg (its ffprobe next to it) and nice level; check()
    names the version and encoders, or why it does not run."""
    real = ffmpeg.check()
    assert real["error"] is None and real["version"] and "aac" in real["encoders"]
    assert real["missing"] == [] or set(real["missing"]) <= {"aac", "alac", "libmp3lame", "flac"}
    bad = ffmpeg.check(str(tmp_path / "ffmpeg"))
    assert bad["version"] is None and "does not run" in bad["error"]
    try:
        ffmpeg.configure("/opt/ff/bin/ffmpeg", 7)
        assert (ffmpeg.FFMPEG, ffmpeg.FFPROBE) == ("/opt/ff/bin/ffmpeg", "/opt/ff/bin/ffprobe")
        assert ffmpeg.nice_prefix() == ["nice", "-n", "7"]
        e = ffmpeg.FfmpegError(["nice", "-n", "7", "/opt/ff/bin/ffmpeg", "-i"], 1, "boom")
        assert str(e).startswith("ffmpeg failed (exit 1)")
    finally:
        ffmpeg.configure(None, 0)
    assert ffmpeg.FFMPEG == ffmpeg.DEFAULT_FFMPEG and ffmpeg.nice_prefix() == []


async def test_a_conversion_runs_niced(tmp_path):
    ffmpeg.configure(None, 5)
    try:
        out = tmp_path / "s.wav"
        await ffmpeg.run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=d=0.1", ffmpeg.fpath(out)])
        assert out.stat().st_size > 0
    finally:
        ffmpeg.configure(None, 0)


@pytest.mark.parametrize("ext", [".mp3", ".flac", ".m4a", ".opus"])
async def test_quick_probe_agrees_with_ffprobe(tmp_path, ext):
    """What the transcode decision reads: codec, rate, depth, bitrate close to ffprobe's."""
    path = make_track(tmp_path / f"Трек{ext}", seconds=2, title="x")
    quick, full = ffmpeg.quick_probe(path), await probe(path)
    assert quick is not None
    assert quick.codec == full.codec
    assert quick.sample_rate == full.sample_rate
    assert quick.bit_depth == full.bit_depth
    assert quick.duration_s == pytest.approx(full.duration_s, abs=0.1)
    if ext != ".flac":  # a 2 s FLAC: ffprobe counts its headers in the overall rate
        assert quick.audio_bitrate == pytest.approx(full.audio_bitrate, rel=0.25)


def test_quick_probe_gives_up_on_other_files(tmp_path):
    wav = tmp_path / "a.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", str(wav)], check=True
    )
    junk = tmp_path / "b.mp3"
    junk.write_bytes(b"not audio")
    assert ffmpeg.quick_probe(wav) is None
    assert ffmpeg.quick_probe(junk) is None


def test_only_an_ffmpeg_binary_is_run(tmp_path, monkeypatch):
    """Security review: Settings → Conversion's path runs whatever it names - only ffmpeg…"""
    from syncmusic.core.models import AppSettings

    sh = tmp_path / "ffmpeg"
    sh.symlink_to("/bin/sh")
    for bad in ("/bin/sh", str(sh)):
        assert ffmpeg.check(bad)["error"]
    with pytest.raises(ValueError):
        AppSettings(ffmpeg_path="/bin/sh")
    monkeypatch.setattr(ffmpeg, "FFMPEG", ffmpeg.FFMPEG)
    monkeypatch.setattr(ffmpeg, "FFPROBE", ffmpeg.FFPROBE)
    ffmpeg.configure("/bin/sh")
    assert ffmpeg.FFMPEG == ffmpeg.DEFAULT_FFMPEG
