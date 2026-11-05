"""Space calculation + SpaceReport."""

from __future__ import annotations

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core.models import (
    ActionKind,
    DeviceConfig,
    ItemRef,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    Rule,
    RuleSet,
    SyncSettings,
)
from syncmusic.core.planner import plan_sync
from syncmusic.core.prepare import Chain
from syncmusic.core.space import (
    left_off,
    plan_with_space,
    safety_margin,
    space_report,
    usage_breakdown,
)
from syncmusic.processors.transcode import Transcode
from tests.factories import NOW, snapshot, track

MB = 1000**2
CAPS = DeviceCapabilities(formats={"mp3", "flac", "aac"})
SETTINGS = SyncSettings(safety_margin_pct=1.0, safety_margin_min_bytes=0)


def _device(rules, priority=None) -> DeviceConfig:
    return DeviceConfig(
        id="d",
        type="fake",
        name="D",
        library_ids=["lib"],
        rule_set=RuleSet(rules=rules, priority=priority or []),
        sync=SETTINGS,
    )


def _tracks():
    return [
        track(
            "r1", title="Rock 1", genres=["Rock"], size_bytes=10 * MB, year=2001, source_state="x"
        ),
        track(
            "r2", title="Rock 2", genres=["Rock"], size_bytes=10 * MB, year=2002, source_state="x"
        ),
        track(
            "j1", title="Jazz 1", genres=["Jazz"], size_bytes=20 * MB, year=2003, source_state="x"
        ),
        track(
            "f1",
            title="Flac",
            genres=["Jazz"],
            format="flac",
            size_bytes=30 * MB,
            duration_s=200,
            source_state="x",
        ),
    ]


RULES = [
    Rule(id="rock", name="Rock", criteria={"is": {"genre": "Rock"}}),
    Rule(id="jazz", name="Jazz", criteria={"is": {"genre": "Jazz"}}),
]


def _plan(free, capacity=1000 * MB, chain=None, force=False, priority=None, reserve=0):
    return plan_with_space(
        _device(RULES, priority),
        {"lib": snapshot(_tracks())},
        None,
        CAPS,
        chain or Chain([]),
        capacity=capacity,
        free=free,
        commit_reserve=lambda n: reserve + 1000 * n,
        force=force,
        now=NOW,
    )


def test_safety_margin():
    assert safety_margin(1000 * MB, SyncSettings()) == 100 * 1024 * 1024  # min 100 MB
    assert safety_margin(30_000 * MB, SyncSettings()) == 300 * MB  # 1 %
    assert safety_margin(464_000 * MB, SyncSettings()) == 500 * 1024 * 1024  # capped
    s = SyncSettings(safety_margin_min_bytes=600 * MB, safety_margin_max_bytes=500 * MB)
    assert safety_margin(464_000 * MB, s) == 600 * MB  # min wins over max


def test_fits_with_reserve_and_margin():
    plan = _plan(free=500 * MB)
    s = plan.space
    assert s.fits and not plan.does_not_fit
    assert s.reserve == 10 * MB + 4 * 1000  # 1 % of 1000 MB + commit reserve for 4 items
    assert s.needed == 70 * MB + s.reserve
    assert {r.rule_id: (r.items, r.bytes) for r in s.per_rule} == {
        "jazz": (2, 50 * MB),
        "rock": (2, 20 * MB),
    }
    # rows in rule priority order, not by size
    assert [r.name for r in s.per_rule] == ["Rock", "Jazz"]
    reordered = _plan(free=500 * MB, priority=["jazz", "rock"]).space.per_rule
    assert [r.rule_id for r in reordered] == ["jazz", "rock"]


def test_does_not_fit_by_reserve_only():
    plan = _plan(free=75 * MB)  # 70 MB of files + 10 MB margin
    assert plan.does_not_fit and not plan.space.fits
    assert any("does not fit" in w for w in plan.warnings)
    sugg = plan.space.suggestions
    assert any("more than is free" in x for x in sugg)
    assert any("lower the limit of rule 'Jazz'" in x for x in sugg)
    assert any("lossless" in x and "space_needed" in x for x in sugg)
    assert any("exclude genre 'Jazz'" in x for x in sugg)


