"""the library browser's header has Settings (the library's settings, back to the browser after Save or
Cancel) and Refresh (reloads now, the timestamp updates) - for every library type, 1280 and 390 px. The Navidrome
and Audiobookshelf libraries point at closed ports (their browser shows the error, the header still works)."""

from __future__ import annotations

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import open_page

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.mark.parametrize("width", [1280, 390])
def test_settings_and_refresh_in_the_header(web_server, chrome, tmp_path, width):
    url, _ = web_server
    make_music(tmp_path / "music" / "a.mp3", title="A", artist="X", album="Y")
    libs = [
        {
            "id": "music",
            "type": "folder",
            "name": "Music",
            "settings": {"root": str(tmp_path / "music")},
        },
        {
            "id": "nd",
            "type": "navidrome",
            "name": "ND",
            "settings": {"url": "http://127.0.0.1:9", "username": "u", "password": "p"},
        },
        {
            "id": "abs",
            "type": "audiobookshelf",
            "name": "ABS",
            "settings": {"url": "http://127.0.0.1:9", "api_token": "t"},
        },
    ]
    for lib in libs:
        _api(url, "POST", "/libraries", json=lib)
    page = open_page(chrome, url + "/#/", width=width)
    for lib in libs:
        page.goto(url + f"/#/libraries/{lib['id']}/browse")
        page.wait_for_selector(
            f'#content[data-route="/libraries/{lib["id"]}/browse"]', timeout=30_000
        )
        head = page.locator(".pagehead")
        assert (
            head.get_by_role("link", name="Settings").get_attribute("href")
            == f"#/libraries/{lib['id']}?back=browse"
        )
        assert head.get_by_role("button", name="Refresh").is_visible()
    # Settings → Cancel → back in the browser; Settings → Save → back in the browser
    page.goto(url + "/#/libraries/music/browse")
    page.wait_for_selector('input[name="sel"]')
    page.locator(".pagehead").get_by_role("link", name="Settings").click()
    page.wait_for_selector("#libName")
    page.get_by_role("link", name="Cancel").click()
    page.wait_for_selector('#content[data-route="/libraries/music/browse"]')
    page.locator(".pagehead").get_by_role("link", name="Settings").click()
    page.wait_for_selector("#libName")
    page.fill("#libName", "Музика")
    page.locator('form[data-action="save"] button[type="submit"]').click()
    page.wait_for_selector('#content[data-route="/libraries/music/browse"]')
    assert page.locator(".pagehead h1").inner_text() == "Музика"
    # Refresh: a new file appears, the timestamp says just now
    make_music(tmp_path / "music" / "b.mp3", title="B", artist="X", album="Y")
    page.locator(".pagehead").get_by_role("button", name="Refresh").click()
    page.wait_for_function(
        "() => document.querySelectorAll('#content input[name=sel]').length === 2",
        timeout=20_000,
    )
    assert page.locator('[data-updated="music"]').inner_text() == "Updated just now"
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
