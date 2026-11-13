"""Reverse playlist sync: device edits of library playlists go back, merged three-way."""

from __future__ import annotations

import pytest

from syncmusic.base.feedback import FeedbackRejected
from syncmusic.core.models import (
    DeviceConfig,
    DevicePlaylist,
    ItemRef,
    LibraryConfig,
    Manifest,
    ManifestEntry,
    ManifestHeader,
    ReverseReport,
    SyncPlan,
)
from syncmusic.core.playlist_reverse import playlists_after_reverse, push_playlists
from syncmusic.devices.HibyR1.compas import HibyCompas
from syncmusic.devices.HibyR1.sonixos import Hiby
from syncmusic.devices.Rockbox.rockbox import Rockbox
from tests.contracts.harness import compas_tree, hiby_sql, hiby_tree, rockbox_tree
from tests.unit.test_reverse import SinkLibrary


class PlaylistSink(SinkLibrary):
    def __init__(self, config, ctx) -> None:
        super().__init__(config, ctx)
        self.lists: dict[str, list[ItemRef]] = {}
        self.saved: list[tuple[str, list[str], str | None]] = []
        self.refuse = False

    async def get_playlist(self, playlist_id):
        return self.lists.get(playlist_id)

    async def save_playlist(self, name, refs, playlist_id=None):
        if self.refuse:
            raise FeedbackRejected("not yours")
        self.saved.append((name, [r.item_id for r in refs], playlist_id))
        self.lists[playlist_id or name] = list(refs)
        return playlist_id or name


def _ref(i: str) -> ItemRef:
    return ItemRef(library_id="nd", item_id=i)


def _setup(tmp_path, kind="rockbox"):
    if kind == "rockbox":
        mount = rockbox_tree(tmp_path / "rb")
        dev = Rockbox(DeviceConfig(id="rb", type="rockbox", name="RB"), mount, None)
    elif kind == "compas":
        mount = compas_tree(tmp_path / "compas")
        dev = HibyCompas(DeviceConfig(id="c", type="hiby_compas", name="C"), mount, None)
    else:
        mount = hiby_tree(tmp_path / "hiby")
        dev = Hiby(DeviceConfig(id="h", type="hiby", name="H"), mount, None)
    items = {}
    for i in "abcde":
        (mount / "Music").mkdir(exist_ok=True)
        (mount / "Music" / f"{i}.mp3").write_bytes(b"x")
        r = _ref(i)
        items[r.key()] = ManifestEntry(
            ref=r, device_ref=f"Music/{i}.mp3", path=f"Music/{i}.mp3", size=1
        )
    m = Manifest(
        header=ManifestHeader(device_id=dev.id),
        items=items,
        playlists=["Mix"],
        playlist_refs={"Mix": ["nd/a", "nd/b", "nd/c"]},
        playlist_sources={"Mix": "nd/pl1"},
    )
    lib = PlaylistSink(
        LibraryConfig(id="nd", type="fake", name="ND", settings={"root": str(tmp_path)}), None
    )
    lib.lists["pl1"] = [_ref("a"), _ref("b"), _ref("c")]
    return dev, m, lib


def _write(dev, entries: list[str], name="Mix") -> None:
    if isinstance(dev, Hiby):  # SonixOS: a table in library.db
        hiby_sql(
            dev.mount,
            "library.db",
            f'CREATE TABLE IF NOT EXISTS "M3U_{name}"(idx INTEGER PRIMARY KEY, path TEXT, title TEXT,'
            " artist TEXT, seconds INT, present INT, mount INT)",
        )
        hiby_sql(dev.mount, "library.db", f'DELETE FROM "M3U_{name}"')
        for e in entries:
            hiby_sql(
                dev.mount,
                "library.db",
                f'INSERT INTO "M3U_{name}"(path, present) VALUES (?, 1)',
                f"/mnt/sd_0/Music/{e}.mp3",
            )
        return
    (dev.mount / "Playlists").mkdir(exist_ok=True)
    if isinstance(
        dev, HibyCompas
    ):  # the player edits its .m3u in place, appending absolute player paths
        lines = ["#EXTM3U", *[f"/data/mnt/sd_0/Music/{e}.mp3" for e in entries]]
        (dev.mount / "Playlists" / f"{name}.m3u").write_text("\n".join(lines) + "\n")
        return
    lines = ["#EXTM3U", *[f"../Music/{e}.mp3" for e in entries]]
    (dev.mount / "Playlists" / f"{name}.m3u8").write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("kind", ["rockbox", "hiby", "compas"])