def test_space_needed_transcode_shrinks_plan():
    tc = Transcode({"rules": [{"from": ["flac"], "to": "aac", "when": "space_needed"}]}, None)
    chain = Chain([tc])
    full = _plan(free=1000 * MB, chain=chain)
    assert not full.space.space_steps_used
    assert next(a for a in full.actions if a.ref.item_id == "f1").item.file.format == "flac"
    plan = _plan(free=60 * MB, chain=chain)  # 70 MB does not fit; FLAC → AAC 256k (~6.8 MB) does
    assert plan.space.fits and plan.space.space_steps_used and not plan.does_not_fit
    f1 = next(a for a in plan.actions if a.ref.item_id == "f1")
    assert f1.item.file.format == "aac" and f1.item.file.size < 8 * MB


def test_forced_sync_drops_lowest_priority_first():
    plan = _plan(free=45 * MB, force=True)  # budget 35 MB after the 10 MB margin
    assert not plan.does_not_fit and plan.space.fits
    kept = [a.ref.item_id for a in plan.actions if a.kind == ActionKind.ADD]
    assert kept == [
        "r1",
        "r2",
    ]  # rock first (20 MB); then jazz in its own order: Flac (30), Jazz 1 (20)
    assert plan.space.dropped == ["lib/f1", "lib/j1"]
    # the plan review lists them with title and size
    assert [(x.key, x.title, x.size) for x in plan.space.dropped_detail] == [
        ("lib/f1", "Flac", 30 * MB),
        ("lib/j1", "Jazz 1", 20 * MB),
    ]
    rules = {r.rule_id: r for r in plan.space.per_rule}
    assert rules["jazz"].dropped_items == 2 and rules["jazz"].dropped_bytes == 50 * MB
    assert rules["rock"].dropped_items == 0
    assert any("2 items skipped" in w for w in plan.warnings)
    assert plan.space.suggestions == []  # the untrimmed plan's suggestions are gone


def test_forced_sync_respects_rule_priority():
    plan = _plan(free=45 * MB, force=True, priority=["jazz", "rock"])
    kept = [a.ref.item_id for a in plan.actions if a.kind == ActionKind.ADD]
    assert kept == ["f1"]  # jazz first: Flac (30 of 35 MB); nothing else fits in the remaining 5 MB
    assert set(plan.space.dropped) == {"lib/j1", "lib/r1", "lib/r2"}


def test_forced_sync_removals_free_space_first_and_replace_kept_old():
    from syncmusic.core.models import Manifest, ManifestEntry, ManifestHeader
    from syncmusic.core.prepare import initial_plan_item

    tracks = _tracks()
    # in the library but not selected by RULES (a source gone from the library would be an orphan)
    old = track("old", title="Old", genres=["Pop"], size_bytes=40 * MB, source_state="x")
    chain = Chain([])
    r1 = tracks[0]
    entries = [
        ManifestEntry(ref=old.ref, device_ref="o", path="o", size=40 * MB, source_state="x"),
        ManifestEntry(
            ref=r1.ref,
            device_ref="r1",
            path="r1",
            size=1 * MB,
            source_state="changed",
            chain_hash=chain.chain_hash(initial_plan_item(r1), CAPS),
        ),
    ]
    m = Manifest(header=ManifestHeader(device_id="d"), items={e.ref.key(): e for e in entries})
    plan = plan_with_space(
        _device(RULES),
        {"lib": snapshot([*tracks, old])},
        m,
        CAPS,
        chain,
        capacity=1000 * MB,
        free=20 * MB,
        commit_reserve=lambda n: 0,
        force=True,
        now=NOW,
    )
    kinds = {a.ref.item_id: a.kind for a in plan.actions}
    # budget: 20 free - 10 margin + 40 removed = 50 MB: r1 replace (+9), r2 (10), f1 (30) fit; j1 (20) not
    assert kinds == {"r1": "replace", "r2": "add", "f1": "add", "old": "remove"}
    assert plan.space.dropped == ["lib/j1"]
    plan = plan_with_space(
        _device(RULES),
        {"lib": snapshot([*tracks, old])},
        m,
        CAPS,
        chain,
        capacity=1000 * MB,
        free=0,
        commit_reserve=lambda n: 0,
        force=True,
        now=NOW,
    )
    kinds = {a.ref.item_id: a.kind for a in plan.actions}
    assert kinds["r1"] == "replace"  # 40 MB freed by the removal minus 10 MB margin: r1 (+9) fits
    plan = plan_with_space(
        _device(RULES),
        {"lib": snapshot([*tracks, old])},
        m.model_copy(update={"items": {r1.ref.key(): entries[1]}}),
        CAPS,
        chain,
        capacity=1000 * MB,
        free=0,
        commit_reserve=lambda n: 0,
        force=True,
        now=NOW,
    )
    a = next(a for a in plan.actions if a.ref.item_id == "r1")
    assert a.kind == "keep" and a.reason == "skipped for space" and a.entry is not None


