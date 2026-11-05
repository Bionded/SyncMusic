"""Planner."""

from __future__ import annotations

from syncmusic.base.capabilities import DeviceCapabilities, PathRules
from syncmusic.core.models import (
    ActionKind,
    DeviceConfig,
    ItemRef,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    MediaKind,
    Playlist,
    Rule,
    RuleSet,
    playlist_hash,
)
from syncmusic.core.planner import meta_hash, meta_snapshot, plan_sync
from syncmusic.core.prepare import CAPS_HASH_VERSION, Chain, initial_plan_item
from syncmusic.processors.retag import Retag
from syncmusic.processors.transcode import Transcode
from tests.factories import NOW, snapshot, track
from tests.fakes import FakeTrailer

FILE_CAPS = DeviceCapabilities(formats={"mp3", "flac"}, supports_podcasts=False)
IPOD_CAPS = DeviceCapabilities(formats={"mp3"}, needs_device_db=True, supports_podcasts=True)
ALL = Rule(id="all", criteria={"all": []})


def _tracks():
    return [
        track("1", title="One", artist="A", source_state="s1"),
        track("2", title="Two", artist="A", source_state="s2"),
        track(
            "3", title="Three", artist="B", format="flac", size_bytes=30_000_000, source_state="s3"
        ),
        track("4", title="Pod", kind=MediaKind.PODCAST, source_state="s4"),
        track("5", title="Weird", format="wma", source_state="s5"),
    ]


def _device(rules=None, mode="mirror", **kw) -> DeviceConfig:
    return DeviceConfig(
        id="dev",
        type="fake",
        name="D",
        library_ids=["lib"],
        rule_set=RuleSet(rules=rules or [ALL], mode=mode),
        **kw,
    )


def _entry(t, chain, caps, **kw) -> ManifestEntry:
    data = {
        "ref": t.ref,
        "device_ref": f"Music/{t.title}.mp3",
        "path": f"Music/{t.title}.mp3",
        "size": t.size_bytes,
        "source_state": t.source_state,
        "chain_hash": chain.chain_hash(initial_plan_item(t), caps),
        "meta_hash": meta_hash(t),
        "rules": ["all"],
        "caps_v": CAPS_HASH_VERSION,
    }
    data.update(kw)
    return ManifestEntry(**data)


def _manifest(entries) -> Manifest:
    return Manifest(header=ManifestHeader(device_id="dev"), items={e.ref.key(): e for e in entries})


def _plan(device, tracks, manifest, caps=FILE_CAPS, chain=None, **kw):
    return plan_sync(
        device, {"lib": snapshot(tracks)}, manifest, caps, chain or Chain([]), now=NOW, **kw
    )


def _kinds(plan):
    return {a.ref.item_id: a.kind for a in plan.actions}


def test_first_sync_adds_and_needs_confirmation():
    plan = _plan(_device(), _tracks(), None)
    assert _kinds(plan) == {"1": "add", "2": "add", "3": "add"}
    assert (
        plan.first_sync and plan.needs_confirmation and "first sync" in plan.confirmation_reasons[0]
    )
    add3 = next(a for a in plan.actions if a.ref.item_id == "3")
    assert (
        add3.size_delta == 30_000_000 and add3.item.file.format == "flac" and add3.rules == ["all"]
    )
    assert any("format not playable" in w and "Weird (wma)" in w for w in plan.warnings)
    assert any("media kind not supported" in w for w in plan.warnings)


def test_rerun_is_all_keep():
    tracks, chain = _tracks()[:3], Chain([])
    m = _manifest([_entry(t, chain, FILE_CAPS) for t in tracks])
    plan = _plan(_device(), tracks, m)
    assert set(_kinds(plan).values()) == {ActionKind.KEEP}
    assert not plan.needs_confirmation and not plan.first_sync


def test_replace_on_source_or_chain_change():
    tracks, chain = _tracks()[:3], Chain([])
    m = _manifest(
        [
            _entry(tracks[0], chain, FILE_CAPS, source_state="old"),
            _entry(tracks[1], chain, FILE_CAPS, chain_hash="other", size=1_000_000),
            _entry(tracks[2], chain, FILE_CAPS),
        ]
    )
    plan = _plan(_device(), tracks, m)
    k = _kinds(plan)
    assert k == {"1": "replace", "2": "replace", "3": "keep"}
    a2 = next(a for a in plan.actions if a.ref.item_id == "2")
    assert a2.size_delta == 5_000_000 - 1_000_000 and a2.reason == "processing changed"
    assert next(a for a in plan.actions if a.ref.item_id == "1").reason == "source changed"


def test_chain_change_replaces_everything():
    tracks = _tracks()[:2]
    m = _manifest([_entry(t, Chain([]), FILE_CAPS) for t in tracks])
    plan = _plan(_device(), tracks, m, chain=Chain([FakeTrailer({}, None)]))
    assert set(_kinds(plan).values()) == {ActionKind.REPLACE}
    assert all(a.item.file.size == 5_000_128 for a in plan.actions)


