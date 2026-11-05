"""Player contents: list what is on the device, remove selected items - a manual item leaves the Manual
selection, an item a rule still selects gets a device exclusion - with a plan that only deletes them."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core import manifest as manifest_io
from syncmusic.core.contents import only_removals
from syncmusic.core.models import ActionKind, RuleSet
from syncmusic.core.rules.templates import EXCLUDE_RULE_ID, MANUAL_RULE_ID, manual_ids
from syncmusic.core.sync import SyncError
from tests.media import make_music
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401 - fixtures


async def _synced(c, app, tmp_path, **kw):
    dev = await _register(c, tmp_path / "stick", name="Stick", **kw)
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    return dev


async def _remove(app, c, dev, keys):
    r = await c.post(f"/api/v1/devices/{dev['id']}/contents/remove", json={"keys": keys})
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    return job.result


async def _own(c, dev) -> RuleSet:
    """The rules of the device's own template, which holds its picks and removals."""
    r = await c.get(f"/api/v1/templates/own-{dev['id']}")
    if r.status_code == 404:
        return RuleSet()
    return RuleSet(rules=r.json()["rules"], excludes=r.json()["excludes"])


async def _contents(c, dev):
    r = await c.get(f"/api/v1/devices/{dev['id']}/contents")
    assert r.status_code == 200, r.text
    return r.json()


async def test_list_and_remove_a_rule_selected_item(env, tmp_path):
    app, c = env
    dev = await _synced(c, app, tmp_path)
    rows = await _contents(c, dev)
    assert [(r["artist"], r["album"], r["title"]) for r in rows] == [
        ("Скрябін", "Група крові", "Група крові"),
        ("Скрябін", "Група крові", "Зачини за мною двері"),
        ("Тартак", "Гранатовий альбом", "Орбіт без цукру"),
    ]
    assert not any(r["manual"] or r["orphan"] for r in rows)
    orbit = rows[2]
    report = await _remove(app, c, dev, [orbit["key"]])
    assert report["counts"]["remove"] == 1 and report["counts"]["add"] == 0
    assert not (tmp_path / "stick" / orbit["path"]).exists()
    assert len(await _contents(c, dev)) == 2
    ex = next(r for r in (await _own(c, dev)).excludes if r.id == EXCLUDE_RULE_ID)
    assert ex.criteria == {"any": [{"in": {"track_id": [orbit["key"].split("/", 1)[1]]}}]}
    cfg = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]
    assert f"own-{dev['id']}" in cfg["rule_set"]["template_ids"]  # linked on first use
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["counts"]["add"] == 0  # it does not come back


async def test_removing_manual_items_and_their_album(env, tmp_path):
    app, c = env
    dev = await _register(c, tmp_path / "stick", name="Stick", rule_set={"rules": []})
    items = (await c.get("/api/v1/libraries/music/items")).json()["items"]
    kino = [i for i in items if i.get("artist") == "Скрябін"]
    orbit = next(i for i in items if i.get("artist") == "Тартак")
    album_id = kino[0]["album_id"]
    await c.post(
        f"/api/v1/devices/{dev['id']}/manual-selection",
        json={"library_id": "music", "ids": [album_id], "field": "album_id"},
    )
    await c.post(
        f"/api/v1/devices/{dev['id']}/manual-selection",
        json={"library_id": "music", "ids": [orbit["item_id"]]},
    )
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    rows = await _contents(c, dev)
    assert len(rows) == 3 and all(r["manual"] for r in rows)

    await _remove(
        app, c, dev, [r["key"] for r in rows if r["artist"] == "Скрябін"]
    )  # the whole album
    rs = await _own(c, dev)
    assert manual_ids(rs, "album_id") == [] and manual_ids(rs, "track_id") == [orbit["item_id"]]
    assert rs.excludes == []  # nothing else selects them: no exclusion needed
    assert [r["title"] for r in await _contents(c, dev)] == ["Орбіт без цукру"]
    assert any(r.id == MANUAL_RULE_ID for r in rs.rules)


