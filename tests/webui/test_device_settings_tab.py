"""One device Settings tab: the old Sync settings hash redirects, Sync behaviour and Device are
saved together by one Save bar, the links to Templates / Processors, and the search over the cards."""

from __future__ import annotations

import datetime as dt
import time

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.mark.parametrize("width", [1280, 390])
def test_merged_settings_tab(web_server, chrome, tmp_path, width):
    url, _ = web_server
    (tmp_path / "stick").mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"},
    )
    page = open_page(chrome, url + "/#/devices/stick/sync", width=width)
    page.wait_for_selector("[data-sync-form]")
    page.wait_for_function("location.hash === '#/devices/stick/settings'")
    assert page.locator("[data-settings-form]").count() == 1
    assert page.locator(".tabs a[aria-selected=true]").inner_text() == "Settings"
    assert page.locator(".ds-link[href='#/devices/stick/rules']").count() == 1
    assert page.locator(".ds-link[href='#/devices/stick/processors']").count() == 1
    shot(page, f"device-settings-{width}")
    assert no_horizontal_scroll(page)

    # one Save bar for both parts: a sync setting and the name in one save
    page.fill("#dsThreshold", "42")
    page.fill("#dsName", "Stick Two")
    assert page.locator(".savebar:not([hidden])").count() == 1
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Stick Two')")
    cfg = _api(url, "GET", "/devices/stick")["config"]
    assert cfg["delete_threshold_pct"] == 42 and cfg["name"] == "Stick Two"

    search = page.locator("[data-ds-search]")
    search.fill("before removing")
    assert page.locator("#dsThreshold").is_visible()
    assert page.locator("#nameCard").is_hidden()
    # the danger zone and profile card filter too
    assert page.locator("#formatZone").is_hidden()
    assert page.locator("#profileCard").count() == 0 or page.locator("#profileCard").is_hidden()
    pad = page.eval_on_selector("[data-ds-search]", "e => getComputedStyle(e).paddingLeft")
    assert pad == "12px"  # styled like the other inputs
    assert page.locator(".savebar:not([hidden])").count() == 0  # searching is no change
    shot(page, f"device-settings-search-{width}")
    search.fill("zzzz")
    assert page.locator("[data-ds-none]").is_visible()
    search.fill("")
    assert page.locator("#nameCard").is_visible()


def test_default_or_changed(web_server, chrome, tmp_path):
    """632.3: each sync setting says whether it is the app default or changed for this device; Reset to default."""
    url, _ = web_server
    (tmp_path / "s2").mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={"name": "S2", "mount_path": str(tmp_path / "s2"), "type": "flashdrive"},
    )
    page = open_page(chrome, url + "/#/devices/s2/settings", width=1280)
    page.wait_for_selector("[data-origin=dsWhen]")
    # a late first draw under -n 2 replaced the bar being clicked
    page.wait_for_load_state("networkidle")
    assert page.locator("[data-origin=dsWhen]").get_attribute("data-state") == "default"
    page.locator('input[name="dsWhen"][value="auto"]').check()
    chip = page.locator("[data-origin=dsWhen]")
    assert chip.get_attribute("data-state") == "changed"
    assert "Changed for this device · default: Never" in chip.inner_text()
    shot(page, "device-settings-origin")
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    assert _api(url, "GET", "/devices/s2")["config"]["on_connect"] == "auto"
    page.wait_for_selector(".savebar", state="hidden")
    page.wait_for_load_state("networkidle")  # the tab is drawn again after the save
    page.wait_for_selector("[data-origin=dsWhen][data-state=changed]")
    page.locator("[data-reset-default=dsWhen]").click()
    assert page.locator('input[name="dsWhen"][value="manual"]').is_checked()
    assert page.locator("[data-origin=dsWhen]").get_attribute("data-state") == "default"
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".savebar", state="hidden")
    assert _api(url, "GET", "/devices/s2")["config"]["on_connect"] == "manual"


@pytest.mark.parametrize("width", [1280, 390])
def test_sync_windows(web_server, chrome, tmp_path, width):
    """add a window (weekdays, from/to) on the device and as the default in Settings; the device
    header says when the window opens while outside it."""
    url, _ = web_server
    (tmp_path / "stick").mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"},
    )
    page = open_page(chrome, url + "/#/devices/stick/settings", width=width)
    editor = page.locator('[data-sw="dev"]')
    editor.get_by_role("button", name="Add window").click()
    row = editor.locator("[data-sw-row]")
    assert row.count() == 1
    row.locator(".sw-day", has_text="Sa").click()  # untick Saturday and Sunday
    row.locator(".sw-day", has_text="Su").click()
    soon = dt.datetime.now() + dt.timedelta(hours=2)
    start, end = soon.strftime("%H:%M"), (soon + dt.timedelta(hours=1)).strftime("%H:%M")
    row.locator("[data-sw-start]").fill(start)
    row.locator("[data-sw-end]").fill(end)
    shot(page, f"device-sync-windows-{width}")
    assert no_horizontal_scroll(page)
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast")
    cfg = _api(url, "GET", "/devices/stick")["config"]
    assert cfg["sync_windows"] == [{"start": start, "end": end, "days": [0, 1, 2, 3, 4]}]
    if soon.weekday() < 5:  # today's window is ahead: the header says when it opens
        page.reload()
        wait = page.wait_for_selector("[data-window-wait]")
        assert "Waiting for the sync window" in wait.inner_text() and start in wait.inner_text()

    page.goto(url + "/#/settings")
    defaults = page.locator('[data-sw="default"]')
    defaults.get_by_role("button", name="Add window").click()
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast")
    s = _api(url, "GET", "/settings")
    assert s["default_sync_windows"] == [
        {"start": "01:00", "end": "06:00", "days": [0, 1, 2, 3, 4, 5, 6]}
    ]
    defaults.locator("[data-sw-del]").click()
    page.locator(".savebar [data-savebar=save]").click()
    for _ in range(50):
        if _api(url, "GET", "/settings")["default_sync_windows"] == []:
            break
        time.sleep(0.1)
    assert _api(url, "GET", "/settings")["default_sync_windows"] == []
    assert not [e for e in page.errors if "Failed to load resource" not in e]