async def test_device_edit_is_saved_merged(tmp_path, kind):
    dev, m, lib = _setup(tmp_path, kind)
    _write(dev, ["c", "a", "d"])  # device: reordered, removed b, added d
    lib.lists["pl1"] = [_ref("a"), _ref("b"), _ref("c"), _ref("e")]  # library: added e at the end
    report = ReverseReport()
    assert await push_playlists(dev, m, {"nd": lib}, report)
    assert lib.saved == [("Mix", ["c", "a", "d", "e"], "pl1")]
    assert report.playlists_pushed == {"Mix": ["nd/c", "nd/a", "nd/d", "nd/e"]}
    assert m.playlist_refs["Mix"] == ["nd/c", "nd/a", "nd/d", "nd/e"]


async def test_unchanged_or_unknown_base_does_nothing(tmp_path):
    dev, m, lib = _setup(tmp_path)
    _write(dev, ["a", "b", "c"])
    assert not await push_playlists(dev, m, {"nd": lib}, ReverseReport())
    m.playlist_refs = {}  # written before no base
    _write(dev, ["c"])
    assert not await push_playlists(dev, m, {"nd": lib}, ReverseReport())
    assert lib.saved == []


@pytest.mark.parametrize("kind", ["rockbox", "hiby", "compas"])
async def test_smart_rule_and_conflict_are_warned_not_pushed(tmp_path, kind):
    dev, m, lib = _setup(tmp_path, kind)
    _write(dev, ["b", "a", "c"])
    m.playlist_smart = {"Mix"}
    report = ReverseReport()
    await push_playlists(dev, m, {"nd": lib}, report)
    assert "smart playlist" in report.warnings[0]
    m.playlist_smart = set()
    m.playlist_sources = {}
    report = ReverseReport()
    await push_playlists(dev, m, {"nd": lib}, report)
    assert "comes from a rule" in report.warnings[0]
    m.playlist_sources = {"Mix": "nd/pl1"}
    lib.lists["pl1"] = [_ref("a"), _ref("c"), _ref("b")]  # reordered in the library too
    report = ReverseReport()
    await push_playlists(dev, m, {"nd": lib}, report)
    assert "reordered on the device and in the library" in report.warnings[0]
    assert lib.saved == []


async def test_refused_library_keeps_the_base(tmp_path):
    dev, m, lib = _setup(tmp_path, "hiby")
    _write(dev, ["a", "b"])
    lib.refuse = True
    report = ReverseReport()
    assert not await push_playlists(dev, m, {"nd": lib}, report)
    assert "not saved" in report.warnings[0] and m.playlist_refs["Mix"] == ["nd/a", "nd/b", "nd/c"]


async def test_flash_drive_playlists_are_not_read_back(tmp_path):
    from syncmusic.devices.flashdrive import FlashDrive

    (tmp_path / "f").mkdir()
    dev = FlashDrive(DeviceConfig(id="f", type="flashdrive", name="F"), tmp_path / "f", None)
    m = Manifest(header=ManifestHeader(device_id="f"))
    assert await dev.read_playlists(m) is None


def test_forward_sync_writes_the_pushed_list():
    plan = SyncPlan(
        id="p",
        device_id="d",
        playlists=[DevicePlaylist(name="Mix", refs=[_ref("a")]), DevicePlaylist(name="Other")],
    )
    out = playlists_after_reverse(plan, ReverseReport(playlists_pushed={"Mix": ["nd/b", "nd/a"]}))
    assert [r.item_id for r in out.playlists[0].refs] == ["b", "a"] and out.playlists[1].refs == []


class NamedSink(PlaylistSink):
    async def playlists_by_name(self):
        return {"Existing": "pl9"}


