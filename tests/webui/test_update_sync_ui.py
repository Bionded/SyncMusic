"""Automatic update syncs in the UI: Device settings has "Update automatically", "Check for
changes every (minutes)" and "Remove in automatic updates" above "Full sync when connected"; the Overview says when
the device was last updated and when the next check is due, or that automatic updates are off."""

from __future__ import annotations

import os
import re

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _shot(page, name):
    if folder := os.environ.get("SYNCMUSIC_SHOTS"):
        page.screenshot(path=os.path.join(folder, name), full_page=True)


@pytest.mark.parametrize("width", [1280, 390])
def test_update_sync_settings_and_status(web_server, chrome, tmp_path, width):
    url, _ = web_server
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    cfg = _api(url, "POST", "/devices/register", json=body)
    assert cfg["on_connect"] == "manual" and cfg["update_sync"]

    page = open_page(chrome, url + "/#/devices/stick", width=width)
    status = page.locator("[data-update-status] [data-update-line]")
    status.wait_for()
    assert status.inner_text() == "Not updated automatically yet"
    _shot(page, f"update-overview-{width}.png")

    page.goto(url + "/#/devices/stick/sync")
    form = page.locator("[data-sync-form]")
    form.wait_for()
    text = form.inner_text()
    for label in (
        "Automatic updates",
        "When connected, keep it updated automatically",
        "Check the libraries for changes every",
        "Updates may remove items that no longer match",
        "Full sync when connected",
    ):
        assert label in text
    assert text.index("Automatic updates") < text.index("Full sync when connected")
    on = page.locator("#dsUpd")
    assert (
        on.is_checked() and page.locator('input[name="dsEvery"][value="60"]').is_checked()
    )  # Default
    assert page.locator('input[name="dsWhen"][value="manual"]').is_checked()  # "Never"
    _shot(page, f"update-settings-{width}.png")
    assert no_horizontal_scroll(page)

    page.locator('input[name="dsEvery"][value="30"]').check()
    on.uncheck()
    # updates off dims the interval, the removal switch and the played-removal switch
    assert page.locator("[data-needs-upd][data-off]").count() == 3
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    page.locator(".toast").last.wait_for(state="detached")  # it covers the Save bar for a moment
    cfg = _api(url, "GET", "/devices/stick")["config"]
    assert (cfg["update_sync"], cfg["update_every_min"]) == (False, 30)
    page.wait_for_selector("#dsUpd:not(:checked)")
    on.check()
    page.locator('input[name="dsEvery"][value="off"]').check()
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    page.locator(".toast").last.wait_for(state="detached")  # it covers the Save bar for a moment
    cfg = _api(url, "GET", "/devices/stick")["config"]
    assert (cfg["update_sync"], cfg["update_every_min"]) == (True, None)
    on.uncheck()
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    page.locator(".toast").last.wait_for(state="detached")  # it covers the Save bar for a moment

    # no-cache mode: its limits show only while the switch is on
    card = page.locator("[data-nocache]")
    assert page.locator("[data-nocache-fields]").is_hidden()
    card.locator("#dsNoCache").check()
    assert card.get_by_label("Files held at once").input_value() == "4"
    assert card.get_by_label("Largest file held in memory (MB)").input_value() == "256"
    card.get_by_label("Files held at once").fill("2")
    _shot(page, f"nocache-settings-{width}.png")
    assert no_horizontal_scroll(page)
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    page.locator(".toast").last.wait_for(state="detached")
    sync = _api(url, "GET", "/devices/stick")["config"]["sync"]
    assert (sync["no_cache"], sync["no_cache_files"], sync["no_cache_file_max_mb"]) == (
        True,
        2,
        256,
    )

    page.goto(url + "/#/devices/stick")
    page.locator("[data-update-status] [data-update-line]").wait_for()
    assert (
        page.locator("[data-update-status] [data-update-line]").inner_text()
        == "Automatic updates are off."
    )
    assert no_horizontal_scroll(page)
    assert page.errors == []


def test_first_full_sync_clears_the_skipped_update_line(web_server, chrome, tmp_path):
    """connect → the update is skipped (no full sync yet) → Sync: the Overview says "Updated …", not the
    stale "Last automatic update skipped … (no update before the first full sync)"."""
    url, app = web_server
    make_music(tmp_path / "music" / "a.mp3", title="A", artist="X", album="Y")
    body = {
        "id": "music",
        "type": "folder",
        "name": "Music",
        "settings": {"root": str(tmp_path / "music")},
    }
    _api(url, "POST", "/libraries", json=body)
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Stick",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "library_ids": ["music"],
    }
    _api(url, "POST", "/devices/register", json=body)
    job = _api(url, "POST", "/devices/stick/update")
    run_on(app, app.jobs.wait(job["id"]), 60)

    page = open_page(chrome, url + "/#/devices/stick", width=1280)
    status = page.locator("[data-update-status] [data-update-line]")
    status.wait_for()
    assert status.inner_text().startswith("Last automatic update skipped")

    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    run_on(app, app.jobs.wait(job["id"]), 60)
    page.reload()
    status.wait_for()
    text = status.inner_text()
    assert text.startswith("Updated ") and "skipped" not in text and "first full sync" not in text
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_paused_update_line(web_server, chrome, tmp_path, width):
    """a plan waiting for review holds the automatic update - the devices list says "Update paused" with
    the paused (clock) icon, in the dark and the light theme."""
    url, app = web_server
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    _api(url, "POST", "/devices/register", json=body)
    app.sync._waiting["stick"] = {"reason": "test", "plan_id": "p1"}
    assert _api(url, "GET", "/devices/stick")["update"]["state"] == "paused"

    page = open_page(chrome, url + "/#/devices", width=width)
    for theme in ("dark", "light"):
        page.evaluate(f"localStorage.setItem('syncmusic.theme', '{theme}')")
        page.reload()
        line = page.locator('[data-update-line][data-state="paused"]').first
        line.wait_for()
        assert line.inner_text() == "Update paused"
        assert page.evaluate("document.documentElement.dataset.theme") == theme
        _shot(page, f"paused-devices-{width}-{theme}.png")
        assert no_horizontal_scroll(page)
    page.evaluate("localStorage.removeItem('syncmusic.theme')")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_waiting_library_change_line(web_server, chrome, tmp_path, width):
    """a library change waiting for its update_change_gap_min slot shows 'Library changed - update …'
    on the device page instead of the next periodic check."""
    import time

    url, app = web_server
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    _api(url, "POST", "/devices/register", json=body)
    app.sync._update_ran["stick"] = time.monotonic()  # an update just ran: the change waits ~15 min
    app.sync._update_dirty.add("stick")
    assert _api(url, "GET", "/devices/stick")["update"]["change_at"]

    page = open_page(chrome, url + "/#/devices/stick", width=width)
    status = page.locator("[data-update-status] [data-update-line]")
    status.wait_for()
    page.wait_for_function(
        "document.querySelector('[data-update-status] [data-update-line]').innerText.includes('Library changed')"
    )
    text = status.inner_text()
    assert "Library changed - update" in text and "next check" not in text, text
    assert re.search(r"update in \d+ min", text), text  # relative, not a date-time
    assert "undefined" not in text and "{when}" not in text, text
    _shot(page, f"update-change-waits-{width}.png")
    assert no_horizontal_scroll(page)
    assert page.errors == []
