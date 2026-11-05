"""transcode processor: rule decisions (pure) and real ffmpeg conversions on synthetic media."""

from __future__ import annotations

from pathlib import Path

import mutagen
import pytest

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.infra import ffmpeg
from syncmusic.processors.transcode import Transcode, source_cap
from tests.media import make_music, media_work_item

IPOD = DeviceCapabilities(
    formats={"mp3", "aac", "alac", "wav"}, max_sample_rate=48000, max_bit_depth=16
)
ROCKBOX = DeviceCapabilities(formats={"mp3", "aac", "alac", "flac", "opus", "ogg", "wav"})
ANY = DeviceCapabilities()  # flash drive: formats unknown = plays everything


def tc(*rules, **settings) -> Transcode:
    return Transcode({"rules": list(rules), **settings}, None)


# --- decisions -----------------------------------------------------------------------------------


def test_not_supported_rule_and_passthrough():
    p = tc({"from": ["flac"], "to": "alac"})
    assert p.decide("flac", 44100, 16, IPOD).to == "alac"
    assert p.decide("flac", 44100, 16, ROCKBOX) is None  # Rockbox plays FLAC
    assert p.decide("mp3", 44100, None, IPOD) is None
    assert p.decide("flac", 44100, 16, ANY) is None


def test_first_match_wins_and_from_any():
    p = tc({"from": ["opus"], "to": "aac"}, {"to": "mp3"})
    assert p.decide("opus", 48000, None, IPOD).to == "aac"
    assert p.decide("flac", 44100, 16, IPOD).to == "mp3"  # empty `from` = any format


def test_always_wins_over_a_format_the_device_plays():
    """an explicit "Always" rule converts a format the device plays; "if needed" keeps it."""
    rule = {"from": ["mp3"], "to": "aac", "when": "always"}
    assert tc(rule).decide("mp3", 44100, None, IPOD).to == "aac"
    flac = DeviceCapabilities(formats={"flac", "mp3"})
    assert (
        tc({"from": ["flac"], "to": "mp3", "when": "always"}).decide("flac", 44100, 16, flac).to
        == "mp3"
    )
    assert (
        tc({"from": ["flac"], "to": "mp3", "when": "not_supported"}).decide("flac", 44100, 16, flac)
        is None
    )
    assert tc(rule).decide("mp3", 44100, None, DeviceCapabilities(formats={"aac"})).to == "aac"


def test_space_needed_only_when_enabled():
    p = tc({"from": ["alac", "flac"], "to": "aac", "when": "space_needed"})
    assert p.decide("alac", 44100, 16, IPOD) is None
    assert p.decide("alac", 44100, 16, IPOD, space_steps=True).to == "aac"


def test_copy_rule_keeps_file():
    assert tc({"from": ["flac"], "to": "copy"}).decide("flac", 44100, 16, ROCKBOX) is None


def test_fallback_for_unsupported_without_rule():
    assert tc().decide("flac", 44100, 16, IPOD).to == "alac"
    assert tc().decide("opus", 48000, None, IPOD).to == "aac"
    assert tc().decide("opus", 48000, None, DeviceCapabilities(formats={"mp3"})).to == "mp3"
    assert tc().decide("flac", 44100, 16, DeviceCapabilities(formats={"wma"})) is None


def test_device_limits():
    p = tc({"from": ["flac"], "to": "alac"})
    d = p.decide("flac", 96000, 24, IPOD)
    assert (d.to, d.sample_rate, d.bit_depth) == ("alac", 48000, 16)
    d = p.decide("flac", 88200, 24, IPOD)
    assert d.sample_rate == 44100  # same family
    d = tc().decide("alac", 96000, 24, IPOD)  # supported but over the limit: re-encode in place
    assert (d.to, d.sample_rate, d.bit_depth) == ("alac", 48000, 16)
    assert tc().decide("alac", 44100, 16, IPOD) is None
    d = p.decide("flac", 44100, 24, IPOD, space_steps=False)
    assert (d.sample_rate, d.bit_depth) == (None, 16)
    unlimited = tc({"from": ["flac"], "to": "alac"}, max_sample_rate=None, max_bit_depth=None)
    d = unlimited.decide("flac", 96000, 24, IPOD)
    assert (d.sample_rate, d.bit_depth) == (None, 24)
    assert tc().decide("flac", 96000, 24, ROCKBOX) is None  # no limits known


def test_suggest():
    slot = Transcode.suggest(IPOD)
    assert slot is not None and slot.type == "transcode"
    p = Transcode(slot.settings, None)
    assert p.decide("flac", 44100, 16, IPOD).to == "alac"
    assert p.decide("ogg", 44100, None, IPOD).to == "aac"
    assert Transcode.suggest(ROCKBOX) is None
    assert Transcode.suggest(ANY) is None


