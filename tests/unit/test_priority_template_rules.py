"""The priority order may list the rules of linked templates (the builder shows them there): no "unknown rule id"
warning for them."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from tests.unit.test_api_sync import _register, env, music  # noqa: F401 - fixtures

RULE = {"id": "skryabin", "name": "Скрябін", "criteria": {"all": [{"is": {"artist": "Скрябін"}}]}}


async def test_priority_with_a_template_rule_gives_no_unknown_rule_warning(env, tmp_path):
    app, c = env
    rule_set = {
        "template_ids": ["default.music.all"],
        "rules": [RULE],
        "priority": ["default.music.all", "skryabin"],
    }
    await _register(c, tmp_path / "flash", rule_set=rule_set)
    body = (await c.post("/api/v1/devices/my-flash/rules/test")).json()
    assert body["items"] == 3
    assert not [w for w in body["warnings"] if "unknown rule id" in w], body["warnings"]


async def test_validate_knows_template_rules_but_still_warns_about_unknown_ids(env):
    app, c = env
    rule_set = {
        "template_ids": ["default.music.all"],
        "rules": [RULE],
        "priority": ["default.music.all", "skryabin", "nope"],
    }
    body = (await c.post("/api/v1/rules/validate", json={"rule_set": rule_set})).json()
    assert body["ok"] and body["warnings"] == ["priority lists unknown rule id 'nope'"], body
