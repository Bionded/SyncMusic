"""Sync service + register / plan / apply / first-sync API on tmp-dir flash drives."""

from __future__ import annotations

import shutil

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core.detection import ConnectedDisk
from syncmusic.core.models import MediaKind
from syncmusic.facade import AppFacade
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.media import make_music

ALL = {"rules": [{"id": "all", "name": "Everything", "criteria": {"all": []}}]}


@pytest.fixture
def music(tmp_path):
    root = tmp_path / "music"
    make_music(
        root / "Скрябін" / "01 Група крові.mp3",
        title="Група крові",
        artist="Скрябін",
        album="Група крові",
    )
    make_music(
        root / "Скрябін" / "02 Зачини за мною двері.mp3",
        title="Зачини за мною двері",
        artist="Скрябін",
        album="Група крові",
    )
    make_music(
        root / "Тартак" / "Орбіт.mp3",
        title="Орбіт без цукру",
        artist="Тартак",
        album="Гранатовий альбом",
    )
    return root


@pytest.fixture
async def env(config, music):
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/libraries",
            json={
                "id": "music",
                "type": "folder",
                "name": "Music",
                "settings": {"root": str(music)},
            },
        )
        assert r.status_code == 201, r.text
        yield app, c
    await app.stop()


async def _removable_true(self, path):
    return True


async def _as_template(c, name, rule_set, library_ids=()):
    """A device has templates only: rules and excludes go into a template of their own, linked
    in the rule set returned (mode, priority and limits kept)."""
    rule_set = dict(rule_set)
    rules, excludes = rule_set.pop("rules", []), rule_set.pop("excludes", [])
    if not rules and not excludes:
        return rule_set
    body = {
        "name": f"{name} rules",
        "library_ids": list(library_ids),
        "rules": rules,
        "excludes": excludes,
    }
    r = await c.post("/api/v1/templates", json=body)
    assert r.status_code in (200, 201), r.text
    return {**rule_set, "template_ids": [*rule_set.get("template_ids", []), r.json()["id"]]}


async def _register(c, path, name="My Flash", **kw):
    """Registers with an own template holding `rule_set`'s rules (default: everything of `library_ids`)."""
    path.mkdir(parents=True, exist_ok=True)
    body = {
        "name": name,
        "mount_path": str(path),
        "type": "flashdrive",
        "library_ids": ["music"],
        "rule_set": ALL,
    }
    body.update(kw)
    body["rule_set"] = await _as_template(c, name, body["rule_set"], body.pop("library_ids"))
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _apply(app, c, plan_id, **body):
    r = await c.post(f"/api/v1/plans/{plan_id}/apply", json=body)
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    return job.result


async def test_register_plan_apply_rerun(env, tmp_path):
    app, c = env
    assert (await c.get("/api/v1/devices")).json() == {
        "registered": [],
        "unknown": [],
        "ignored": [],
    }
    cfg = await _register(c, tmp_path / "flash")
    assert cfg["id"] == "my-flash" and cfg["type"] == "flashdrive"
    assert [p["type"] for p in cfg["processors"]] == [
        "transcode",
        "retag",
        "lyrics",
    ]  # suggested for flash caps
    devs = (await c.get("/api/v1/devices")).json()["registered"]
    assert devs[0]["connected"] and devs[0]["name"] == "My Flash"

    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    assert plan["first_sync"] and plan["needs_confirmation"] and plan["counts"]["add"] == 3
    assert plan["space"]["fits"] and plan["first_sync_matches"] == plan["first_sync_existing"] == 0
    assert (await c.get(f"/api/v1/plans/{plan['id']}")).json()["id"] == plan["id"]
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={})
    assert r.status_code == 409 and r.json()["error"] == "confirmation_required"
    report = await _apply(app, c, plan["id"], first_sync="keep")
    assert report["status"] == "succeeded" and report["counts"]["add"] == 3
    files = sorted(
        p.relative_to(tmp_path / "flash").as_posix() for p in (tmp_path / "flash").rglob("*.mp3")
    )
    assert files == [
        "Music/Скрябін/Група крові/Група крові.mp3",
        "Music/Скрябін/Група крові/Зачини за мною двері.mp3",
        "Music/Тартак/Гранатовий альбом/Орбіт без цукру.mp3",
    ]

    plan2 = (await c.post("/api/v1/devices/my-flash/plan")).json()
    assert not plan2["first_sync"] and plan2["counts"]["keep"] == 3 and plan2["counts"]["add"] == 0
    rep2 = await _apply(app, c, plan2["id"])
    assert rep2["bytes_written"] == 0
    reports = (await c.get("/api/v1/sync-reports", params={"device": "my-flash"})).json()
    assert [r["status"] for r in reports] == ["succeeded", "succeeded"]
    dev = (await c.get("/api/v1/devices/my-flash")).json()
    assert dev["last_report"]["id"] == reports[0]["id"]
    assert (
        await c.post(f"/api/v1/plans/{plan2['id']}/apply", json={})
    ).status_code == 404  # used up