def test_settings_validation():
    with pytest.raises(ValueError):
        tc({"from": ["flac"], "to": "wma"})
    with pytest.raises(ValueError):
        tc({"form": ["flac"], "to": "aac"})


def test_cache_key_depends_on_planned_output(tmp_path):
    p = tc({"from": ["flac"], "to": "alac"})
    item = media_work_item(
        make_music(tmp_path / "a.flac", sample_rate=96000, bit_depth=24), tmp_path
    )
    k_ipod = p.cache_key(p.plan(item.plan, IPOD))
    k_rockbox = p.cache_key(p.plan(item.plan, ROCKBOX))
    assert k_ipod != k_rockbox


# --- conversions -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "codec", "ext"),
    [
        ({"to": "alac"}, "alac", "m4a"),
        ({"to": "aac", "quality": {"bitrate_kbps": 192}}, "aac", "m4a"),
        ({"to": "mp3", "quality": {"bitrate_kbps": 192}}, "mp3", "mp3"),
        ({"to": "mp3", "quality": {"vbr": 2}}, "mp3", "mp3"),
        ({"to": "opus", "quality": {"bitrate_kbps": 128}}, "opus", "opus"),
        ({"to": "ogg"}, "vorbis", "ogg"),
    ],
)
async def test_flac_conversions_keep_tags(tmp_path, rule, codec, ext):
    src = make_music(
        tmp_path / "src" / "Зірка на ймення Сонце.flac",
        cover=True,
        title="Зірка на ймення Сонце",
        artist="Скрябін",
        album="Зірка на ймення Сонце",
        tracknumber="1",
        date="1989",
    )
    p = tc({"from": ["flac"], "when": "always", **rule})
    item = media_work_item(src, tmp_path / "work")
    planned = p.plan(item.plan, ROCKBOX)
    out = await p.run(item.model_copy(update={"plan": planned}), ROCKBOX)
    assert out.path != src and out.path.suffix == f".{ext}" and out.path.parent == item.work_dir
    assert src.exists()  # the source (maybe a library original) is never touched
    info = await ffmpeg.probe(out.path)
    assert info.codec == codec
    assert info.duration_s == pytest.approx(3.0, abs=0.1)
    assert info.tags["title"] == "Зірка на ймення Сонце"
    assert info.tags["artist"] == "Скрябін"
    assert info.tags["album"] == "Зірка на ймення Сонце"
    assert info.has_cover  # every target keeps the cover, Ogg too (METADATA_BLOCK_PICTURE)
    assert out.steps_done == ["transcode"]
    assert out.notes["encoder"] in (
        "alac",
        "aac",
        "libfdk_aac",
        "libmp3lame",
        "libopus",
        "libvorbis",
    )
    assert out.plan.file.format == planned.file.format
    assert out.plan.file.ext == ext
    assert out.plan.file.sample_rate == planned.file.sample_rate
    assert out.plan.file.size == out.path.stat().st_size
    tolerance = 0.08 if codec == "alac" else 0.10  # pink noise; test media: 4.8
    if rule.get("quality", {}).get("vbr") is None and codec not in ("vorbis", "opus"):  # CBR only
        assert abs(planned.file.size - out.plan.file.size) <= tolerance * out.plan.file.size
    mutagen_tags = mutagen.File(out.path, easy=True)
    assert mutagen_tags is not None and "Скрябін" in str(mutagen_tags.tags)


async def test_hi_res_flac_to_ipod_limits(tmp_path):
    src = make_music(tmp_path / "hr.flac", sample_rate=96000, bit_depth=24, title="HR")
    p = tc({"from": ["flac"], "to": "alac"})
    item = media_work_item(src, tmp_path / "work")
    planned = p.plan(item.plan, IPOD)
    assert (planned.file.sample_rate, planned.file.bit_depth) == (48000, 16)
    out = await p.run(item, IPOD)
    info = await ffmpeg.probe(out.path)
    assert (info.codec, info.sample_rate, info.bit_depth) == ("alac", 48000, 16)
    assert (out.plan.file.sample_rate, out.plan.file.bit_depth) == (48000, 16)
    # the plan may only err high: 24 → 16 bit scales the FLAC size by 16/24, but the 8 dropped bits of noise
    # did not compress at all, so the result is ~15 % smaller; a plan below the result could overfill a device
    actual = out.path.stat().st_size
    assert 0.95 * actual <= planned.file.size <= 1.20 * actual, (planned.file.size, actual)