def test_metadata_change_file_device_with_tag_processor_replaces():
    tracks = _tracks()[:1]
    chain = Chain([FakeTrailer({}, None)])  # stage "tags"
    m = _manifest([_entry(tracks[0], chain, FILE_CAPS)])
    renamed = [tracks[0].model_copy(update={"rating": 5})]
    assert _kinds(_plan(_device(), renamed, m, chain=chain)) == {"1": "replace"}
    # without a tag-writing processor the file is not touched
    m2 = _manifest([_entry(tracks[0], Chain([]), FILE_CAPS)])
    assert _kinds(_plan(_device(), renamed, m2)) == {"1": "keep"}


def test_metadata_change_ipod_is_update_meta():
    tracks = _tracks()[:1]
    m = _manifest([_entry(tracks[0], Chain([]), IPOD_CAPS)])
    renamed = [tracks[0].model_copy(update={"title": "One (Remastered)"})]
    plan = _plan(_device(), renamed, m, caps=IPOD_CAPS)
    assert _kinds(plan) == {"1": "update_meta"}
    # play counts are reverse-synced, not metadata
    played = [tracks[0].model_copy(update={"play_count": 9})]
    assert _kinds(_plan(_device(), played, m, caps=IPOD_CAPS)) == {"1": "keep"}


def test_plan_shows_metadata_old_and_new():
    """an update_meta action lists each changed tag (old → new) from the entry's snapshot; an
    entry without one says "metadata" changed; a rating and a spoken-word bookmark the sync writes are listed."""
    caps = IPOD_CAPS.model_copy(update={"supports_ratings_write": True})
    one = _tracks()[0]
    m = _manifest([_entry(one, Chain([]), caps, meta=meta_snapshot(one))])
    renamed = one.model_copy(update={"title": "One (Remastered)", "rating": 4})
    (a,) = _plan(_device(), [renamed], m, caps=caps).actions
    assert a.kind is ActionKind.UPDATE_META
    got = {(c.field, c.old, c.new, c.direction) for c in a.changes}
    assert got == {
        ("title", "One", "One (Remastered)", "to_device"),
        ("rating", None, "4", "to_device"),
    }
    old = _manifest([_entry(one, Chain([]), caps)])  # written before the snapshot
    (a,) = _plan(_device(), [renamed], old, caps=caps).actions
    assert [c.field for c in a.changes] == ["metadata", "rating"]
    pod = track("4", title="Pod", kind=MediaKind.PODCAST, source_state="s4")
    entry = _entry(
        pod, Chain([]), caps, kind=MediaKind.PODCAST, meta=meta_snapshot(pod), last_position_s=750
    )
    moved = pod.model_copy(update={"position_s": 6.0})
    (a,) = _plan(_device(), [moved], _manifest([entry]), caps=caps).actions
    assert a.kind is ActionKind.KEEP
    assert [(c.field, c.old, c.new) for c in a.changes] == [("position", "750", "6")]
    # unchanged: nothing to show
    (a,) = _plan(_device(), [one], m, caps=caps).actions
    assert a.changes == []


def test_unplayed_episode_is_no_position_change():
    """none, 0:00 and a few seconds are all "not started" - no 'position (none) → 0' row either
    way; 0 → 12:30 still is a change."""
    from syncmusic.core.models import DeviceFeedback, PositionInfo, ReverseSyncSettings
    from syncmusic.core.planner import position_changed, reverse_changes

    assert (
        not position_changed(0, None)
        and not position_changed(None, 3)
        and not position_changed(4.5, 0)
    )
    assert (
        position_changed(750, None) and position_changed(6, 0) and not position_changed(750.4, 750)
    )
    caps = IPOD_CAPS
    for lib, last in ((0.0, None), (2.0, None), (0.0, 0.0), (None, 0.0)):
        pod = track("4", title="Pod", kind=MediaKind.PODCAST, source_state="s4", position_s=lib)
        entry = _entry(
            pod,
            Chain([]),
            caps,
            kind=MediaKind.PODCAST,
            meta=meta_snapshot(pod),
            last_position_s=last,
        )
        plan = _plan(_device(), [pod], _manifest([entry]), caps=caps)
        assert [a.changes for a in plan.actions] == [[]], (lib, last)
        fb = DeviceFeedback(positions={pod.ref.key(): PositionInfo(position_s=0)})
        reverse_changes(plan, fb, ReverseSyncSettings(), "device")
        assert [a.changes for a in plan.actions] == [[]], (lib, last)
    pod = track("4", title="Pod", kind=MediaKind.PODCAST, source_state="s4", position_s=750.0)
    entry = _entry(
        pod, Chain([]), caps, kind=MediaKind.PODCAST, meta=meta_snapshot(pod), last_position_s=0
    )
    (a,) = _plan(_device(), [pod], _manifest([entry]), caps=caps).actions
    assert [(c.field, c.old, c.new) for c in a.changes] == [("position", "0", "750")]


def _only(tracks):
    """A rule that selects just these tracks."""
    return [
        Rule(id="some", criteria={"any": [{"in": {"track_id": [t.ref.item_id for t in tracks]}}]})
    ]