async def test_refused_removal_leaves_the_rules_unchanged(env, tmp_path, monkeypatch):
    """a removal refused while planning (a library is down) changes neither exclusions nor Manual."""
    app, c = env
    dev = await _register(c, tmp_path / "stick", name="Stick")
    items = (await c.get("/api/v1/libraries/music/items")).json()["items"]
    orbit = next(i for i in items if i.get("artist") == "Тартак")
    await c.post(
        f"/api/v1/devices/{dev['id']}/manual-selection",
        json={"library_id": "music", "ids": [orbit["item_id"]]},
    )
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    before = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]["rule_set"]
    own_before = await _own(c, dev)
    rows = await _contents(c, dev)

    async def down(self, device_id, **kw):
        raise SyncError("library 'nd' is not available: down", "library_unavailable")

    monkeypatch.setattr(type(app.sync), "plan", down)
    for keys in (
        [r["key"] for r in rows if not r["manual"]],
        [r["key"] for r in rows if r["manual"]],
    ):
        assert keys
        r = await c.post(f"/api/v1/devices/{dev['id']}/contents/remove", json={"keys": keys})
        assert r.status_code >= 400 and r.json()["error"] == "library_unavailable", r.text
        after = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]["rule_set"]
        assert after == before and await _own(c, dev) == own_before
    monkeypatch.undo()
    assert len(await _contents(c, dev)) == len(rows)
    await _remove(app, c, dev, [rows[0]["key"]])  # the success path still works
    assert len(await _contents(c, dev)) == len(rows) - 1


async def test_removal_plan_ignores_unrelated_changes(env, music, tmp_path):
    app, c = env
    dev = await _synced(c, app, tmp_path)
    make_music(music / "Нове" / "x.mp3", title="Нова", artist="Нове", album="N")  # would be an add
    rows = await _contents(c, dev)
    report = await _remove(app, c, dev, [rows[0]["key"]])
    assert report["counts"]["add"] == 0 and report["counts"]["remove"] == 1
    assert {r["title"] for r in await _contents(c, dev)} == {
        "Зачини за мною двері",
        "Орбіт без цукру",
    }


async def test_only_removals_keeps_everything_else(env, tmp_path):
    app, c = env
    dev = await _synced(c, app, tmp_path)
    pc = await app.sync.plan(dev["id"])
    keys = list(manifest_io.read(tmp_path / "stick").items)
    plan = only_removals(pc.plan, {keys[0]})
    assert [a.kind for a in plan.actions].count(ActionKind.REMOVE) == 1
    assert {a.kind for a in plan.actions} == {ActionKind.REMOVE, ActionKind.KEEP}
    assert not plan.needs_confirmation and plan.db is None


async def test_playback_file_and_refusals(env, tmp_path):
    app, c = env
    dev = await _synced(c, app, tmp_path)
    row = (await _contents(c, dev))[0]
    r = await c.get(f"/api/v1/devices/{dev['id']}/contents/file", params={"key": row["key"]})
    assert r.status_code == 200 and r.content == (tmp_path / "stick" / row["path"]).read_bytes()
    assert r.headers["content-type"] == "audio/mpeg"
    # ALAC (Firefox cannot decode it) comes as an MP3 copy; a file that is not media is a 400
    alac = make_music(tmp_path / "alac.m4a", title="x")
    (tmp_path / "stick" / row["path"]).write_bytes(alac.read_bytes())
    r = await c.get(f"/api/v1/devices/{dev['id']}/contents/file", params={"key": row["key"]})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg", r.text
    assert r.content[:3] == b"ID3" or r.content[:2] == b"\xff\xfb", r.content[:8]
    (tmp_path / "stick" / row["path"]).write_bytes(b"not audio")
    r = await c.get(f"/api/v1/devices/{dev['id']}/contents/file", params={"key": row["key"]})
    assert r.status_code == 400 and "cannot play" in r.json()["message"], r.text
    r = await c.get(f"/api/v1/devices/{dev['id']}/contents/file", params={"key": "music/nope"})
    assert r.status_code == 404
    m = manifest_io.read(tmp_path / "stick")
    m.items[row["key"]].path = "../outside.mp3"
    (tmp_path / "outside.mp3").write_bytes(b"x")
    manifest_io.write(tmp_path / "stick", m)
    r = await c.get(f"/api/v1/devices/{dev['id']}/contents/file", params={"key": row["key"]})
    assert r.status_code == 404
    r = await c.post(f"/api/v1/devices/{dev['id']}/contents/remove", json={"keys": ["music/nope"]})
    assert r.status_code == 400


