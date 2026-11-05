"""after Save in the rule template editor (create and edit) the Templates list comes back with a toast
and the saved template highlighted and in view; the processor template editor does the same
(test_processor_templates_ui.py). 1280 and 390 px."""

from __future__ import annotations

import httpx
import pytest

from tests.webui.conftest import open_page

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_save_returns_to_the_list(web_server, chrome, width):
    url, _ = web_server
    for i in range(12):  # a long list: the saved one must be scrolled into view
        r = httpx.post(
            f"{url}/api/v1/templates",
            json={"name": f"A{i:02d}", "rules": [{"id": f"r{i}", "criteria": {"all": []}}]},
            timeout=30,
        )
        assert r.status_code == 201, r.text
    page = open_page(chrome, url + "/#/templates/new", width=width, height=700)
    page.wait_for_selector("#tplName")
    page.fill("#tplName", "Zz останній")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".toast:has-text('Template Zz останній created')")
    row = page.locator(".row.highlight")
    row.wait_for()
    assert page.evaluate("location.hash").startswith("#/templates?saved=")
    assert "Zz останній" in row.inner_text() and row.is_visible()
    box = row.bounding_box()
    assert 0 <= box["y"] < 700
    tid = row.get_attribute("data-tpl")
    # edit: the same
    page.goto(url + f"/#/templates/{tid}")
    page.wait_for_selector("#tplName")
    page.fill("#tplName", "Zz перейменовано")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".toast:has-text('Template saved.')")
    page.wait_for_selector(f'.row.highlight[data-tpl="{tid}"]:has-text("Zz перейменовано")')
    assert page.locator(".row.highlight").count() == 1
    assert page.errors == []
