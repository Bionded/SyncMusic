"""Device profile resolution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from syncmusic.base.capabilities import CoverSpec, DeviceCapabilities
from syncmusic.core import profile as pf

DEFAULTS = DeviceCapabilities(
    formats={"mp3", "flac"}, cover=CoverSpec(max_size=300), max_sample_rate=None
)


def test_every_database_entry_is_valid_and_resolves():
    db = pf.known_models()
    assert {"ipod", "rockbox", "flashdrive", "ftp"} <= set(db)
    for type_name, models in db.items():
        assert pf.GENERIC in models, type_name
        for model, entry in models.items():
            prof = pf.resolve_profile(type_name, model, DEFAULTS)
            assert prof.label == entry.label
            assert prof.known == (model != pf.GENERIC)
            for name, value in entry.values.items():
                assert prof.fields[name].value == value
                assert prof.fields[name].source == "known_model"
            pf.apply_profile(DEFAULTS, prof)  # applies cleanly


def test_known_model_values():
    rb = pf.resolve_profile("rockbox", "ipodmini2g", DEFAULTS)
    assert rb.values("known_model") == {
        "cover_size": 100,
        "max_sample_rate": 48000,
        "max_bit_depth": 16,
        "prefer_lossy": True,
    }
    # the native and the hosted Rockbox build of the EROS Q are the same player
    for target in ("erosqnative", "aigoerosq"):
        eros = pf.resolve_profile("rockbox", target, DEFAULTS)
        assert eros.known and eros.label.startswith("AIGO EROS Q")
        assert eros.fields["prefer_lossy"].value is False and eros.fields["cover_size"].value == 200
    caps = pf.apply_profile(DEFAULTS, rb)
    assert caps.max_sample_rate == 48000 and caps.cover and caps.cover.max_size == 100
    # model ids are case-insensitive
    shuffle = pf.apply_profile(DEFAULTS, pf.resolve_profile("ipod", "SHUFFLE_2", DEFAULTS))
    assert shuffle.cover is None and not shuffle.supports_playlists


def test_unknown_model_falls_back_to_generic():
    prof = pf.resolve_profile("rockbox", "someplayer9000", DEFAULTS)
    assert not prof.known and prof.model == "someplayer9000"
    assert prof.label == pf.known_models()["rockbox"]["generic"].label
    assert all(f.source == "default" for f in prof.fields.values())
    assert prof.fields["formats"].value == {"mp3", "flac"}
    assert prof.fields["cover_size"].value == 300
    none = pf.resolve_profile("ipod", None, DEFAULTS)
    assert not none.known and none.label == pf.known_models()["ipod"]["generic"].label
    assert pf.resolve_profile("notatype", "x", DEFAULTS).label is None


def test_detection_beats_the_database_and_an_override_beats_both():
    prof = pf.resolve_profile(
        "rockbox",
        "ipodmini2g",
        DEFAULTS,
        detected={"formats": {"mp3"}, "has_rtc": True, "max_sample_rate": 44100},
        overrides={"max_sample_rate": 96000, "cover_size": None, "lyrics": "none"},
    )
    f = prof.fields
    assert (f["formats"].value, f["formats"].source) == ({"mp3"}, "detected")
    assert (f["has_rtc"].value, f["has_rtc"].source) == (True, "detected")
    assert (f["max_sample_rate"].value, f["max_sample_rate"].source) == (96000, "override")
    assert (f["cover_size"].value, f["cover_size"].source) == (None, "override")
    assert (f["max_bit_depth"].value, f["max_bit_depth"].source) == (16, "known_model")
    assert (f["max_tracks"].value, f["max_tracks"].source) == (None, "default")
    assert "lyrics" not in f  # not a profile field
    caps = pf.apply_profile(DEFAULTS, prof)
    assert caps.formats == {"mp3"} and caps.max_sample_rate == 96000 and caps.cover is None


def test_cover_size_override():
    assert DEFAULTS.with_overrides({"cover_size": 120}).cover == CoverSpec(max_size=120)
    added = DeviceCapabilities().with_overrides({"cover_size": 200})
    assert added.cover is not None and added.cover.max_size == 200
    assert DEFAULTS.with_overrides({"cover_size": None}).cover is None
    with pytest.raises(ValidationError):
        DEFAULTS.with_overrides({"cover_size": "big"})


def test_database_check_rejects_unknown_fields():
    with pytest.raises(ValueError, match="unknown fields"):
        pf._check("rockbox", "x", {"max_samplerate": 48000})
    with pytest.raises(ValidationError):
        pf._check("rockbox", "x", {"max_tracks": "many"})


# --- devices, transcode and planner use the profile ----------------------------------------------


def _rockbox(mount, overrides=None, **settings):
    from syncmusic.core.models import DeviceConfig
    from syncmusic.devices.Rockbox.rockbox import Rockbox

    cfg = DeviceConfig(
        id="rb", type="rockbox", name="RB", settings=settings, capability_overrides=overrides or {}
    )
    return Rockbox(cfg, mount, None)


def test_rockbox_profile_sources(tmp_path):
    from tests.contracts.harness import rockbox_tree

    mount = rockbox_tree(tmp_path / "rb")  # a mini 2G: Target ipodmini2g, Features with rtc
    prof = _rockbox(mount).profile()
    assert prof.model == "ipodmini2g" and prof.known
    f = prof.fields
    assert (f["has_rtc"].value, f["has_rtc"].source) == (True, "detected")
    assert (f["max_sample_rate"].value, f["max_sample_rate"].source) == (48000, "known_model")
    assert f["formats"].source == "default"  # no codecs folder in the fixture
    caps = _rockbox(mount).effective_capabilities()
    assert caps.max_sample_rate == 48000 and caps.max_bit_depth == 16 and caps.prefer_lossy
    assert caps.cover is not None and caps.cover.max_size == 100
    assert caps.cover.sidecar_name == "cover.jpg"  # the class's cover spec, resized

    over = _rockbox(mount, {"max_sample_rate": 96000, "cover_size": None}, formats=["mp3"])
    f = over.profile().fields
    assert (f["max_sample_rate"].value, f["max_sample_rate"].source) == (96000, "override")
    assert (f["formats"].value, f["formats"].source) == ({"mp3"}, "setting")  # Rockbox setting
    caps = over.effective_capabilities()
    assert caps.max_sample_rate == 96000 and caps.cover is None and caps.formats == {"mp3"}


def test_ipod_profile_sources(tmp_path):
    from syncmusic.core.models import DeviceConfig
    from syncmusic.devices.iPod.ipod import IPod
    from tests.contracts.harness import ipod_tree

    mount = ipod_tree(tmp_path / "classic")
    prof = IPod(DeviceConfig(id="i", type="ipod", name="iPod"), mount, None).profile()
    assert prof.model == "classic_3" and prof.label == "iPod classic (7th generation)"
    assert prof.fields["formats"].source == "detected" and "alac" in prof.fields["formats"].value
    assert prof.fields["cover_size"].source == "detected"
    off = IPod(
        DeviceConfig(id="i", type="ipod", name="iPod", settings={"covers": False}), mount, None
    )
    assert off.profile().fields["cover_size"].source == "setting"
    assert off.effective_capabilities().cover is None


def test_flashdrive_profile_is_generic_with_setting_overrides(tmp_path):
    from syncmusic.core.models import DeviceConfig
    from syncmusic.devices.flashdrive import FlashDrive

    plain = FlashDrive(DeviceConfig(id="f", type="flashdrive", name="F"), tmp_path, None).profile()
    assert plain.label == "USB flash drive" and not plain.known
    assert all(f.source == "default" for f in plain.fields.values())
    cfg = DeviceConfig(
        id="f",
        type="flashdrive",
        name="F",
        settings={"formats": ["mp3"], "playlists": False},
        capability_overrides={"max_tracks": 500},
    )
    f = FlashDrive(cfg, tmp_path, None).profile().fields
    assert {n for n, v in f.items() if v.source == "setting"} == {"formats", "supports_playlists"}
    assert f["max_tracks"].source == "override"


def test_transcode_resamples_hires_for_a_limited_profile(tmp_path):
    from syncmusic.processors.transcode import Transcode
    from tests.contracts.harness import rockbox_tree

    caps = _rockbox(rockbox_tree(tmp_path / "rb")).effective_capabilities()
    d = Transcode({"rules": []}, None).decide("flac", 96000, 24, caps)
    assert d is not None and (d.to, d.sample_rate, d.bit_depth) == ("flac", 48000, 16)
    eros = tmp_path / "eros"
    (eros / ".rockbox").mkdir(parents=True)
    (eros / ".rockbox" / "rockbox-info.txt").write_text("Target: erosqnative\nFeatures: rtc\n")
    fast = _rockbox(eros).effective_capabilities()
    assert Transcode({"rules": []}, None).decide("flac", 96000, 24, fast) is None
    assert not fast.prefer_lossy


def test_planner_trims_to_the_players_track_limit():
    from syncmusic.core.models import DeviceConfig, Rule, RuleSet
    from syncmusic.core.planner import plan_sync
    from syncmusic.core.prepare import Chain
    from tests.factories import NOW, snapshot, track

    tracks = [track(str(i), artist=chr(64 + i)) for i in range(1, 7)]

    def plan(caps, **rs):
        dev = DeviceConfig(
            id="d",
            type="fake",
            name="D",
            library_ids=["lib"],
            rule_set=RuleSet(rules=[Rule(id="all", criteria={"all": []})], **rs),
        )
        return plan_sync(dev, {"lib": snapshot(tracks)}, None, caps, Chain([]), now=NOW)

    p = plan(DeviceCapabilities(formats={"mp3"}, max_tracks=4))
    assert sum(a.kind == "add" for a in p.actions) == 4
    assert any("4 tracks - the most this player handles" in w for w in p.warnings)
    p = plan(DeviceCapabilities(formats={"mp3"}, max_tracks=4), limit_tracks=2)
    assert sum(a.kind == "add" for a in p.actions) == 2
    assert not any("the most this player handles" in w for w in p.warnings)
    assert not plan(DeviceCapabilities(formats={"mp3"}, max_tracks=10)).warnings
