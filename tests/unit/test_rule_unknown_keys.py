"""Unknown keys in JSON rules must be refused, not dropped."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import pytest

from tests.unit.test_api_sync import _register, env, music  # noqa: F401 - fixtures

RULE = {"id": "skryabin", "name": "Скрябін", "criteria": {"all": [{"is": {"artist": "Скрябін"}}]}}


@pytest.mark.parametrize(
    "req",
    [
        {"rule": {**RULE, "limt": 5}},  # typo of limit
        {
            "rule": {"id": "x", "all": [{"is": {"artist": "Скрябін"}}]}
        },  # nsp criteria at the top level
        {"rule_set": {"rules": [RULE], "exclude": [{"id": "e"}]}},  # typo of excludes
    ],
)
async def test_validate_refuses_unknown_rule_keys(env, req):
    app, c = env
    body = (await c.post("/api/v1/rules/validate", json=req)).json()
    assert not body["ok"] and body["errors"]


async def test_device_patch_refuses_misspelt_excludes(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash")
    r = await c.patch(
        "/api/v1/devices/my-flash",
        json={"rule_set": {"rules": [RULE], "exlcudes": [{"id": "e", "criteria": {}}]}},
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid"


async def test_validate_names_the_unknown_key(env):
    app, c = env
    body = (
        await c.post("/api/v1/rules/validate", json={"rule_set": {"rules": [{**RULE, "limt": 5}]}})
    ).json()
    assert "rules.0.limt" in body["errors"][0]


async def test_template_refuses_unknown_rule_keys(env):
    app, c = env
    r = await c.post("/api/v1/templates", json={"name": "t", "rules": [{**RULE, "limt": 5}]})
    assert r.status_code == 400 and r.json()["error"] == "invalid"


async def test_text_rules_refuse_nsp_criteria_at_rule_level(env):
    app, c = env
    text = '{"rules":[{"id":"short","kind":["music"],"all":[{"lt":{"duration":200}}]}]}'
    body = (await c.post("/api/v1/rules/validate", json={"text": text})).json()
    assert not body["ok"] and "all" in body["errors"][0]
