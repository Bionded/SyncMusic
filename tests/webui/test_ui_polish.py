"""UI polish: the Devices list says the type in words, an unnamed rule is listed by its place (never by
its generated id) in the device's priority list."""

from __future__ import annotations

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=30, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.mark.parametrize("width", [1280, 390])
def test_type_in_words_and_unnamed_rules_by_place(web_server, chrome, tmp_path, width):
    url, _ = web_server
    (tmp_path / "music").mkdir()
    lib = {
        "id": "m",
        "type": "folder",
        "name": "Музика",
        "settings": {"root": str(tmp_path / "music")},
    }
    _api(url, "POST", "/libraries", json=lib)
    rules = [
        {"id": "rule-mvkas7mj2", "criteria": {"all": []}},
        {"id": "named", "name": "Усе з бібліотеки", "criteria": {"all": []}},
    ]
    tpl = _api(url, "POST", "/templates", json={"name": "Т", "library_ids": ["m"], "rules": rules})
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Флешка",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "rule_set": {"template_ids": [tpl["id"]]},
    }
    dev = _api(url, "POST", "/devices/register", json=body)["id"]

    page = open_page(chrome, url + "/#/devices", width)
    label = page.locator("[data-device-row] [data-type-label]").first
    label.wait_for()
    assert label.inner_text().startswith("USB flash drive")
    page.goto(f"{url}/#/devices/{dev}/rules")
    # the Templates tab lists templates, never a rule's generated id
    page.wait_for_selector(f'[data-tpl-row="{tpl["id"]}"]')
    assert "rule-mvkas7mj2" not in page.inner_text("#content")
    rs = _api(url, "GET", f"/devices/{dev}")["config"]["rule_set"]
    assert rs["template_ids"] == [tpl["id"]]
    assert no_horizontal_scroll(page)
    shot(page, f"polish-351-rules-{width}")
    assert page.errors == []


def test_a_tab_strip_wider_than_the_phone_shows_the_active_tab_and_fades(
    web_server, chrome, tmp_path
):
    """at 390 px the device tabs scroll sideways; the active one is whole on screen and the strip fades at
    the side where more tabs are."""
    url, _ = web_server
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    dev = _api(url, "POST", "/devices/register", json=body)["id"]
    page = open_page(chrome, f"{url}/#/devices/{dev}/history", width=390, height=844)
    page.wait_for_selector('.dev-tabs [aria-selected="true"]')
    strip, tab = page.locator(".dev-tabs"), page.locator('.dev-tabs [aria-selected="true"]')
    s, a = strip.bounding_box(), tab.bounding_box()
    assert s["x"] <= a["x"] and a["x"] + a["width"] <= s["x"] + s["width"] + 1
    assert strip.get_attribute("data-more") in ("left", "both")
    assert no_horizontal_scroll(page)
    assert page.errors == []