async def test_file_removed_while_requested_is_404(env, tmp_path, monkeypatch):
    """a Remove job deletes the file between the lookup and the response (the browser player's range
    requests, a cover): 404, never a 500."""
    app, c = env
    dev = await _synced(c, app, tmp_path)
    row = (await _contents(c, dev))[0]
    resolve = app.sync.content_file
    data = (tmp_path / "stick" / row["path"]).read_bytes()

    async def vanishing(device_id: str, key: str):
        path = await resolve(device_id, key)
        path.unlink()
        return path

    monkeypatch.setattr(app.sync, "content_file", vanishing)
    for what in ("file", "thumb"):
        r = await c.get(f"/api/v1/devices/{dev['id']}/contents/{what}", params={"key": row["key"]})
        assert r.status_code in (400, 404), (what, r.status_code, r.text)
        (tmp_path / "stick" / row["path"]).write_bytes(data)


async def test_manual_item_stays_and_orphans_are_kept_until_removed(env, music, tmp_path):
    """an item added from the library keeps syncing when the templates change; an item gone from its
    library stays on the device as an orphan (plan, Player contents) until 'Remove orphans' deletes only it."""
    app, c = env
    dev = await _register(
        c, tmp_path / "stick", name="Stick", rule_set={"template_ids": ["default.music.all"]}
    )
    items = (await c.get("/api/v1/libraries/music/items")).json()["items"]
    orbit = next(i for i in items if i.get("artist") == "Тартак")
    r = await c.post(
        f"/api/v1/devices/{dev['id']}/manual-selection",
        json={"library_id": "music", "ids": [orbit["item_id"]]},
    )
    assert r.status_code == 200, r.text
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    assert len(await _contents(c, dev)) == 3
    r = await c.post("/api/v1/devices/stick/orphans/remove")
    assert r.status_code == 400 and "no orphans" in r.text

    # templates change: only the manual item (its own template) is left selected; mirror removes the rest
    rs = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]["rule_set"]
    assert rs["template_ids"] == ["default.music.all", "own-stick"]
    await c.patch(
        f"/api/v1/devices/{dev['id']}", json={"rule_set": {**rs, "template_ids": ["own-stick"]}}
    )
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], confirm=True)
    assert [r["title"] for r in await _contents(c, dev)] == ["Орбіт без цукру"]

    # back to everything, then a source file disappears: orphan, kept (with `remove_deleted` off)
    await c.patch(
        f"/api/v1/devices/{dev['id']}",
        json={
            "rule_set": {**rs, "template_ids": ["default.music.all", "own-stick"]},
            "remove_deleted": False,
        },
    )
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"])
    (music / "Скрябін" / "02 Зачини за мною двері.mp3").unlink()
    await c.post("/api/v1/libraries/music/refresh")
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["counts"]["remove"] == 0 and plan["orphans"] == 1
    report = await _apply(app, c, plan["id"])
    assert report["counts"]["remove"] == 0
    rows = await _contents(c, dev)
    assert len(rows) == 3 and [r["title"] for r in rows if r["orphan"]] == [
        "Зачини за мною двері"  # its tags, not the file name
    ]
    m = manifest_io.read(tmp_path / "stick")
    assert [e.path for e in m.items.values() if e.orphan] == [
        next(r for r in rows if r["orphan"])["path"]
    ]

    r = await c.post(f"/api/v1/devices/{dev['id']}/orphans/remove")
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded" and job.result["counts"]["remove"] == 1
    rows = await _contents(c, dev)
    assert len(rows) == 2 and not any(r["orphan"] for r in rows)
    assert (await _own(c, dev)).excludes == []  # an orphan needs no exclusion: it cannot come back


