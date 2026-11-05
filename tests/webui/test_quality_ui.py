"""Processors tab → Quality: a profile replaces the chain; editing it makes it Custom."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_plan_flow import _api, device  # noqa: F401 - fixture

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_quality_profiles(device, chrome, width):  # noqa: F811
    url, _app, _stick = device
    page = open_page(chrome, url + "/#/devices/stick/processors", width)
    page.wait_for_selector("[data-quality]")
    assert page.locator("input[name=procQuality]:checked").get_attribute("value") == "custom"
    page.locator("label.option", has_text="Save space").click()
    page.wait_for_selector(".toast:has-text('Save space: the chain below is replaced.')")
    page.wait_for_function(
        "document.querySelector('input[name=procQuality]:checked')?.value === 'space'"
    )
    cfg = _api(url, "GET", "/devices/stick")["config"]
    assert cfg["quality_profile"] == "space" and cfg["processors"][0]["type"] == "transcode"
    assert cfg["processors"][0]["settings"]["rules"][0]["to"] == "mp3"
    page.wait_for_function(
        "document.querySelector('[data-proc] .proc-what .name')?.textContent === 'Convert audio'"
    )
    shot(page, f"quality-{width}")
    assert no_horizontal_scroll(page)
    # a hand edit through the API makes it Custom
    _api(url, "PATCH", "/devices/stick", json={"processors": cfg["processors"][1:]})
    page.reload()
    page.wait_for_selector("[data-quality]")
    assert page.locator("input[name=procQuality]:checked").get_attribute("value") == "custom"
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