def test_space_report_direct():
    plan = plan_sync(_device(RULES), {"lib": snapshot(_tracks())}, None, CAPS, Chain([]), now=NOW)
    rep = space_report(plan, capacity=100, free=10, reserve=5)
    assert rep.needed == 70 * MB + 5 and not rep.fits and rep.capacity == 100


def test_plan_summary_bytes_per_kind_and_written():
    """the plan review's Space after bar and "Apply · write N" come from the summary."""
    from syncmusic.core.sync import PlanContext, plan_summary

    data = plan_summary(PlanContext(plan=_plan(free=500 * MB), matches=[]))
    assert data["by_kind_delta"] == {"music": 70 * MB, "podcast": 0, "audiobook": 0, "video": 0}
    assert data["write_bytes"] == 70 * MB


def _entry(lib: str, item: str, size: int, rules: list[str]) -> ManifestEntry:
    ref = ItemRef(library_id=lib, item_id=item)
    return ManifestEntry(ref=ref, device_ref=item, path=f"{item}.mp3", size=size, rules=rules)


def test_usage_breakdown_by_library_rule_and_rest():
    """the space map from the manifest; an item counts under its first rule."""
    m = Manifest(
        header=ManifestHeader(device_id="d"),
        items={
            e.ref.key(): e
            for e in (
                _entry("music", "a", 30 * MB, ["rock", "jazz"]),
                _entry("music", "b", 10 * MB, ["jazz"]),
                _entry("books", "c", 50 * MB, []),
            )
        },
    )
    out = usage_breakdown(
        m,
        used=100 * MB,
        db_size=4 * MB,
        rule_names={"rock": "Rock"},
        library_names={"music": "Navidrome"},
    )
    assert out["by_library"] == [
        {"id": "books", "name": "books", "items": 1, "bytes": 50 * MB},
        {"id": "music", "name": "Navidrome", "items": 2, "bytes": 40 * MB},
    ]
    assert [(r["id"], r["name"], r["bytes"]) for r in out["by_rule"]] == [  # type: ignore[union-attr]
        ("", "", 50 * MB),
        ("rock", "Rock", 30 * MB),
        ("jazz", "jazz", 10 * MB),
    ]
    assert out["database"] == 4 * MB and out["foreign"] == 6 * MB
    # the database never takes more than the used space left over
    assert (
        usage_breakdown(m, used=91 * MB, db_size=4 * MB, rule_names={}, library_names={})[
            "database"
        ]
        == 1 * MB
    )
    empty = usage_breakdown(None, used=5, db_size=None, rule_names={}, library_names={})
    assert empty == {"by_library": [], "by_rule": [], "database": 0, "foreign": 5}


