"""Chains that make no sense: each rule warns once with a fix that makes the warning go away;
the chains SyncMusic suggests (every device type, every quality profile) raise none."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import pytest

from syncmusic.core import quality, registry
from syncmusic.core.chain_lint import lint
from syncmusic.core.models import ProcessorSlot
from syncmusic.core.sync import suggested_processors
from tests.unit.test_api_sync import _register, env, music  # noqa: F401

TYPES = ["ipod", "rockbox", "flashdrive", "ftp"]


def _caps(device_type):
    return registry.device_class(device_type).capabilities


def _slots(*specs):
    return [ProcessorSlot(type=t, settings=s, enabled=e) for t, s, e in specs]


@pytest.mark.parametrize("device_type", TYPES)
def test_suggested_chains_and_profiles_are_clean(device_type):
    caps = _caps(device_type)
    suggested = suggested_processors(caps)
    assert lint(suggested, caps) == []
    for p in quality.PROFILES:
        assert lint(quality.chain(p, caps, suggested), caps) == [], p


def _one(slots, caps, id_):
    found = [w for w in lint(slots, caps) if w["id"] == id_]
    assert len(found) == 1, lint(slots, caps)
    fixed = [ProcessorSlot.model_validate(p) for p in found[0]["processors"]]
    assert not [w for w in lint(fixed, caps) if w["id"] == id_]  # the fix fixes it
    return found[0], fixed


def test_order_duplicate_and_missing_tags():
    caps = _caps("flashdrive")
    conv = ("transcode", {"rules": [{"from": ["flac"], "to": "mp3"}]}, True)
    tags = ("retag", {}, True)
    w, fixed = _one(_slots(tags, conv), caps, "order")
    assert w["step"] == "retag" and [s.type for s in fixed] == ["transcode", "retag"]
    w, fixed = _one(_slots(conv, tags, tags), caps, "duplicate")
    assert [s.type for s in fixed] == ["transcode", "retag"]
    w, fixed = _one(_slots(conv, ("retag", {}, False)), caps, "no_tags")
    assert w["fix"] == "Switch it on" and fixed[1].enabled
    w, fixed = _one(_slots(conv), caps, "no_tags")
    assert w["fix"] == "Add it" and [s.type for s in fixed] == ["transcode", "retag"]
    # a step switched off does not count for the order
    assert not [
        x for x in lint(_slots(("covers", {}, False), conv, tags), caps) if x["id"] == "order"
    ]


def test_unplayable_target_and_lossy_to_lossy():
    caps = _caps("ipod")  # no Opus, no Ogg
    tags = ("retag", {}, True)
    w, fixed = _one(
        _slots(("transcode", {"rules": [{"from": ["flac"], "to": "opus"}]}, True), tags),
        caps,
        "unplayable",
    )
    assert "OPUS" in w["message"] and fixed[0].settings["rules"][0]["to"] == "aac"
    rule = {"from": ["mp3", "flac"], "to": "aac", "when": "always"}
    w, fixed = _one(_slots(("transcode", {"rules": [rule]}, True), tags), caps, "lossy_lossy")
    assert (
        "MP3 into AAC" in w["message"] and fixed[0].settings["rules"][0]["when"] == "not_supported"
    )
    # above the target only (shrink to fit) is no lossy → lossy re-encode of everything
    ok = {**rule, "above_target_only": True}
    assert lint(_slots(("transcode", {"rules": [ok]}, True), tags), caps) == []


def test_embedded_lyrics_on_rockbox():
    caps = _caps("rockbox")
    slots = _slots(("retag", {}, True), ("lyrics", {"synced": "embed", "plain": "lrc"}, True))
    w, fixed = _one(slots, caps, "lyrics_embedded")
    assert fixed[1].settings == {"synced": "lrc", "plain": "lrc"}
    assert lint(slots, _caps("ipod")) == []  # the iPod shows embedded lyrics


async def test_api(env, tmp_path):
    _app, c = env
    dev = await _register(c, tmp_path / "flash", name="Flash")
    r = await c.post(
        f"/api/v1/devices/{dev['id']}/chain-lint",
        json={"processors": [{"type": "transcode", "settings": {"rules": [{"to": "mp3"}]}}]},
    )
    assert r.status_code == 200, r.text
    assert [w["id"] for w in r.json()] == ["no_tags"]
    r = await c.post(f"/api/v1/devices/{dev['id']}/chain-lint", json={"processors": [{"type": 1}]})
    assert r.status_code == 400


def test_any_format_rule_into_lossy_is_warned_but_not_the_weak_profile():
    """a rule without "from" converts every format, MP3 and AAC too; the "For a weak player" profile's own
    rule (everything → MP3 192k) is a deliberate choice and stays clean."""
    caps = _caps("rockbox")
    tags = ("retag", {}, True)
    rule = {"from": [], "to": "opus", "when": "always", "quality": {"bitrate_kbps": 96}}
    w, fixed = _one(_slots(("transcode", {"rules": [rule]}, True), tags), caps, "lossy_lossy")
    assert "into OPUS" in w["message"] and "MP3" in w["message"]
    weak = quality.chain("weak", caps, suggested_processors(caps))
    assert lint(weak, caps) == []
    # the Processors page posts the rule with the form's defaults filled in: still the profile's own rule (468)
    for s in weak:
        if s.type == "transcode":
            s.settings["rules"] = [
                {
                    "from": [],
                    "above_target_only": False,
                    **r,
                    "quality": {"vbr": None, "bitrate_kbps": None, **r.get("quality", {})},
                }
                for r in s.settings["rules"]
            ]
            assert s.settings["rules"][0]["quality"]["vbr"] is None
    assert lint(weak, caps) == []
