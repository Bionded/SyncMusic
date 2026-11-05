"""Quality profiles: each yields a valid chain for every device type; choosing one replaces the
chain and detaches a processor template; editing the chain by hand makes it Custom."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import json
import subprocess

import pytest

from syncmusic.core import quality, registry
from syncmusic.core.sync import suggested_processors
from tests.media import make_music
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401


@pytest.mark.parametrize("device_type", ["ipod", "rockbox", "flashdrive", "ftp"])
@pytest.mark.parametrize("profile", quality.PROFILES)
def test_every_profile_is_a_valid_chain(device_type, profile):
    caps = registry.device_class(device_type).capabilities
    slots = quality.chain(profile, caps, suggested_processors(caps))
    types = [s.type for s in slots]
    assert "retag" in types  # the tag step is in every chain
    for s in slots:
        registry.processor_class(s.type).Settings.model_validate(s.settings)
    rules = next((s.settings.get("rules", []) for s in slots if s.type == "transcode"), [])
    targets = {r["to"] for r in rules}
    assert targets <= caps.formats | {"mp3"}
    if profile == "weak":
        assert targets == {"mp3"} and rules[0]["quality"] == {"bitrate_kbps": 192}


def test_ipod_profiles():
    caps = registry.device_class("ipod").capabilities
    sug = suggested_processors(caps)
    mx = quality.chain("max", caps, sug)[0].settings["rules"]
    assert mx[0]["to"] == "alac" and "flac" in mx[0]["from"]
    space = quality.chain("space", caps, sug)[0].settings["rules"]
    assert space[0] == {
        "from": quality.LOSSLESS_SOURCES,
        "to": "mp3",
        "when": "always",
        "quality": {"vbr": 2},
    }


async def test_choose_a_profile_then_edit_by_hand(env, tmp_path):
    _app, c = env
    dev = await _register(c, tmp_path / "flash", name="Flash")
    profiles = (await c.get(f"/api/v1/devices/{dev['id']}/quality-profiles")).json()
    assert [p["id"] for p in profiles] == ["max", "space", "weak"]
    r = await c.patch(
        f"/api/v1/devices/{dev['id']}", json={"processor_template_id": "builtin-flashdrive"}
    )
    assert r.json()["processor_template_id"] == "builtin-flashdrive"
    cfg = (await c.patch(f"/api/v1/devices/{dev['id']}", json={"quality_profile": "weak"})).json()
    assert cfg["quality_profile"] == "weak" and cfg["processor_template_id"] is None
    weak = next(p for p in profiles if p["id"] == "weak")["processors"]
    assert cfg["processors"] == weak
    edited = [*cfg["processors"]]
    edited[0] = {**edited[0], "enabled": False}
    cfg = (await c.patch(f"/api/v1/devices/{dev['id']}", json={"processors": edited})).json()
    assert cfg["quality_profile"] is None  # Custom


@pytest.mark.parametrize("device_type", ["rockbox", "flashdrive"])
def test_weak_profile_converts_lossy_sources_above_192(device_type):
    """'For a weak player' promises 'Everything becomes MP3 192 kbit/s': an MP3 320k source is converted too, not
    kept because its format already is MP3: Rockbox kept the 320k MP3s)."""
    from syncmusic.processors.transcode import Transcode

    caps = registry.device_class(device_type).capabilities
    slot = next(
        s for s in quality.chain("weak", caps, suggested_processors(caps)) if s.type == "transcode"
    )
    t = Transcode(slot.settings, None)
    d = t.decide("mp3", 44100, None, caps, source_kbps=320)
    assert d is not None and d.to == "mp3" and d.quality.bitrate_kbps == 192
    assert t.decide("mp3", 44100, None, caps, source_kbps=192) is None  # at the target: kept
    assert (
        t.decide("mp3", 44100, None, caps, source_kbps=128) is None
    )  # below it: kept, not inflated


def _kbps(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=bit_rate", "-of", "json", str(path)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return int(json.loads(out)["format"]["bit_rate"]) // 1000


def _mp3_320(wav, dest):
    dest.parent.mkdir(parents=True)
    tags = [x for k in ("title", "artist", "album") for x in ("-metadata", f"{k}=Гучно")]
    cmd = ["ffmpeg", "-v", "error", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "320k"]
    subprocess.run([*cmd, *tags, str(dest)], check=True)
    return dest


async def test_weak_profile_sync_turns_a_320k_mp3_into_192k(env, music, tmp_path):
    """A 320k MP3 arrives on a folder device as MP3 192k, a 192k one as it is."""
    app, c = env
    wav = make_music(tmp_path / "src" / "loud.wav", seconds=3.0)
    loud = _mp3_320(wav, music / "Гучно" / "Гучно.mp3")
    assert _kbps(loud) >= 300
    r = await c.post("/api/v1/libraries/music/refresh")
    await app.jobs.wait(r.json()["id"])
    dev = await _register(c, tmp_path / "flash", name="Flash")
    await c.patch(f"/api/v1/devices/{dev['id']}", json={"quality_profile": "weak"})
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    out = {p.stem: _kbps(p) for p in (tmp_path / "flash").rglob("*.mp3")}
    loud_out = next(v for k, v in out.items() if "Гучно" in k)
    assert 185 <= loud_out <= 200, out
    assert all(185 <= v <= 200 for v in out.values()), out  # the 192k sources stay 192k