def test_left_off_explains_space():
    """nothing left off → None; does not fit → per rule wanted bytes + advice; forced → lost items."""
    assert left_off(_plan(free=500 * MB)) is None
    out = left_off(_plan(free=45 * MB))
    assert out is not None and out["does_not_fit"] and out["short_by"] > 0
    assert {r["rule_id"] for r in out["per_rule"]} == {"rock", "jazz"}  # type: ignore[union-attr]
    assert out["suggestions"] and out["dropped_total"] == 0
    forced = left_off(_plan(free=45 * MB, force=True))
    assert forced is not None and not forced["does_not_fit"] and forced["dropped_total"] > 0
    assert forced["dropped_bytes"] == sum(d["size"] for d in forced["dropped"])  # type: ignore[union-attr]
    assert all(r["dropped_items"] for r in forced["per_rule"])  # type: ignore[union-attr]


# --- tests that kill surviving mutants ---


def _act(kind, iid, *, rules=("r",), size=0, sidecar=0, entry_size=None, delta=0):
    from syncmusic.core.models import PlanAction, PlanItem, PlannedFile

    t = track(iid)
    item = PlanItem(
        track=t, file=PlannedFile(format="mp3", ext="mp3", size=size), sidecar_bytes=sidecar
    )
    entry = None
    if entry_size is not None:
        entry = ManifestEntry(
            ref=t.ref, device_ref=iid, path=iid, size=entry_size, source_state="s"
        )
    return PlanAction(
        kind=kind, ref=t.ref, item=item, entry=entry, rules=list(rules), size_delta=delta
    )


def _splan(actions):
    from syncmusic.core.models import SyncPlan

    return SyncPlan(id="p", device_id="d", actions=actions)


def test_safety_margin_is_a_share_of_the_capacity():
    s = SyncSettings(
        safety_margin_pct=2.0, safety_margin_min_bytes=0, safety_margin_max_bytes=10**12
    )
    assert safety_margin(1000 * MB, s) == 20 * MB
    assert safety_margin(1000 * MB, s.model_copy(update={"safety_margin_max_bytes": MB})) == MB
    assert (
        safety_margin(10 * MB, s.model_copy(update={"safety_margin_min_bytes": 5 * MB})) == 5 * MB
    )


def test_space_report_per_rule_bytes_on_the_device_after_the_plan():
    plan = _splan(
        [
            _act(ActionKind.ADD, "a", size=3 * MB, sidecar=MB, delta=4 * MB),  # file + sidecar
            _act(ActionKind.KEEP, "k", size=99 * MB, entry_size=2 * MB),  # the file already there
            _act(ActionKind.KEEP, "n", size=99 * MB),  # no entry: nothing known on the device
            _act(ActionKind.REMOVE, "x", entry_size=50 * MB, delta=-50 * MB),  # leaves
            _act(ActionKind.KEEP, "m", rules=(), entry_size=7 * MB),  # manual item: no rule
            _act(
                ActionKind.ADD, "b", rules=("s",), size=MB, delta=MB
            ),  # listed after an unruled one
        ]
    )
    rep = space_report(
        plan, capacity=100 * MB, free=MB, reserve=MB, rule_names={"r": "R", "s": "S"}
    )
    assert [(r.rule_id, r.items, r.bytes) for r in rep.per_rule] == [("r", 3, 6 * MB), ("s", 1, MB)]
    assert rep.needed == 4 * MB - 50 * MB + MB + MB and rep.fits


def test_space_report_fits_exactly_and_orders_unnamed_rules_biggest_first():
    plan = _splan(
        [
            _act(ActionKind.ADD, "a", rules=("small",), size=MB, delta=MB),
            _act(ActionKind.ADD, "b", rules=("big",), size=2 * MB, delta=2 * MB),
            _act(ActionKind.ADD, "c", rules=("named",), size=MB, delta=MB),
        ]
    )
    rep = space_report(plan, capacity=10 * MB, free=5 * MB, reserve=MB, rule_names={"named": "N"})
    assert rep.fits and rep.needed == rep.free  # needed == free still fits
    assert [r.rule_id for r in rep.per_rule] == ["named", "big", "small"]
    assert not space_report(plan, capacity=10 * MB, free=5 * MB - 1, reserve=MB).fits


