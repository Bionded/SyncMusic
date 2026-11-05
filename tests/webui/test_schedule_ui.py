"""Daily sync time in Device settings: a time field next to "When connected", saved and cleared."""

from __future__ import annotations

import os

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.mark.parametrize("width", [1280, 390])
def test_daily_sync_time(web_server, chrome, tmp_path, width):
    url, _ = web_server
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    _api(url, "POST", "/devices/register", json=body)
    page = open_page(chrome, url + "/#/devices/stick/sync", width=width)
    field = page.locator('input[type="time"][id$="sync_at"]')
    field.wait_for()
    # the design: a switch "every night" with the time beside it
    assert field.is_disabled() and not page.locator("#dsNightly").is_checked()
    page.locator("#dsNightly").check()
    field.fill("03:30")
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    assert _api(url, "GET", "/devices/stick")["config"]["sync_at"] == "03:30"
    if folder := os.environ.get("SYNCMUSIC_SHOTS"):
        page.locator("[data-sync-form] .ds-card").nth(2).screenshot(
            path=os.path.join(folder, f"sync-at-{width}.png")
        )
    assert no_horizontal_scroll(page)
    page.reload()
    field = page.locator('input[type="time"][id$="sync_at"]')
    field.wait_for()
    assert field.input_value() == "03:30" and page.locator("#dsNightly").is_checked()
    page.locator("#dsNightly").uncheck()
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    assert _api(url, "GET", "/devices/stick")["config"]["sync_at"] is None
    assert page.errors == []
