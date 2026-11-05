"""Processor templates: CRUD, built-ins read-only and copyable, apply / link a template to a device, a
linked device follows template edits, editing the chain by hand detaches, delete-in-use detaches, a sync uses the
template's chain."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from tests.media import make_music
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401 - fixtures

MP3 = [
    {
        "type": "transcode",
        "enabled": True,
        "settings": {
            "rules": [{"from": ["flac"], "to": "mp3", "when": "always"}],
        },
    }
]
TAG = {"type": "retag", "enabled": True, "settings": {}}
MP3_TAGGED = [*MP3, TAG]  # every chain gets the tag step


async def _new(c, **body):
    r = await c.post(
        "/api/v1/processor-templates", json={"name": "To MP3", "processors": MP3, **body}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_builtins_crud_and_copy(env):
    app, c = env
    tpls = {t["id"]: t for t in (await c.get("/api/v1/processor-templates")).json()}
    assert {"builtin-ipod", "builtin-rockbox", "builtin-flashdrive"} <= set(tpls)
    ipod = tpls["builtin-ipod"]
    assert ipod["builtin"] and ipod["name"] == "iPod: ALAC/AAC" and ipod["device_type"] == "ipod"
    assert ipod["processors"][0]["type"] == "transcode"
    r = await c.patch("/api/v1/processor-templates/builtin-ipod", json={"name": "x"})
    assert r.status_code == 409
    assert (await c.delete("/api/v1/processor-templates/builtin-ipod")).status_code == 409
    draft = (await c.post("/api/v1/processor-templates/builtin-ipod/copy", json={})).json()
    assert "id" not in draft and len((await c.get("/api/v1/processor-templates")).json()) == len(
        tpls
    )
    copy = (await c.post("/api/v1/processor-templates", json=draft)).json()
    assert (
        copy["name"] == "Copy of iPod: ALAC/AAC"
        and copy["id"] == "copy-of-ipod-alac-aac"
        and not copy["builtin"]
        and copy["processors"] == ipod["processors"]
    )
    t = await _new(c)
    assert t["id"] == "to-mp3" and t["used_by"] == [] and t["device_type"] is None
    r = await c.patch(f"/api/v1/processor-templates/{t['id']}", json={"name": "MP3 always"})
    assert r.status_code == 200 and r.json()["name"] == "MP3 always"
    bad = await c.post(
        "/api/v1/processor-templates", json={"name": "Bad", "processors": [{"type": "nope"}]}
    )
    assert bad.status_code == 400
    bad = await c.post(
        "/api/v1/processor-templates", json={"name": "Bad", "device_type": "toaster"}
    )
    assert bad.status_code == 400
    assert (await c.delete(f"/api/v1/processor-templates/{t['id']}")).status_code == 204
    assert (await c.get(f"/api/v1/processor-templates/{t['id']}")).status_code == 404


async def test_link_follow_detach_and_delete_in_use(env, tmp_path):
    app, c = env
    t = await _new(c)
    dev = await _register(c, tmp_path / "stick", name="Stick")
    own = dev["processors"]
    # link: the chain is copied in
    r = await c.patch(f"/api/v1/devices/{dev['id']}", json={"processor_template_id": t["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["processors"] == MP3_TAGGED and r.json()["processor_template_id"] == t["id"]
    assert (await c.get(f"/api/v1/processor-templates/{t['id']}")).json()["used_by"] == [
        {"id": dev["id"], "name": "Stick"}
    ]
    # the template changes: the linked device follows
    chain = [*MP3, {"type": "retag", "enabled": True, "settings": {}}]
    await c.patch(f"/api/v1/processor-templates/{t['id']}", json={"processors": chain})
    cfg = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]
    assert cfg["processors"] == chain and cfg["processor_template_id"] == t["id"]
    # editing the chain by hand detaches
    r = await c.patch(f"/api/v1/devices/{dev['id']}", json={"processors": own})
    assert r.json()["processor_template_id"] is None and r.json()["processors"] == own
    # apply once without linking
    r = await c.patch(
        f"/api/v1/devices/{dev['id']}", json={"processors": MP3, "processor_template_id": None}
    )
    assert r.json()["processors"] == MP3_TAGGED and r.json()["processor_template_id"] is None
    # delete in use: refused, then detached (the device keeps the chain)
    await c.patch(f"/api/v1/devices/{dev['id']}", json={"processor_template_id": t["id"]})
    assert (await c.delete(f"/api/v1/processor-templates/{t['id']}")).status_code == 409
    assert (await c.delete(f"/api/v1/processor-templates/{t['id']}?detach=true")).status_code == 204
    cfg = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]
    assert cfg["processor_template_id"] is None and cfg["processors"] == chain
    # an unknown template cannot be linked
    r = await c.patch(f"/api/v1/devices/{dev['id']}", json={"processor_template_id": "nope"})
    assert r.status_code == 404


async def test_sync_uses_the_linked_templates_chain(env, music, tmp_path):
    app, c = env
    make_music(music / "Скрябін" / "lossless.flac", title="Lossless", artist="Скрябін", album="F")
    await c.post("/api/v1/libraries/music/refresh")
    t = await _new(c)
    dev = await _register(c, tmp_path / "stick", name="Stick", processors=[])
    await c.patch(f"/api/v1/devices/{dev['id']}", json={"processor_template_id": t["id"]})
    import asyncio

    for _ in range(100):
        plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
        if (
            any(a.get("title") == "Lossless" for a in plan.get("actions", []))
            or plan["counts"]["add"] >= 4
        ):
            break
        await asyncio.sleep(0.05)
    await _apply(app, c, plan["id"], first_sync="keep")
    files = sorted(
        p.name for p in (tmp_path / "stick").rglob("*.*") if p.parent.name != ".syncmusic"
    )
    assert any(f.startswith("Lossless") and f.endswith(".mp3") for f in files), files
    assert not any(f.endswith(".flac") for f in files)


async def test_devices_stored_before_keep_their_chain(env, tmp_path):
    """No template link for devices from before their own chain stays as it was."""
    app, c = env
    dev = await _register(c, tmp_path / "stick", name="Stick")
    cfg = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]
    assert cfg["processor_template_id"] is None and cfg["processors"] == dev["processors"]