async def test_partial_sync_job_message_names_counts(env, tmp_path, monkeypatch):
    """items skipped for space at apply time show up in the job message, not just 'done'."""
    from syncmusic.devices._filedevice import FileDeviceMixin

    app, c = env
    await _register(c, tmp_path / "flash")
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    assert plan["space"]["fits"]

    async def no_space(self):
        return 0

    monkeypatch.setattr(
        FileDeviceMixin, "free_space", no_space
    )  # the disk filled up after planning
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded" and job.result["status"] == "partial"
    msg = "partial: 0 synced, 3 skipped for space, 0 failed"
    got = (await c.get(f"/api/v1/jobs/{job.id}")).json()
    assert got["message"] == msg and job.result["message"] == msg


async def test_first_sync_adopt_and_wipe(env, tmp_path, music, monkeypatch):
    app, c = env
    monkeypatch.setattr(
        type(app.sync), "_removable", _removable_true
    )  # wipe needs a removable disk
    adopt_dir = tmp_path / "adopt"
    (adopt_dir / "old").mkdir(parents=True)
    shutil.copy(music / "Тартак" / "Орбіт.mp3", adopt_dir / "old" / "orbit.mp3")
    (adopt_dir / "notes.txt").write_text("user's file")
    await _register(c, adopt_dir, name="Adopt")
    plan = (await c.post("/api/v1/devices/adopt/plan")).json()
    assert (
        plan["first_sync_matches"] == 1 and plan["first_sync_existing"] == 1
    )  # the mp3, not notes.txt
    await _apply(app, c, plan["id"], first_sync="adopt")
    m = await app.sync.open_device("adopt")
    manifest = await m.read_manifest()
    assert "old/orbit.mp3" in {e.path for e in manifest.items.values()}  # adopted, not copied again
    assert len(manifest.items) == 3 and (adopt_dir / "notes.txt").exists()
    assert (
        not list((adopt_dir / "Music").rglob("Орбіт*")) if (adopt_dir / "Music").exists() else True
    )

    wipe_dir = tmp_path / "wipe"
    (wipe_dir / "junk").mkdir(parents=True)
    (wipe_dir / "junk" / "x.mp3").write_bytes(b"x")
    await _register(c, wipe_dir, name="Wipe")
    plan = (await c.post("/api/v1/devices/wipe/plan")).json()
    await _apply(app, c, plan["id"], first_sync="wipe")
    assert not (wipe_dir / "junk").exists() and len(list(wipe_dir.rglob("*.mp3"))) == 3


