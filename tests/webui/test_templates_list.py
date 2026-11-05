"""Templates list: groups per library with counts, badges Default / Default · edited / Own,
usage "On N devices" / "Not used", the filter field and All / Defaults / Own, Reset to default for an edited
default, and deleting a template in use only after its name is typed."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_device_overview import _api, _synced_stick

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_templates_list_v2(web_server, chrome, tmp_path, width):
    url, app = web_server
    _synced_stick(url, app, tmp_path)  # links default.music.all
    tpl = _api(url, "GET", "/templates/default.music.all")
    body = {k: tpl[k] for k in ("library_ids", "rules", "excludes")}
    _api(url, "PATCH", "/templates/default.music.all", json={**body, "name": "Уся музика (моя)"})
    _api(url, "POST", "/templates", json={**body, "id": "mine", "name": "Мій вибір"})
    page = open_page(chrome, f"{url}/#/templates", width)
    group = page.locator('[data-tpl-group="music"]')
    group.wait_for()
    assert (
        group.locator("[data-group-count]").inner_text()
        == f"{group.locator('[data-tpl]').count()} templates"
    )
    edited = page.locator('[data-tpl="default.music.all"]')
    assert "Default · edited" in edited.inner_text() and "On 1 device" in edited.inner_text()
    mine = page.locator('[data-tpl="mine"]')
    assert "Own" in mine.inner_text() and "Not used" in mine.inner_text()
    assert mine.get_by_role("button", name="Reset to default").count() == 0
    assert no_horizontal_scroll(page)
    shot(page, f"templates-list-{width}")
    # the filter hides rows by name; nothing left says so
    page.fill("[data-tpl-filter]", "мій")
    assert mine.is_visible() and edited.is_hidden()
    page.fill("[data-tpl-filter]", "zzz")
    page.wait_for_selector("[data-tpl-nomatch]:visible")
    page.fill("[data-tpl-filter]", "")
    # Own / Defaults
    page.locator('[data-tpl-show="own"]').click()
    page.wait_for_selector('[data-tpl-show="own"][aria-checked="true"]')
    assert page.locator('[data-tpl="default.music.all"]').count() == 0
    page.locator('[data-tpl-show="defaults"]').click()
    page.wait_for_selector('[data-tpl-show="defaults"][aria-checked="true"]')
    assert page.locator('[data-tpl="mine"]').count() == 0
    # Reset to default
    page.locator('[data-tpl="default.music.all"]').get_by_role(
        "button", name="Reset to default"
    ).click()
    page.locator(".modal").get_by_role("button", name="Reset").click()
    page.wait_for_selector('.row.highlight[data-tpl="default.music.all"]')
    assert _api(url, "GET", "/templates/default.music.all")["edited"] is False
    assert "Default · edited" not in page.locator('[data-tpl="default.music.all"]').inner_text()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_deleting_a_template_in_use_needs_its_name(web_server, chrome, tmp_path):
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, f"{url}/#/templates", 1280)
    row = page.locator('[data-tpl="default.music.all"]')
    row.wait_for()
    row.get_by_role("button", name="Delete").click()
    modal = page.locator(".modal")
    modal.wait_for()
    ok = modal.get_by_role("button", name="Unlink and delete")
    assert ok.get_attribute("data-ready") == "false"
    page.fill("#typedName", "All music")
    ok.click()
    page.wait_for_selector(".toast:has-text('deleted')")
    assert _api(url, "GET", f"/devices/{dev}")["config"]["rule_set"]["template_ids"] == []
