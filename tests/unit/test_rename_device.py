"""Rename a device on the device itself: a device action like Format, not a sync step."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core import manifest as manifest_io
from tests.contracts.harness import _ipod_feedback, ipod_tree
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401 - fixtures


async def test_rename_ipod_writes_its_name_and_keeps_contents_and_plays(env, tmp_path):
    from pygpod import Database

    app, c = env
    mount = ipod_tree(tmp_path / "ipod", name="Old iPod")
    await _register(c, mount, name="iPod", type="ipod")
    plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    manifest = manifest_io.read(mount)
    await _ipod_feedback(await app.sync.open_device("ipod"), manifest)
    plays = len((await (await app.sync.open_device("ipod")).read_feedback(manifest)).plays)
    assert plays == 2 * len(manifest.items)
    jobs = len(app.jobs.jobs())

    r = await c.post("/api/v1/devices/ipod/rename", json={"name": "Плеєр", "preview": True})
    assert r.status_code == 200 and r.json()["name"] == "Плеєр" and r.json()["label"] is None
    assert Database(str(mount), apply_play_counts=False).master_playlist.name == "Old iPod"

    r = await c.post("/api/v1/devices/ipod/rename", json={"name": "  Плеєр   на   кухні "})
    assert r.status_code == 200, r.text
    assert (
        r.json()["name"] == "Плеєр на кухні" and r.json()["label"] is None
    )  # a directory: no label
    db = Database(str(mount), apply_play_counts=False)
    assert db.master_playlist.name == "Плеєр на кухні"
    assert (await c.get("/api/v1/devices/ipod")).json()["config"]["name"] == "Плеєр на кухні"
    assert manifest_io.read(mount).items.keys() == manifest.items.keys()
    assert len(db.tracks) == len(manifest.items)
    # the plays are still there for the reverse sync (the Play Counts file was not consumed)
    fb = await (await app.sync.open_device("ipod")).read_feedback(manifest)
    assert len(fb.plays) == plays
    assert len(app.jobs.jobs()) == jobs  # not a sync


async def test_rename_checks(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    (tmp_path / "flash" / "keep.txt").write_text("x")
    for name in ("", "   ", "x" * 65):
        r = await c.post("/api/v1/devices/flash/rename", json={"name": name})
        assert r.status_code == 400, (name, r.text)
    async with app.jobs.device_lock("flash"):  # a sync or format running on it
        r = await c.post("/api/v1/devices/flash/rename", json={"name": "New"})
        assert r.status_code == 409 and "busy" in r.text
    assert (await c.post("/api/v1/devices/nope/rename", json={"name": "X"})).status_code == 404
    r = await c.post("/api/v1/devices/flash/rename", json={"name": "Новий Stick"})
    assert r.status_code == 200 and r.json()["config"]["name"] == "Новий Stick"
    assert (tmp_path / "flash" / "keep.txt").read_text() == "x"


async def test_ipod_without_itunesdb_gives_a_clear_answer_not_500(env, tmp_path):
    """An iPod folder with iPod_Control but no iTunesDB (never synced, or the DB was deleted): plan and rename
    answer with a status the UI can show (or the first sync creates the database), never an unhandled 500.
    the plan sees an empty iPod, the first sync and Rename create the database (like libgpod)."""
    from pygpod import Database

    app, c = env
    mount = tmp_path / "ipod"
    (mount / "iPod_Control" / "iTunes").mkdir(parents=True)
    (mount / "iPod_Control" / "Device").mkdir()
    await _register(c, mount, name="iPod", type="ipod")
    plan = await c.post("/api/v1/devices/ipod/plan", json={})
    assert plan.status_code == 200, plan.text
    assert plan.json()["counts"]["add"] > 0 and plan.json()["db"]["estimated"] > 0
    assert not (mount / "iPod_Control" / "iTunes" / "iTunesDB").exists()  # planning writes nothing
    r = await c.post("/api/v1/devices/ipod/rename", json={"name": "Плеєр"})
    assert r.status_code == 200, r.text
    assert Database(str(mount)).master_playlist.name == "Плеєр"

    # the database deleted again: the first sync creates it and puts the tracks in
    (mount / "iPod_Control" / "iTunes" / "iTunesDB").unlink()
    plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    db = Database(str(mount))
    assert len(db.tracks) == plan["counts"]["add"] and db.master_playlist.name == "Плеєр"