def test_remove_only_in_mirror_and_only_managed():
    tracks, chain = _tracks()[:3], Chain([])
    unselected = track("9", title="Unselected", source_state="x")
    m = _manifest([_entry(t, chain, FILE_CAPS) for t in [*tracks, unselected]])
    plan = _plan(_device(_only(tracks)), [*tracks, unselected], m)
    rem = [a for a in plan.actions if a.kind == ActionKind.REMOVE]
    assert [a.ref.item_id for a in rem] == ["9"] and rem[0].size_delta == -unselected.size_bytes
    additive = _plan(_device(_only(tracks), mode="additive"), [*tracks, unselected], m)
    assert not additive.count(ActionKind.REMOVE) and _kinds(additive)["9"] == "keep"


def test_an_item_deleted_from_its_library_is_removed():
    """in both modes a deleted item is a REMOVE naming the library; an empty library keeps everything as
    orphans; `remove_deleted` off keeps the orphan behaviour."""
    tracks, chain = _tracks()[:3], Chain([])
    gone = track("9", title="Gone", source_state="x")
    m = _manifest([_entry(t, chain, FILE_CAPS) for t in [*tracks, gone]])
    for mode in ("mirror", "additive"):
        plan = _plan(_device(mode=mode), tracks, m, library_names={"lib": "Navidrome"})
        (a,) = [a for a in plan.actions if a.ref.item_id == "9"]
        assert a.kind == ActionKind.REMOVE and a.deleted and not a.orphan
        assert a.reason == "deleted from Navidrome" and a.size_delta == -gone.size_bytes
        assert not plan.needs_confirmation and not any("orphan" in w for w in plan.warnings)
    off = _plan(_device(remove_deleted=False), tracks, m)
    (a,) = [a for a in off.actions if a.ref.item_id == "9"]
    assert a.kind == ActionKind.KEEP and a.orphan and a.entry is not None and a.size_delta == 0
    assert not off.count(ActionKind.REMOVE) and not off.needs_confirmation
    assert any("1 item on the device is gone from its library" in w for w in off.warnings)
    empty = _plan(_device(), [], m)
    assert not empty.count(ActionKind.REMOVE) and sum(a.orphan for a in empty.actions) == 4
    assert any("returned no items" in w for w in empty.warnings)
    # the file is gone from the device too: nothing to keep, the entry goes (0 bytes)
    plan = _plan(
        _device(mode="additive", remove_deleted=False), tracks, m, missing={gone.ref.key()}
    )
    (a,) = [a for a in plan.actions if a.ref.item_id == "9"]
    assert a.kind == ActionKind.REMOVE and a.size_delta == 0 and not a.orphan


def test_a_mass_vanish_keeps_the_items_and_asks():
    """more than 20 % (and more than 5 items) of a library gone at once looks like a glitch: orphans,
    a warning, and an automatic sync asks; a smaller share is removed under the delete threshold."""
    tracks, chain = [track(str(i), source_state="s") for i in range(20)], Chain([])
    m = _manifest([_entry(t, chain, FILE_CAPS) for t in tracks])
    plan = _plan(_device(), tracks[:14], m, auto=True)  # 6 of 20 gone
    assert not plan.count(ActionKind.REMOVE) and sum(a.orphan for a in plan.actions) == 6
    assert any("vanished at once" in w for w in plan.warnings) and plan.needs_confirmation
    assert not _plan(_device(), tracks[:14], m).needs_confirmation  # manual: the warning only
    few = _plan(_device(), tracks[:16], m, auto=True)  # 4 of 20: deleted
    assert few.count(ActionKind.REMOVE) == 4 and not few.needs_confirmation


def test_orphan_and_vanish_warning_texts():
    """The exact counts, library name and plural forms of
    the orphan / mass-vanish warnings, and an empty library warns only that it returned nothing."""
    tracks, chain = [track(str(i), source_state="s") for i in range(20)], Chain([])
    m = _manifest([_entry(t, chain, FILE_CAPS) for t in tracks])
    names = {"lib": "Navidrome"}
    plan = _plan(_device(), tracks[:14], m, auto=True, library_names=names)
    assert plan.warnings == [
        "6 items on the device are gone from their library (orphans): they stay on the device; "
        "remove them in Device settings",
        "6 of 20 items from Navidrome vanished at once (more than 20 %): kept on the device in case it is a "
        "library glitch",
    ]
    assert plan.confirmation_reasons == [
        "6 items vanished from Navidrome at once: they stay on the device; remove them in Device settings if "
        "they were deleted"
    ]
    empty = _plan(_device(), [], m, auto=True, library_names=names)
    assert empty.warnings[1:] == ["library 'lib' returned no items (is it mounted / reachable?)"]
    assert not empty.confirmation_reasons
    off = _plan(_device(remove_deleted=False), tracks[:14], m, auto=True)
    assert (
        len(off.warnings) == 1 and not off.needs_confirmation
    )  # no mass-vanish check without remove_deleted
    assert _plan(_device(), tracks, m).warnings == []


