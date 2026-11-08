# SPDX-License-Identifier: GPL-3.0-or-later
"""Templates on the device through the API: a full sync writes the linked ones, another installation's rows stay,
importing makes linked copies, a newer original offers "update my copy"."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from syncmusic.core import device_templates as dt_io
from syncmusic.core.models import ManifestLibrary, Rule, RuleTemplate
from tests.contracts.harness import rockbox_tree
from tests.unit.test_api_sync import _apply, env, music  # noqa: F401

OTHER_ID, OTHER_NAME = "other-instance", "Kitchen box"
SHARE = "folder:smb://nas.example/music"  # their library; paired with our "music" below


def _file(mount: Path) -> dict:
    return json.loads((mount / ".syncmusic" / dt_io.FILE_NAME).read_text())


def _other_row(name: str, when: dt.datetime) -> dt_io.DeviceTemplate:
    tpl = RuleTemplate(
        id="their-mix",
        name=name,
        library_ids=["their-music"],
        rules=[Rule(id="r1", name="Rock", libraries=["their-music"], criteria={"all": []})],
        updated_at=when,
    )
    libs = {"their-music": ManifestLibrary(source_id=SHARE, type="folder", name="Their music")}
    return dt_io.export(tpl, libs, OTHER_ID, OTHER_NAME)


async def _sync(app, c, dev: str) -> None:
    plan = (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()
    await _apply(app, c, plan["id"], first_sync="keep")


async def test_import_update_and_keep_other_rows(env, tmp_path):
    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    body = {"name": "Mini", "type": "rockbox", "mount_path": str(mount), "library_ids": ["music"]}
    dev = (await c.post("/api/v1/devices/register", json=body)).json()["id"]
    await _sync(app, c, dev)
    rows = _file(mount)["templates"]
    assert rows and all(r["instance_id"] == app.instance_id for r in rows)

    await app.store.put_library_alias(SHARE, "music")
    await app.update_manifest_context()
    t0 = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
    their = _other_row("Their mix", t0)
    path = mount / ".syncmusic" / dt_io.FILE_NAME
    path.write_bytes(dt_io.dumps([*dt_io.loads(path.read_bytes()), their]))

    listed = (await c.get(f"/api/v1/devices/{dev}/device-templates")).json()["templates"]
    assert [(t["name"], t["state"], t["instance_name"]) for t in listed] == [
        ("Their mix", "importable", OTHER_NAME)
    ]
    r = await c.post(
        f"/api/v1/devices/{dev}/device-templates/import", json={"uids": [their.template_uid]}
    )
    assert r.status_code == 200, r.text
    [new_id] = r.json()["linked"]
    tpl = (await c.get(f"/api/v1/templates/{new_id}")).json()
    assert tpl["library_ids"] == ["music"] and tpl["origin_name"] == OTHER_NAME
    cfg = (await c.get(f"/api/v1/devices/{dev}")).json()
    assert new_id in cfg["config"]["rule_set"]["template_ids"]

    # the next sync writes our rows (now with the copy) and keeps theirs
    await _sync(app, c, dev)
    owners = [(r["instance_id"], r["name"]) for r in _file(mount)["templates"]]
    assert (OTHER_ID, "Their mix") in owners and (app.instance_id, "Their mix") in owners

    # their original changes: "updated", then update my copy
    newer = _other_row("Their mix v2", t0 + dt.timedelta(days=1))
    rows = [r for r in dt_io.loads(path.read_bytes()) if r.instance_id != OTHER_ID]
    path.write_bytes(dt_io.dumps([*rows, newer]))
    [row] = (await c.get(f"/api/v1/devices/{dev}/device-templates")).json()["templates"]
    assert row["state"] == "copy" and row["updated"] and row["linked"] and row["copy_id"] == new_id
    r = await c.post(f"/api/v1/devices/{dev}/device-templates/update/{new_id}")
    assert r.status_code == 200 and r.json()["name"] == "Their mix v2", r.text
    [row] = (await c.get(f"/api/v1/devices/{dev}/device-templates")).json()["templates"]
    assert not row["updated"]


async def test_import_of_unknown_uid_is_404(env, tmp_path):
    app, c = env
    mount = rockbox_tree(tmp_path / "mini")
    body = {"name": "Mini", "type": "rockbox", "mount_path": str(mount), "library_ids": ["music"]}
    dev = (await c.post("/api/v1/devices/register", json=body)).json()["id"]
    r = await c.post(f"/api/v1/devices/{dev}/device-templates/import", json={"uids": ["x:y"]})
    assert r.status_code == 404, r.text