async def test_device_made_playlist_is_created_once_then_followed(tmp_path):
    dev, m, _ = _setup(tmp_path, "hiby")
    lib = NamedSink(
        LibraryConfig(id="nd", type="fake", name="ND", settings={"root": str(tmp_path)}), None
    )
    lib.lists["pl1"] = [_ref("a"), _ref("b"), _ref("c")]
    _write(dev, ["a", "b", "c"])  # the managed one, unchanged
    _write(dev, ["d", "e", "zzz"], name="Road")  # made on the player; zzz is no synced song
    _write(dev, ["a"], name="Existing")  # the library has one of that name: left alone (566.3)
    report = ReverseReport()
    assert await push_playlists(dev, m, {"nd": lib}, report)
    assert lib.saved == [("Road", ["d", "e"], None)] and report.playlists_created == ["Road"]
    assert m.playlist_created == {"Road": "nd/Road"} and m.playlist_refs["Road"] == ["nd/d", "nd/e"]
    assert any("Existing" in w for w in report.warnings)
    from syncmusic.core import manifest as mf

    back = mf.loads(mf.dumps(m))
    assert back.playlist_created == m.playlist_created and back.playlist_refs["Road"] == [
        "nd/d",
        "nd/e",
    ]
    assert "Road" not in back.playlists  # not managed: the forward sync never deletes it

    # not created again; a later edit on the player goes to that library playlist
    lib.saved.clear()
    _write(dev, ["e", "d"], name="Road")
    report = ReverseReport()
    await push_playlists(dev, back, {"nd": lib}, report)
    assert lib.saved == [("Road", ["e", "d"], "Road")] and report.playlists_created == []


async def _conflicts(tmp_path, policy):
    """Both conflicts at once: Mix reordered on both sides, Existing made on the player and in the library."""
    dev, m, _ = _setup(tmp_path / policy)
    lib = NamedSink(
        LibraryConfig(id="nd", type="fake", name="ND", settings={"root": str(tmp_path)}), None
    )
    lib.lists["pl1"] = [_ref("a"), _ref("c"), _ref("b")]
    lib.lists["pl9"] = [_ref("e")]
    _write(dev, ["b", "a", "c"])
    _write(dev, ["d"], name="Existing")
    report = ReverseReport()
    await push_playlists(dev, m, {"nd": lib}, report, policy, "R1")
    return lib, m, report


async def test_policy_ask_and_ignore_change_nothing(tmp_path):
    for policy in ("ask", "ignore"):
        lib, m, report = await _conflicts(tmp_path, policy)
        assert lib.saved == [] and not m.playlist_created
        assert report.playlist_conflicts == (["Existing", "Mix"] if policy == "ask" else [])


async def test_policy_merge(tmp_path):
    lib, m, _ = await _conflicts(tmp_path, "merge")
    assert ("Existing", ["d", "e"], "pl9") in lib.saved  # union with the library's songs
    assert ("Mix", ["b", "a", "c"], "pl1") in lib.saved  # the device's order
    assert m.playlist_created == {"Existing": "nd/pl9"}


async def test_policy_replace(tmp_path):
    lib, _, _ = await _conflicts(tmp_path, "replace")
    assert ("Existing", ["d"], "pl9") in lib.saved and ("Mix", ["b", "a", "c"], "pl1") in lib.saved


async def test_policy_rename_once(tmp_path):
    lib, m, report = await _conflicts(tmp_path, "rename")
    assert sorted(lib.saved) == [
        ("Existing (R1)", ["d"], None),
        ("Mix (R1)", ["b", "a", "c"], None),
    ]
    assert sorted(report.playlists_created) == ["Existing (R1)", "Mix (R1)"]
    lib.saved.clear()
    dev = Rockbox(
        DeviceConfig(id="rb", type="rockbox", name="RB"), tmp_path / "rename" / "rb", None
    )
    await push_playlists(dev, m, {"nd": lib}, ReverseReport(), "rename", "R1")
    assert lib.saved == []  # not saved again


