"""Import / export of device settings and templates between installs."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core import transfer
from syncmusic.core.models import LibraryConfig, RuleTemplate
from tests.unit.test_api_sync import _register, env, music  # noqa: F401


def test_library_mapping_and_default_ids():
    exported = [
        transfer.LibraryRef(id="nd", name="Navidrome", type="subsonic"),
        transfer.LibraryRef(id="abs", name="Books", type="audiobookshelf"),
        transfer.LibraryRef(id="f", name="Folder", type="folder"),
    ]
    local = [
        LibraryConfig(id="navi", name="navidrome", type="subsonic"),
        LibraryConfig(id="a2", name="Audio", type="audiobookshelf"),
    ]
    libs = transfer.library_map(exported, local)
    assert libs == {"nd": "navi", "abs": "a2", "f": None}
    assert transfer.map_id("default.nd.all-music", libs) == "default.navi.all-music"
    assert transfer.map_id("mine-1", libs) == "mine-1"
    tpl = RuleTemplate(
        id="default.nd.x", name="X", library_ids=["nd"], default_of="nd", edited=True
    )
    m = transfer.mapped_template(tpl, libs)
    assert (m.id, m.library_ids, m.default_of) == ("default.navi.x", ["navi"], "navi")


async def test_round_trip_into_the_same_install(env, tmp_path):
    _app, c = env
    dev = await _register(c, tmp_path / "flash", name="Флешка")
    data = (await c.get("/api/v1/settings/export", params={"what": "devices"})).json()
    assert data["format"] == "syncmusic-export"
    (part,) = data["devices"]
    assert "identity" not in part and "mount_path" not in part and part["name"] == "Флешка"
    assert [t["name"] for t in data["templates"]] == ["Флешка rules"]

    # changed afterwards: the import brings the exported settings back
    await c.patch(f"/api/v1/devices/{dev['id']}", json={"delete_threshold_pct": 77})
    tpl_id = dev["rule_set"]["template_ids"][0]
    await c.patch(f"/api/v1/templates/{tpl_id}", json={"name": "Other name"})
    preview = (await c.post("/api/v1/settings/import/preview", json=data)).json()
    assert preview["libraries"] == [
        {"from": "Music", "type": "folder", "to": "music", "to_name": "Music"}
    ]
    actions = {(x["kind"], x["action"]) for x in preview["changes"]}
    assert actions == {("template", "replace"), ("device", "replace")}
    r = await c.post("/api/v1/settings/import", json=data)
    assert r.status_code == 200, r.text
    cfg = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]
    assert cfg["delete_threshold_pct"] == dev["delete_threshold_pct"]
    assert cfg["mount_path"] == dev["mount_path"]  # the device stays bound to its disk / folder
    names = [t["name"] for t in (await c.get("/api/v1/templates")).json()]
    assert "Флешка rules" in names


async def test_unknown_device_and_wrong_file(env):
    _app, c = env
    data = (await c.get("/api/v1/settings/export")).json()
    data["devices"] = [{"name": "Walkman", "type": "flashdrive", "rule_set": {}}]
    preview = (await c.post("/api/v1/settings/import/preview", json=data)).json()
    (dev,) = [x for x in preview["changes"] if x["kind"] == "device"]
    assert dev["action"] == "skip" and "register it first" in dev["reason"]
    r = await c.post("/api/v1/settings/import/preview", json={"format": "syncmusic-backup"})
    assert r.status_code == 400 and "not a SyncMusic export" in r.json()["message"]