def test_a_library_notice_explains_its_orphans():
    """a library that no longer provides a kind (music from a video-only server): its music stays on the
    device as orphans and the plan says why - only when there are orphans of it."""
    tracks, chain = [track(str(i), source_state="s") for i in range(10)], Chain([])
    m = _manifest([_entry(t, chain, FILE_CAPS) for t in tracks])
    snap = snapshot([]).model_copy(update={"notice": "provides video only"})
    plan = plan_sync(_device(), {"lib": snap}, m, FILE_CAPS, chain, now=NOW)
    assert not plan.count(ActionKind.REMOVE) and sum(a.orphan for a in plan.actions) == 10
    assert "provides video only" in plan.warnings
    clean = snapshot(tracks).model_copy(update={"notice": "provides video only"})
    assert plan_sync(_device(), {"lib": clean}, m, FILE_CAPS, chain, now=NOW).warnings == []


def test_manual_items_stay_when_templates_change():
    """an item of the Manual selection keeps syncing whatever the templates select."""
    from syncmusic.core.models import RuleTemplate
    from syncmusic.core.rules.templates import add_to_manual_selection

    tracks, chain = _tracks()[:3], Chain([])
    m = _manifest([_entry(t, chain, FILE_CAPS) for t in tracks])
    tpl = RuleTemplate(id="t", name="T", rules=_only(tracks[:1]))
    rs = add_to_manual_selection(RuleSet(template_ids=["t"]), "track_id", [tracks[2].ref.item_id])
    dev = _device().model_copy(update={"rule_set": rs})
    plan = _plan(dev, tracks, m, templates={"t": tpl})
    kinds = _kinds(plan)
    assert [kinds[t.ref.item_id] for t in tracks] == ["keep", "remove", "keep"]
    # the template emptied, or detached: the manual item still stays, the template's item goes
    for templates, tids in (({"t": tpl.model_copy(update={"rules": []})}, ["t"]), ({}, [])):
        d = dev.model_copy(update={"rule_set": rs.model_copy(update={"template_ids": tids})})
        kinds = _kinds(_plan(d, tracks, m, templates=templates))
        assert [kinds[t.ref.item_id] for t in tracks] == ["remove", "remove", "keep"]


def test_rule_change_removes_and_adds():
    tracks, chain = _tracks()[:3], Chain([])
    m = _manifest([_entry(t, chain, FILE_CAPS) for t in tracks[:2]])
    only_b = Rule(id="b", criteria={"is": {"artist": "B"}})
    plan = _plan(_device(rules=[only_b]), tracks, m)
    assert _kinds(plan) == {"1": "remove", "2": "remove", "3": "add"}


def test_delete_threshold_in_auto_mode():
    tracks, chain = [track(str(i), source_state="s") for i in range(10)], Chain([])
    m = _manifest([_entry(t, chain, FILE_CAPS) for t in tracks])
    plan = _plan(_device(_only(tracks[:7]), delete_threshold_pct=20), tracks, m, auto=True)
    assert plan.count(ActionKind.REMOVE) == 3 and plan.needs_confirmation
    assert "removes 3 of 10" in plan.confirmation_reasons[0]
    assert not _plan(
        _device(_only(tracks[:8]), delete_threshold_pct=20), tracks, m, auto=True
    ).needs_confirmation
    manual = _plan(_device(_only(tracks[:7]), delete_threshold_pct=20), tracks, m, auto=False)
    assert not manual.needs_confirmation and manual.removals_over_threshold  # the plan page asks
    assert not _plan(
        _device(_only(tracks[:8]), delete_threshold_pct=20), tracks, m, auto=False
    ).removals_over_threshold


def test_detached_library_items_are_orphans():
    """an item of a library that is no longer attached (detached or removed) is kept as an orphan in
    both modes, like an item gone from its library; a removal is up to the user."""
    tracks, chain = _tracks()[:2], Chain([])
    other = track("x", library_id="old", source_state="s")
    m = _manifest([*(_entry(t, chain, FILE_CAPS) for t in tracks), _entry(other, chain, FILE_CAPS)])
    plan = _plan(_device(), tracks, m)
    x = next(a for a in plan.actions if a.ref.item_id == "x")
    assert x.kind == "keep" and x.orphan
    assert any("orphan" in w for w in plan.warnings)


def test_playlists_only_with_items_on_device():
    tracks = _tracks()
    pl = Playlist(
        ref=ItemRef(library_id="lib", item_id="pl1"), name="Mix", track_refs=[t.ref for t in tracks]
    )
    rules = [
        Rule(id="all", criteria={"all": []}, as_playlist="Everything"),
        Rule(id="mix", criteria={"inPlaylist": {"id": "pl1"}}, include_playlists=True),
    ]
    snap = snapshot(tracks, playlists=[pl])
    plan = plan_sync(_device(rules=rules), {"lib": snap}, None, FILE_CAPS, Chain([]), now=NOW)
    by_name = {p.name: [r.item_id for r in p.refs] for p in plan.playlists}
    assert by_name == {"Everything": ["1", "2", "3"], "Mix": ["1", "2", "3"]}  # no podcast, no wma
    no_pl = FILE_CAPS.model_copy(update={"supports_playlists": False})
    plan = plan_sync(_device(rules=rules), {"lib": snap}, None, no_pl, Chain([]), now=NOW)
    assert not plan.playlists and any("playlists skipped" in w for w in plan.warnings)