async def test_items_of_a_detached_and_removed_library_are_orphans_and_removable(env, tmp_path):
    """after the library is detached and removed, its items on the device are orphans; Player contents
    Remove and Remove orphans delete them without any library; a sync plan is still refused."""
    app, c = env
    dev = await _register(c, tmp_path / "stick", name="Stick", rule_set={"rules": []})
    items = (await c.get("/api/v1/libraries/music/items")).json()["items"]
    kino = [i for i in items if i.get("artist") == "Скрябін"]
    await c.post(
        f"/api/v1/devices/{dev['id']}/manual-selection",
        json={"library_id": "music", "ids": [i["item_id"] for i in kino]},
    )
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    assert not any(r["orphan"] for r in await _contents(c, dev))

    # detached (its rule no longer draws from the library)
    own = (await c.get("/api/v1/templates/own-stick")).json()
    assert own["rules"][0]["libraries"] == ["music"]
    own["rules"][0]["libraries"] = []
    r = await c.patch("/api/v1/templates/own-stick", json={"rules": own["rules"]})
    assert r.status_code == 200, r.text
    rows = await _contents(c, dev)
    assert len(rows) == 2 and all(r["orphan"] and r["manual"] for r in rows)
    r = await c.delete("/api/v1/libraries/music")
    assert r.status_code == 204, r.text
    rows = await _contents(c, dev)
    assert len(rows) == 2 and all(r["orphan"] for r in rows)
    r = await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})
    assert r.status_code == 400 and "no templates" in r.json()["message"], r.text

    report = await _remove(app, c, dev, [rows[0]["key"]])
    assert report["counts"]["remove"] == 1 and report["counts"]["add"] == 0
    assert not (tmp_path / "stick" / rows[0]["path"]).exists()
    rs = await _own(c, dev)
    assert rows[0]["key"].split("/", 1)[1] not in manual_ids(
        rs, "track_id"
    )  # the Manual entry is cleaned

    r = await c.post(f"/api/v1/devices/{dev['id']}/orphans/remove")
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    assert await _contents(c, dev) == []
    assert not (tmp_path / "stick" / rows[1]["path"]).exists()


async def test_contents_lists_foreign_items_with_their_tags(env, tmp_path):
    """a file another tool put on the device is listed (`foreign`, key `device:<path>`) with its tags;
    a managed item whose file was deleted outside SyncMusic stays listed as `missing`; playlist positions."""
    app, c = env
    dev = await _synced(c, app, tmp_path)
    stick = tmp_path / "stick"
    make_music(
        stick / "Other" / "x.mp3",
        title="Чужа",
        artist="Воплі Відоплясова",
        album="Радіо Африка",
        tracknumber="3",
    )
    managed = [r for r in await _contents(c, dev) if not r["foreign"]]
    gone = managed[0]
    (stick / gone["path"]).unlink()
    (stick / "Playlists").mkdir(exist_ok=True)
    (stick / "Playlists" / "Mix.m3u8").write_text("#EXTM3U\n../Other/x.mp3\n", encoding="utf-8")
    # changed behind SyncMusic's back while mounted: Refresh reads it again (the cache)
    rows = {
        r["key"]: r
        for r in (await c.get(f"/api/v1/devices/{dev['id']}/contents?refresh=true")).json()
    }
    foreign = rows["device:Other/x.mp3"]
    assert foreign["foreign"] and not foreign["orphan"] and not foreign["manual"]
    assert (foreign["title"], foreign["artist"], foreign["album"], foreign["track_number"]) == (
        "Чужа",
        "Воплі Відоплясова",
        "Радіо Африка",
        3,
    )
    assert foreign["playlists"] == {"Mix": 0} and foreign["library_id"] is None
    r = await c.get(f"/api/v1/devices/{dev['id']}/contents/file", params={"key": foreign["key"]})
    assert r.status_code == 200 and r.content == (stick / "Other" / "x.mp3").read_bytes()
    r = await c.get(
        f"/api/v1/devices/{dev['id']}/contents/file", params={"key": "device:../../etc/passwd"}
    )
    assert r.status_code == 404
    assert rows[gone["key"]]["missing"] and rows[gone["key"]]["title"] == gone["title"]
    assert sum(not r["foreign"] for r in rows.values()) == len(managed)


async def test_remove_foreign_and_managed_items_together(env, tmp_path):
    """a file another tool put on a synced device is removed by the same job as a managed item; the other
    items, the manifest and the rules for the foreign file stay as they were."""
    app, c = env
    dev = await _synced(c, app, tmp_path)
    stick = tmp_path / "stick"
    make_music(stick / "Other" / "x.mp3", title="Чужа")
    rows = await _contents(c, dev)
    managed = next(r for r in rows if not r["foreign"])
    report = await _remove(app, c, dev, ["device:Other/x.mp3", managed["key"]])
    assert report["counts"]["remove"] == 2 and report["failures"] == []
    assert not (stick / "Other" / "x.mp3").exists() and not (stick / managed["path"]).exists()
    left = await _contents(c, dev)
    assert len(left) == len(rows) - 2 and not any(r["foreign"] for r in left)
    assert set(manifest_io.read(stick).items) == {r["key"] for r in left}
    assert "x.mp3" not in str((await _own(c, dev)).excludes)
    r = await c.post(
        f"/api/v1/devices/{dev['id']}/contents/remove", json={"keys": ["device:Other/x.mp3"]}
    )
    assert r.status_code == 400  # gone now


