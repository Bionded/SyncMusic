"""Activity page: what a sync did, across devices, with filters; the same action log in the
sync report and the device History."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_device_overview import _api, _synced_stick

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_activity_page_report_and_history(web_server, chrome, tmp_path, width):
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    acts = _api(url, "GET", "/activity")
    assert acts and acts[-1]["kind"] == "files" and acts[-1]["counts"]["add"] == 3
    page = open_page(chrome, url + "/#/activity", width)
    rows = page.locator("[data-activity] .act-row")
    rows.first.wait_for()
    assert ": added 3 files" in page.locator("[data-activity]").inner_text()
    assert (
        page.locator('.side a[href="#/activity"], nav a[href="#/activity"]').count() >= 1
        or width == 390
    )
    shot(page, f"activity-{width}")
    assert no_horizontal_scroll(page)

    page.locator('[data-act-show="playlists"]').click()
    page.wait_for_function("location.hash.includes('show=playlists')")
    # the filtered list replaces the old one asynchronously: wait for it, not for the old rows
    page.wait_for_function(
        "[...document.querySelectorAll('[data-activity] .act-row')]"
        ".every(x => x.dataset.act.startsWith('playlist'))"
    )

    # the report it opens shows the same list
    page.goto(url + "/#/activity")
    page.locator("[data-activity] .act-row a").last.click()
    page.wait_for_selector("[data-report-activity]")
    assert "added 3 files" in page.locator("[data-report-activity]").inner_text()

    page.goto(f"{url}/#/devices/{dev}/history")
    page.wait_for_selector("[data-hist-activity]")
    assert "added 3 files" in page.locator("[data-hist-activity]").first.inner_text()
