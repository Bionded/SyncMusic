# SPDX-License-Identifier: GPL-3.0-or-later
"""Register a USB disk (here a folder) as a backup: purpose, a base folder picked in the folder picker, no
processing to choose; the player purpose keeps the processor choice."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=30, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.mark.parametrize("width", [1280, 390])
def test_register_a_backup_disk(web_server, chrome, tmp_path, width):
    url, _ = web_server
    target = tmp_path / "ssd"
    (target / "Archive" / "Old").mkdir(parents=True)
    page = open_page(chrome, url + "/#/devices/register", width=width)
    page.wait_for_selector("#regPath:visible")
    page.fill("#regPath", str(target))
    page.locator("#regPath").dispatch_event("change")
    page.locator(".reg-type", has_text="USB flash drive").click()
    page.fill("#regName", "Backup SSD")
    # a player by default: processors to choose, no base folder
    assert page.locator('input[name="regPurpose"]:checked').get_attribute("value") == "player"
    assert page.locator("#regProcs").is_visible()
    page.locator(".reg-opt", has_text="A backup").click()
    assert page.locator("#regBaseFolder").input_value() == "SyncMusic"
    assert page.locator("#regProcs").is_hidden() and page.locator("#regBackupProcs").is_visible()
    # the picker: open Archive, add a new folder inside it
    page.fill("#regBaseFolder", "")
    page.locator('[data-pick="dev-disk"]').click()
    dlg = page.get_by_role("dialog")
    dlg.locator('[data-pick-into="Archive"]').click()
    dlg.locator('[data-pick-into="Old"]').wait_for()
    expect(dlg.locator("[data-pick-space]")).to_contain_text("Free on the disk:")
    dlg.locator("#pickNewName").fill("Music 2026")
    dlg.get_by_role("button", name="New folder").click()
    shot(page, f"register-backup-picker-{width}")
    dlg.get_by_role("button", name="Use this folder").click()
    assert page.locator("#regBaseFolder").input_value() == "Archive/Music 2026"
    assert no_horizontal_scroll(page)
    shot(page, f"register-backup-{width}")
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.includes('/overview')")
    dev = page.evaluate("decodeURIComponent(location.hash.split('/')[2])")
    cfg = _api(url, "GET", f"/devices/{dev}")["config"]
    settings = cfg["settings"]
    assert settings["purpose"] == "backup"
    assert settings["base_folder"] == "Archive/Music 2026"
    assert set(settings["layout"].values()) == {"{library_path}"}
    assert cfg["rule_set"]["mode"] == "additive" and cfg["sync"]["verify"] == "all"
    assert not [p for p in cfg["processors"] if p["enabled"] and p["type"] != "transcode"]
    assert page.errors == []