async def test_remove_foreign_items_from_a_device_syncmusic_never_synced(env, tmp_path):
    """no manifest, no library - the files go, and the device stays unmanaged (no manifest is left, so its
    first sync still asks keep / wipe)."""
    app, c = env
    stick = tmp_path / "fresh"
    make_music(stick / "A" / "a.mp3", title="A")
    make_music(stick / "A" / "b.mp3", title="B")
    dev = await _register(c, stick, name="Fresh", library_ids=[])
    rows = await _contents(c, dev)
    assert [r["foreign"] for r in rows] == [True, True]
    report = await _remove(app, c, dev, ["device:A/a.mp3"])
    assert report["counts"] == {"remove": 1} and report["status"] == "succeeded"
    assert not (stick / "A" / "a.mp3").exists() and (stick / "A" / "b.mp3").exists()
    assert manifest_io.read(stick) is None and not (stick / manifest_io.STATE_DIR).exists()
    assert [r["title"] for r in await _contents(c, dev)] == ["B"]
    assert (await c.get(f"/api/v1/devices/{dev['id']}")).json()["last_report"]["counts"] == {
        "remove": 1
    }


async def test_remove_foreign_tracks_from_an_ipod(env, tmp_path):
    """On a stock iPod another tool filled: the track leaves the iTunesDB and its Fxx file goes; the
    other track stays; titles come from the database."""
    from pygpod import Database

    from tests.contracts.harness import ipod_tree

    app, c = env
    mount = ipod_tree(tmp_path / "ipod")
    src = make_music(tmp_path / "src" / "x.mp3", title="file tag")
    db = Database(str(mount))
    keep = db.add_track(str(src), title="Лишається", artist="Скрябін")
    drop = db.add_track(str(src), title="Іде", artist="Скрябін")
    db.save()
    drop_file = mount / "/".join(p for p in drop.ipod_path.split(":") if p)
    dev = await _register(c, mount, name="Pod", type="ipod", library_ids=[])
    rows = {r["title"]: r for r in await _contents(c, dev)}
    assert set(rows) == {"Лишається", "Іде"} and rows["Іде"]["foreign"]
    assert drop_file.exists()
    await _remove(app, c, dev, [rows["Іде"]["key"]])
    titles = [t.title for t in Database(str(mount)).tracks]
    assert titles == ["Лишається"] and not drop_file.exists()
    assert manifest_io.read(mount) is None
    assert [r["title"] for r in await _contents(c, dev)] == ["Лишається"]
    _ = keep


async def test_one_contents_read_survives_a_reload(env, tmp_path, monkeypatch):
    """the page is reloaded while Player contents is still being read - the first request goes
    away, the second waits for the same read (only one runs) and gets its result."""
    import asyncio

    from syncmusic.devices.flashdrive import FlashDrive

    app, c = env
    dev = (await _synced(c, app, tmp_path))["id"]
    real = FlashDrive.read_contents
    started = asyncio.Event()
    release = asyncio.Event()
    reads = 0

    async def slow(self):
        nonlocal reads
        reads += 1
        started.set()
        await release.wait()
        return await real(self)

    monkeypatch.setattr(FlashDrive, "read_contents", slow)
    first = asyncio.create_task(app.sync.contents(dev))
    await asyncio.wait_for(started.wait(), 10)
    first.cancel()  # the browser left (reload)
    second = asyncio.create_task(app.sync.contents(dev))
    await asyncio.sleep(0.05)
    release.set()
    rows = await second
    assert rows and reads == 1
    assert first.cancelled()
    await app.sync.contents(dev)  # a later request answers from the cache
    assert reads == 1
    await app.sync.contents(dev, refresh=True)
    assert reads == 2