async def test_errors(env, tmp_path):
    app, c = env
    assert (await c.post("/api/v1/devices/nope/plan")).status_code == 404
    r = await c.post(
        "/api/v1/devices/register", json={"name": "X", "mount_path": str(tmp_path / "missing")}
    )
    assert r.status_code == 400
    r = await c.post("/api/v1/devices/register", json={"name": "X"})
    assert r.status_code == 400
    await _register(c, tmp_path / "f", name="F")
    r = await c.post("/api/v1/devices/f/format", json={"confirm": "wrong"})
    assert r.status_code == 409 and r.json()["error"] == "confirmation_required"
    r = await c.post("/api/v1/devices/f/format", json={"confirm": "F"})
    assert (
        r.status_code == 400 and "nothing to format" in r.json()["message"]
    )  # a directory, no disk
    r = await c.post(
        "/api/v1/libraries",
        json={"id": "bad", "type": "folder", "name": "B", "settings": {"rooot": "/x"}},
    )
    assert r.status_code == 400
    await c.post(
        "/api/v1/libraries",
        json={
            "id": "gone",
            "type": "folder",
            "name": "G",
            "settings": {"root": str(tmp_path / "nope")},
        },
    )
    gone = {"rules": [{"id": "all", "libraries": ["gone"], "criteria": {"all": []}}]}
    await c.patch("/api/v1/devices/f", json={"rule_set": await _as_template(c, "gone", gone)})
    r = await c.post("/api/v1/devices/f/plan")
    assert r.status_code == 503 and r.json()["error"] == "library_unavailable"
    assert (await c.post("/api/v1/devices/f/eject")).status_code == 204
    assert (await c.delete("/api/v1/devices/f")).status_code == 204
    assert (await c.get("/api/v1/devices/f")).status_code == 404


async def test_manifest_too_new(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "n", name="N")
    (tmp_path / "n" / ".syncmusic").mkdir(exist_ok=True)
    (tmp_path / "n" / ".syncmusic" / "manifest.jsonl").write_text(
        '{"type":"header","manifest_version":99,"device_id":"n"}\n'
    )
    r = await c.post("/api/v1/devices/n/plan")
    assert r.status_code == 409 and r.json()["choices"] == ["wipe", "try_anyway"]


async def test_on_connect_auto_and_ask(env, tmp_path):
    app, c = env
    q = app.bus.subscribe()
    await _register(c, tmp_path / "auto", name="Auto", on_connect="auto")
    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz")
    await app.sync.on_disk_added(ConnectedDisk(disk=disk, device_id="auto"))
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert any(
        e.type == "sync.needs_confirmation" and "first sync" in e.data["reason"] for e in events
    )
    plan = await app.sync.plan("auto")
    await _apply(app, c, plan.plan.id, first_sync="keep")
    # no longer selected (a source that is gone would be an orphan and stay)
    excl = {"id": "x", "criteria": {"all": [{"is": {"artist": "Тартак"}}]}}
    rs = await _as_template(c, "Auto 2", {**ALL, "excludes": [excl]}, ["music"])
    r = await c.patch("/api/v1/devices/auto", json={"rule_set": rs})
    assert r.status_code == 200, r.text
    while not q.empty():
        q.get_nowait()
    await app.sync.on_disk_added(ConnectedDisk(disk=disk, device_id="auto"))
    reasons = [e.data.get("reason", "") for e in (q.get_nowait() for _ in range(q.qsize()))]
    assert any("removes 1 of 3" in r for r in reasons)  # 33 % > the 20 % delete threshold: ask
    await c.patch("/api/v1/devices/auto", json={"delete_threshold_pct": 50})
    await app.sync.on_disk_added(ConnectedDisk(disk=disk, device_id="auto"))  # auto: plan + apply
    job = app.jobs.jobs()[0]
    assert job.kind == "sync" and job.device_id == "auto"
    await app.jobs.wait(job.id)
    assert job.result["counts"]["remove"] == 1
    await _register(c, tmp_path / "ask", name="Ask", on_connect="ask")
    while not q.empty():
        q.get_nowait()
    await app.sync.on_disk_added(ConnectedDisk(disk=disk, device_id="ask"))
    assert q.get_nowait().data["reason"] == "on_connect: ask"


