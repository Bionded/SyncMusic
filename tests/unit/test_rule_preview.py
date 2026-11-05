"""Live rule preview: the numbers of an unsaved rule set are those of the real plan once saved."""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

import os
import shutil

from tests.unit.test_api_sync import _apply, _as_template, _register, env, music  # noqa: F401

KINO = {
    "rules": [
        {"id": "skryabin", "name": "Скрябін", "criteria": {"all": [{"is": {"artist": "Скрябін"}}]}}
    ],
}


async def _preview(c, rs, device="my-flash"):
    r = await c.post(f"/api/v1/devices/{device}/rules/preview", json={"rule_set": rs})
    assert r.status_code == 200, r.text
    return r.json()


async def _plan(c, device="my-flash"):
    r = await c.post(f"/api/v1/devices/{device}/plan", json={})
    assert r.status_code == 200, r.text
    return r.json()


def _on_device_bytes(plan):
    return sum(
        (a["item"]["file"]["size"] + a["item"]["sidecar_bytes"])
        if a["item"]
        else (a["entry"]["size"] if a["entry"] else 0)
        for a in plan["actions"]
        if a["kind"] != "remove" and not a["orphan"]
    )


def _frozen_statvfs(monkeypatch, root):
    """The device folder lives on the shared /tmp tmpfs: another process writing there between the preview and the
    plan changes the real free space. The first reading under `root` is kept."""
    real, seen = os.statvfs, {}

    def statvfs(path):
        if not str(path).startswith(str(root)):
            return real(path)
        if "st" not in seen:
            seen["st"] = real(path)
        return seen["st"]

    monkeypatch.setattr(os, "statvfs", statvfs)


async def test_preview_equals_the_plan_after_saving(env, tmp_path, monkeypatch):
    app, c = env
    _frozen_statvfs(monkeypatch, tmp_path / "flash")
    await _register(c, tmp_path / "flash")
    plan = await _plan(c)
    await _apply(app, c, plan["id"], first_sync="keep")  # all 3 songs on the device

    draft = {**await _as_template(c, "Skryabin", KINO, ["music"]), "limit_tracks": 1}
    pv = await _preview(c, draft)
    saved = (await c.get("/api/v1/devices/my-flash")).json()["config"]["rule_set"]
    assert saved["template_ids"] == ["my-flash-rules"]  # the preview stored nothing
    r = await c.patch("/api/v1/devices/my-flash", json={"rule_set": draft})
    assert r.status_code == 200, r.text
    plan = await _plan(c)
    assert pv["connected"] is True
    assert pv["counts"] == plan["counts"]
    assert pv["counts"]["remove"] == 2 and pv["items"] == 1 and pv["left_out"] == 1
    # what the rule loses to the limit, and the bytes by kind (all music here)
    ((lost,),) = [pv["left_out_per_rule"]]
    assert (
        lost["items"] == 1
        and lost["bytes"] > 0
        and lost["rule_id"] in {r["rule_id"] for r in pv["per_rule"]}
    )
    assert pv["by_kind"] == {"music": pv["bytes"]}
    assert pv["bytes"] == _on_device_bytes(plan)
    assert (pv["needed"], pv["free"], pv["capacity"]) == (
        plan["space"]["needed"],
        plan["space"]["free"],
        plan["space"]["capacity"],
    )
    assert pv["fits"] is True and pv["free_after"] == pv["free"] - pv["needed"]
    assert len(pv["sample"]) == 1 and pv["sample"][0]["artist"] == "Скрябін"
    assert pv["sample"][0]["action"] == "keep"
    assert any("left out by the device limit" in w for w in pv["warnings"])


async def test_preview_without_the_device_uses_the_library(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash")
    shutil.rmtree(tmp_path / "flash")  # unplugged
    pv = await _preview(c, KINO)
    assert pv["connected"] is False and "not available" in pv["reason"]
    assert pv["items"] == 2 and pv["bytes"] > 0 and "fits" not in pv


async def test_invalid_rules_are_refused(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash")
    bad = {"rules": [{"id": "x", "criteria": {"nonsense": {"artist": "x"}}}]}
    r = await c.post("/api/v1/devices/my-flash/rules/preview", json={"rule_set": bad})
    assert r.status_code == 400, r.text
