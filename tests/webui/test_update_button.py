"""Settings → Updates → Update: with a newer commit found, Update asks first, then says it
installs and waits for the restart; installing itself is the root helper's (tests/unit/test_self_update.py)."""

from __future__ import annotations

import json

import pytest

from tests.webui.conftest import open_page

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def test_update_button_asks_then_installs(web_server, chrome, monkeypatch, tmp_path):
    url, app = web_server
    # a single-file binary updates itself
    monkeypatch.setenv("SYNCMUSIC_BINARY", str(tmp_path / "syncmusic"))
    app.updates.available, app.updates.latest = True, "c" * 40
    app.updates.install_type, app.updates.checked = "service", True
    app.updates.latest_date, app.updates.notes = "2026-10-24", ["feat: Оновлення"]
    page = open_page(chrome, url + "/#/settings")
    calls = []

    def fake_install(route):
        calls.append(route.request.method)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"installing": "c" * 40}),
        )

    page.route("**/api/v1/updates/install", fake_install)
    button = page.locator('[data-action="installUpdate"]')
    button.wait_for()
    button.click()
    modal = page.locator(".modal")
    modal.wait_for()
    assert "Install the update?" in modal.inner_text()
    modal.get_by_role("button", name="Update").click()
    page.wait_for_selector("[data-update-progress]:has-text('Installing ccccccccc')")
    assert calls == ["POST"] and page.errors == []