async def test_waiting_confirmation_is_listed_until_applied_or_unplugged(env, tmp_path):
    """GET /devices shows a connect-time sync that waits for the user: the dashboard's Review."""
    app, c = env

    async def waiting(device_id):
        devices = (await c.get("/api/v1/devices")).json()["registered"]
        return next(d for d in devices if d["id"] == device_id)["waiting"]

    await _register(c, tmp_path / "auto", name="Auto", on_connect="auto")
    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz")
    assert await waiting("auto") is None
    await app.sync.on_disk_added(ConnectedDisk(disk=disk, device_id="auto"))
    w = await waiting("auto")
    assert "first sync" in w["reason"] and w["plan_id"]
    r = await c.get(f"/api/v1/plans/{w['plan_id']}")
    assert r.status_code == 200 and r.json()["first_sync"]
    await _apply(app, c, w["plan_id"], first_sync="keep")
    assert await waiting("auto") is None

    await _register(c, tmp_path / "ask", name="Ask", on_connect="ask")
    cd = ConnectedDisk(disk=BlockDeviceInfo(name="sdy", path="/dev/sdy"), device_id="ask")
    await app.sync.on_disk_added(cd)
    assert await waiting("ask") == {"reason": "on_connect: ask", "plan_id": None}
    app.sync.detector.connected["sdy"] = cd
    await app.sync.on_disk_removed("sdy")
    assert await waiting("ask") is None


def test_cli_status_lists_devices(monkeypatch):
    from typer.testing import CliRunner

    from syncmusic.cli import main

    answers = {
        "/health": {"status": "ok", "db": True, "mode": "daemon"},
        "/version": {"version": "0.1"},
        "/devices": {
            "registered": [
                {"id": "f", "name": "Flash", "type": "flashdrive", "connected": True, "busy": None}
            ],
            "unknown": [
                {
                    "disk": "/dev/sdb",
                    "vendor": "Apple",
                    "model": "iPod",
                    "probes": [{"type_name": "flashdrive"}],
                }
            ],
        },
    }
    monkeypatch.setattr(main, "_get", lambda url, path: answers[path])
    out = CliRunner().invoke(main.cli, ["status"]).output
    assert "Flash [flashdrive, id f]: connected" in out and "unknown disk /dev/sdb" in out


async def test_device_space_and_manual_selection(env, tmp_path):
    """Device page capacity bar and the library browser's "add to device…"."""
    app, c = env
    await _register(c, tmp_path / "flash", rule_set={"rules": []}, library_ids=[])
    space = (await c.get("/api/v1/devices/my-flash/space")).json()
    assert space["capacity"] >= space["free"] > 0 and space["planned"] is None

    items = (await c.get("/api/v1/libraries/music/items", params={"query": "орбіт"})).json()
    ids = [i["item_id"] for i in items["items"]]
    r = await c.post(
        "/api/v1/devices/my-flash/manual-selection", json={"library_id": "music", "ids": ids}
    )
    assert r.status_code == 200, r.text
    # the picks go into the device's own template, linked to it; the rule draws from that library
    assert r.json()["rule_set"]["template_ids"] == ["own-my-flash"]
    own = (await c.get("/api/v1/templates/own-my-flash")).json()
    assert own["name"] == "My Flash - own" and own["library_ids"] == []
    assert (
        own["rules"][0]["libraries"] == ["music"] and own["rules"][0]["name"] == "Manual selection"
    )
    r = await c.post(
        "/api/v1/devices/my-flash/manual-selection", json={"library_id": "music", "ids": ids}
    )
    assert r.json()["rule_set"]["template_ids"] == ["own-my-flash"]  # linked once
    own = (await c.get("/api/v1/templates/own-my-flash")).json()
    assert own["rules"][0]["criteria"] == {"any": [{"in": {"track_id": ids}}]}

    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    assert plan["counts"]["add"] == 1
    space = (await c.get("/api/v1/devices/my-flash/space")).json()
    assert space["planned"] == plan["space"]["needed"] > 0
    assert space["planned_reserve"] == plan["space"]["reserve"]
    # the space map's parts are there; nothing synced yet, nothing left off
    assert space["by_library"] == [] and space["by_rule"] == [] and space["left_off"] is None
    assert space["foreign"] + space["database"] == space["used"] and space["reserve"] > 0

    r = await c.post(
        "/api/v1/devices/my-flash/manual-selection", json={"library_id": "nope", "ids": ids}
    )
    assert r.status_code == 404
    r = await c.post(
        "/api/v1/devices/my-flash/manual-selection", json={"library_id": "music", "ids": []}
    )
    assert r.status_code == 400
    # blank and unknown ids are refused and nothing is written
    before = (await c.get("/api/v1/templates/own-my-flash")).json()["rules"]
    for bad, says in (
        ([""], "empty id"),
        (["  "], "empty id"),
        ([ids[0], "nonexistent-id"], "nonexistent-id"),
    ):
        r = await c.post(
            "/api/v1/devices/my-flash/manual-selection", json={"library_id": "music", "ids": bad}
        )
        assert r.status_code == 400 and says in r.json()["message"], r.text
    r = await c.post(
        "/api/v1/devices/my-flash/manual-selection",
        json={"library_id": "music", "ids": ["no-album"], "field": "album_id"},
    )
    assert r.status_code == 400 and "no album 'no-album'" in r.json()["message"]
    assert (await c.get("/api/v1/templates/own-my-flash")).json()["rules"] == before
    assert (await c.get("/api/v1/devices/nope/space")).status_code == 404


