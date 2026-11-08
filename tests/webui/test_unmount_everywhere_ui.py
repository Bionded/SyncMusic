# SPDX-License-Identifier: GPL-3.0-or-later
"""Check and repair blocked by another program's mount offers Unmount everywhere and continues; a busy mount
offers a lazy detach as a second step."""

from __future__ import annotations

import pytest

from syncmusic.core.sync import SyncError
from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_plan_flow import device  # noqa: F401 - fixture

pytestmark = [pytest.mark.ui, pytest.mark.slow]

MOUNTS = [
    {"partition": "/dev/sdq2", "path": "/run/app/stick", "ours": True},
    {"partition": "/dev/sdq2", "path": "/media/someone/PLAYER", "ours": False},
]


def test_repair_offers_unmount_everywhere(device, chrome, monkeypatch):  # noqa: F811
    url, app, _stick = device
    app.sync._damaged["stick"] = "Input/output error"
    calls: list[str] = []

    async def repair(device_id):
        calls.append("repair")
        if calls.count("repair") == 1:
            raise SyncError(
                "Stick is also mounted by another program at /media/someone/PLAYER: "
                "unmount it everywhere first",
                "mounted_elsewhere",
            )
        raise SyncError("Stick: second try reached", "invalid")

    async def mounts(device_id):
        return MOUNTS

    async def unmount_everywhere(device_id, *, lazy=False):
        calls.append(f"unmount lazy={lazy}")
        if not lazy:
            raise SyncError("/media/someone/PLAYER is in use by thunar (4242)", "mount_busy")
        return MOUNTS

    monkeypatch.setattr(app.sync, "repair", repair)
    monkeypatch.setattr(app.sync, "mounts", mounts)
    monkeypatch.setattr(app.sync, "unmount_everywhere", unmount_everywhere)
    page = open_page(chrome, url + "/#/devices/stick", 1280)
    page.locator("[data-fs-damaged]").get_by_role("button", name="Check and repair").click()
    dlg = page.get_by_role("dialog")
    dlg.get_by_text("/media/someone/PLAYER").first.wait_for()
    shot(page, "repair-blocked-1280")
    dlg.get_by_role("button", name="Unmount everywhere and continue").click()
    page.get_by_role("dialog").get_by_text("thunar (4242)").wait_for()
    shot(page, "unmount-busy-1280")
    page.get_by_role("dialog").get_by_role("button", name="Detach anyway").click()
    page.wait_for_selector(".toast:has-text('second try reached')")
    assert calls == ["repair", "unmount lazy=False", "unmount lazy=True", "repair"]
    page.errors.clear()


@pytest.mark.parametrize("width", [1280, 390])
def test_foreign_mount_note(device, chrome, monkeypatch, width):  # noqa: F811
    url, app, _stick = device
    orig = app.sync.list_devices

    async def list_devices():
        out = await orig()
        for d in out["registered"]:
            if d["id"] == "stick":
                d["foreign_mounts"] = ["/media/someone/A very long automount name for the player"]
        return out

    monkeypatch.setattr(app.sync, "list_devices", list_devices)
    page = open_page(chrome, url + "/#/devices/stick", width)
    note = page.locator("[data-foreign-mounts]")
    note.wait_for()
    assert "Also mounted by the system" in note.inner_text()
    assert note.get_by_role("button", name="Unmount everywhere").is_enabled()
    shot(page, f"foreign-mount-{width}")
    assert no_horizontal_scroll(page)


def test_lease_banner(device, chrome, monkeypatch):  # noqa: F811
    url, app, _stick = device
    orig = app.sync.list_devices

    async def list_devices():
        out = await orig()
        for d in out["registered"]:
            if d["id"] == "stick":
                d["lease"] = {"name": "Kitchen box", "since": "2026-03-01T10:00:00+00:00"}
        return out

    monkeypatch.setattr(app.sync, "list_devices", list_devices)
    page = open_page(chrome, url + "/#/devices/stick", 390)
    note = page.locator("[data-lease]")
    note.wait_for()
    assert "Being synced by Kitchen box" in note.inner_text()
    shot(page, "lease-banner-390")
    assert no_horizontal_scroll(page)