def test_playlist_changes():
    """the plan says per device playlist whether it is created, updated, removed or unchanged."""
    tracks = _tracks()
    pl = Playlist(
        ref=ItemRef(library_id="lib", item_id="pl1"), name="Mix", track_refs=[t.ref for t in tracks]
    )
    rules = [
        Rule(id="all", criteria={"all": []}, as_playlist="Everything"),
        Rule(id="mix", criteria={"inPlaylist": {"id": "pl1"}}, include_playlists=True),
    ]
    snap = snapshot(tracks, playlists=[pl])
    plan = plan_sync(_device(rules=rules), {"lib": snap}, None, FILE_CAPS, Chain([]), now=NOW)
    assert {(c.name, c.change, c.songs, c.rule) for c in plan.playlist_changes} == {
        ("Everything", "create", 3, True),
        ("Mix", "create", 3, False),
    }
    chain = Chain([])
    m = _manifest([_entry(t, chain, FILE_CAPS) for t in tracks if t.ref.item_id in "123"])
    every = next(p for p in plan.playlists if p.name == "Everything")
    m.playlists = ["Everything", "Mix", "Old"]
    m.playlist_hashes = {"Everything": playlist_hash(every.refs), "Mix": "changed", "Old": "x"}
    plan = plan_sync(_device(rules=rules), {"lib": snap}, m, FILE_CAPS, chain, now=NOW)
    assert {(c.name, c.change) for c in plan.playlist_changes} == {
        ("Everything", "same"),
        ("Mix", "update"),
        ("Old", "remove"),
    }


def test_only_attached_libraries():
    a, b = track("1", library_id="lib"), track("1", library_id="other")
    dev = _device()
    plan = plan_sync(
        dev,
        {"lib": snapshot([a]), "other": snapshot([b], library_id="other")},
        None,
        FILE_CAPS,
        Chain([]),
        now=NOW,
    )
    assert [x.ref.key() for x in plan.actions] == ["lib/1"]


def test_processor_without_a_step_keeps_the_item():
    """a processor that plans no step for an item does not change its chain hash."""
    tracks = _tracks()[:3]  # two MP3s, one FLAC
    retag = Retag({}, None)
    flac_rule = {"rules": [{"from": ["flac"], "to": "aac", "when": "always"}]}
    caps = FILE_CAPS.model_copy(update={"formats": {"mp3", "flac", "aac"}})
    m = _manifest([_entry(t, Chain([retag]), caps) for t in tracks])

    # "if needed": the device plays FLAC, so the transcoder does nothing at all
    if_needed = {"rules": [{**flac_rule["rules"][0], "when": "not_supported"}]}
    noop = Chain([Transcode(if_needed, None), retag])
    assert set(_kinds(_plan(_device(), tracks, m, caps=caps, chain=noop)).values()) == {"keep"}
    # "always" wins and applies to the FLAC only: the MP3s stay
    flac_only = Chain([Transcode(flac_rule, None), retag])
    assert _kinds(_plan(_device(), tracks, m, caps=caps, chain=flac_only)) == {
        "1": "keep",
        "2": "keep",
        "3": "replace",
    }
    # its settings change: still only the FLAC
    m2 = _manifest([_entry(t, flac_only, caps) for t in tracks])
    lower = Chain(
        [
            Transcode(
                {
                    "rules": [{**flac_rule["rules"][0], "quality": {"bitrate_kbps": 128}}],
                },
                None,
            ),
            retag,
        ]
    )
    assert _kinds(_plan(_device(), tracks, m2, caps=caps, chain=lower)) == {
        "1": "keep",
        "2": "keep",
        "3": "replace",
    }
    # a setting that changes every item's output still replaces every item
    v23 = Chain([Transcode(flac_rule, None), Retag({"id3v23": True}, None)])
    assert set(_kinds(_plan(_device(), tracks, m, caps=caps, chain=v23)).values()) == {"replace"}


def test_transcode_rule_that_matches_nothing_keeps_every_item():
    """only the decision for an item counts, not the whole rule list."""
    tracks = _tracks()[:3]  # two MP3s, one FLAC
    caps = FILE_CAPS.model_copy(update={"formats": {"mp3", "flac", "aac"}})
    flac = {"from": ["flac"], "to": "aac", "when": "always"}

    def chain(*rules: dict) -> Chain:
        return Chain([Transcode({"rules": list(rules)}, None)])

    m = _manifest([_entry(t, chain(flac), caps) for t in tracks])
    opus = {"from": ["opus"], "to": "mp3", "when": "always"}  # no opus in the library
    assert set(
        _kinds(_plan(_device(), tracks, m, caps=caps, chain=chain(flac, opus))).values()
    ) == {"keep"}
    # the same output spelt differently (explicit default bitrate, or the rule moved after another one)
    same = {**flac, "quality": {"bitrate_kbps": 256}}
    assert set(
        _kinds(_plan(_device(), tracks, m, caps=caps, chain=chain(opus, same))).values()
    ) == {"keep"}
    # a changed target or encoder mode replaces the FLAC only
    for changed in ({**flac, "to": "mp3"}, {**flac, "quality": {"vbr": 4}}):
        assert _kinds(_plan(_device(), tracks, m, caps=caps, chain=chain(opus, changed))) == {
            "1": "keep",
            "2": "keep",
            "3": "replace",
        }, changed