async def test_device_space_by_media_kind(env, tmp_path):
    """the Overview's capacity bar splits the used space by media kind from the manifest; the rest of
    the used space (the file system, files SyncMusic did not write) is `other`."""
    app, c = env
    await _register(c, tmp_path / "flash", rule_set={"rules": []}, library_ids=[])
    space = (await c.get("/api/v1/devices/my-flash/space")).json()
    kinds = space["by_kind"]
    assert kinds["music"] == kinds["audiobook"] == kinds["podcast"] == 0
    assert kinds["other"] == space["used"] == space["capacity"] - space["free"]

    items = (await c.get("/api/v1/libraries/music/items", params={"query": "орбіт"})).json()
    ids = [i["item_id"] for i in items["items"]]
    await c.post(
        "/api/v1/devices/my-flash/manual-selection", json={"library_id": "music", "ids": ids}
    )
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    space = (await c.get("/api/v1/devices/my-flash/space")).json()
    kinds = space["by_kind"]
    sizes = sum(p.stat().st_size for p in (tmp_path / "flash").rglob("*.mp3"))
    assert kinds["music"] == sizes > 0 and kinds["audiobook"] == kinds["podcast"] == 0
    assert kinds["other"] == max(0, space["used"] - kinds["music"])
    # a video in the manifest is its own bucket, not a KeyError
    read = app.sync._read_manifest

    async def _with_video(cfg, device):
        m = await read(cfg, device)
        e = next(iter(m.items.values()))
        m.items["video"] = e.model_copy(update={"kind": MediaKind.VIDEO, "size": 7})
        return m

    app.sync._read_manifest = _with_video
    r = await c.get("/api/v1/devices/my-flash/space")
    assert r.status_code == 200 and r.json()["by_kind"]["video"] == 7
    del app.sync._read_manifest
    # the Overview's Full report opens the job that wrote the last report
    last = (await c.get("/api/v1/devices/my-flash")).json()["last_report"]
    assert last["job_id"] in [j["id"] for j in (await c.get("/api/v1/jobs")).json()]


async def test_device_ids_from_any_script_and_known_types_listed(env, tmp_path):
    """ids from non-Latin names are readable (same rule as template ids); unknown types name the known."""
    app, c = env
    assert (await _register(c, tmp_path / "a", name="Флешка тест"))["id"] == "флешка-тест"
    assert (await _register(c, tmp_path / "b", name="Флешка тест!"))["id"].startswith(
        "флешка-тест-"
    )
    assert (await _register(c, tmp_path / "c", name="«»"))["id"] == "device"
    assert (await c.get("/api/v1/devices/флешка-тест")).json()["config"]["name"] == "Флешка тест"
    (tmp_path / "d").mkdir()
    r = await c.post(
        "/api/v1/devices/register",
        json={"name": "X", "mount_path": str(tmp_path / "d"), "type": "flash"},
    )
    assert r.status_code == 400
    assert (
        r.json()["message"]
        == "unknown device type 'flash' (known: android, flashdrive, ftp, hiby, ipod, rockbox, smb)"
    )
    r = await c.patch("/api/v1/devices/флешка-тест", json={"processors": [{"type": "convert"}]})
    assert r.status_code == 400
    assert r.json()["message"] == (
        "unknown processor type 'convert' (known: covers, lyrics, retag, transcode, video)"
    )
    r = await c.post(
        "/api/v1/libraries", json={"id": "x", "type": "emby", "name": "X", "settings": {}}
    )
    assert "(known: audiobookshelf, folder, jellyfin, subsonic)" in r.json()["message"]


