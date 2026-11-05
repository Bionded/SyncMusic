"""Transcode to fit: the offer when a plan does not fit (MP3 320 first), estimates per target that change
with the bitrate, files at or below the target copied as they are, accepting sets the device's transcode rule."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import pytest

from syncmusic.core.fit import TARGETS, with_fit_rule
from syncmusic.core.models import ProcessorSlot
from syncmusic.devices._filedevice import FileDeviceMixin
from tests.media import make_music
from tests.unit.test_api_sync import _apply, _register, env  # noqa: F401 - fixtures

MB = 1_000_000


@pytest.fixture
def music(tmp_path):
    """Two lossless tracks and one 192k MP3."""
    root = tmp_path / "music"
    make_music(root / "A" / "01.flac", seconds=20, title="Lossless 1", artist="A", album="X")
    make_music(root / "A" / "02.flac", seconds=20, title="Lossless 2", artist="A", album="X")
    make_music(root / "B" / "low.mp3", seconds=20, title="Low", artist="B", album="Y")
    return root


def test_with_fit_rule():
    slots = [
        ProcessorSlot(type="retag"),
        ProcessorSlot(
            type="transcode", enabled=False, settings={"rules": [{"from": ["flac"], "to": "alac"}]}
        ),
    ]
    out = with_fit_rule(slots, "mp3", 320)
    tr = out[1]
    assert tr.enabled and [r.get("above_target_only", False) for r in tr.settings["rules"]] == [
        True,
        False,
    ]
    assert tr.settings["rules"][0]["quality"] == {"bitrate_kbps": 320}
    again = with_fit_rule(out, "aac", 128)[1].settings["rules"]
    assert len(again) == 2 and again[0]["to"] == "aac"  # replaced, not stacked
    assert slots[1].enabled is False  # the input is not changed
    added = with_fit_rule([ProcessorSlot(type="retag")], "mp3", 256)
    assert [s.type for s in added] == ["transcode", "retag"]
    with pytest.raises(ValueError, match="offered"):
        with_fit_rule([], "mp3", 999)
    assert TARGETS[0] == ("mp3", 320)


async def test_offer_estimate_and_accept(env, tmp_path, monkeypatch):
    app, c = env
    space = {"free": 10**12}

    async def free_space(self):
        return space["free"]

    async def capacity(self):
        return 10**12

    monkeypatch.setattr(FileDeviceMixin, "free_space", free_space)
    monkeypatch.setattr(FileDeviceMixin, "capacity", capacity)
    dev = await _register(c, tmp_path / "stick", name="Stick", processors=[])
    full = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert full["fit_targets"] == []  # it fits: nothing offered
    sizes = {a["title"]: a["size_delta"] for a in full["actions"]}
    space["free"] = full["space"]["needed"] - sizes["Lossless 1"] // 2  # a little too small
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["does_not_fit"]
    assert plan["fit_targets"][0] == {"to": "mp3", "bitrate_kbps": 320}
    assert [(t["to"], t["bitrate_kbps"]) for t in plan["fit_targets"]] == TARGETS

    est = {}
    for to, k in TARGETS:
        r = await c.post(
            f"/api/v1/devices/{dev['id']}/fit/estimate", json={"to": to, "bitrate_kbps": k}
        )
        assert r.status_code == 200, r.text
        est[(to, k)] = r.json()
        # the two FLACs; the 192k MP3 only for a target below 192k
        assert est[(to, k)]["converted"] == (3 if k < 192 else 2)
        if k >= 192:  # duration x bitrate: 20 s at k kbit/s, plus a little container overhead
            lossless = est[(to, k)]["on_device"] - sizes["Low"]
            assert 2 * 20 * k * 1000 / 8 <= lossless <= 2 * 20 * k * 1000 / 8 * 1.1
    assert est[("mp3", 320)]["needed"] > est[("mp3", 256)]["needed"] > est[("mp3", 192)]["needed"]
    assert est[("mp3", 320)]["fits"] and est[("mp3", 320)]["needed"] < plan["space"]["needed"]
    # nothing stored by an estimate
    procs = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]["processors"]
    assert [p["type"] for p in procs] == [
        "transcode",
        "retag",
    ]  # the steps every chain has
    r = await c.post(
        f"/api/v1/devices/{dev['id']}/fit/estimate", json={"to": "mp3", "bitrate_kbps": 100}
    )
    assert r.status_code == 400

    r = await c.post(f"/api/v1/devices/{dev['id']}/fit", json={"to": "mp3", "bitrate_kbps": 320})
    assert r.status_code == 200, r.text
    new = r.json()
    assert not new["does_not_fit"] and new["fit_targets"] == []
    procs = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]["processors"]
    assert procs[0]["type"] == "transcode" and procs[0]["settings"]["rules"][0]["above_target_only"]
    report = await _apply(app, c, new["id"], first_sync="keep")
    assert report["status"] == "succeeded"
    files = sorted(p.name for p in (tmp_path / "stick").rglob("*.mp3"))
    assert len(files) == 3 and not list((tmp_path / "stick").rglob("*.flac"))