def test_every_change_says_why():
    """an add names the rule that selects it; a removal says whether an exclude took it or no rule
    selects it any more (naming the rule that had put it there)."""
    tracks, chain = _tracks()[:3], Chain([])
    m = _manifest(
        [_entry(tracks[1], chain, FILE_CAPS), _entry(tracks[2], chain, FILE_CAPS, rules=["old"])]
    )
    rules = [Rule(id="a", name="Artist A", criteria={"is": {"artist": "A"}})]
    excludes = [Rule(id="no2", name="Not Two", criteria={"is": {"title": "Two"}})]
    device = _device(rules=rules)
    device.rule_set.excludes = excludes
    plan = _plan(device, tracks, m)
    why = {a.ref.item_id: (a.kind, a.reason) for a in plan.actions}
    assert why["1"] == (ActionKind.ADD, "selected by “Artist A”")
    assert why["2"] == (ActionKind.REMOVE, "excluded by “Not Two”")
    assert why["3"] == (ActionKind.REMOVE, "no rule selects it any more (was “old”)")
    # the library still knows it: named by its title and artist - album, not its file
    three = next(a for a in plan.actions if a.ref.item_id == "3")
    assert (three.title, three.subtitle) == (
        tracks[2].title,
        " - ".join(x for x in (tracks[2].artist, tracks[2].album) if x),
    )


def test_a_kept_item_says_which_rule_still_selects_it():
    """the plan's Keep list says why an item stays."""
    tracks, chain = _tracks()[:1], Chain([])
    m = _manifest([_entry(tracks[0], chain, FILE_CAPS)])
    rules = [Rule(id="a", name="Artist A", criteria={"is": {"artist": "A"}})]
    (a,) = _plan(_device(rules=rules), tracks, m).actions
    assert (a.kind, a.reason) == (ActionKind.KEEP, "still selected by “Artist A”")


def test_a_removal_names_the_template_that_had_put_it_there():
    """an item whose template is no longer linked says which template it was in, by its name."""
    from syncmusic.core.models import RuleTemplate

    tracks, chain = _tracks()[:1], Chain([])
    rid = "default.papka.all-podcast"
    m = _manifest([_entry(tracks[0], chain, FILE_CAPS, rules=[rid])])
    tpl = RuleTemplate(
        id="default.papka", name="All podcasts", rules=[Rule(id=rid, name="Every podcast file")]
    )
    nobody = [Rule(id="x", name="X", criteria={"is": {"artist": "nobody"}})]
    plan = _plan(_device(rules=nobody), tracks, m, templates={tpl.id: tpl})
    (a,) = plan.actions
    assert (a.kind, a.reason) == (
        ActionKind.REMOVE,
        "no template selects it any more (was in “All podcasts”)",
    )


def test_stock_ipod_skips_non_mp4_video_without_convert_video():
    """an MKV would fail at put on a stock iPod; without Convert video it is skipped at plan time."""
    caps = IPOD_CAPS.model_copy(update={"supports_video": True, "video_profile": "ipod"})
    vids = [
        track("v1", title="Film", kind=MediaKind.VIDEO, format="mkv", source_state="v1"),
        track("v2", title="Ep", kind=MediaKind.VIDEO, format="m4v", source_state="v2"),
    ]
    plan = _plan(_device(), vids, None, caps=caps)
    assert _kinds(plan) == {"v2": "add"}
    assert any("format not playable" in w and "Film (mkv)" in w for w in plan.warnings)


def test_plan_lists_listening_data_sent_back():
    """a dry read of the device's feedback shows plays, ratings, played state and positions going
    to the library; a bookmark moved on the device replaces the forward one; both sides changed → a conflict."""
    from datetime import timedelta

    from syncmusic.core.models import DeviceFeedback, PlayEvent, PositionInfo, ReverseSyncSettings
    from syncmusic.core.planner import reverse_changes

    caps = IPOD_CAPS.model_copy(update={"supports_ratings_write": True})
    song = track("1", title="One", source_state="s1", play_count=2, rating=3)
    pod = track("4", title="Pod", kind=MediaKind.PODCAST, source_state="s4", position_s=300.0)
    m = _manifest(
        [
            _entry(
                song, Chain([]), caps, meta=meta_snapshot(song), last_rating=2, last_playcount=2
            ),
            _entry(
                pod,
                Chain([]),
                caps,
                kind=MediaKind.PODCAST,
                meta=meta_snapshot(pod),
                last_position_s=100,
            ),
        ]
    )
    plan = _plan(_device(), [song, pod], m, caps=caps)
    assert {c.field for a in plan.actions for c in a.changes} == {
        "rating",
        "position",
    }  # forward only
    fb = DeviceFeedback(
        plays=[
            PlayEvent(ref=song.ref, played_at=NOW),
            PlayEvent(ref=song.ref, played_at=NOW - timedelta(hours=1)),
        ],
        ratings={song.ref.key(): 5},
        positions={pod.ref.key(): PositionInfo(position_s=1800, finished=True)},
    )
    reverse_changes(plan, fb, ReverseSyncSettings(), "device")
    got = {
        a.ref.item_id: [(c.field, c.old, c.new, c.direction, bool(c.conflict)) for c in a.changes]
        for a in plan.actions
    }
    assert got["1"] == [
        ("play_count", "2", "4", "to_library", False),
        ("rating", "3", "5", "to_library", True),
    ]
    assert got["4"] == [
        ("played", "unplayed", "played", "to_library", False),
        ("position", "300", "1800", "to_library", True),
    ]


