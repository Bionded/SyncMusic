"""Copy of a template opens an unsaved draft - leaving it creates nothing; Save creates exactly one new
template whose id comes from its final name (never the original's), with new rule ids; the original is unchanged.
Rule templates and processor templates."""

from __future__ import annotations

import httpx
import pytest

from tests.webui.conftest import open_page

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def test_rule_template_copy_is_a_draft(web_server, chrome):
    url, _ = web_server
    original = _api(
        url,
        "POST",
        "/templates",
        json={
            "name": "Скрябін",
            "rules": [
                {
                    "id": "skryabin",
                    "name": "Скрябін",
                    "criteria": {"all": [{"contains": {"artist": "Скрябін"}}]},
                }
            ],
        },
    )
    before = _api(url, "GET", "/templates")
    page = open_page(chrome, url + "/#/templates")
    page.locator(f'[data-tpl="{original["id"]}"]').get_by_role("button", name="Copy").click()
    page.wait_for_selector("#tplName")
    assert page.locator("#tplName").input_value() == "Copy of Скрябін"
    # leave without saving: nothing new
    page.locator("[data-tpl-back]").click()
    page.wait_for_selector(f'[data-tpl="{original["id"]}"]')
    assert _api(url, "GET", "/templates") == before
    # copy again, rename, save
    page.locator(f'[data-tpl="{original["id"]}"]').get_by_role("button", name="Copy").click()
    page.wait_for_selector("#tplName")
    page.fill("#tplName", "Скрябін 2")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    after = _api(url, "GET", "/templates")
    new = [x for x in after if x["id"] not in {b["id"] for b in before}]
    assert (
        len(new) == 1
        and new[0]["name"] == "Скрябін 2"
        and new[0]["id"] not in (original["id"], "copy-of-скрябін")
    )
    assert (
        new[0]["rules"][0]["id"] != "skryabin"
        and new[0]["rules"][0]["criteria"] == original["rules"][0]["criteria"]
    )
    assert _api(url, "GET", f"/templates/{original['id']}") == original
    # "New template" after a copy is blank again
    page.get_by_role("link", name="New template").click()
    page.wait_for_selector("#tplName")
    assert page.locator("#tplName").input_value() == ""
    assert page.errors == []


def test_processor_template_copy_is_a_draft(web_server, chrome):
    url, _ = web_server
    before = _api(url, "GET", "/processor-templates")
    page = open_page(chrome, url + "/#/processor-templates/builtin-ipod")
    page.get_by_role("button", name="Copy to edit").click()
    page.wait_for_selector("[data-ptpl-draft]")
    assert page.locator("#ptplName").input_value() == "Copy of iPod: ALAC/AAC"
    page.goto(url + "/#/")
    page.wait_for_selector("#content[data-route='/']")
    assert _api(url, "GET", "/processor-templates") == before
    assert _api(url, "GET", "/processor-templates/builtin-ipod") == next(
        x for x in before if x["id"] == "builtin-ipod"
    )
    assert page.errors == []
