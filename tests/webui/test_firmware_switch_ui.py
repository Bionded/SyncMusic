# SPDX-License-Identifier: GPL-3.0-or-later
"""A registered player that now runs another firmware: the device page offers to switch the device type."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_plan_flow import device  # noqa: F401 - fixture

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_firmware_switch_note(device, chrome, monkeypatch, width):  # noqa: F811
    url, app, _stick = device
    orig = app.sync.list_devices
    saved: list[str] = []

    async def list_devices():
        out = await orig()
        for d in out["registered"]:
            if d["id"] == "stick":
                d["firmware_switch"] = "hiby_compas"
        return out

    async def update_device(cfg):
        saved.append(cfg.type)
        return cfg

    monkeypatch.setattr(app.sync, "list_devices", list_devices)
    monkeypatch.setattr(app.sync, "update_device", update_device)
    page = open_page(chrome, url + "/#/devices/stick", width)
    note = page.locator("[data-firmware-switch]")
    note.wait_for()
    assert "now runs Compás" in note.inner_text()
    assert no_horizontal_scroll(page)
    shot(page, f"firmware-switch-{width}")
    note.get_by_role("button", name="Switch to Compás").click()
    page.wait_for_selector(".toast:has-text('now syncs for Compás')")
    assert saved == ["hiby_compas"]
    page.errors.clear()