async def test_supported_input_passes_through(tmp_path):
    src = make_music(tmp_path / "a.mp3", title="a")
    item = media_work_item(src, tmp_path / "work")
    p = tc({"from": ["flac"], "to": "alac"})
    assert p.plan(item.plan, IPOD) == item.plan
    assert await p.run(item, IPOD) == item


async def test_run_trusts_the_file_over_metadata(tmp_path):
    # the library said "mp3" but the file is FLAC: run converts anyway
    src = make_music(tmp_path / "a.flac", title="a")
    item = media_work_item(src, tmp_path / "work")
    lying = item.plan.model_copy(
        update={"file": item.plan.file.model_copy(update={"format": "mp3"})}
    )
    item = item.model_copy(update={"plan": lying})
    out = await tc().run(item, IPOD)
    assert (await ffmpeg.probe(out.path)).codec == "alac"


async def test_failed_conversion_leaves_no_output(tmp_path, monkeypatch):
    src = make_music(tmp_path / "a.flac", title="a")
    item = media_work_item(src, tmp_path / "work")

    async def boom(args, **kw):
        Path(args[-1]).write_bytes(b"partial")  # noqa: ASYNC240
        raise ffmpeg.FfmpegError(["ffmpeg"], 1, "boom")

    monkeypatch.setattr(ffmpeg, "run_ffmpeg", boom)
    monkeypatch.setattr(ffmpeg, "run_ffmpeg_measured", boom)  # music is measured in the same pass
    with pytest.raises(ffmpeg.FfmpegError):
        await tc().run(item, IPOD)
    assert list(item.work_dir.iterdir()) == []


async def test_hi_res_flac_resampled_in_place_keeps_24_bit(tmp_path):
    caps = ROCKBOX.model_copy(update={"max_sample_rate": 48000})
    src = make_music(tmp_path / "hr.flac", sample_rate=96000, bit_depth=24, title="HR")
    out = await tc().run(media_work_item(src, tmp_path / "work"), caps)
    info = await ffmpeg.probe(out.path)
    assert (info.codec, info.sample_rate, info.bit_depth) == ("flac", 48000, 24)
    assert out.path.name == "hr.transcode.flac"


async def test_lossy_source_bitrate_caps_target(tmp_path):
    """A 96 kbit/s Opus going to an MP3-only player must not be blown up to the 320k default."""
    mp3_only = DeviceCapabilities(formats={"mp3"})
    src = make_music(tmp_path / "src" / "talk.opus", seconds=10.0)
    item = media_work_item(src, tmp_path / "work")
    p = tc()
    planned = p.plan(item.plan, mp3_only)
    assert planned.file.format == "mp3"
    assert planned.file.bitrate_kbps is not None and planned.file.bitrate_kbps <= 160
    out = await p.run(item, mp3_only)
    info = await ffmpeg.probe(out.path)
    assert info.codec == "mp3" and (info.bitrate or 0) <= 170_000


@pytest.mark.parametrize(
    ("fmt", "kbps", "want"),
    [
        ("aac", 128, 128),
        ("aac", 129, 128),
        ("aac", 135, 128),
        ("opus", 96, 96),
        ("mp3", 150, 160),
        ("aac", 500, None),
        ("flac", 900, None),
        ("aac", None, None),
        ("aac", 0, None),
    ],
)
def test_source_cap(fmt, kbps, want):
    assert source_cap(fmt, kbps) == want


def _plan_kbps(p: Transcode, fmt: str, kbps: int | None, caps=None):
    d = p.decide(fmt, 44100, None, caps or DeviceCapabilities(formats={"mp3"}), source_kbps=kbps)
    assert d is not None
    return p._kbps(d)


def test_cap_applies_to_default_and_explicit_bitrate_not_vbr():
    assert _plan_kbps(tc(), "aac", 128) == 128
    assert (
        _plan_kbps(
            tc({"to": "mp3", "when": "always", "quality": {"bitrate_kbps": 320}}), "ogg", 112
        )
        == 112
    )
    assert (
        _plan_kbps(tc({"to": "mp3", "when": "always", "quality": {"bitrate_kbps": 96}}), "ogg", 192)
        == 96
    )
    assert _plan_kbps(tc({"to": "mp3", "when": "always", "quality": {"vbr": 2}}), "aac", 64) == 190
    assert _plan_kbps(tc(cap_to_source=False), "aac", 128) == 320
    assert _plan_kbps(tc(), "flac", 900) == 320


