"""Domain models: construct, validate, JSON round-trip."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from syncmusic.base.capabilities import DeviceCapabilities, PathRules
from syncmusic.core import models as m
from tests.factories import NOW, track

REF = m.ItemRef(library_id="nd", item_id="Виконавець/Альбом/01 - Трек.flac")


def _plan_item() -> m.PlanItem:
    return m.PlanItem(
        track=track("1", genres=["Rock"], extra={"bpm": 120}),
        file=m.PlannedFile(format="mp3", ext=".mp3", size=1000),
        steps=["transcode"],
        rules=["r1"],
    )


def _manifest() -> m.Manifest:
    entry = m.ManifestEntry(ref=REF, device_ref="d1", path="Music/a.mp3", size=10, rules=["r1"])
    return m.Manifest(
        header=m.ManifestHeader(device_id="dev"), items={REF.key(): entry}, playlists=["P"]
    )


def _sync_plan() -> m.SyncPlan:
    pi = _plan_item()
    return m.SyncPlan(
        id="p1",
        device_id="dev",
        actions=[
            m.PlanAction(kind=m.ActionKind.ADD, ref=pi.track.ref, item=pi, size_delta=1000),
            m.PlanAction(kind=m.ActionKind.REMOVE, ref=REF, entry=_manifest().items[REF.key()]),
        ],
        playlists=[m.DevicePlaylist(name="P", refs=[REF])],
        space=m.SpaceReport(capacity=10, free=5, per_rule=[m.RuleSpace(rule_id="r1")]),
    )


# One populated instance per model; every model in core.models must appear here.
SAMPLES: list[BaseModel] = [
    REF,
    track("1", kind=m.MediaKind.PODCAST, series="Pod", published=NOW, last_played=NOW),
    m.Playlist(ref=REF, name="Плейлист", track_refs=[REF], smart=True),
    m.LibrarySnapshot(library_id="nd", state_token="t", tracks={"1": track("1")}),
    m.Lyrics(synced=True, lines=[(1.5, "la")], language="en"),
    m.HealthStatus(ok=False, message="down", details={"code": 401}),
    m.Rule(id="r1", criteria={"all": [{"is": {"loved": True}}]}, sort="-year", limit=5),
    m.RuleSet(rules=[m.Rule(id="r1")], excludes=[m.Rule(id="x")], template_ids=["t1"]),
    m.RuleSet(limit_bytes=10**9, limit_tracks=5, fill_order=["random", "rating"]),
    m.RuleTemplate(id="t1", name="T", rules=[m.Rule(id="r1")]),
    m.ProcessorTemplate(
        id="p1", name="P", device_type="ipod", processors=[m.ProcessorSlot(type="retag")]
    ),
    m.Selection(items={REF.key(): ["r1"]}, playlists=[m.DevicePlaylist(name="P", refs=[REF])]),
    m.LibraryConfig(id="nd", type="navidrome", name="ND", settings={"url": "http://x"}),
    m.LibraryAccount(name="Anna", settings={"username": "anna"}),
    m.AppSettings(
        cache_max_gb=5, concurrency=2, webhook_url="http://hook", default_on_connect="auto"
    ),
    m.DeviceIdentity(usb_serial="S", fs_uuid="U"),
    m.ProcessorSlot(type="transcode", settings={"bitrate": 256}),
    m.ReverseSyncSettings(),
    m.SyncSettings(),
    m.DeviceConfig(
        id="dev",
        type="flashdrive",
        name="Stick",
        library_ids=["nd"],
        processors=[m.ProcessorSlot(type="covers")],
        rule_set=m.RuleSet(rules=[m.Rule(id="r1")]),
    ),
    m.PlayEvent(ref=REF, played_at=NOW, source_device="dev"),
    m.PositionInfo(position_s=12.5, finished=True, updated_at=NOW),
    m.MetaChange(
        field="position", old="750", new="6", direction="to_library", conflict="both moved"
    ),
    m.DeviceFeedback(
        plays=[m.PlayEvent(ref=REF, played_at=NOW)],
        ratings={REF.key(): 4},
        positions={REF.key(): m.PositionInfo(position_s=1)},
        play_counts={REF.key(): 3},
    ),
    m.ManifestHeader(device_id="dev"),
    m.ManifestLibrary(id="nav", source_id="subsonic:abc", type="subsonic", instance="i1"),
    m.InstanceInfo(id="i1", name="alpha", app_version="0.2"),
    m.Interruption(plan_id="p1", done=2, total=5, at=NOW),
    m.ErasedMark(how="wiped", at=NOW, removed=3),
    m.QuietHours(start="23:00", end="07:30"),
    m.SyncWindow(start="23:00", end="06:00", days=[0, 4]),
    m.ManifestHeader(
        device_id="dev", interrupted=m.Interruption(plan_id="p1", done=2, total=5, at=NOW)
    ),
    m.ManifestEntry(ref=REF, device_ref="d1", path="a.mp3", size=1),
    _manifest(),
    m.DeviceTrackRef(device_ref="d1", path="a.mp3", size=1),
    m.DeviceStats(playcount=4, rating=3),
    m.DeviceTrackRef(device_ref="d1", path="a.mp3", size=1, stats=m.DeviceStats()),
    m.DeviceItem(device_ref="d1", path="a.mp3", title="A"),
    m.DeviceContents(items=[m.DeviceItem(device_ref="d1", path="a.mp3")], playlists=["P"]),
    m.DevicePlaylist(name="P", refs=[REF]),
    m.PlannedFile(format="aac", ext=".m4a", size=5),
    _plan_item(),
    m.WorkItem(plan=_plan_item(), path=Path("/w/1.mp3"), work_dir=Path("/w")),
    m.PlanAction(kind=m.ActionKind.KEEP, ref=REF),
    m.PlaylistConflict(name="Мікс", kind="reorder"),
    m.PlaylistChange(name="Мікс", change="update", songs=3),
    m.RuleSpace(rule_id="r1", items=2, bytes=10),
    m.SpaceReport(capacity=100, free=50, needed=10, per_rule=[m.RuleSpace(rule_id="r1")]),
    m.DbSize(current=1000, estimated=2000, limit=63 * 1024 * 1024, level="ok"),
    m.LeftOut(key=REF.key(), title="A – B", size=5),
    m.LimitReport(
        limit_tracks=2, fill_order=["rating"], kept_items=2, left_out=[m.LeftOut(key=REF.key())]
    ),
    _sync_plan(),
    m.ItemFailure(ref=REF.key(), error="boom"),
    m.ReverseReport(plays_found=2),
    m.SyncReport(id="s1", device_id="dev", reverse=m.ReverseReport(), counts={"add": 1}),
    DeviceCapabilities(formats={"mp3"}, path_rules=PathRules.for_filesystem("vfat")),
]


def test_every_model_has_a_sample():
    declared = {
        obj.__name__
        for obj in vars(m).values()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__ == m.__name__
    }
    sampled = {type(s).__name__ for s in SAMPLES}
    assert declared <= sampled, f"models without a sample: {sorted(declared - sampled)}"


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: type(s).__name__)
def test_json_roundtrip(sample: BaseModel):
    restored = type(sample).model_validate_json(sample.model_dump_json())
    assert restored == sample


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: type(s).__name__)
def test_dict_roundtrip(sample: BaseModel):
    assert type(sample).model_validate(sample.model_dump(mode="json")) == sample


def test_itemref_key_parse_and_frozen():
    assert m.ItemRef.parse(REF.key()) == REF  # '/' inside item_id survives
    assert hash(REF) == hash(m.ItemRef.parse(REF.key()))
    with pytest.raises(ValidationError):
        REF.item_id = "x"  # type: ignore[misc]


def test_naive_datetimes_become_utc():
    t = track("1", last_played=datetime(2026, 1, 1, 12, 0))
    assert t.last_played == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    p = m.PlayEvent.model_validate({"ref": REF.model_dump(), "played_at": "2026-01-01T12:00:00"})
    assert p.played_at.tzinfo is not None


def test_validation_rejects_bad_input():
    with pytest.raises(ValidationError):
        m.Track(ref=REF)  # type: ignore[call-arg]  # title missing
    with pytest.raises(ValidationError):
        track("1", kind="hologram")  # "video" is a kind since
    with pytest.raises(ValidationError):
        m.DeviceConfig(id="d", type="x", name="n", on_connect="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        m.Rule(id="r", order="up")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        m.PlanAction(kind="move", ref=REF)  # type: ignore[arg-type]


def test_mutable_defaults_are_not_shared():
    a, b = track("1"), track("2")
    a.genres.append("Rock")
    a.extra["k"] = 1
    assert b.genres == [] and b.extra == {}
    c1 = m.DeviceConfig(id="1", type="x", name="a")
    c2 = m.DeviceConfig(id="2", type="x", name="b")
    c1.rule_set.rules.append(m.Rule(id="r"))
    c1.reverse_sync.plays = False
    assert c2.rule_set.rules == [] and c2.reverse_sync.plays is True


def test_lyrics_to_lrc():
    assert m.Lyrics(text="a\nb").to_lrc() == "a\nb\n"
    lrc = m.Lyrics(synced=True, lines=[(0, "x"), (65.25, "y")]).to_lrc()
    assert lrc == "[00:00.00]x\n[01:05.25]y\n"


def test_feedback_and_plan_helpers():
    assert m.DeviceFeedback().is_empty()
    assert not m.DeviceFeedback(ratings={REF.key(): None}).is_empty()
    plan = _sync_plan()
    assert plan.count(m.ActionKind.ADD) == 1 and plan.count(m.ActionKind.REMOVE) == 1
    assert set(_manifest().by_device_ref()) == {"d1"}
    sel = m.Selection(items={REF.key(): ["r1"]})
    assert sel.refs() == [REF]


def test_capability_overrides():
    caps = DeviceCapabilities(formats={"mp3"})
    over = caps.with_overrides({"formats": {"mp3", "flac"}, "supports_podcasts": True})
    assert over.formats == {"mp3", "flac"} and over.supports_podcasts
    assert caps.formats == {"mp3"}  # original untouched
    assert caps.supports_kind(m.MediaKind.MUSIC) and not caps.supports_kind(m.MediaKind.PODCAST)
    with pytest.raises(ValidationError):
        caps.with_overrides({"lyrics": "karaoke"})


def test_lyrics_to_lrc_rounding():
    lrc = m.Lyrics(synced=True, lines=[(59.999, "x"), (119.996, "y")]).to_lrc()
    assert lrc == "[01:00.00]x\n[02:00.00]y\n"


def _datetime_fields(cls: type[BaseModel]) -> list[str]:
    return [n for n, f in cls.model_fields.items() if "datetime" in str(f.annotation)]


def test_all_datetimes_aware():
    naive = "2026-01-01T00:00:00"
    for sample in SAMPLES:
        cls = type(sample)
        fields = _datetime_fields(cls)
        if not fields:
            continue
        data = sample.model_dump(mode="json")
        data.update(dict.fromkeys(fields, naive))
        obj = cls.model_validate(data)
        for f in fields:
            assert getattr(obj, f).tzinfo is not None, f"{cls.__name__}.{f} stays naive"


def test_validation_ranges():
    for bad in ({"rating": 9}, {"rating": -1}, {"play_count": -5}, {"size_bytes": -1}):
        with pytest.raises(ValidationError):
            track("1", **bad)
    with pytest.raises(ValidationError):
        DeviceCapabilities().with_overrides({"fromats": ["flac"]})


def test_compact_tracks_shares_fields_set_and_keeps_behaviour() -> None:
    from syncmusic.core.models import ItemRef, Track, compact_tracks

    a = Track(ref=ItemRef(library_id="lib", item_id="1"), title="A", artist="X")
    b = Track(ref=ItemRef(library_id="lib", item_id="2"), title="B")
    before = a.model_dump()
    compact_tracks([a, b])
    assert a.__pydantic_fields_set__ is b.__pydantic_fields_set__
    assert a.model_dump() == before
    a.title = "A2"  # assignment still works
    c = a.model_copy(update={"album": "Z"})
    assert c.album == "Z" and b.album is None
    assert Track.model_validate_json(a.model_dump_json()) == a
