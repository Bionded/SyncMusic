"""Settings restructure: six groups, an explanation for every setting, the search box."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]

# a setting's explanation: the <small> under a label, a field's .hint, or a section/sub text
MISSING = """[...document.querySelectorAll('#content [data-set-row]')].filter((r) =>
  !r.matches('[data-set-matrix]') && !r.querySelector('small, .hint')).map((r) => r.textContent.trim().slice(0, 60))"""


@pytest.mark.parametrize("width", [1280, 390])
def test_groups_explanations_search(web_server, chrome, width):
    url, _ = web_server
    page = open_page(chrome, url + "/#/settings", width)
    page.wait_for_selector("#content[data-route='/settings']")
    assert page.locator("[data-set-section]").evaluate_all("els => els.map(e => e.dataset.setSection)") == [
        "sync", "libs", "storage", "notif", "system", "about"]  # fmt: skip
    assert page.evaluate(MISSING) == []
    assert page.locator("[data-set-row]").count() > 25

    search = page.locator("[data-set-search]")
    search.fill("webhook")
    visible = page.locator("[data-set-section]:not([hidden])").evaluate_all(
        "els => els.map(e => e.dataset.setSection)"
    )
    assert visible == ["notif", "storage"] or set(visible) <= {
        "notif",
        "storage",
    }  # backup "…the webhook URL"
    assert "notif" in visible
    assert page.locator("#set-webhook_url").is_visible()
    assert page.locator("#set-cache_max_gb").is_hidden()
    assert page.evaluate("CSS.highlights.get('set-hit').size") > 0
    shot(page, f"settings-search-{width}")
    no_horizontal_scroll(page)
    # typing in the search box is not an unsaved change
    assert page.locator(".savebar").is_hidden()

    search.fill("RAM")  # matches only an explanation (nc_budget_sub)
    assert page.locator("#set-default_no_cache_budget_mb").is_visible()
    assert page.locator("#set-write_limit_mb_s").is_hidden()

    search.fill("Storage")  # a section title keeps the whole section
    assert page.locator("#set-cache_max_gb").is_visible()
    assert page.locator("#set-history_days").is_visible()

    search.fill("zzzz nothing")
    assert page.locator("[data-set-none]").is_visible()
    search.fill("")
    assert page.locator("[data-set-none]").is_hidden()
    assert page.locator("[data-set-section][hidden]").count() == 0
    assert page.locator("[data-set-row][hidden]").count() == 0


def test_search_points_to_device_settings(web_server, chrome, tmp_path):
    """632.4: a device-only setting found from the app search links to each device's Settings tab, pre-searched."""
    import httpx

    url, _ = web_server
    (tmp_path / "st").mkdir()
    r = httpx.post(
        f"{url}/api/v1/devices/register",
        json={"name": "St", "mount_path": str(tmp_path / "st"), "type": "flashdrive"},
    )
    assert r.status_code < 400, r.text
    page = open_page(chrome, url + "/#/settings", 1280)
    page.wait_for_selector("#content[data-route='/settings']")
    page.locator("[data-set-search]").fill("gentle")
    link = page.locator("[data-set-perdev] a", has_text="Open the settings of St")
    link.click()
    page.wait_for_selector("[data-ds-search]")
    assert page.locator("[data-ds-search]").input_value() == "gentle"
    assert page.locator("#nameCard").is_hidden()
