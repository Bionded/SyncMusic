"""Library management, types, rules, templates and settings API."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.core import registry
from syncmusic.facade import AppFacade
from syncmusic.infra import notify
from tests.media import make_music
from tests.unit.test_api_sync import ALL, _register, env, music  # noqa: F401 - fixtures

RULE = {"id": "skryabin", "name": "Скрябін", "criteria": {"all": [{"is": {"artist": "Скрябін"}}]}}


async def _job(app, r):
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    return job.result


# --- types ---------------------------------------------------------------------------------------


async def test_types_list_every_registered_class_with_a_schema(env):
    app, c = env
    for path, classes in (
        ("library-types", registry.library_classes()),
        ("device-types", registry.device_classes()),
        ("processor-types", registry.processor_classes()),
    ):
        r = await c.get(f"/api/v1/{path}")
        assert r.status_code == 200
        body = r.json()
        assert sorted(t["type"] for t in body) == sorted(cls.type_name for cls in classes)
        for t in body:
            assert t["display_name"] and t["settings_schema"]["type"] == "object"
    flash = next(
        t for t in (await c.get("/api/v1/device-types")).json() if t["type"] == "flashdrive"
    )
    assert [p["type"] for p in flash["suggested_processors"]] == ["transcode", "retag", "lyrics"]
    lib_types = {t["type"]: t for t in (await c.get("/api/v1/library-types")).json()}
    assert "root" in lib_types["folder"]["settings_schema"]["properties"]
    assert [k for k, t in lib_types.items() if t["beta"]] == ["jellyfin"]
    assert lib_types["jellyfin"]["capabilities"]["kinds"] == ["video"]


# --- libraries -----------------------------------------------------------------------------------


async def test_library_patch_merges_settings_and_refuses_id_or_type(env, tmp_path):
    app, c = env
    other = tmp_path / "other"
    make_music(other / "a.mp3", title="Зірка", artist="Скрябін")
    r = await c.patch("/api/v1/libraries/music", json={"name": "Музика"})
    assert r.status_code == 200 and r.json()["name"] == "Музика"
    assert r.json()["settings"]["root"].endswith("music")  # kept
    r = await c.patch("/api/v1/libraries/music", json={"settings": {"root": str(other)}})
    assert r.status_code == 200
    items = (await c.get("/api/v1/libraries/music/items")).json()
    assert [i["title"] for i in items["items"]] == ["Зірка"]  # the cached snapshot was dropped
    for bad in ({"id": "x"}, {"type": "navidrome"}, {"colour": "red"}, {"settings": {"nope": 1}}):
        r = await c.patch("/api/v1/libraries/music", json=bad)
        assert r.status_code == 400 and r.json()["error"] == "invalid", (bad, r.text)
    r = await c.patch("/api/v1/libraries/nope", json={"name": "x"})
    assert r.status_code == 404


async def test_library_delete_refused_while_attached(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash")
    r = await c.delete("/api/v1/libraries/music")
    assert r.status_code == 409 and "My Flash" in r.json()["message"]
    # its template draws from it
    dev = (await c.get("/api/v1/devices/my-flash")).json()
    assert dev["libraries"] == ["music"]
    tid = dev["config"]["rule_set"]["template_ids"][0]
    r = await c.delete(f"/api/v1/templates/{tid}", params={"detach": "true"})
    assert r.status_code == 204, r.text
    assert (await c.delete("/api/v1/libraries/music")).status_code == 204
    assert (await c.get("/api/v1/libraries/music")).status_code == 404
    assert (await c.delete("/api/v1/libraries/music")).status_code == 404


async def test_library_refresh_job_and_items_paging(env, music):
    app, c = env
    r = await c.post("/api/v1/libraries/music/refresh")
    assert r.json()["name"] == "refresh Music"  # the library's name, not its id
    result = await _job(app, r)
    assert result == {"tracks": 3, "playlists": 0}
    make_music(
        music / "Скрябін" / "03 Війна.mp3", title="Війна", artist="Скрябін", album="Група крові"
    )
    result = await _job(app, await c.post("/api/v1/libraries/music/refresh"))
    assert result["tracks"] == 4

    body = (await c.get("/api/v1/libraries/music/items", params={"page_size": 3})).json()
    assert body["total"] == 4 and len(body["items"]) == 3 and body["page"] == 0
    assert [i["artist"] for i in body["items"]] == ["Скрябін"] * 3  # sorted by artist, album, track
    page2 = (
        await c.get("/api/v1/libraries/music/items", params={"page_size": 3, "page": 1})
    ).json()
    assert [i["artist"] for i in page2["items"]] == ["Тартак"]
    found = (await c.get("/api/v1/libraries/music/items", params={"query": "скрябін ГРУПА"})).json()
    assert found["total"] == 3 and all(i["kind"] == "music" for i in found["items"])
    none = (await c.get("/api/v1/libraries/music/items", params={"kind": "podcast"})).json()
    assert none["total"] == 0
    r = await c.get("/api/v1/libraries/music/items", params={"page_size": 0})
    assert r.status_code == 400
    r = await c.get("/api/v1/libraries/music/items", params={"kind": "hologram"})
    assert r.status_code == 400
    assert (await c.post("/api/v1/libraries/nope/refresh")).status_code == 404


# --- rules -----------------------------------------------------------------------------------------


async def test_rules_validate(env):
    app, c = env
    r = await c.post("/api/v1/rules/validate", json={"rule": RULE})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["errors"] == []
    bad = {**RULE, "criteria": {"all": [{"nonsense": {"artist": "x"}}]}}
    body = (await c.post("/api/v1/rules/validate", json={"rule_set": {"rules": [bad]}})).json()
    assert not body["ok"] and body["errors"]
    body = (
        await c.post("/api/v1/rules/validate", json={"text": '{"all": [{"is": {"loved": true}}]}'})
    ).json()
    assert body["ok"] and body["rule_set"]["rules"][0]["criteria"]["all"]  # bare .nsp parsed
    body = (await c.post("/api/v1/rules/validate", json={"text": "{"})).json()
    assert not body["ok"] and body["rule_set"] is None
    for req in ({}, {"rule": RULE, "text": "x"}):
        r = await c.post("/api/v1/rules/validate", json=req)
        assert r.status_code == 400 and r.json()["error"] == "invalid"


async def test_device_rules_test_preview(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash")
    body = (
        await c.post("/api/v1/devices/my-flash/rules/test")
    ).json()  # the device's own: everything
    assert body["items"] == 3 and body["bytes"] > 0 and len(body["sample"]) == 3
    (row,) = body["per_rule"]
    assert {k: row[k] for k in ("rule_id", "name", "items", "bytes", "matched")} == {
        "rule_id": "all",
        "name": "Everything",
        "items": 3,
        "bytes": body["bytes"],
        "matched": 3,
    }  # name:
    assert len(row["examples"]) == 3 and row["matches_nothing"] is False
    # a rule below that only matches what the first brings: 0 items of its own, 3 matched
    every = {"id": "all", "name": "Everything", "criteria": {"all": []}}
    again = {**every, "id": "again", "name": "Again"}
    r = await c.post(
        "/api/v1/devices/my-flash/rules/test", json={"rule_set": {"rules": [every, again]}}
    )
    rows = {x["rule_id"]: x for x in r.json()["per_rule"]}
    assert (rows["again"]["items"], rows["again"]["matched"], rows["all"]["items"]) == (0, 3, 3), (
        rows
    )
    r = await c.post("/api/v1/devices/my-flash/rules/test", json={"rule_set": {"rules": [RULE]}})
    body = r.json()
    assert body["items"] == 2 and {s["artist"] for s in body["sample"]} == {"Скрябін"}
    r = await c.post(
        "/api/v1/devices/my-flash/rules/test",
        json={"rule_set": {"rules": [{**RULE, "limit": -1}]}},
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid"
    assert (await c.post("/api/v1/devices/nope/rules/test")).status_code == 404


# --- templates -------------------------------------------------------------------------------------


async def test_templates_crud_and_usage(env, tmp_path):
    app, c = env
    r = await c.post("/api/v1/templates", json={"name": "Український рок", "rules": [RULE]})
    assert r.status_code == 201, r.text
    tpl = r.json()
    assert tpl["id"] == "український-рок"  # slug of the name
    tid = tpl["id"]
    r = await c.post("/api/v1/templates", json={"id": tid, "name": "again"})
    assert r.status_code == 409
    r = await c.post("/api/v1/templates", json={"name": "Bad", "rules": [{**RULE, "limit": -1}]})
    assert r.status_code == 400
    listed = [t["id"] for t in (await c.get("/api/v1/templates")).json() if not t["default_of"]]
    assert listed == [tid]  # plus the library's default templates

    await _register(c, tmp_path / "flash", rule_set={"template_ids": [tid]})
    assert (await c.get(f"/api/v1/templates/{tid}/usage")).json() == [
        {"id": "my-flash", "name": "My Flash"}
    ]
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    assert plan["counts"]["add"] == 2
    r = await c.patch(f"/api/v1/templates/{tid}", json={"rules": [{**RULE, "limit": 1}]})
    assert r.status_code == 200 and r.json()["rules"][0]["limit"] == 1
    assert (await c.get(f"/api/v1/plans/{plan['id']}")).status_code == 404  # outdated plan dropped
    plan = (await c.post("/api/v1/devices/my-flash/plan", json={})).json()
    assert plan["counts"]["add"] == 1
    for bad in ({"id": "other"}, {"colour": 1}, {"rules": [{**RULE, "limit": -1}]}):
        r = await c.patch(f"/api/v1/templates/{tid}", json=bad)
        assert r.status_code == 400, (bad, r.text)

    r = await c.delete(f"/api/v1/templates/{tid}")
    assert r.status_code == 409 and "My Flash" in r.json()["message"]
    assert (
        await c.delete(f"/api/v1/templates/{tid}", params={"detach": "true"})
    ).status_code == 204
    device = (await c.get("/api/v1/devices/my-flash")).json()["config"]
    assert tid not in device["rule_set"]["template_ids"]  # unlinked, not copied
    assert device["rule_set"]["rules"] == []
    assert (await c.get(f"/api/v1/templates/{tid}")).status_code == 404
    assert (await c.get(f"/api/v1/templates/{tid}/usage")).status_code == 404


# --- settings --------------------------------------------------------------------------------------


async def _first_run(app, api) -> None:
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
        defaults = (await c.get("/api/v1/settings")).json()
        assert defaults == {
            "cache_max_gb": 20.0,
            "cache_combined_books": True,
            "concurrency": None,
            "max_device_syncs": None,
            "webhook_url": None,
            "webhook_events": list(notify.DEFAULT_EVENTS),
            "default_on_connect": "manual",
            "default_sync_windows": [],
            "default_update_sync": True,
            "default_update_every_min": 60,
            "default_verify": "all",
            "default_eject_after": "off",
            "default_no_cache": False,
            "default_no_cache_files": 4,
            "default_no_cache_budget_mb": 1000,
            "default_no_cache_file_max_mb": 256,
            "write_limit_mb_s": None,
            "db_backup_every": "daily",
            "db_backup_keep": 7,
            "db_backup_dir": None,
            "snapshot_refresh": "sync",
            "update_change_gap_min": 15,
            "ffmpeg_path": None,
            "convert_nice": 10,
            "log_level": None,
            "update_channel": "stable",
            "auto_update": "off",
            "history_days": 90,
            "log_days": 14,
            "notification_days": 7,
            "disk_warn_gb": 5.0,
            "inbox_events": list(notify.INBOX_EVENTS),
            "quiet_hours": None,
        }

        r = await c.patch(
            "/api/v1/settings",
            json={"cache_max_gb": 1.5, "concurrency": 2, "default_on_connect": "ask"},
        )
        assert r.status_code == 200 and r.json()["cache_max_gb"] == 1.5
        assert app.cache.max_bytes == int(1.5 * 1000**3)
        for bad in (
            {"cache_max_gb": 0},
            {"concurrency": 99},
            {"max_device_syncs": 0},
            {"default_verify": "some"},
            {"write_limit_mb_s": 0},
            {"colour": "red"},
        ):
            r = await c.patch("/api/v1/settings", json=bad)
            assert r.status_code == 400 and r.json()["error"] == "invalid", (bad, r.text)


async def test_settings_persist_across_restart(config, music, tmp_path):  # noqa: F811
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False)
    try:
        await _first_run(app, api)
    finally:
        await app.stop()

    app = Application(config)
    await app.start()
    try:
        assert app.jobs.max_workers == 2 and app.cache.max_bytes == int(1.5 * 1000**3)
        api = create_api(AppFacade(app), manage_lifecycle=False)
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            assert (await c.get("/api/v1/settings")).json()["default_on_connect"] == "ask"
            await c.post(
                "/api/v1/libraries",
                json={
                    "id": "music",
                    "type": "folder",
                    "name": "M",
                    "settings": {"root": str(music)},
                },
            )
            cfg = await _register(c, tmp_path / "flash")
            assert cfg["on_connect"] == "ask"  # the stored default for a registration without one
            cfg = await _register(c, tmp_path / "flash2", name="Two", on_connect="auto")
            assert cfg["on_connect"] == "auto"
    finally:
        await app.stop()


# --- OpenAPI ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/libraries/{library_id}/items",
        "/api/v1/libraries/{library_id}/refresh",
        "/api/v1/library-types",
        "/api/v1/device-types",
        "/api/v1/processor-types",
        "/api/v1/rules/validate",
        "/api/v1/devices/{device_id}/rules/test",
        "/api/v1/templates",
        "/api/v1/templates/{template_id}",
        "/api/v1/templates/{template_id}/usage",
        "/api/v1/settings",
    ],
)
async def test_openapi_lists_the_routes(env, path):
    app, c = env
    schema = (await c.get("/openapi.json")).json()
    assert path in schema["paths"]


async def test_openapi_lists_every_route(env):
    app, c = env
    schema = (await c.get("/openapi.json")).json()
    from syncmusic.api.routes import manage, sync

    facade = AppFacade(app)
    routes = {
        ("/api/v1" + r.path, m.lower())
        for module in (sync, manage)
        for r in module.router(facade).routes
        for m in getattr(r, "methods", ())
    }
    assert len(routes) >= 36
    documented = {(p, m) for p, ops in schema["paths"].items() for m in ops}
    assert routes <= documented


@pytest.mark.parametrize("root", ["", "  ", "music", "./music"])
async def test_folder_root_must_be_absolute(env, root):
    """an empty or relative root would scan the daemon's working directory."""
    app, c = env
    r = await c.post(
        "/api/v1/libraries",
        json={"id": "rel", "type": "folder", "name": "Rel", "settings": {"root": root}},
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid"
    assert "root:" in r.json()["message"] and "\n" not in r.json()["message"]
    r = await c.patch("/api/v1/libraries/music", json={"settings": {"root": root}})
    assert r.status_code == 400 and r.json()["error"] == "invalid"


async def test_sync_defaults_apply_to_a_new_device(env, tmp_path):  # noqa: F811
    """Settings → Sync defaults: a newly registered device starts with them; existing ones keep theirs."""
    _app, c = env
    before = await _register(c, tmp_path / "old", name="Old")
    r = await c.patch(
        "/api/v1/settings",
        json={"default_update_sync": False, "default_verify": "sample", "write_limit_mb_s": 4},
    )
    assert r.status_code == 200, r.text
    cfg = await _register(c, tmp_path / "new", name="New")
    assert cfg["update_sync"] is False and cfg["sync"]["verify"] == "sample"
    old = (await c.get(f"/api/v1/devices/{before['id']}")).json()["config"]
    assert old["update_sync"] is True and old["sync"]["verify"] == "all"
    assert old["update_every_min"] == 60  # the default interval
    r = await c.patch("/api/v1/settings", json={"default_update_every_min": None})
    assert r.status_code == 200, r.text
    off = await _register(c, tmp_path / "off", name="Off")
    assert off["update_every_min"] is None and cfg["update_every_min"] == 60


async def test_cache_default_applies_to_a_new_device(env, tmp_path):  # noqa: F811
    """Settings → Sync defaults: with or without the processing cache and the no-cache memory
    limits; a new device takes them, an existing one keeps its own."""
    _app, c = env
    before = await _register(c, tmp_path / "old", name="Old")
    r = await c.patch(
        "/api/v1/settings",
        json={
            "default_no_cache": True,
            "default_no_cache_files": 8,
            "default_no_cache_budget_mb": 500,
        },
    )
    assert r.status_code == 200, r.text
    cfg = await _register(c, tmp_path / "new", name="New")
    assert cfg["sync"]["no_cache"] is True and cfg["sync"]["no_cache_files"] == 8
    assert cfg["sync"]["no_cache_budget_mb"] == 500 and cfg["sync"]["no_cache_file_max_mb"] == 256
    old = (await c.get(f"/api/v1/devices/{before['id']}")).json()["config"]
    assert old["sync"]["no_cache"] is False and old["sync"]["no_cache_files"] == 4
    r = await c.patch("/api/v1/settings", json={"default_no_cache_files": 99})
    assert r.status_code == 400


async def test_version_names_the_host(env):  # noqa: F811
    _app, c = env
    v = (await c.get("/api/v1/version")).json()
    assert v["host"] and v["python"] and "uptime_s" in v and "model" in v


async def test_a_wrong_ffmpeg_path_is_refused(env, tmp_path):  # noqa: F811
    _app, c = env
    r = await c.patch("/api/v1/settings", json={"ffmpeg_path": str(tmp_path / "nope" / "ffmpeg")})
    assert r.status_code == 400 and "does not run" in r.json()["message"]
    info = (await c.get("/api/v1/settings/ffmpeg")).json()
    assert info["error"] is None and info["version"]
    r = await c.patch("/api/v1/settings", json={"snapshot_refresh": "hourly", "convert_nice": 19})
    assert r.status_code == 200 and _app.sync.snapshots.policy == "hourly"


async def test_rule_test_shows_every_rule(env, tmp_path):
    """each rule has its count and a few examples, a rule matching nothing says so, the sample
    covers every rule (not the first rule's matches) and the full list downloads as CSV."""
    app, c = env
    kino = {
        "id": "skryabin",
        "name": "Скрябін",
        "criteria": {"all": [{"is": {"artist": "Скрябін"}}]},
    }
    splin = {"id": "tartak", "name": "Тартак", "criteria": {"all": [{"is": {"artist": "Тартак"}}]}}
    none = {"id": "none", "name": "Nobody", "criteria": {"all": [{"is": {"artist": "Nobody"}}]}}
    body = {"rule_set": {"rules": [kino, splin, none]}, "library_ids": ["music"]}
    res = (await c.post("/api/v1/rules/test", json=body)).json()
    rows = {x["rule_id"]: x for x in res["per_rule"]}
    assert list(rows) == ["skryabin", "tartak", "none"]
    assert [e["artist"] for e in rows["tartak"]["examples"]] == ["Тартак"]
    assert {e["artist"] for e in rows["skryabin"]["examples"]} == {"Скрябін"}
    assert rows["none"]["matches_nothing"] and rows["none"]["examples"] == []
    assert {s["artist"] for s in res["sample"]} == {"Скрябін", "Тартак"}
    r = await c.post("/api/v1/rules/test/csv", json=body)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("rules,title,artist") and len(lines) == 4
    assert any(line.startswith("Тартак,Орбіт без цукру,Тартак") for line in lines), lines