async def test_choices_override_the_policy_per_playlist(tmp_path):
    """The plan review's choice per conflict wins over the device's policy."""
    dev, m, _ = _setup(tmp_path)
    lib = NamedSink(
        LibraryConfig(id="nd", type="fake", name="ND", settings={"root": str(tmp_path)}), None
    )
    lib.lists["pl1"] = [_ref("a"), _ref("c"), _ref("b")]
    lib.lists["pl9"] = [_ref("e")]
    _write(dev, ["b", "a", "c"])
    _write(dev, ["d"], name="Existing")
    report = ReverseReport()
    await push_playlists(dev, m, {"nd": lib}, report, "ask", "R1", choices={"Mix": "replace"})
    assert lib.saved == [("Mix", ["b", "a", "c"], "pl1")]
    assert report.playlist_conflicts == ["Existing"]  # no choice: still asked


async def test_dry_run_lists_conflicts_and_saves_nothing(tmp_path):
    dev, m, _ = _setup(tmp_path)
    lib = NamedSink(
        LibraryConfig(id="nd", type="fake", name="ND", settings={"root": str(tmp_path)}), None
    )
    lib.lists["pl1"] = [_ref("a"), _ref("c"), _ref("b")]
    _write(dev, ["b", "a", "c"])
    _write(dev, ["d"], name="Existing")
    _write(dev, ["e"], name="Road")  # new, no conflict: not created by a dry run
    before = m.model_copy(deep=True)
    report = ReverseReport()
    await push_playlists(dev, m, {"nd": lib}, report, dry=True)
    assert lib.saved == [] and m == before
    assert sorted(report.playlist_conflicts) == ["Existing", "Mix"]


async def _no_names():
    return {}


async def _forward(dev, m, playlists):
    """The forward step's playlist part (`Applier._playlists`) and commit, on the device's own manifest."""
    from syncmusic.core.applier import Applier
    from syncmusic.core.models import SyncReport
    from syncmusic.core.prepare import Chain

    s = await dev.begin()
    s.manifest = m.model_copy(deep=True)
    plan = SyncPlan(id="p", device_id=dev.id, playlists=playlists)
    report = SyncReport(id="r", device_id=dev.id)
    await Applier(dev, {}, Chain([]), None)._playlists(s, plan, m, dict(playlists=0), report)  # type: ignore[arg-type]
    await dev.commit(s, m)
    return report


async def test_pushed_device_playlist_leaves_the_device_unless_selected(tmp_path):
    """a playlist made on the player goes to the library, then off the player when no rule
    selects it; a selected one stays (managed from then on); a failed push keeps it on the player."""
    for kind in ("rockbox", "hiby"):
        dev, m, lib = _setup(tmp_path / kind, kind)
        _write(dev, ["a", "b", "c"])
        _write(dev, ["d", "e"], name="Road")
        _write(dev, ["a", "e"], name="Kept")
        _write(dev, ["b"], name="Failed")
        if kind == "hiby":  # a table of ours (written by a sync) for the managed one
            m.playlist_paths = {"Mix": "M3U_Mix"}
        lib.playlists_by_name = _no_names  # type: ignore[method-assign]
        orig = lib.save_playlist

        async def save(name, refs, playlist_id=None, _orig=orig):
            if name == "Failed":
                raise RuntimeError("library down")
            return await _orig(name, refs, playlist_id)

        lib.save_playlist = save  # type: ignore[method-assign]
        await push_playlists(dev, m, {"nd": lib}, ReverseReport())
        assert set(m.playlist_created) == {"Road", "Kept"}
        report = await _forward(
            dev,
            m,
            [
                DevicePlaylist(name="Mix", refs=[_ref("a")]),
                DevicePlaylist(name="Kept", refs=[_ref("a"), _ref("e")]),
            ],
        )
        assert report.playlists_removed == ["Road"], kind
        left = set(await dev.read_playlists(m) or {})
        assert left == {"Mix", "Kept", "Failed"}, kind
        assert not m.playlist_created and "Road" not in m.playlist_refs


def test_a_playlist_whose_entries_all_match_nothing_is_left_out():
    from syncmusic.core.models import Manifest, ManifestHeader
    from syncmusic.core.playlist_reverse import device_lists

    lists, unknown = device_lists(
        Manifest(header=ManifestHeader(device_id="a")),
        {"Mix": ["elsewhere/x.mp3"], "Empty": []},
        True,
    )
    assert lists == {"Empty": []} and unknown == 1
