"""Reverse sync service: collect, dedup, push, pending retries, commit, reset_feedback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from syncmusic.base.capabilities import LibraryCapabilities
from syncmusic.base.device import DeviceSession
from syncmusic.base.feedback import FeedbackRejected, FeedbackSink, SubmitResult
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import (
    DeviceConfig,
    DeviceFeedback,
    ItemRef,
    LibraryConfig,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    MediaKind,
    PlayEvent,
    PositionInfo,
    ReverseSyncSettings,
)
from syncmusic.core.reverse import MAX_ATTEMPTS, ReverseSync
from syncmusic.infra.db import Database
from syncmusic.infra.store import Store
from tests.fakes import FakeDevice, FakeLibrary

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
A, B = ItemRef(library_id="nd", item_id="a"), ItemRef(library_id="nd", item_id="b")
LOCAL = ItemRef(library_id="folder", item_id="c")


class SinkLibrary(FakeLibrary, FeedbackSink):
    """A fake server that takes plays, ratings and positions; `fail` = item ids it rejects (permanent, e.g. deleted),
    `down` = everything fails transiently."""

    sink_name = "fakesink"
    capabilities = LibraryCapabilities(
        kinds={MediaKind.MUSIC, MediaKind.PODCAST},
        supports_scrobble=True,
        supports_ratings=True,
        supports_positions=True,
    )

    def __init__(self, config, ctx) -> None:
        super().__init__(config, ctx)
        self.plays: list[PlayEvent] = []
        self.ratings: dict[str, int | None] = {}
        self.positions: dict[str, tuple[float, bool]] = {}
        self.fail: set[str] = set()
        self.down = False

    async def submit_plays(self, plays: list[PlayEvent]) -> SubmitResult:
        res = SubmitResult()
        for p in plays:
            if self.down:
                res.failed.append((p, "down"))
            elif p.ref.item_id in self.fail:
                res.rejected.append((p, "not found"))
            else:
                self.plays.append(p)
                res.accepted.append(p)
        return res

    async def set_rating(self, ref: ItemRef, rating: int | None) -> None:
        self._check(ref)
        self.ratings[ref.item_id] = rating

    async def set_position(self, ref: ItemRef, position_s: float, finished: bool) -> None:
        self._check(ref)
        self.positions[ref.item_id] = (position_s, finished)

    async def play_count(self, ref: ItemRef) -> int | None:
        return sum(1 for p in self.plays if p.ref == ref)

    def _check(self, ref: ItemRef) -> None:
        if self.down:
            raise ConnectionError("down")
        if ref.item_id in self.fail:
            raise FeedbackRejected("not found")


class ScriptedDevice(FakeDevice):
    """read_feedback returns `self.feedback` once (like a device whose data was consumed); records the order."""

    type_name: ClassVar[str] = "fake"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.feedback = DeviceFeedback()
        self.events: list[str] = []
        self.resets: list[DeviceFeedback] = []

    async def read_feedback(self, manifest: Manifest | None) -> DeviceFeedback:
        return self.feedback.model_copy(deep=True)

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        await super().commit(s, manifest)
        self.events.append("commit")

    async def reset_feedback(self, s: DeviceSession, consumed: DeviceFeedback) -> None:
        self.events.append("reset")
        self.resets.append(consumed)
        self.feedback = DeviceFeedback()  # the device forgets what was consumed


@pytest.fixture
async def store(tmp_path):
    db = Database(tmp_path / "sd.db")
    await db.open()
    yield Store(db)
    await db.close()


@pytest.fixture
def env(tmp_path, store):
    root = tmp_path / "lib"
    root.mkdir()
    sink = SinkLibrary(
        LibraryConfig(id="nd", type="fake", name="ND", settings={"root": str(root)}), None
    )
    folder = FakeLibrary(
        LibraryConfig(id="folder", type="fake", name="F", settings={"root": str(root)}), None
    )
    mount = tmp_path / "dev"
    mount.mkdir()
    dev = ScriptedDevice(DeviceConfig(id="dev", type="fake", name="D"), mount, None)
    manifest = Manifest(header=ManifestHeader(device_id="dev"))
    for ref in (A, B, LOCAL):
        manifest.items[ref.key()] = ManifestEntry(
            ref=ref, device_ref=ref.item_id, path=f"{ref.item_id}.mp3", size=1, duration_s=200
        )
    manifest_io.write(mount, manifest)
    cfg = DeviceConfig(id="dev", type="fake", name="D", library_ids=["nd", "folder"])
    return ReverseSync(store), cfg, dev, {"nd": sink, "folder": folder}, sink


def _plays(ref: ItemRef, n: int, device: str = "dev") -> list[PlayEvent]:
    return [
        PlayEvent(ref=ref, played_at=T0 + timedelta(minutes=5 * i), source_device=device)
        for i in range(n)
    ]


async def test_plays_pushed_once_and_manifest_updated(env):
    rs, cfg, dev, libs, sink = env
    dev.feedback = DeviceFeedback(plays=_plays(A, 2) + _plays(B, 1), play_counts={A.key(): 7})
    rep = await rs.run(cfg, dev, libs)
    assert (rep.plays_found, rep.plays_submitted, rep.plays_duplicate, rep.pending) == (3, 3, 0, 0)
    assert len(sink.plays) == 3
    m = manifest_io.read(dev.mount)
    assert m.items[A.key()].last_playcount == 7  # absolute count from the device wins
    assert m.items[B.key()].last_playcount == 1  # no absolute count: += plays
    assert dev.events == ["commit", "reset"]  # manifest first, then the device's data is consumed

    # the same plays read again (crash before the device was consumed): deduplicated
    dev.feedback = DeviceFeedback(plays=_plays(A, 2))
    rep = await rs.run(cfg, dev, libs)
    assert (rep.plays_submitted, rep.plays_duplicate) == (0, 2) and len(sink.plays) == 3


async def test_same_time_on_another_device_is_another_play(env, store):
    rs, cfg, dev, libs, sink = env
    dev.feedback = DeviceFeedback(plays=_plays(A, 1))
    await rs.run(cfg, dev, libs)
    other = cfg.model_copy(update={"id": "dev2"})
    dev.feedback = DeviceFeedback(plays=_plays(A, 1, device="dev2"))
    rep = await rs.run(other, dev, libs)
    assert rep.plays_submitted == 1 and len(sink.plays) == 2


async def test_failures_kept_pending_and_retried_first(env, store):
    rs, cfg, dev, libs, sink = env
    sink.down = True
    dev.feedback = DeviceFeedback(
        plays=_plays(A, 2),
        ratings={A.key(): 4},
        positions={B.key(): PositionInfo(position_s=30.0)},
    )
    rep = await rs.run(cfg, dev, libs)
    assert rep.pending == 4 and rep.plays_submitted == 0 and rep.errors
    assert {r.kind for r in await store.pending_feedback("dev")} == {"play", "rating", "position"}
    # the device was consumed anyway: the data is safe in the database before that
    assert dev.events == ["commit", "reset"] and dev.feedback.is_empty()
    m = manifest_io.read(dev.mount)
    assert m.items[A.key()].last_playcount == 2 and m.items[A.key()].last_rating == 4

    sink.down = False
    rep = await rs.run(cfg, dev, libs)
    assert (rep.plays_submitted, rep.ratings_pushed, rep.positions_pushed, rep.pending) == (
        2,
        1,
        1,
        0,
    )
    assert sink.ratings == {"a": 4} and sink.positions == {"b": (30.0, False)}
    assert await store.pending_feedback("dev") == []
    assert dev.events == ["commit", "reset"]  # nothing new on the device: no second commit


async def test_partial_failure_only_failed_items_pending(env, store):
    rs, cfg, dev, libs, sink = env
    sink.fail = {"b"}
    dev.feedback = DeviceFeedback(plays=_plays(A, 1) + _plays(B, 2))
    rep = await rs.run(cfg, dev, libs)
    assert rep.plays_submitted == 1 and rep.pending == 2
    rows = await store.pending_feedback("dev")
    assert [PlayEvent.model_validate(r.data).ref for r in rows] == [B, B]
    assert all(r.attempts == 1 and r.last_error == "not found" for r in rows)


async def test_newer_rating_supersedes_pending(env, store):
    rs, cfg, dev, libs, sink = env
    sink.down = True
    dev.feedback = DeviceFeedback(ratings={A.key(): 2})
    await rs.run(cfg, dev, libs)
    sink.down = False
    dev.feedback = DeviceFeedback(ratings={A.key(): 5})
    rep = await rs.run(cfg, dev, libs)
    assert rep.ratings_pushed == 1 and sink.ratings == {"a": 5}
    assert [(c.kind, c.ref, c.value) for c in rep.changes] == [("rating", A.key(), 5)]
    assert await store.pending_feedback("dev") == []


async def test_unavailable_library_keeps_data_pending(env, store):
    rs, cfg, dev, libs, sink = env
    dev.feedback = DeviceFeedback(plays=_plays(A, 1))
    rep = await rs.run(cfg, dev, {**libs, "nd": None})
    assert rep.pending == 1 and "not available" in rep.errors[0]
    rep = await rs.run(cfg, dev, libs)
    assert rep.plays_submitted == 1 and rep.pending == 0


async def test_dropped_after_max_attempts(env, store):
    rs, cfg, dev, libs, sink = env
    sink.fail = {"a"}  # e.g. the song was deleted from the library
    dev.feedback = DeviceFeedback(plays=_plays(A, 1))
    for _ in range(MAX_ATTEMPTS - 1):
        rep = await rs.run(cfg, dev, libs)
        assert rep.pending == 1
    rep = await rs.run(cfg, dev, libs)
    assert rep.pending == 0 and any("dropped after" in e for e in rep.errors)
    assert await store.pending_feedback("dev") == []
    # kept for the UI's "Not delivered" list with its reason
    assert rep.dropped == 1
    (d,) = await store.dropped_feedback("dev")
    assert d["kind"] == "play" and d["attempts"] == MAX_ATTEMPTS and d["error"]
    assert await store.feedback_counts() == {"dev": (0, 1)}
    await store.clear_dropped_feedback("dev")
    assert await store.dropped_feedback("dev") == []


async def test_pending_rows_say_when_they_were_tried(env, store):
    """a failed delivery stores its error and time; the report counts what waits per library."""
    rs, cfg, dev, libs, sink = env
    sink.down = True
    dev.feedback = DeviceFeedback(plays=_plays(A, 2), ratings={A.key(): 4})
    rep = await rs.run(cfg, dev, libs)
    assert rep.pending == 3 and rep.pending_by == {"nd": {"play": 2, "rating": 1}}
    rows = await store.pending_feedback("dev")
    assert all(r.tried_at and r.created_at and r.last_error for r in rows)
    assert await store.feedback_counts() == {"dev": (3, 0)}


async def test_unavailable_library_is_named_with_its_cause(env, store):
    """rows of a library that is down keep 'Navidrome - тест is not reachable: <cause>' (given by the
    caller), not the library id; every failed try is counted."""
    rs, cfg, dev, libs, sink = env
    dev.feedback = DeviceFeedback(plays=_plays(A, 1))
    why = {"nd": "Navidrome - тест is not reachable: connection refused"}
    for _ in range(2):
        await rs.run(cfg, dev, {**libs, "nd": None}, why)
        dev.feedback = DeviceFeedback()
    (r,) = await store.pending_feedback("dev")
    assert r.last_error == why["nd"] and r.tries == 2 and r.attempts == 0


async def test_detached_library_dropped(env, store):
    rs, cfg, dev, libs, sink = env
    dev.feedback = DeviceFeedback(plays=_plays(A, 1))
    rep = await rs.run(cfg, dev, {"folder": libs["folder"]})
    assert rep.pending == 0 and "not attached" in rep.errors[0]
    assert await store.pending_feedback("dev") == []


async def test_folder_library_gets_no_reverse_sync(env, store):
    """a library that is not a FeedbackSink (folder) gets no reverse sync - its plays, ratings and
    positions are consumed from the device, never pending or stored; rows captured before are dropped."""
    rs, cfg, dev, libs, sink = env
    dev.feedback = DeviceFeedback(
        plays=_plays(LOCAL, 3),
        ratings={LOCAL.key(): 3},
        positions={LOCAL.key(): PositionInfo(position_s=12.0)},
    )
    rep = await rs.run(cfg, dev, libs)
    assert rep.pending == 0 and rep.plays_submitted == 0 and rep.errors == [] and sink.plays == []
    assert await store.local_feedback("folder", "c") is None
    assert await store.pending_feedback("dev") == []
    assert dev.events == ["commit", "reset"]  # consumed all the same
    # pending from an older version (or while the folder was missing): dropped without an error
    await store.capture_feedback(
        "dev", [("play", _plays(LOCAL, 1)[0].model_dump(mode="json"))], [], None
    )
    assert len(await store.pending_feedback("dev")) == 1
    rep = await rs.run(cfg, dev, libs)
    assert (
        rep.pending == 0 and rep.errors == [] and await store.local_feedback("folder", "c") is None
    )


async def test_positions_newest_last(env):
    rs, cfg, dev, libs, sink = env
    order: list[str] = []

    async def record(ref, position_s, finished):
        order.append(ref.item_id)

    sink.set_position = record  # type: ignore[method-assign]
    dev.feedback = DeviceFeedback(
        positions={
            A.key(): PositionInfo(position_s=1, updated_at=T0 + timedelta(hours=1)),
            B.key(): PositionInfo(position_s=2, updated_at=T0),
        }
    )
    await rs.run(cfg, dev, libs)
    assert order == ["b", "a"]


async def test_nothing_read_nothing_written(env):
    rs, cfg, dev, libs, sink = env
    rep = await rs.run(cfg, dev, libs)
    assert rep.plays_found == 0 and rep.pending == 0 and rep.errors == []
    assert dev.events == [] and dev.resets == []


async def test_disabled_kinds_not_pushed_but_consumed(env):
    rs, cfg, dev, libs, sink = env
    cfg = cfg.model_copy(update={"reverse_sync": ReverseSyncSettings(ratings=False)})
    dev.feedback = DeviceFeedback(plays=_plays(A, 1), ratings={A.key(): 5})
    rep = await rs.run(cfg, dev, libs)
    assert rep.plays_submitted == 1 and sink.ratings == {}
    assert dev.events == ["commit", "reset"]
    off = cfg.model_copy(update={"reverse_sync": ReverseSyncSettings(enabled=False)})
    dev.feedback = DeviceFeedback(plays=_plays(B, 1))
    await rs.run(off, dev, libs)
    assert len(sink.plays) == 1 and not dev.feedback.is_empty()  # disabled: the device is untouched


async def test_cancel_during_submit_does_not_duplicate_plays(env):
    """A job cancel (or kill) while plays are being scrobbled: the ones the server already took must not be
    sent again by the next run (Navidrome counts every scrobble)."""
    import asyncio

    rs, cfg, dev, libs, sink = env
    dev.feedback = DeviceFeedback(plays=_plays(A, 3))
    real = sink.submit_plays

    async def cancelled_after_first(plays: list[PlayEvent]) -> SubmitResult:
        sink.plays.append(plays[0])  # the server took the first scrobble, then the job is cancelled
        raise asyncio.CancelledError

    sink.submit_plays = cancelled_after_first  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await rs.run(cfg, dev, libs)
    sink.submit_plays = real  # type: ignore[method-assign]
    await rs.run(cfg, dev, libs)
    assert len(sink.plays) == 3, [p.played_at for p in sink.plays]


async def test_failed_device_commit_does_not_duplicate_estimated_plays(env):
    """Plays delivered, then the device commit fails (unplugged while writing the manifest). The device still
    holds the data; estimated timestamps (tagcache deltas, iPod Play Counts without a time) differ on the next
    read, so `submitted_plays` cannot dedup them: the next run must not deliver them a second time."""
    rs, cfg, dev, libs, sink = env
    reads = 0

    async def estimated(manifest: Manifest | None) -> DeviceFeedback:
        nonlocal reads
        reads += 1  # estimates walk back from "now", which moves between runs
        start = T0 + timedelta(minutes=reads)
        return DeviceFeedback(
            plays=[
                PlayEvent(ref=A, played_at=start + timedelta(minutes=5 * i), source_device="dev")
                for i in range(2)
            ],
            play_counts={A.key(): 2},
        )

    dev.read_feedback = estimated  # type: ignore[method-assign]
    real_commit = dev.commit

    async def unplugged(s: DeviceSession, manifest: Manifest) -> None:
        raise OSError(5, "Input/output error")

    dev.commit = unplugged  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await rs.run(cfg, dev, libs)
    dev.commit = real_commit  # type: ignore[method-assign]
    await rs.run(cfg, dev, libs)
    assert len(sink.plays) == 2, [p.played_at for p in sink.plays]


async def test_server_outage_never_drops_plays(env, store):
    """MAX_ATTEMPTS is for items that can never be delivered (song deleted). Runs while the server is down or the
    library is unavailable (an outage over several reconnects / auto syncs) must not count towards it."""
    rs, cfg, dev, libs, sink = env
    dev.feedback = DeviceFeedback(plays=_plays(A, 1))
    for _ in range(MAX_ATTEMPTS // 2):
        await rs.run(cfg, dev, {**libs, "nd": None})  # library not available
    sink.down = True
    for _ in range(MAX_ATTEMPTS):
        await rs.run(cfg, dev, libs)  # server answers with errors
    sink.down = False
    rep = await rs.run(cfg, dev, libs)
    assert rep.plays_submitted == 1 and len(sink.plays) == 1, rep.errors


async def test_transient_rating_failures_do_not_count(env, store):
    """A rating the server cannot take while it is down stays pending; only rejections count."""
    rs, cfg, dev, libs, sink = env
    dev.feedback = DeviceFeedback(ratings={A.key(): 4})
    sink.down = True
    for _ in range(MAX_ATTEMPTS + 2):
        rep = await rs.run(cfg, dev, libs)
        dev.feedback = DeviceFeedback()
    assert rep.pending == 1 and [r.attempts for r in await store.pending_feedback("dev")] == [0]
    sink.down = False
    rep = await rs.run(cfg, dev, libs)
    assert rep.ratings_pushed == 1 and sink.ratings == {"a": 4}
    assert await store.pending_feedback("dev") == []


def test_uncaptured_plays_keeps_the_newest_beyond_the_journal():
    from syncmusic.core.reverse import _uncaptured_plays

    plays = list(reversed(_plays(A, 3))) + _plays(B, 2)
    fb = DeviceFeedback(plays=plays, play_counts={A.key(): 5, B.key(): 2})
    # 4 of A's 5 were captured before: only the newest A play is new; B is not in the journal
    out = _uncaptured_plays(fb, {A.key(): 4})
    assert [(p.ref, p.played_at) for p in out] == [(A, T0 + timedelta(minutes=10))] + [
        (B, p.played_at) for p in _plays(B, 2)
    ]
    assert [p.ref for p in _uncaptured_plays(fb, {A.key(): 5, B.key(): 2})] == []
    no_count = DeviceFeedback(plays=_plays(A, 2))  # no absolute count: nothing to compare with
    assert len(_uncaptured_plays(no_count, {A.key(): 9})) == 2


async def test_journal_cleared_after_a_successful_commit(env, store):
    rs, cfg, dev, libs, sink = env
    dev.feedback = DeviceFeedback(plays=_plays(A, 2), play_counts={A.key(): 2})
    await rs.run(cfg, dev, libs)
    assert await store.feedback_journal("dev") is None and len(sink.plays) == 2


async def test_orphans_are_skipped(env, store):
    """plays / ratings / positions of an orphan (gone from its library) are consumed, never delivered or
    kept pending."""
    rs, cfg, dev, libs, sink = env
    m = manifest_io.read(dev.mount)
    m.items[B.key()].orphan = True
    manifest_io.write(dev.mount, m)
    cfg = cfg.model_copy(
        update={"sync": cfg.sync.model_copy(update={"rating_priority": "device"})}, deep=True
    )
    dev.feedback = DeviceFeedback(
        plays=_plays(A, 1) + _plays(B, 2),
        ratings={B.key(): 4},
        positions={B.key(): PositionInfo(position_s=30)},
    )
    rep = await rs.run(cfg, dev, libs)
    assert [p.ref for p in sink.plays] == [A] and sink.ratings == {} and sink.positions == {}
    assert rep.pending == 0 and await store.pending_feedback("dev") == []
    assert dev.events == ["commit", "reset"]  # consumed from the device all the same


async def test_position_scopes_are_replaced_per_library_and_go_with_the_device(store):
    """a run records one library's scopes without touching another library's; deleting the device
    deletes its record."""
    await store.set_position_scopes("dev", "abs", {"abs/b1": 700.0, "abs/p1:e1": 5.0})
    await store.set_position_scopes("dev", "abs2", {"abs2/b1": 1.0})
    await store.set_position_scopes("dev", "abs", {"abs/b1": 1500.0})
    await store.set_position_scopes("other", "abs", {"abs/b1": 3.0})
    assert await store.position_scopes("dev") == {"abs/b1": 1500.0, "abs2/b1": 1.0}
    await store.delete_device("dev")
    assert await store.position_scopes("dev") == {}
    assert await store.position_scopes("other") == {"abs/b1": 3.0}


def test_summary_in_words():
    """the log line says what the reverse sync did in words, not a dict."""
    from syncmusic.core.models import ReverseReport
    from syncmusic.core.reverse import summary

    assert summary(ReverseReport()) == "nothing new"
    text = summary(ReverseReport(plays_found=3, plays_submitted=2, ratings_pushed=1, pending=1))
    assert text == "3 plays found, 2 sent, 1 rating, 1 waiting for the library"
    assert "{" not in text


class StarSink(SinkLibrary):
    capabilities = SinkLibrary.capabilities.model_copy(update={"supports_starred": True})


def test_new_feedback_filters_by_sink_settings_and_orphans(tmp_path):
    """Hearts go only to libraries
    with favourites (or an unavailable one, delivered later), never for orphans or libraries without a sink;
    device ratings only when the device wins; each kind obeys its reverse sync setting."""
    from syncmusic.core.reverse import _new_feedback

    def lib(cls, lid):
        return cls(
            LibraryConfig(id=lid, type="fake", name=lid, settings={"root": str(tmp_path)}), None
        )

    libs = {
        "nd": lib(StarSink, "nd"),
        "abs": lib(SinkLibrary, "abs"),
        "folder": lib(FakeLibrary, "folder"),
    }
    libs["gone"] = None
    keys = ["nd/a", "nd/orphan", "abs/b", "folder/c", "gone/d"]
    manifest = Manifest(header=ManifestHeader(device_id="dev"))
    for k in keys:
        ref = ItemRef.parse(k)
        manifest.items[k] = ManifestEntry(
            ref=ref,
            device_ref=ref.item_id,
            path=f"{ref.item_id}.mp3",
            size=1,
            orphan=k == "nd/orphan",
        )
    pos = PositionInfo(position_s=10.0, updated_at=T0)
    plays = [p for k in keys for p in _plays(ItemRef.parse(k), 1)]
    fb = DeviceFeedback(
        plays=plays,
        ratings=dict.fromkeys(keys, 4),
        positions=dict.fromkeys(keys, pos),
        stars=dict.fromkeys(keys, True),
    )
    wanted = {"nd/a", "abs/b", "gone/d"}
    new = _new_feedback(fb, plays, manifest, libs, ReverseSyncSettings(), device_wins=True)
    assert {p.ref.key() for p in new.plays} == wanted
    assert set(new.ratings) == set(new.positions) == wanted
    assert set(new.stars) == {"nd/a", "gone/d"}
    assert not _new_feedback(
        fb, plays, manifest, libs, ReverseSyncSettings(), device_wins=False
    ).ratings
    off = ReverseSyncSettings(plays=False, ratings=False, positions=False)
    none = _new_feedback(fb, plays, manifest, libs, off, device_wins=True)
    assert none.is_empty()
    # no manifest: nothing is an orphan
    assert "nd/orphan" in _new_feedback(fb, plays, None, libs, ReverseSyncSettings(), True).stars


async def test_plays_of_another_installations_items_are_kept_for_it(env):
    """Items of a library this installation does not have (`ext:`): their plays are not sent and not lost - the
    manifest keeps them (`pending_plays`) for the installation whose library it is, which sends them later."""
    rs, cfg, dev, libs, sink = env
    theirs = ItemRef(library_id="ext:L9", item_id="x")
    m = manifest_io.read(dev.mount)
    m.items[theirs.key()] = ManifestEntry(
        ref=theirs, device_ref="x", path="x.mp3", size=1, duration_s=200
    )
    manifest_io.write(dev.mount, m)
    dev.feedback = DeviceFeedback(plays=_plays(A, 1) + _plays(theirs, 2))
    rep = await rs.run(cfg, dev, libs)
    assert rep.plays_submitted == 1 and not rep.errors
    assert [p.ref for p in sink.plays] == [A]
    assert manifest_io.read(dev.mount).items[theirs.key()].pending_plays == 2

    # the other installation's view of the same player: the item is in its library "nd"
    m = manifest_io.read(dev.mount)
    ours = ItemRef(library_id="nd", item_id="x")
    entry = m.items.pop(theirs.key())
    m.items[ours.key()] = entry.model_copy(update={"ref": ours})
    manifest_io.write(dev.mount, m)
    rep = await rs.run(cfg, dev, libs)
    sent = [p for p in sink.plays if p.ref == ours]
    assert len(sent) == 2 and all(p.played_at_estimated for p in sent)
    assert manifest_io.read(dev.mount).items[ours.key()].pending_plays == 0
    rep = await rs.run(cfg, dev, libs)  # nothing twice
    assert rep.plays_submitted == 0 and len([p for p in sink.plays if p.ref == ours]) == 2


def _starred_manifest(n: int) -> Manifest:
    manifest = Manifest(header=ManifestHeader(device_id="dev"))
    for i in range(n):
        ref = ItemRef.parse(f"nd/t{i}")
        manifest.items[ref.key()] = ManifestEntry(
            ref=ref, device_ref=ref.item_id, path=f"t{i}.mp3", size=1, last_starred=True
        )
    return manifest


def test_mass_unstar_of_a_reset_player_database_is_not_sent():
    """All hearts gone at once (the player's database was reset): no unstar goes, the entries are returned so the
    forward sync writes the hearts back; a few removals are the user's and still go."""
    from syncmusic.core.reverse import _mass_unstar

    manifest = _starred_manifest(69)
    new = DeviceFeedback(stars=dict.fromkeys(manifest.items, False) | {"nd/new": True})
    reset = _mass_unstar(new, manifest)
    assert len(reset) == 69
    assert new.stars == {"nd/new": True}

    few = DeviceFeedback(stars=dict.fromkeys(list(manifest.items)[:3], False))
    assert _mass_unstar(few, manifest) == []
    assert len(few.stars) == 3
    # below the minimum: a small library's user may remove all of them
    small = _starred_manifest(4)
    all4 = DeviceFeedback(stars=dict.fromkeys(small.items, False))
    assert _mass_unstar(all4, small) == [] and len(all4.stars) == 4


def test_mass_rating_clear_of_a_reset_player_database_is_not_sent():
    from syncmusic.core.reverse import _mass_unrate

    manifest = _starred_manifest(20)
    for e in manifest.items.values():
        e.last_rating = 4
    keys = list(manifest.items)
    fb = DeviceFeedback(ratings=dict.fromkeys(keys, None) | {"nd/x": 5})
    new = DeviceFeedback(ratings=dict(fb.ratings))
    assert _mass_unrate(fb, new, manifest) == 20
    assert fb.ratings == new.ratings == {"nd/x": 5}
    few = DeviceFeedback(ratings=dict.fromkeys(keys[:2], None))
    assert _mass_unrate(few, DeviceFeedback(), manifest) == 0 and len(few.ratings) == 2


def test_mass_rating_clear_writes_the_library_ratings_back_on_the_next_sync():
    """Like favourites: after a reset player database the forward sync puts the library's ratings back on the
    device, otherwise every later reverse sync sees the same mass clear again."""
    from syncmusic.base.capabilities import DeviceCapabilities
    from syncmusic.core.planner import meta_changes
    from syncmusic.core.reverse import _mass_unrate
    from tests.factories import track as make_track

    manifest = _starred_manifest(20)
    for e in manifest.items.values():
        e.last_rating = 4
    keys = list(manifest.items)
    fb = DeviceFeedback(ratings=dict.fromkeys(keys, None))
    assert _mass_unrate(fb, DeviceFeedback(ratings=dict(fb.ratings)), manifest) == 20
    caps = DeviceCapabilities(formats={"mp3"}, supports_ratings_write=True)
    entry = manifest.items[keys[0]]
    track = make_track("t", rating=4)
    assert any(c.field == "rating" for c in meta_changes(entry, track, caps, tags=False))
