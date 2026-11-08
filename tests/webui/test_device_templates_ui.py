# SPDX-License-Identifier: GPL-3.0-or-later
"""Device → Templates: another installation's templates on the player, with Import, Update my copy and the ones
that need libraries this installation lacks."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_plan_flow import device  # noqa: F401 - fixture

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def rows():
    base = {
        "instance_name": "Kitchen box",
        "updated_at": "2026-03-01T00:00:00+00:00",
        "copy_id": None,
        "linked": False,
        "updated": False,
        "missing_libraries": [],
    }
    return [
        {**base, "uid": "k:mix", "name": "Road trip", "state": "importable"},
        {**base, "uid": "k:news", "name": "Morning news", "state": "importable"},
        {
            **base,
            "uid": "k:old",
            "name": "Старі записи",
            "state": "copy",
            "copy_id": "stari",
            "linked": True,
            "updated": True,
        },
        {
            **base,
            "uid": "k:books",
            "name": "Bedtime books",
            "state": "unavailable",
            "missing_libraries": ["Audiobooks at the cottage"],
        },
    ]


@pytest.mark.parametrize("width", [1280, 390])
def test_other_installations_templates(device, chrome, monkeypatch, width):  # noqa: F811
    url, app, _stick = device
    imported: list[list[str]] = []

    async def device_templates(device_id):
        return rows()

    async def import_device_templates(device_id, uids):
        imported.append(uids)
        return []

    monkeypatch.setattr(app.sync, "device_templates", device_templates)
    monkeypatch.setattr(app.sync, "import_device_templates", import_device_templates)
    page = open_page(chrome, url + "/#/devices/stick/rules", width)
    card = page.locator("[data-others]")
    card.wait_for()
    text = card.inner_text()
    assert "From other installations" in text and "from Kitchen box" in text
    assert "Updated on Kitchen box" in text and "Audiobooks at the cottage" in text
    assert card.locator('[data-other="k:books"] button').count() == 0
    assert (
        card.locator('[data-other="k:old"]')
        .get_by_role("button", name="Update my copy")
        .is_visible()
    )
    shot(page, f"device-templates-others-{width}")
    assert no_horizontal_scroll(page)
    card.get_by_role("button", name="Import all").click()
    page.wait_for_selector(".toast:has-text('imported and linked')")
    assert imported == [["k:mix", "k:news"]]