async def test_duplicate_device_names_are_refused(env, tmp_path):
    """Two devices with the same name could not be told apart in the UI."""
    _, c = env
    first = await _register(c, tmp_path / "a", name="Флешка тест")
    body = {"name": " флешка ТЕСТ ", "mount_path": str(tmp_path / "b"), "type": "flashdrive"}
    (tmp_path / "b").mkdir()
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 400 and r.json()["error"] == "invalid"
    assert r.json()["message"] == f"a device named 'Флешка тест' already exists (id {first['id']})"
    other = await _register(c, tmp_path / "c", name="Інша")
    r = await c.patch(f"/api/v1/devices/{other['id']}", json={"name": "Флешка тест"})
    assert r.status_code == 400 and "already exists" in r.json()["message"]
    r = await c.patch(
        f"/api/v1/devices/{first['id']}", json={"name": "Флешка тест", "on_connect": "manual"}
    )
    assert r.status_code == 200  # its own name is fine


async def test_register_refuses_unknown_fields(env, tmp_path):
    """a typo or a field register does not take is invalid 400 naming it, not silently dropped."""
    app, c = env
    for extra in (
        {"libraries": ["music"]},
        {"sync": {"safety_margin_min_bytes": 1}},
        {"first_sync_choice": "merge"},  # taken, but only keep / adopt / wipe
    ):
        (tmp_path / "x").mkdir(exist_ok=True)
        body = {"name": "X", "mount_path": str(tmp_path / "x"), "type": "flashdrive", **extra}
        r = await c.post("/api/v1/devices/register", json=body)
        assert r.status_code == 400, r.text
        assert r.json()["error"] == "invalid" and next(iter(extra)) in r.json()["message"]
    # library_ids link each library's "All …" default; own rules are refused
    body = {
        "name": "Ok",
        "mount_path": str(tmp_path / "x"),
        "type": "flashdrive",
        "library_ids": ["music"],
    }
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["rule_set"]["template_ids"] == ["default.music.all"]
    (tmp_path / "y").mkdir()
    body = {"name": "Own", "mount_path": str(tmp_path / "y"), "type": "flashdrive", "rule_set": ALL}
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 400 and "templates only" in r.json()["message"]
    r = await c.patch("/api/v1/devices/ok", json={"rule_set": ALL})
    assert r.status_code == 400 and "templates only" in r.json()["message"]
    # the first sync's choice made on Register is stored for the plan review
    (tmp_path / "z").mkdir()
    body = {
        "name": "Z",
        "mount_path": str(tmp_path / "z"),
        "type": "flashdrive",
        "first_sync_choice": "adopt",
    }
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 201 and r.json()["first_sync_choice"] == "adopt"


async def test_register_links_a_processor_template(env, tmp_path):
    """registration may link a processor template (its chain copied, the link kept); unknown → 404."""
    app, c = env
    dev = await _register(
        c, tmp_path / "pt", name="Linked", processor_template_id="builtin-flashdrive"
    )
    tpl = (await c.get("/api/v1/processor-templates/builtin-flashdrive")).json()
    assert dev["processor_template_id"] == "builtin-flashdrive"
    assert dev["processors"] == tpl["processors"]
    (tmp_path / "bad").mkdir()
    body = {
        "name": "Bad",
        "mount_path": str(tmp_path / "bad"),
        "type": "flashdrive",
        "processor_template_id": "nope",
    }
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 404, r.text
    plain = await _register(
        c, tmp_path / "plain", name="Plain"
    )  # default: suggested chain, not linked
    assert plain["processor_template_id"] is None