def test_mass_processing_change_asks_in_auto_mode():
    """a SyncMusic update that re-processes most items stops an automatic sync and asks."""
    tracks, chain = [track(str(i), source_state="s") for i in range(10)], Chain([])
    entries = [_entry(t, chain, FILE_CAPS, chain_hash="old") for t in tracks[:3]]
    m = _manifest(entries + [_entry(t, chain, FILE_CAPS) for t in tracks[3:]])
    auto = _plan(_device(delete_threshold_pct=20), tracks, m, auto=True)
    assert auto.count(ActionKind.REPLACE) == 3 and auto.needs_confirmation
    assert "the processing changed for 3 of 10 items:" in auto.confirmation_reasons[0]
    # it may be the user's own edit, not an update
    assert "SyncMusic update" not in auto.confirmation_reasons[0]
    # a manual plan asks too
    assert _plan(_device(delete_threshold_pct=20), tracks, m).needs_confirmation
    # source changes are library changes: they never ask
    m2 = _manifest(
        [_entry(t, chain, FILE_CAPS, source_state="old") for t in tracks[:3]]
        + [_entry(t, chain, FILE_CAPS) for t in tracks[3:]]
    )
    assert not _plan(_device(delete_threshold_pct=20), tracks, m2, auto=True).needs_confirmation


def _processed_entry(t, chain, caps):
    item = initial_plan_item(t)
    return _entry(t, chain, caps, processing=chain.processing(item, caps))


def test_processing_change_names_the_processor_and_tags_only():
    """the plan says which processor changed and whether only the tags (not the audio) differ."""
    caps = DeviceCapabilities(formats={"mp3"})
    t = track("1", format="flac", source_state="s")
    to_mp3 = {"rules": [{"to": "mp3", "quality": {"vbr": 2}}]}
    old = Chain([Transcode(to_mp3, None), Retag({}, None)])
    m = _manifest([_processed_entry(t, old, caps)])
    assert _kinds(_plan(_device(), [t], m, caps=caps, chain=old)) == {"1": "keep"}

    retag = Chain([Transcode(to_mp3, None), Retag({"id3v23": True}, None)])
    a = _plan(_device(), [t], m, caps=caps, chain=retag).actions[0]
    assert a.kind is ActionKind.REPLACE and a.tags_only
    assert a.reason == "processing changed: retag (tags only)"
    assert a.processing == retag.processing(initial_plan_item(t), caps)

    v0 = {"rules": [{"to": "mp3", "quality": {"vbr": 0}}]}
    audio = Chain([Transcode(v0, None), Retag({"id3v23": True}, None)])
    a = _plan(_device(), [t], m, caps=caps, chain=audio).actions[0]
    assert not a.tags_only and a.reason == "processing changed: retag, transcode"
    # the auto-sync confirmation names the changed processors
    auto = _plan(_device(delete_threshold_pct=0), [t], m, caps=caps, chain=retag, auto=True)
    assert auto.confirmation_reasons == [
        "the processing changed for 1 of 1 items (retag): they would be processed and copied again"
        " - retag: previous values unknown (stored from now on)"  # a manifest without inputs
    ]

    other_caps = caps.model_copy(update={"formats": {"mp3", "aac"}})
    a = _plan(_device(), [t], m, caps=other_caps, chain=old).actions[0]
    assert not a.tags_only and "device" in a.reason


def test_new_caps_field_does_not_replace():
    """a caps field that does not change the file (track limit, RTC, a field at its default)
    keeps every item; a playable-format change still re-processes."""
    caps = DeviceCapabilities(formats={"mp3"})
    t = track("1", format="flac", source_state="s")
    chain = Chain([Transcode({"rules": [{"to": "mp3", "quality": {"vbr": 2}}]}, None)])
    m = _manifest([_processed_entry(t, chain, caps)])
    limits = caps.model_copy(update={"max_tracks": 5, "has_rtc": True, "path_rules": PathRules()})
    assert _kinds(_plan(_device(), [t], m, caps=limits, chain=chain)) == {"1": "keep"}
    aac = caps.model_copy(update={"formats": {"mp3", "aac"}})
    assert _kinds(_plan(_device(), [t], m, caps=aac, chain=chain)) == {"1": "replace"}