async def test_remove_without_excluding_changes_no_rule(env, tmp_path):
    """ "Also exclude them from this device's templates" unticked - the files go, the rules stay,
    and the next sync puts them back."""
    app, c = env
    dev = await _synced(c, app, tmp_path)
    orbit = (await _contents(c, dev))[2]
    r = await c.post(
        f"/api/v1/devices/{dev['id']}/contents/remove",
        json={"keys": [orbit["key"]], "exclude": False},
    )
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded" and job.result["counts"]["remove"] == 1
    assert not (tmp_path / "stick" / orbit["path"]).exists()
    assert (await _own(c, dev)).excludes == []
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["counts"]["add"] == 1  # the template still selects it


async def test_remove_succeeds_when_another_item_is_already_missing(env, tmp_path):
    """a file deleted from the device by hand failed the next removal's read-back ('Sync failed')."""
    app, c = env
    dev = await _synced(c, app, tmp_path)
    rows = await _contents(c, dev)
    lost, orbit = rows[0], rows[2]
    (tmp_path / "stick" / lost["path"]).unlink()
    report = await _remove(app, c, dev, [orbit["key"]])
    assert report["status"] == "succeeded" and not report["failures"], report["failures"]
    assert any("1 item is missing" in w for w in report["warnings"]), report["warnings"]
    assert report["counts"]["remove"] == 1 and report["counts"]["verified"] == 1


async def test_a_missing_item_outside_the_plan_is_named_by_its_file(env, tmp_path):
    """a read-back failure for an item the plan does not hold showed its id, not something a person knows."""
    from syncmusic.core.models import SyncReport

    app, c = env
    dev = await _synced(c, app, tmp_path)
    lost = (await _contents(c, dev))[0]
    (tmp_path / "stick" / lost["path"]).unlink()
    cfg = await app.sync.config(dev["id"])
    report = SyncReport(id="r", device_id=dev["id"])
    await app.sync._verify_written(cfg, await app.sync.open_device(dev["id"]), report, None)
    assert report.status == "failed"
    (f,) = report.failures
    assert f.title == f.item == "Група крові" and f.path == lost["path"] and f.ref == lost["key"]


def test_video_rows_carry_show_season_episode():
    """a video row says its show / season / episode (from the library) so Player contents groups by show."""
    from syncmusic.core.contents import contents
    from syncmusic.core.models import (
        DeviceContents,
        DeviceItem,
        ItemRef,
        LibrarySnapshot,
        Manifest,
        ManifestEntry,
        ManifestHeader,
        MediaKind,
        Track,
    )

    ep = Track(
        ref=ItemRef(library_id="vid", item_id="e1"),
        kind=MediaKind.VIDEO,
        title="Ну постривай",
        series="Мультики",
        extra={"season": 1, "episode": 2},
    )
    movie = Track(ref=ItemRef(library_id="vid", item_id="m1"), kind=MediaKind.VIDEO, title="Фільм")
    snap = LibrarySnapshot(library_id="vid", state_token="x", tracks={"e1": ep, "m1": movie})
    entries = {
        t.ref.key(): ManifestEntry(
            ref=t.ref,
            device_ref=f"Videos/{t.title}.mp4",
            path=f"Videos/{t.title}.mp4",
            size=1,
            kind=MediaKind.VIDEO,
        )
        for t in (ep, movie)
    }
    found = DeviceContents(
        items=[
            DeviceItem(device_ref=e.path, path=e.path, kind=MediaKind.VIDEO)
            for e in entries.values()
        ]
    )
    manifest = Manifest(header=ManifestHeader(device_id="d"), items=entries)
    rows = {r["title"]: r for r in contents(found, manifest, {"vid": snap}, {"vid"})}
    assert (
        rows["Ну постривай"]["show"],
        rows["Ну постривай"]["season"],
        rows["Ну постривай"]["episode"],
    ) == ("Мультики", 1, 2)
    assert (rows["Фільм"]["show"], rows["Фільм"]["season"], rows["Фільм"]["episode"]) == (
        None,
        None,
        None,
    )