def test_default_mp3_quality_is_cbr_320k(tmp_path):
    """MP3 without an explicit quality is CBR 320k."""
    p = tc({"from": ["flac"], "to": "mp3"})
    d = p.decide("flac", 44100, 16, DeviceCapabilities(formats={"mp3"}))
    assert d is not None and d.to == "mp3"
    args, _ = p._args(d, tmp_path / "a.flac", tmp_path / "a.mp3")
    assert args[args.index("-b:a") + 1] == "320k"
    assert "-q:a" not in args


def test_plan_cap_falls_back_to_size_over_duration(tmp_path):
    """No bitrate from the library (Opus via mutagen): the cap comes from size / duration."""
    src = make_music(tmp_path / "src" / "talk.opus", seconds=10.0)
    item = media_work_item(src, tmp_path / "work")
    plan = item.plan.model_copy(
        update={"file": item.plan.file.model_copy(update={"bitrate_kbps": None})}
    )
    planned = tc().plan(plan, DeviceCapabilities(formats={"mp3"}))
    assert planned.file.bitrate_kbps is not None and planned.file.bitrate_kbps <= 128


async def test_truncated_source_fails_and_leaves_no_output(tmp_path, monkeypatch):
    from syncmusic.processors.transcode import CorruptSource, TruncatedSource

    src = make_music(tmp_path / "src" / "full.flac", seconds=10.0, title="t")
    item = media_work_item(src, tmp_path / "work")
    assert item.track.duration_s and item.track.duration_s > 9.5
    half = tmp_path / "src" / "half.flac"
    data = src.read_bytes()
    half.write_bytes(data[: len(data) // 2])  # STREAMINFO still says 10 s
    item = item.model_copy(update={"path": half})
    p = tc({"from": ["flac"], "to": "alac", "when": "always"})
    with pytest.raises(CorruptSource):  # the cut frame is a decode error
        await p.run(item, IPOD)
    monkeypatch.setattr(ffmpeg, "decode_errors", lambda stderr: [])  # a cut on a frame boundary
    with pytest.raises(TruncatedSource, match="truncated"):
        await p.run(item, IPOD)
    monkeypatch.undo()
    assert not list((tmp_path / "work").glob("*.transcode.*"))
    ok = await p.run(item.model_copy(update={"path": src}), IPOD)  # the intact file converts fine
    assert ok.path.suffix == ".m4a"


async def test_corrupt_block_fails_item(tmp_path):
    from syncmusic.processors.transcode import CorruptSource

    src = make_music(tmp_path / "src" / "full.flac", seconds=10.0, title="t")
    bad = tmp_path / "src" / "bad.flac"
    data = bytearray(src.read_bytes())
    mid = len(data) // 2
    # flipped bits (ffmpeg 6 skips a zeroed block without a word)
    for i in range(mid, mid + 50_000, 500):
        data[i] ^= 0xFF
    bad.write_bytes(bytes(data))
    item = media_work_item(bad, tmp_path / "work")
    p = tc({"from": ["flac"], "to": "alac", "when": "always"})
    with pytest.raises(CorruptSource, match="damaged audio data"):
        await p.run(item, IPOD)
    assert not list((tmp_path / "work").glob("*.transcode.*"))
    ok = await p.run(media_work_item(src, tmp_path / "work"), IPOD)
    assert "ffmpeg_messages" not in ok.notes


def test_decode_errors_filter():
    err = "[flac @ 0x1] invalid subframe padding\n[flac @ 0x1] decode_frame() failed\nsome muxer note\n"
    assert ffmpeg.decode_errors(err) == ["[flac @ 0x1] decode_frame() failed"]
    assert ffmpeg.decode_errors("") == []


def test_above_target_only_converts_lossless_and_lossy_above_the_target():
    """transcode to fit (MP3 320): lossless always, lossy only above 320k; the rest keeps its file,
    also on a device that plays it; an unknown bitrate is copied."""
    p = tc(
        {
            "from": [],
            "to": "mp3",
            "when": "always",
            "quality": {"bitrate_kbps": 320},
            "above_target_only": True,
        }
    )
    assert p.decide("flac", 44100, 16, IPOD).to == "mp3"
    assert p.decide("alac", 44100, 16, IPOD, source_kbps=900).to == "mp3"
    assert p.decide("mp3", 44100, None, IPOD, source_kbps=320) is None
    assert (
        p.decide("mp3", 44100, None, IPOD, source_kbps=331) is None
    )  # VBR overshoot of a 320k file
    assert p.decide("mp3", 44100, None, IPOD, source_kbps=192) is None
    assert p.decide("aac", 44100, None, IPOD, source_kbps=256) is None
    assert p.decide("wav", 44100, 16, IPOD).to == "mp3"
    assert p.decide("mp3", 44100, None, IPOD) is None  # unknown bitrate: not known to be above
    p192 = tc(
        {"to": "mp3", "when": "always", "quality": {"bitrate_kbps": 192}, "above_target_only": True}
    )
    d = p192.decide("mp3", 44100, None, IPOD, source_kbps=320)
    assert d.to == "mp3" and p192._kbps(d) == 192
    assert p192.decide("aac", 44100, None, IPOD, source_kbps=128) is None
    # the next rules still apply to what the fit rule leaves (an unsupported format is converted anyway)
    both = tc(
        {
            "to": "mp3",
            "when": "always",
            "quality": {"bitrate_kbps": 320},
            "above_target_only": True,
        },
        {"from": ["opus"], "to": "aac"},
    )
    assert both.decide("opus", 48000, None, IPOD, source_kbps=96).to == "aac"


def test_copy_rule_never_keeps_an_unplayable_file():
    """A `copy` rule only keeps formats the device plays; the others are converted."""
    p = tc({"to": "copy"})
    assert p.decide("flac", 44100, 16, ROCKBOX) is None
    assert p.decide("dsf", 2822400, 1, ROCKBOX).to == "aac"
    assert p.decide("wma", 44100, None, IPOD).to == "aac"


def test_every_chain_converts_what_the_device_cannot_play():
    """Built-in templates, chains without a convert step and the empty API chain all
    convert an unplayable format instead of leaving the item off."""
    from syncmusic.core.models import ItemRef, ProcessorSlot, Track
    from syncmusic.core.prepare import Chain, initial_plan_item
    from syncmusic.core.proc_templates import builtin_processor_templates, with_tag_step

    def plan_item(fmt, sample_rate=44100, bit_depth=16):
        t = Track(
            ref=ItemRef(library_id="l", item_id=fmt),
            title="t",
            format=fmt,
            size_bytes=30_000_000,
            duration_s=240,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
        )
        return initial_plan_item(t)

    no_opus = DeviceCapabilities(formats={"mp3", "aac", "flac", "alac"})
    chains = [t.processors for t in builtin_processor_templates().values()]
    chains += [with_tag_step([]), with_tag_step([ProcessorSlot(type="transcode", enabled=False)])]
    for slots in chains:
        chain = Chain.from_slots(slots)
        for fmt in ("opus", "dsf"):
            planned = chain.plan(plan_item(fmt), no_opus)
            assert planned.file.format in no_opus.formats, (slots, fmt)
    # a file it plays stays as it is in a copy chain (no limits in the fallback step)
    copy = Chain.from_slots(builtin_processor_templates()["builtin-rockbox-copy"].processors)
    hi_res = plan_item("flac", sample_rate=192000, bit_depth=24)
    kept = copy.plan(hi_res, no_opus).file
    assert (kept.format, kept.sample_rate, kept.bit_depth) == ("flac", 192000, 24)


async def test_music_is_measured_for_replaygain_in_the_transcode_pass(tmp_path, monkeypatch):
    """the transcode's ffmpeg pass measures ReplayGain on an asplit branch; the encoded audio is the
    same as without it, and retag uses the value instead of decoding the file again."""
    import subprocess

    from syncmusic.processors.retag import Retag

    src = make_music(tmp_path / "a.flac", title="a", sample_rate=96000, bit_depth=24)
    out = await tc().run(media_work_item(src, tmp_path / "work"), IPOD)
    gain, peak = out.notes["replaygain_measured"]
    assert -30 < gain < 30 and 0 < peak <= 1.5
    # the same decision without the measuring branch gives the same audio
    item = media_work_item(src, tmp_path / "work2")
    info = await ffmpeg.probe(src)
    d = tc().decide(
        "flac", info.sample_rate, info.bit_depth, IPOD, space_steps=item.plan.enable_space_steps
    )
    ref = tmp_path / "ref.m4a"
    await ffmpeg.run_ffmpeg(tc()._args(d, src, ref, False)[0])

    def md5(p):
        cmd = [ffmpeg.FFMPEG, "-v", "error", "-i", str(p), "-map", "0:a", "-f", "md5", "-"]
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

    assert md5(out.path) == md5(ref)
    standalone = await ffmpeg.replaygain(out.path)
    # measured on the source, before resampling: within a fraction of a dB of the output's value
    assert standalone is not None and abs(standalone[0] - gain) < 0.5

    async def no_second_decode(*a, **kw):
        raise AssertionError("measured again")

    monkeypatch.setattr(ffmpeg, "replaygain", no_second_decode)
    tagged = await Retag({"replaygain": "missing"}, None).run(out, IPOD)
    assert tagged.notes["replaygain"] == f"{gain:+.2f} dB"