def test_old_caps_hash_scheme_is_rehashed_not_replaced():
    """entries whose "device" part was hashed over the whole caps dump are kept on the first plan
    after the fix; the plan carries the new hash for the applier to store. Other parts changed: replaced."""
    caps = DeviceCapabilities(formats={"mp3"})
    t = track("1", format="flac", source_state="s")
    chain = Chain([Transcode({"rules": [{"to": "mp3", "quality": {"vbr": 2}}]}, None)])
    entry = _processed_entry(t, chain, caps)
    old_device = {**entry.processing, "device": "0123456789abcdef"}
    legacy = entry.model_copy(
        update={"chain_hash": "legacy", "processing": old_device, "caps_v": 0}
    )
    a = _plan(_device(), [t], _manifest([legacy]), caps=caps, chain=chain).actions[0]
    assert a.kind is ActionKind.KEEP and a.rehash and a.chain_hash == entry.chain_hash
    current = legacy.model_copy(update={"caps_v": CAPS_HASH_VERSION})
    assert _kinds(_plan(_device(), [t], _manifest([current]), caps=caps, chain=chain)) == {
        "1": "replace"
    }
    v0 = Chain([Transcode({"rules": [{"to": "mp3", "quality": {"vbr": 0}}]}, None)])
    assert _kinds(_plan(_device(), [t], _manifest([legacy]), caps=caps, chain=v0)) == {
        "1": "replace"
    }


def test_entries_without_processing_parts_say_processing_changed():
    caps = DeviceCapabilities(formats={"mp3"})
    t = track("1", format="flac", source_state="s")
    m = _manifest([_entry(t, Chain([]), caps, chain_hash="old")])
    to_mp3 = Chain([Transcode({"rules": [{"to": "mp3"}]}, None)])
    a = _plan(_device(), [t], m, caps=caps, chain=to_mp3).actions[0]
    assert a.reason == "processing changed" and not a.tags_only


# --- tests that kill surviving mutants ---


def test_path_layout_change_replaces_at_the_new_path():
    """an item written with another path template is re-written (no test covered this branch)."""
    caps = FILE_CAPS.model_copy(
        update={
            "path_layout": {MediaKind.MUSIC: "{artist}/{title}"},
            "path_layout_multi_disc": "{artist}/CD{disc}/{title}",
        }
    )
    t = track("1", source_state="s")
    old = _manifest([_entry(t, Chain([]), caps, layout="{title}", size=1_000_000)])
    a = _plan(_device(), [t], old, caps=caps).actions[0]
    assert (a.kind, a.reason) == (ActionKind.REPLACE, "path layout changed")
    assert a.size_delta == t.size_bytes - 1_000_000 and a.changes == []
    same = _manifest([_entry(t, Chain([]), caps, layout="{artist}/{title}")])
    assert _kinds(_plan(_device(), [t], same, caps=caps)) == {"1": "keep"}
    # a multi-disc release uses its own template
    two = t.model_copy(update={"disc_total": 2})
    multi = _manifest([_entry(two, Chain([]), caps, layout="{artist}/CD{disc}/{title}")])
    assert _kinds(_plan(_device(), [two], multi, caps=caps)) == {"1": "keep"}
    assert _kinds(_plan(_device(), [two], same, caps=caps)) == {"1": "replace"}
    # a device without a template for the kind keeps whatever layout the file has
    assert _kinds(_plan(_device(), [t], old, caps=FILE_CAPS)) == {"1": "keep"}


def test_a_removed_processor_is_named_as_changed():
    caps = DeviceCapabilities(formats={"mp3"})
    t = track("1", format="flac", source_state="s")
    to_mp3 = {"rules": [{"to": "mp3", "quality": {"vbr": 2}}]}
    m = _manifest([_processed_entry(t, Chain([Transcode(to_mp3, None), Retag({}, None)]), caps)])
    a = _plan(_device(), [t], m, caps=caps, chain=Chain([Transcode(to_mp3, None)])).actions[0]
    # a processor no longer in the chain counts as an audio change (conservative)
    assert a.kind is ActionKind.REPLACE and a.reason == "processing changed: retag"


def test_update_meta_has_no_size_delta_and_lists_the_changes():
    t = _tracks()[0]
    m = _manifest([_entry(t, Chain([]), IPOD_CAPS, size=1)])
    a = _plan(_device(), [t.model_copy(update={"title": "New"})], m, caps=IPOD_CAPS).actions[0]
    assert a.kind is ActionKind.UPDATE_META and a.size_delta == 0
    assert a.changes and a.reason == "metadata changed"
    keep = _plan(_device(), [t], m, caps=IPOD_CAPS).actions[0]
    assert keep.kind is ActionKind.KEEP and keep.size_delta == 0


def test_missing_on_device_add_says_so():
    t = _tracks()[0]
    m = _manifest([_entry(t, Chain([]), FILE_CAPS)])
    a = _plan(_device(), [t], m, missing={t.ref.key()}).actions[0]
    assert a.kind is ActionKind.ADD and a.reason.startswith("missing on the device · selected by")


def test_over_delete_threshold_with_an_empty_device():
    from syncmusic.core.planner import over_delete_threshold

    assert not over_delete_threshold(1, 0, 10)
    assert over_delete_threshold(2, 10, 10) and not over_delete_threshold(1, 10, 10)
