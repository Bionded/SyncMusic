"""a device has templates only; its libraries are derived from them."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

import json

from syncmusic.core.models import DeviceConfig, Rule, RuleSet, RuleTemplate
from syncmusic.core.rules.templates import device_libraries
from tests.unit.test_api_sync import _apply, env, music  # noqa: F401

TPLS = {
    "nd-only": RuleTemplate(id="nd-only", name="N", library_ids=["nd"], rules=[Rule(id="a")]),
    "every": RuleTemplate(id="every", name="E", rules=[Rule(id="b")]),
}


def test_libraries_come_from_the_templates_rules():
    known = ["nd", "abs", "f"]
    assert device_libraries(RuleSet(), TPLS, known) == []
    assert device_libraries(RuleSet(template_ids=["nd-only"]), TPLS, known) == ["nd"]
    assert device_libraries(RuleSet(template_ids=["every"]), TPLS, known) == known
    assert device_libraries(RuleSet(template_ids=["gone"]), TPLS, known) == []
    # a device's own template: no libraries of its own, each rule names its library
    own = RuleTemplate(
        id="own-d",
        name="D - own",
        rules=[Rule(id="m", libraries=["f", "x"]), Rule(id="off", enabled=False)],
        excludes=[Rule(id="x")],
    )
    tpls = {**TPLS, "own-d": own}
    assert device_libraries(RuleSet(template_ids=["nd-only", "own-d"]), tpls, known) == ["nd", "f"]
    only_excludes = own.model_copy(update={"rules": []})
    assert device_libraries(RuleSet(template_ids=["own-d"]), {"own-d": only_excludes}, known) == []


async def test_a_template_alone_makes_a_device_sync(env, tmp_path):
    """A device with only a linked default template syncs that template's library, and its status and reverse
    sync follow (no `library_ids` anywhere)."""
    app, c = env
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 201, r.text
    listed = (await c.get("/api/v1/devices")).json()["registered"][0]
    assert listed["no_libraries"] and listed["libraries"] == []
    r = await c.post("/api/v1/devices/stick/plan")
    assert r.status_code == 400 and "no templates" in r.json()["message"]
    r = await c.post("/api/v1/devices/stick/rules/preview", json={"rule_set": {}})
    assert r.status_code == 200 and r.json()["items"] == 0, r.text  # nothing selected, no 400
    assert r.json()["connected"] is False and "no templates" in r.json()["reason"]

    rs = {"template_ids": ["default.music.all"]}
    assert (await c.patch("/api/v1/devices/stick", json={"rule_set": rs})).status_code == 200
    listed = (await c.get("/api/v1/devices")).json()["registered"][0]
    assert not listed["no_libraries"] and listed["libraries"] == ["music"]
    assert (await c.get("/api/v1/devices/stick")).json()["libraries"] == ["music"]
    plan = (await c.post("/api/v1/devices/stick/plan")).json()
    result = await _apply(app, c, plan["id"], first_sync="keep")
    assert result["counts"]["add"] == 3
    cfg = await app.sync.config("stick")
    assert list((await app.sync._libraries_now(cfg))[0]) == ["music"]  # the reverse sync reaches it
    # a library that is down maps to None, with why by its name and the cause
    lib = await app.sync.library("music")
    lib.settings = lib.settings.model_copy(update={"root": tmp_path / "gone"})
    libs, why = await app.sync._libraries_now(cfg)
    assert libs["music"] is None
    assert why["music"] == f"Music is not reachable: folder {tmp_path / 'gone'} does not exist"


async def test_a_library_account_is_its_own_instance(env, tmp_path):
    """a library account is an instance with the overridden settings and its own snapshot key,
    shared by every device that signs in as it; other devices and the library keep the main account; editing
    the library drops them all."""
    app, c = env
    other = tmp_path / "kid-music"
    other.mkdir()
    accounts = {"kid": {"name": "Kid", "settings": {"root": str(other)}}}
    assert (
        await c.patch("/api/v1/libraries/music", json={"accounts": accounts})
    ).status_code == 200
    main = await app.sync.library("music")
    kid = DeviceConfig(id="ipod", type="flashdrive", name="iPod", library_accounts={"music": "kid"})
    lib = await app.sync.library("music", kid)
    assert lib is not main and lib.account == "kid" and lib.cache_key == "music@kid"
    assert lib.id == "music" and str(lib.settings.root) == str(other)
    assert str(main.settings.root) != str(other) and main.cache_key == "music"
    stick = DeviceConfig(id="stick", type="flashdrive", name="S", library_accounts={"music": "kid"})
    assert await app.sync.library("music", stick) is lib  # one instance, one snapshot for both
    plain = DeviceConfig(id="mum", type="flashdrive", name="Mum")
    assert await app.sync.library("music", plain) is main
    await app.sync.snapshots.get(lib)
    assert app.sync.snapshots.counts("music@kid") is not None
    await app.sync.library_changed("music")
    assert "music@kid" not in app.sync._libraries and "music" not in app.sync._libraries
    assert await app.sync.snapshots.load("music@kid") is None


async def test_a_device_plans_and_syncs_as_a_library_account(env, tmp_path):
    """the device picks a library account → it plans from that account (here: another
    folder), the library and other devices stay on the main one; account secrets come back masked and a mask
    sent back keeps them; an account a device uses cannot be removed; an unknown account is refused."""
    from tests.media import make_music

    app, c = env
    kid_root = tmp_path / "kid"
    make_music(kid_root / "Kid" / "01 Lullaby.mp3", title="Lullaby", artist="Kid")
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    assert (await c.post("/api/v1/devices/register", json=body)).status_code == 201
    rs = {"template_ids": ["default.music.all"]}
    assert (await c.patch("/api/v1/devices/stick", json={"rule_set": rs})).status_code == 200
    main_adds = (await c.post("/api/v1/devices/stick/plan")).json()["counts"]["add"]

    acc = {"kid": {"name": "Kid", "settings": {"root": str(kid_root), "password": "s3cret"}}}
    r = await c.patch("/api/v1/libraries/music", json={"accounts": acc})
    assert r.status_code == 200, r.text
    assert r.json()["accounts"]["kid"]["settings"]["password"] == "********"
    acc["kid"]["settings"]["password"] = "********"
    assert (await c.patch("/api/v1/libraries/music", json={"accounts": acc})).status_code == 200
    assert (await app.store.library("music")).accounts["kid"].settings["password"] == "s3cret"

    r = await c.patch("/api/v1/devices/stick", json={"library_accounts": {"music": "nope"}})
    assert r.status_code == 400, r.text
    r = await c.patch("/api/v1/devices/stick", json={"library_accounts": {"music": "kid"}})
    assert r.status_code == 200 and r.json()["library_accounts"] == {"music": "kid"}
    plan = (await c.post("/api/v1/devices/stick/plan")).json()
    assert plan["counts"]["add"] == 1 != main_adds
    result = await _apply(app, c, plan["id"], first_sync="keep")
    assert result["counts"]["add"] == 1
    assert "music@kid" in app.sync._libraries
    main = await app.sync.library("music")
    assert len((await app.sync.snapshots.get(main)).tracks) == main_adds

    r = await c.patch("/api/v1/libraries/music", json={"accounts": {}})
    assert r.status_code == 409 and "Stick (Kid)" in r.text, r.text
    r = await c.patch("/api/v1/devices/stick", json={"library_accounts": {"music": None}})
    assert r.status_code == 200 and r.json()["library_accounts"] == {}
    plan = (await c.post("/api/v1/devices/stick/plan")).json()
    assert plan["counts"]["add"] == main_adds  # back on the main account
    assert (await c.patch("/api/v1/libraries/music", json={"accounts": {}})).status_code == 200


async def test_account_fields_and_the_account_test(env, tmp_path):
    """/library-types names each type's account fields (none for a folder); Test as an
    account resolves a masked secret to the account's stored value, not the library's."""
    app, c = env
    types = {t["type"]: t["account_fields"] for t in (await c.get("/api/v1/library-types")).json()}
    assert types["folder"] == [] and types["subsonic"] == ["username", "password"]
    assert types["audiobookshelf"] == ["api_token"]
    other = tmp_path / "other"
    other.mkdir()
    acc = {"kid": {"name": "Kid", "settings": {"root": str(other)}}}
    await c.patch("/api/v1/libraries/music", json={"accounts": acc})
    r = await c.post("/api/v1/libraries/music/check", json={"account": "kid"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    r = await c.post(
        "/api/v1/libraries/music/check",
        json={"account": "kid", "settings": {"root": str(tmp_path / "gone")}},
    )
    assert not r.json()["ok"]


async def test_device_credentials_move_into_library_accounts(env, tmp_path):
    """credentials stored in a device become a library account named after the user on
    load; a second device with the same credentials shares it; nothing is lost."""
    app, _c = env
    creds = {"root": str(tmp_path), "username": "anna"}
    for dev_id in ("a", "b"):
        cfg = DeviceConfig(id=dev_id, type="flashdrive", name=dev_id.upper())
        await app.store.put_device(cfg)
        row = {**json.loads(cfg.model_dump_json()), "library_accounts": {"music": creds}}
        await app.store.db.execute(  # as stored before
            "UPDATE devices SET config = ? WHERE id = ?", (json.dumps(row), dev_id)
        )
        stored = await app.store.device(dev_id)
        assert stored is not None and stored.legacy_library_accounts == {"music": creds}
    await app.sync.load()
    lib = await app.store.library("music")
    assert lib is not None and [(a.name, a.settings) for a in lib.accounts.values()] == [
        ("anna", creds)
    ]
    aid = next(iter(lib.accounts))
    for dev_id in ("a", "b"):
        dev = await app.store.device(dev_id)
        assert dev is not None and dev.library_accounts == {"music": aid}
        assert dev.legacy_library_accounts == {}


async def test_browse_pick_and_test_as_a_library_account(env, tmp_path):
    """the browser's items and playlists, value suggestions and the template rule test read the library
    as the chosen account; without one, the main account."""
    from tests.media import make_music

    app, c = env
    kid_root = tmp_path / "kid"
    make_music(kid_root / "Kid" / "01 Lullaby.mp3", title="Lullaby", artist="Kiddo")
    acc = {"kid": {"name": "Kid", "settings": {"root": str(kid_root)}}}
    assert (await c.patch("/api/v1/libraries/music", json={"accounts": acc})).status_code == 200
    main = (await c.get("/api/v1/libraries/music/items")).json()
    kid = (await c.get("/api/v1/libraries/music/items?account=kid")).json()
    assert [i["title"] for i in kid["items"]] == ["Lullaby"]
    assert "Lullaby" not in [i["title"] for i in main["items"]]
    r = await c.get("/api/v1/libraries/music/playlists?account=kid")
    assert r.status_code == 200 and r.json() == []
    r = await c.get("/api/v1/libraries/music/items?account=nope")
    assert r.status_code == 404
    r = await c.get("/api/v1/rules/values?field=artist&libraries=music&accounts=music:kid")
    assert [v["value"] for v in r.json()] == ["Kiddo"]
    r = await c.get("/api/v1/rules/values?field=artist&libraries=music")
    assert "Kiddo" not in [v["value"] for v in r.json()]
    body = {"rule_set": {"rules": [{"id": "a"}]}, "library_ids": ["music"]}
    as_kid = (
        await c.post("/api/v1/rules/test", json={**body, "accounts": {"music": "kid"}})
    ).json()
    as_main = (await c.post("/api/v1/rules/test", json=body)).json()
    assert as_kid != as_main


async def test_the_plan_names_picks_missing_in_the_accounts_library(env, tmp_path):
    """a device signed in as another user plans from that user's library; a pick or playlist that
    library lacks is not dropped silently: the plan warns "not in Kid's library"."""
    from tests.media import make_music

    app, c = env
    kid_root = tmp_path / "kid"
    make_music(kid_root / "Kiddo" / "01 Lullaby.mp3", title="Lullaby", artist="Kiddo")
    acc = {"kid": {"name": "Kid", "settings": {"root": str(kid_root)}}}
    await c.patch("/api/v1/libraries/music", json={"accounts": acc})
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    await c.post("/api/v1/devices/register", json=body)
    conds = [
        {"is": {"artist": "Kiddo"}},
        {"is": {"artist": "Nobody Here"}},
        {"inPlaylist": {"name": "Road trip"}},
    ]
    tpl = {"id": "picks", "name": "Picks", "library_ids": ["music"]}
    tpl["rules"] = [{"id": "p", "criteria": {"any": conds}}]
    assert (await c.post("/api/v1/templates", json=tpl)).status_code < 400
    rs = {"template_ids": ["picks"]}
    await c.patch(
        "/api/v1/devices/stick", json={"rule_set": rs, "library_accounts": {"music": "kid"}}
    )
    plan = (await c.post("/api/v1/devices/stick/plan")).json()
    assert plan["counts"]["add"] == 1
    text = " ".join(plan["warnings"])
    assert "1 picked item is not in Kid's library (Nobody Here)" in text, text
    assert "Road trip" in text and "in Kid's library" in text
