# SPDX-License-Identifier: GPL-3.0-or-later
"""Managed items whose library ids changed (server reinstalled, library re-created) are re-linked, not copied again."""

from __future__ import annotations

from pathlib import Path

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import (
    ItemRef,
    LibrarySnapshot,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    Track,
)
from syncmusic.core.planner import meta_snapshot
from syncmusic.core.relink import apply_relinks, find_relinks
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401 - fixtures


def _track(item: str, title: str, dur: float = 200.0, lib: str = "lib") -> Track:
    return Track(
        ref=ItemRef(library_id=lib, item_id=item),
        title=title,
        artist="Океан Ельзи",
        album="Земля",
        track_no=1,
        duration_s=dur,
    )


def _entry(t: Track, **kw: object) -> ManifestEntry:
    return ManifestEntry(
        ref=t.ref,
        device_ref=f"Music/{t.title}.mp3",
        path=f"Music/{t.title}.mp3",
        size=1,
        meta=meta_snapshot(t),
        duration_s=t.duration_s,
        **kw,
    )


def _manifest(*tracks: Track) -> Manifest:
    return Manifest(
        header=ManifestHeader(device_id="dev"), items={t.ref.key(): _entry(t) for t in tracks}
    )


def _snap(*tracks: Track) -> dict[str, LibrarySnapshot]:
    return {
        "lib": LibrarySnapshot(
            library_id="lib", state_token="x", tracks={t.ref.item_id: t for t in tracks}
        )
    }


def test_all_ids_changed_are_relinked_with_their_baselines() -> None:
    old = [_track("a1", "Без бою"), _track("a2", "Мовчати")]
    m = _manifest(*old)
    m.items["lib/a1"].last_playcount = 3
    m.playlist_refs = {"Mix": ["lib/a2", "lib/a1"]}
    relinks = find_relinks(m, _snap(_track("b1", "Без бою", 201.0), _track("b2", "Мовчати")))
    assert relinks == {"lib/a1": "lib/b1", "lib/a2": "lib/b2"}
    apply_relinks(m, relinks)
    assert set(m.items) == {"lib/b1", "lib/b2"}
    assert m.items["lib/b1"].ref.item_id == "b1" and m.items["lib/b1"].last_playcount == 3
    assert m.playlist_refs == {"Mix": ["lib/b2", "lib/b1"]}


def test_partial_change_and_unsure_matches() -> None:
    kept, moved, twin = _track("a1", "Без бою"), _track("a2", "Мовчати"), _track("a3", "Друг")
    m = _manifest(kept, moved, twin)
    snap = _snap(
        kept,
        _track("b2", "Мовчати"),
        _track("b3", "Друг"),
        _track("b4", "Друг"),  # two new items fit: unsure, left alone
    )
    assert find_relinks(m, snap) == {"lib/a2": "lib/b2"}


def test_no_relink_for_other_duration_empty_library_or_no_meta() -> None:
    t = _track("a1", "Без бою")
    m = _manifest(t)
    assert find_relinks(m, _snap(_track("b1", "Без бою", 260.0))) == {}
    assert find_relinks(m, _snap()) == {}
    m.items["lib/a1"].meta = {}
    assert find_relinks(m, _snap(_track("b1", "Без бою"))) == {}


async def test_plan_relinks_instead_of_copying_again(env, tmp_path):  # noqa: F811
    app, c = env
    await _register(c, tmp_path / "flash")
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    root = tmp_path / "flash"
    m = manifest_io.read(root)
    assert m is not None and len(m.items) == 3
    # the library "was re-created": the device holds the items under ids it no longer has
    old = {f"music/old-{i}": k for i, k in enumerate(sorted(m.items))}
    m.items = {o: m.items[k].model_copy(update={"ref": ItemRef.parse(o)}) for o, k in old.items()}
    manifest_io.write(root, m)
    manifest_io.drop_cache()
    await app.store.db.execute(
        "INSERT INTO pending_feedback (device_id, kind, data, attempts, created_at) VALUES (?, ?, ?, 3, ?)",
        ("my-flash", "rating", '{"ref": "music/old-0", "rating": 4}', "2026-01-01T00:00:00+00:00"),
    )

    plan2 = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    assert plan2["counts"]["add"] == 0 and plan2["counts"]["keep"] == 3, plan2["counts"]
    assert any("re-linked 3" in w for w in plan2["warnings"])
    m2 = manifest_io.read(root)
    assert m2 is not None and set(m2.items) == set(old.values())
    [row] = await app.store.pending_feedback("my-flash")
    assert row.data["ref"] == old["music/old-0"] and row.attempts == 0
    await _apply(app, c, plan2["id"])
    assert len(list(Path(root).rglob("*.mp3"))) == 3  # noqa: ASYNC240 - no "(2)" copies


async def test_reverse_sync_relinks_first(env, tmp_path):  # noqa: F811
    """The update path runs the reverse sync before any plan: the re-link must happen there too."""
    app, c = env
    await _register(c, tmp_path / "flash")
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    root = tmp_path / "flash"
    m = manifest_io.read(root)
    assert m is not None
    old = {f"music/old-{i}": k for i, k in enumerate(sorted(m.items))}
    m.items = {o: m.items[k].model_copy(update={"ref": ItemRef.parse(o)}) for o, k in old.items()}
    manifest_io.write(root, m)
    manifest_io.drop_cache()
    await app.store.db.execute(
        "INSERT INTO pending_feedback (device_id, kind, data, attempts, created_at) VALUES (?, ?, ?, 2, ?)",
        ("my-flash", "rating", '{"ref": "music/old-1", "rating": 5}', "2026-01-01T00:00:00+00:00"),
    )

    cfg = await app.sync.config("my-flash")
    await app.sync._step0(cfg, await app.sync.open_device("my-flash"))
    manifest_io.drop_cache()
    m2 = manifest_io.read(root)
    assert m2 is not None and set(m2.items) == set(old.values())
    rows = await app.store.pending_feedback("my-flash")
    assert all(not r.data["ref"].startswith("music/old-") for r in rows)


async def test_take_over_relinks_instead_of_copying_again(env, tmp_path):  # noqa: F811
    """A device synced by another registration whose library ids changed since: the take-over keeps its files."""
    app, c = env
    await _register(c, tmp_path / "flash")
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    root = tmp_path / "flash"
    m = manifest_io.read(root)
    assert m is not None and len(m.items) == 3
    ours = m.header.device_uid
    old = {f"music/old-{i}": k for i, k in enumerate(sorted(m.items))}
    m.items = {o: m.items[k].model_copy(update={"ref": ItemRef.parse(o)}) for o, k in old.items()}
    m.header.device_uid = "another-registration"
    manifest_io.write(root, m)
    manifest_io.drop_cache()

    plan2 = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    assert plan2["counts"]["add"] == 0 and plan2["counts"]["keep"] == 3, plan2["counts"]
    assert plan2["needs_confirmation"] and any("re-linked 3" in w for w in plan2["warnings"])
    manifest_io.drop_cache()
    untouched = manifest_io.read(root)  # nothing written before the take-over is confirmed
    assert untouched is not None and set(untouched.items) == set(old)
    await _apply(app, c, plan2["id"], confirm=True)
    assert len(list(Path(root).rglob("*.mp3"))) == 3  # noqa: ASYNC240 - no "(2)" copies
    manifest_io.drop_cache()
    m2 = manifest_io.read(root)
    assert m2 is not None and set(m2.items) == set(old.values())
    assert m2.header.device_uid == ours
