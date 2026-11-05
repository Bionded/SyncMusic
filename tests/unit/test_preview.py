"""Browser playback of device files: playable codecs pass through with their Content-Type, ALAC and other
codecs become one cached MP3."""

from __future__ import annotations

import asyncio

import pytest

from syncmusic.core import preview
from syncmusic.infra import ffmpeg
from tests.media import make_music


async def _aac(path):
    src = make_music(path.with_suffix(".wav"), title="x")
    await ffmpeg.run_ffmpeg(
        ["-i", ffmpeg.fpath(src), "-c:a", "aac", "-b:a", "96k", ffmpeg.fpath(path)]
    )
    return path


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("a.mp3", "audio/mpeg"),
        ("a.flac", "audio/flac"),
        ("a.opus", "audio/ogg"),
        ("a.wav", "audio/wav"),
    ],
)
async def test_playable_files_pass_through(tmp_path, name, media_type):
    src = make_music(tmp_path / name, title="x")
    assert await preview.browser_file(src, tmp_path / "cache") == (src, media_type)
    assert not (tmp_path / "cache").exists()


async def test_aac_m4a_passes_through(tmp_path):
    src = await _aac(tmp_path / "aac.m4a")
    assert await preview.browser_file(src, tmp_path / "cache") == (src, "audio/mp4")


async def test_alac_becomes_one_cached_mp3(tmp_path):
    src = make_music(tmp_path / "alac.m4a", title="x")
    cache = tmp_path / "cache"
    # several range requests at once convert the file once
    results = await asyncio.gather(*(preview.browser_file(src, cache) for _ in range(3)))
    out, media_type = results[0]
    assert media_type == "audio/mpeg" and all(r == results[0] for r in results)
    assert out.parent == cache / "preview" and [p.name for p in out.parent.iterdir()] == [out.name]
    info = await ffmpeg.probe(out)
    assert info.codec == "mp3" and info.duration_s == pytest.approx(3.0, abs=0.2)
    assert await preview.browser_file(src, cache) == (out, "audio/mpeg")


async def test_old_previews_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "KEEP", 2)
    cache = tmp_path / "cache"
    outs = []
    for i in range(3):
        src = make_music(tmp_path / f"{i}" / "alac.m4a", title="x")
        outs.append((await preview.browser_file(src, cache))[0])
    assert sorted(p.name for p in (cache / "preview").iterdir()) == sorted(o.name for o in outs[1:])


async def test_not_media_raises(tmp_path):
    bad = tmp_path / "x.m4a"
    bad.write_bytes(b"nope")
    with pytest.raises(ffmpeg.FfmpegError):
        await preview.browser_file(bad, tmp_path / "cache")