def test_db_level_warns_from_ninety_percent():
    from syncmusic.core.space import db_level

    assert [db_level(s, 100) for s in (89, 90, 100, 101)] == ["ok", "warn", "warn", "over"]


def test_lossless_suggestion_only_for_lossless_items_and_lossy_capable_devices():
    from syncmusic.core.space import suggestions

    flac = _act(ActionKind.ADD, "f", size=30 * MB, delta=30 * MB)
    flac.item.file.format = "flac"
    mp3 = _act(ActionKind.ADD, "m", size=5 * MB, delta=5 * MB)
    gone = _act(ActionKind.REMOVE, "g", size=9 * MB, entry_size=9 * MB)
    gone.item.file.format = "flac"
    rep = space_report(_splan([flac, mp3, gone]), capacity=100 * MB, free=10 * MB, reserve=0)
    out = suggestions(_splan([flac, mp3, gone]), rep, CAPS)
    assert out[0] == "the plan needs 25.0 MB more than is free on the device"
    assert "convert lossless files to AAC 256k when space is needed (1 item, 30.0 MB)" in out[2]
    no_aac = DeviceCapabilities(formats={"mp3", "flac"})
    assert "to a lossy format when" in suggestions(_splan([flac]), rep, no_aac)[2]
    lossless_only = DeviceCapabilities(formats={"flac", "wav"})
    assert not any("convert" in s for s in suggestions(_splan([flac]), rep, lossless_only))
    assert not any("convert" in s for s in suggestions(_splan([mp3, gone]), rep, CAPS))
    used = rep.model_copy(update={"space_steps_used": True})
    assert not any("convert" in s for s in suggestions(_splan([flac]), used, CAPS))


def test_trim_to_fit_keeps_priority_order_and_the_old_version_of_a_dropped_replace():
    from syncmusic.core.models import DevicePlaylist
    from syncmusic.core.space import trim_to_fit

    acts = [
        _act(ActionKind.REMOVE, "rm", entry_size=2 * MB, delta=-2 * MB),  # frees space first
        _act(ActionKind.REPLACE, "shrink", size=MB, entry_size=4 * MB, delta=-3 * MB),  # frees too
        _act(ActionKind.ADD, "a", size=6 * MB, delta=6 * MB),  # exactly the budget left
        _act(ActionKind.REPLACE, "r", size=9 * MB, entry_size=MB, delta=8 * MB),  # does not fit
        _act(ActionKind.ADD, "b", size=MB, delta=MB),  # does not fit either
        _act(ActionKind.KEEP, "k", entry_size=MB),
    ]
    plan = _splan(acts)
    keys = {a.ref.item_id: a.ref for a in acts}
    plan.playlists = [DevicePlaylist(name="P", refs=[keys["a"], keys["r"], keys["b"]])]
    dropped = trim_to_fit(plan, MB)  # 1 MB + 2 MB removed + 3 MB from the shrinking replace = 6 MB
    assert dropped == [keys["r"].key(), keys["b"].key()]
    kinds = {a.ref.item_id: (a.kind, a.size_delta, a.item is None) for a in plan.actions}
    assert kinds == {
        "rm": (ActionKind.REMOVE, -2 * MB, False),
        "shrink": (ActionKind.REPLACE, -3 * MB, False),
        "a": (ActionKind.ADD, 6 * MB, False),
        "r": (ActionKind.KEEP, 0, True),  # the old file stays on the device
        "k": (ActionKind.KEEP, 0, False),
    }
    assert next(a for a in plan.actions if a.ref.item_id == "r").reason == "skipped for space"
    # the kept old version stays in the playlist, the never-copied add leaves it
    assert plan.playlists[0].refs == [keys["a"], keys["r"]]


def test_unknown_space_from_the_sentinel_capacity():
    from syncmusic.base.device import UNKNOWN_SPACE

    assert space_report(_splan([]), capacity=UNKNOWN_SPACE, free=0, reserve=0).unknown
    assert not space_report(_splan([]), capacity=UNKNOWN_SPACE - 1, free=0, reserve=0).unknown