async def test_device_profile_api_and_overrides(env, tmp_path):
    """Device profile: sources per field, overrides saved with PATCH and used by the plan."""
    from tests.contracts.harness import rockbox_tree

    app, c = env
    await _register(c, tmp_path / "flash")
    prof = (await c.get("/api/v1/devices/my-flash/profile")).json()
    assert prof["connected"] and prof["label"] == "USB flash drive" and not prof["known"]
    assert prof["fields"]["formats"]["source"] == "default"
    assert prof["fields"]["supports_podcasts"]["value"] is True and not prof["music_only"]

    r = await c.patch(
        "/api/v1/devices/my-flash",
        json={"capability_overrides": {"max_tracks": 2, "cover_size": 200}},
    )
    assert r.status_code == 200, r.text
    f = (await c.get("/api/v1/devices/my-flash/profile")).json()["fields"]
    assert f["max_tracks"] == {"value": 2, "source": "override"}
    assert f["cover_size"] == {"value": 200, "source": "override"}
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    assert plan["counts"]["add"] == 2
    assert any("the most this player handles" in w for w in plan["warnings"])

    r = await c.patch(
        "/api/v1/devices/my-flash", json={"capability_overrides": {"max_tracks": "x"}}
    )
    assert r.status_code in (400, 422)
    r = await c.patch("/api/v1/devices/my-flash", json={"capability_overrides": {"fromats": []}})
    assert r.status_code in (400, 422)
    r = await c.patch("/api/v1/devices/my-flash", json={"capability_overrides": {}})  # Reset
    assert (await c.get("/api/v1/devices/my-flash/profile")).json()["fields"]["max_tracks"] == {
        "value": None,
        "source": "default",
    }

    shutil.rmtree(tmp_path / "flash")
    assert not (await c.get("/api/v1/devices/my-flash/profile")).json()["connected"]
    assert (await c.get("/api/v1/devices/nope/profile")).status_code == 404

    rb = rockbox_tree(tmp_path / "rb")
    await _register(c, rb, name="Mini", type="rockbox")
    prof = (await c.get("/api/v1/devices/mini/profile")).json()
    assert prof["connected"] and prof["model"] == "ipodmini2g" and prof["known"]
    assert prof["music_only"] and "supports_podcasts" not in prof["fields"]
    assert prof["fields"]["has_rtc"] == {"value": True, "source": "detected"}
    assert prof["fields"]["max_sample_rate"] == {"value": 48000, "source": "known_model"}


async def test_sync_job_reports_its_phases(env, tmp_path):
    """the job view's checklist - the apply job's progress walks reverse → copy → database → verify and
    keeps the counters in `stats`."""
    app, c = env
    await _register(c, tmp_path / "flash")
    events = app.bus.subscribe(4096)
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    job = await app.jobs.wait(r.json()["id"])
    phases: list[str] = []
    while not events.empty():
        ev = events.get_nowait()
        if ev.type == "job.progress" and ev.data["job_id"] == job.id:
            phase = ev.data["stats"].get("phase")
            if phase and (not phases or phases[-1] != phase):
                phases.append(phase)
    assert phases == ["reverse", "copy", "database", "verify"]
    added = job.result["counts"]["add"]
    assert job.stats["files_done"] == job.stats["files_total"] == added > 0
    assert job.stats["bytes"] == job.result["bytes_written"] and job.stats["failed"] == 0


async def test_register_takes_the_combine_audiobooks_default(env, tmp_path):
    """a new iPod combines multi-file audiobooks, a flash drive does not."""
    from tests.contracts.harness import ipod_tree

    app, c = env
    flash = await _register(c, tmp_path / "flash")
    assert flash["sync"]["combine_audiobooks"] is False
    ipod = await _register(c, ipod_tree(tmp_path / "ipod"), name="iPod", type="ipod")
    assert ipod["sync"]["combine_audiobooks"] is True
