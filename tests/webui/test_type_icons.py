"""In the browser: device and library lists show each type's own icon with an accessible name."""

from __future__ import annotations

import httpx
import pytest

from tests.contracts.harness import _ftp_server
from tests.webui.conftest import open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=30, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def test_lists_show_type_icons(web_server, chrome, tmp_path):
    url, _ = web_server
    (tmp_path / "music").mkdir()
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "m",
            "type": "folder",
            "name": "Music",
            "settings": {"root": str(tmp_path / "music")},
        },
    )
    (tmp_path / "stick").mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"},
    )
    root, port = _ftp_server()
    (root / "icons").mkdir(exist_ok=True)
    settings = {"host": "127.0.0.1", "port": port, "base_path": "/icons"}
    _api(
        url, "POST", "/devices/register", json={"name": "NAS", "type": "ftp", "settings": settings}
    )
    page = open_page(chrome, url + "/#/devices")
    page.wait_for_selector('[data-device-row] svg[data-type-icon="ftp"]')
    assert (
        page.locator('[data-device-row] svg[data-type-icon="flashdrive"]').get_attribute(
            "aria-label"
        )
        == "USB flash drive"
    )
    assert (
        page.locator('[data-device-row] svg[data-type-icon="ftp"]').get_attribute("aria-label")
        == "FTP server"
    )
    # sidebar card; the rail copy of the icon lives in .mini-rail
    assert page.locator('.device-mini .mini-top svg[data-type-icon="ftp"]').count() == 1
    shot(page, "type-icons-devices")
    page.goto(url + "/#/libraries")
    page.wait_for_selector('.lib-card svg[data-type-icon="folder"]')
    assert (
        page.locator('.lib-card svg[data-type-icon="folder"]').get_attribute("aria-label")
        == "Folder library"
    )
    page.goto(url + "/#/")
    # the v2 dashboard has no device list: library health rows carry the icons
    page.wait_for_selector('[data-library-row] svg[data-type-icon="folder"]')
    # no raw type id ('folder', 'flashdrive') as text on the dashboard
    text = page.inner_text("#content")
    assert "flashdrive" not in text and "folder" not in text.lower().replace(
        "folder library", ""
    ), text
    page.close()
