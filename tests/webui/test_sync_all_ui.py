"""'Sync all connected': the dashboard and sidebar buttons, the full / update choice and the toast
that names skipped devices with their reason."""

from __future__ import annotations

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _register(url, path, name):
    path.mkdir()
    r = httpx.post(
        f"{url}/api/v1/devices/register",
        json={"name": name, "mount_path": str(path), "type": "flashdrive"},
        timeout=60,
    )
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("width", [1280, 390])
def test_sync_all_buttons(web_server, chrome, tmp_path, width):
    url, _ = web_server
    _register(url, tmp_path / "one", "Stick One")
    _register(url, tmp_path / "two", "Stick Two")
    page = open_page(chrome, url + "/#/", width=width)
    btn = page.locator("[data-action=syncAll]")
    btn.wait_for()
    if width == 1280:
        assert page.locator("[data-sync-all]").count() == 1  # the sidebar header too
    assert no_horizontal_scroll(page)
    btn.click()
    page.wait_for_selector(".modal:has-text('Sync all connected devices')")
    shot(page, f"sync-all-dialog-{width}")
    page.locator(".modalfoot button:has-text('Full sync')").click()
    # no template pulls from a library: both are skipped, and the toast says why
    toast = page.locator(".toast:has-text('Skipped')")
    toast.wait_for()
    assert "Stick One (no library)" in toast.inner_text()
    shot(page, f"sync-all-toast-{width}")
    assert (
        page.locator(".toast:has-text('Sync all finished')").count() == 0
    )  # nothing started: no summary