async def test_contents_and_covers_come_from_the_cache(env, tmp_path, monkeypatch):
    """a second look (another tab, a reload) and the covers answer from the cache, no device read;
    a change on the device (its manifest written by a removal) reads it again."""
    from syncmusic.devices.flashdrive import FlashDrive

    app, c = env
    dev = await _synced(c, app, tmp_path)
    reads = {"contents": 0, "manifest": 0}
    real_contents, real_manifest = FlashDrive.read_contents, FlashDrive.read_manifest

    async def read_contents(self):
        reads["contents"] += 1
        return await real_contents(self)

    async def read_manifest(self):
        reads["manifest"] += 1
        return await real_manifest(self)

    monkeypatch.setattr(FlashDrive, "read_contents", read_contents)
    monkeypatch.setattr(FlashDrive, "read_manifest", read_manifest)
    url = f"/api/v1/devices/{dev['id']}/contents"
    rows = (await c.get(url)).json()
    assert reads == {"contents": 1, "manifest": 1}
    assert (await c.get(url)).json() == rows
    for row in rows:
        r = await c.get(f"{url}/thumb", params={"key": row["key"]})
        assert r.status_code in (200, 204)
    assert reads == {"contents": 1, "manifest": 1}  # nothing read again
    await _remove(app, c, dev, [rows[0]["key"]])
    after = (await c.get(url)).json()
    assert reads["contents"] == 2 and len(after) == len(rows) - 1


async def test_contents_revalidate_with_an_etag(env, tmp_path):
    """a tab switch sends the ETag back and gets a 304 instead of every row again; a change on the
    device (a removal writes the manifest) or Refresh answers with the rows."""
    app, c = env
    dev = await _synced(c, app, tmp_path)
    url = f"/api/v1/devices/{dev['id']}/contents"
    first = await c.get(url)
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "no-cache"
    again = await c.get(url, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.headers["etag"] == etag
    fresh = await c.get(url, params={"refresh": "true"}, headers={"If-None-Match": etag})
    assert fresh.status_code == 200 and fresh.json() == first.json()
    await _remove(app, c, dev, [first.json()[0]["key"]])
    changed = await c.get(url, headers={"If-None-Match": etag})
    assert changed.status_code == 200 and len(changed.json()) == len(first.json()) - 1
    assert changed.headers["etag"] != etag


async def test_thumbnails_are_made_in_the_background(env, tmp_path, monkeypatch):
    """opening Player contents makes every row's thumbnail in the background, so scrolling serves
    them from the cache and reads no cover from the device."""
    import asyncio

    from syncmusic import facade

    app, c = env
    dev = await _synced(c, app, tmp_path)
    calls: list[str] = []
    real = facade.file_cover

    def counted(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(facade, "file_cover", counted)
    url = f"/api/v1/devices/{dev['id']}/contents"
    rows = (await c.get(url)).json()
    for _ in range(200):
        if len(calls) >= len(rows):
            break
        await asyncio.sleep(0.05)
    assert len(calls) == len(rows)
    await c.get(url)  # same contents: not warmed again
    for row in rows:
        assert (await c.get(f"{url}/thumb", params={"key": row["key"]})).status_code in (200, 204)
    await asyncio.sleep(0.1)
    assert len(calls) == len(rows)


async def test_thumbnail_warm_up_parses_the_manifest_once(env, tmp_path, monkeypatch):
    """live idled at ~85 % CPU - every warm-up row re-read the whole manifest when the contents cache
    missed. The warm-up takes the rows' paths; a content_file miss parses the manifest once per (size, mtime)."""
    import asyncio

    from syncmusic import facade
    from syncmusic.devices.flashdrive import FlashDrive

    app, c = env
    dev = await _synced(c, app, tmp_path)
    reads = {"manifest": 0}
    real = FlashDrive.read_manifest

    async def read_manifest(self):
        reads["manifest"] += 1
        return await real(self)

    monkeypatch.setattr(FlashDrive, "read_manifest", read_manifest)
    calls: list[str] = []
    real_cover = facade.file_cover
    monkeypatch.setattr(facade, "file_cover", lambda p: calls.append(str(p)) or real_cover(p))
    url = f"/api/v1/devices/{dev['id']}/contents"
    rows = (await c.get(url)).json()
    for _ in range(200):
        if len(calls) >= len(rows):
            break
        await asyncio.sleep(0.05)
    assert len(calls) == len(rows) and reads["manifest"] == 1
    app.sync._contents_cache.clear()  # a stamp miss: content_file falls back to the manifest
    for row in rows:
        r = await c.get(f"{url}/file", params={"key": row["key"]})
        assert r.status_code == 200
    assert reads["manifest"] == 2
