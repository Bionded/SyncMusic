"""Device page with a damaged file system: a banner with Check and repair and Wipe…, the sidebar
card and the dashboard say what is wrong."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_plan_flow import device  # noqa: F401 - fixture

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_damaged_file_system_banner(device, chrome, width):  # noqa: F811
    url, app, _stick = device
    app.sync._damaged["stick"] = "Input/output error"
    page = open_page(chrome, url + "/#/devices/stick", width)
    banner = page.locator("[data-problem]", has=page.locator("[data-fs-damaged]"))
    banner.wait_for()
    text = banner.inner_text()
    assert "File system damaged" in text and "Input/output error" in text
    assert banner.get_by_role("button", name="Check and repair").is_enabled()
    assert (
        banner.get_by_role("link", name="Wipe…").get_attribute("href") == "#/devices/stick/settings"
    )
    # no Update now: it would be refused while damaged
    assert page.locator('[data-update-status] [data-action="updateNow"]').count() == 0
    shot(page, f"fs-damaged-{width}")
    assert no_horizontal_scroll(page)
    if width == 1280:
        card = page.locator('[data-device-card="stick"] [data-card-error]')
        assert "file system damaged" in card.inner_text()
        # the click reaches the API: a folder device has no partition to repair -> error toast, button back
        button = banner.get_by_role("button", name="Check and repair")
        button.click()
        page.wait_for_selector(".toast:has-text('is not a disk: nothing to repair')")
        assert button.is_enabled()
        page.errors.clear()  # the refused POST is logged by the browser
    page.goto(url + "/#/")
    page.locator('[data-need="error"][data-device-row="stick"]').wait_for()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
