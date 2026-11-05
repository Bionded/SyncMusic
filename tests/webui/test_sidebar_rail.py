"""Collapsible sidebar: a toggle (and "[") collapses it to an icon rail on wide screens, remembered
per browser; labels stay accessible, a tip names an item on hover/focus, device cards become their type icon with a
status dot; phones keep the drawer. Settings uses the gear icon. Temp folders only."""

from __future__ import annotations

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _side_width(page) -> float:
    return page.evaluate("document.querySelector('#sidebar').getBoundingClientRect().width")


def test_sidebar_collapses_to_a_rail(web_server, chrome, tmp_path):
    url, _app = web_server
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="A", artist="X", album="Y")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    (tmp_path / "stick").mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={
            "name": "Stick",
            "mount_path": str(tmp_path / "stick"),
            "type": "flashdrive",
            "library_ids": ["music"],
        },
    )
    page = open_page(chrome, f"{url}/#/")
    page.wait_for_selector("#sideBottom [data-device-card=stick]")
    toggle = page.locator("#sideCollapse")
    assert toggle.get_attribute("aria-expanded") == "true"
    assert _side_width(page) == 256
    shot(page, "706-expanded-1280")

    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "false"
    assert toggle.get_attribute("aria-label") == "Expand the sidebar"
    page.wait_for_function(
        "document.querySelector('#sidebar').getBoundingClientRect().width === 64"
    )
    main_left = page.evaluate("document.querySelector('.main').getBoundingClientRect().left")
    assert main_left == 64
    # labels hidden visually but still the links' accessible names
    assert page.get_by_role("link", name="Devices", exact=True).is_visible()
    assert page.locator("[data-nav=devices] .lbl").bounding_box()["width"] <= 1
    # the device card is its type icon with a status dot
    assert page.locator("[data-device-card=stick] .mini-rail").is_visible()
    assert not page.locator("[data-device-card=stick] .mini-top").is_visible()
    # a tip on hover and keyboard focus
    page.hover("[data-nav=templates]")
    tip = page.locator("#sideTip")
    assert tip.is_visible() and tip.inner_text() == "Templates"
    page.focus("[data-device-card=stick]")
    assert tip.is_visible() and tip.inner_text().startswith("Stick · ")
    assert no_horizontal_scroll(page)
    shot(page, "706-collapsed-1280")

    # remembered after a reload; "[" expands it again
    page.reload()
    page.wait_for_selector("body.side-rail #nav [data-nav=dashboard]")
    page.locator("main").click(position={"x": 400, "y": 300})
    page.keyboard.press("[")
    page.wait_for_function(
        "document.querySelector('#sidebar').getBoundingClientRect().width === 256"
    )
    assert page.evaluate("localStorage.getItem('syncmusic.sidebar')") == "full"
    assert not page.errors


def test_phone_keeps_the_drawer(web_server, chrome):
    url, _app = web_server
    page = open_page(chrome, f"{url}/#/", width=390, height=800)
    page.evaluate("localStorage.setItem('syncmusic.sidebar', 'rail')")
    page.reload()
    page.wait_for_selector("#nav [data-nav=dashboard]", state="attached")
    page.click("#burger")
    page.wait_for_function("document.querySelector('#sidebar').getBoundingClientRect().left >= 0")
    assert _side_width(page) > 200
    assert page.locator("[data-nav=devices] .lbl").is_visible()
    assert not page.locator("#sideCollapse").is_visible()
    assert no_horizontal_scroll(page)
    assert not page.errors


def test_settings_icon_is_a_gear():
    from syncmusic.webui import STATIC_DIR

    src = (STATIC_DIR / "js" / "icons.js").read_text()
    line = next(x for x in src.splitlines() if x.strip().startswith("settings:"))
    assert "M12 2v3" not in line  # the old rayed circle (a sun)
