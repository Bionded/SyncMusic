"""ReplayGain in the retag step: measured once per library item, written into the copy only."""

from __future__ import annotations

from types import SimpleNamespace

import mutagen
import pytest
from mutagen.id3 import ID3, TXXX

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core.models import MediaKind
from syncmusic.infra import ffmpeg
from syncmusic.infra.db import Database
from syncmusic.processors.retag import Retag
from tests.media import make_music, media_work_item

CAPS = DeviceCapabilities(formats={"mp3", "aac", "alac", "flac", "ogg", "opus"})


@pytest.fixture
async def ctx():
    db = Database(":memory:")
    await db.open()
    yield SimpleNamespace(db=db)
    await db.close()


def item_for(tmp_path, name: str, state: str = "size=1;mtime=1", **track):
    src = make_music(tmp_path / "src" / name, title="Ой у лузі червона калина")
    item = media_work_item(src, tmp_path / "work" / state.replace(";", "_"))
    t = item.track.model_copy(update={"source_state": state, **track})
    return item.model_copy(update={"plan": item.plan.model_copy(update={"track": t})})


def gain_of(path) -> str | None:
    """The track gain tag of a file, whatever the tag format and spelling."""
    audio = mutagen.File(path)
    for key, value in (audio.tags or {}).items():
        if "replaygain_track_gain" in str(key).casefold():
            v = value if isinstance(value, list) else getattr(value, "text", [value])
            first = v[0]
            return first.decode() if isinstance(first, bytes) else str(first)
    return None


async def run(item, settings=None, ctx=None):
    before = item.path.read_bytes()
    out = await Retag(settings or {}, ctx).run(item, CAPS)
    assert item.path.read_bytes() == before  # the library original is never changed
    return out


@pytest.mark.parametrize("name", ["a.mp3", "a.flac", "a.m4a", "a.opus"])
async def test_missing_gain_is_measured_and_written(tmp_path, ctx, name):
    item = item_for(tmp_path, name)
    expected = await ffmpeg.replaygain(item.path)
    assert expected is not None
    out = await run(item, ctx=ctx)
    text = gain_of(out.path)
    assert text is not None and text.endswith(" dB")
    assert abs(float(text.removesuffix(" dB")) - expected[0]) < 0.5
    assert out.notes["replaygain"] == text
    row = await ctx.db.fetchone("SELECT gain_db, peak FROM replaygain")
    assert row is not None and abs(row["gain_db"] - expected[0]) < 0.01


async def test_measured_once_per_source_state(tmp_path, ctx, monkeypatch):
    first = await run(item_for(tmp_path, "a.mp3"), ctx=ctx)
    calls = []

    async def counting(path, **kw):
        calls.append(path)
        return (-3.0, 0.5)

    monkeypatch.setattr(ffmpeg, "replaygain", counting)
    again = await run(item_for(tmp_path, "a.mp3"), ctx=ctx)
    assert calls == [] and gain_of(again.path) == gain_of(first.path)  # from the cache
    changed = await run(item_for(tmp_path, "a.mp3", state="size=2;mtime=2"), ctx=ctx)
    # the source changed: measured again
    assert len(calls) == 1 and gain_of(changed.path) == "-3.00 dB"


async def test_file_with_gain_tag_is_left_alone_unless_always(tmp_path, ctx, monkeypatch):
    item = item_for(tmp_path, "a.mp3")
    id3 = ID3(item.path)
    id3.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=["-7.77 dB"]))
    id3.save(item.path)

    async def fake(path, **kw):
        return (-1.5, 0.25)

    monkeypatch.setattr(ffmpeg, "replaygain", fake)
    kept = await run(item, ctx=ctx)
    assert gain_of(kept.path) == "-7.77 dB" and "replaygain" not in kept.notes
    replaced = await run(item, {"replaygain": "always"}, ctx=ctx)
    frames = [f for f in ID3(replaced.path).getall("TXXX") if "track_gain" in f.desc.casefold()]
    assert [f.text[0] for f in frames] == ["-1.50 dB"]  # one value, the old spelling is gone


async def test_off_and_non_music_are_not_measured(tmp_path, monkeypatch):
    async def boom(path, **kw):
        raise AssertionError("must not be measured")

    monkeypatch.setattr(ffmpeg, "replaygain", boom)
    off = await run(item_for(tmp_path, "a.flac"), {"replaygain": "off"})
    assert gain_of(off.path) is None
    podcast = await run(item_for(tmp_path, "b.flac", kind=MediaKind.PODCAST))
    assert gain_of(podcast.path) is None


async def test_measure_failure_syncs_without_gain(tmp_path, monkeypatch, caplog):
    async def fails(path, **kw):
        raise ffmpeg.FfmpegError(["ffmpeg"], 1, "boom")

    monkeypatch.setattr(ffmpeg, "replaygain", fails)
    out = await run(item_for(tmp_path, "a.mp3"))  # no ctx: nothing cached, still works
    assert gain_of(out.path) is None and out.steps_done == ["retag"]
    assert "Ой у лузі червона калина" in caplog.text


async def test_louder_file_gets_lower_gain(tmp_path):
    quiet = await ffmpeg.replaygain(make_music(tmp_path / "q.flac", seconds=2))
    loud = tmp_path / "l.flac"
    await ffmpeg.run_ffmpeg(["-i", str(tmp_path / "q.flac"), "-af", "volume=10dB", str(loud)])
    measured = await ffmpeg.replaygain(loud)
    assert quiet is not None and measured is not None
    assert 9 < quiet[0] - measured[0] < 11
