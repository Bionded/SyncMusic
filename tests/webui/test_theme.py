"""Theme: dark by default, the Settings switch stores light per browser and a reload paints
light from the start (js/theme.js runs before the first paint)."""

from __future__ import annotations

import pytest

from tests.webui.conftest import open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]

BG = "getComputedStyle(document.body).backgroundColor"


def test_theme_switch_applies_before_paint(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url + "/#/settings")
    page.wait_for_selector("#themeCard")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.evaluate(BG) == "rgb(17, 18, 19)"  # --bg dark
    page.click('#themeCard [data-value="light"]')
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    assert page.get_attribute('#themeCard [data-value="light"]', "aria-checked") == "true"
    assert page.evaluate(BG) == "rgb(245, 244, 241)"  # --bg light
    # before any module ran: the theme is already set on the first parse of the document
    assert page.evaluate("localStorage.getItem('syncmusic.theme')") == "light"
    page.reload(wait_until="commit")
    page.wait_for_function("document.documentElement.dataset.theme !== undefined")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    page.wait_for_selector("#themeCard")
    shot(page, "theme-light-settings-1280")
    page.click('#themeCard [data-value="dark"]')
    assert page.evaluate(BG) == "rgb(17, 18, 19)"
    assert not page.errors


@pytest.mark.parametrize("width", [1280, 390])
def test_theme_system_and_topbar(web_server, chrome, width):
    """ "System" follows prefers-color-scheme (live, and after a reload); the top-bar button cycles
    dark → light → system and Settings shows the same choice."""
    url, _ = web_server
    page = open_page(chrome, url + "/#/settings", width)
    page.wait_for_selector("#themeCard")
    page.emulate_media(color_scheme="light")
    page.click('#themeCard [data-value="system"]')
    assert page.evaluate("localStorage.getItem('syncmusic.theme')") == "system"
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    page.emulate_media(color_scheme="dark")
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    page.reload()
    page.wait_for_selector("#themeCard")
    assert page.get_attribute('#themeCard [data-value="system"]', "aria-checked") == "true"
    assert page.evaluate(BG) == "rgb(17, 18, 19)"
    btn = page.locator("#themeBtn")
    assert btn.get_attribute("data-mode") == "system"
    btn.click()  # system → dark
    assert page.evaluate("document.documentElement.dataset.themeMode") == "dark"
    btn.click()  # dark → light
    assert page.evaluate(BG) == "rgb(245, 244, 241)"
    assert page.get_attribute('#themeCard [data-value="light"]', "aria-checked") == "true"
    assert "Theme: light" in (btn.get_attribute("aria-label") or "")
    shot(page, f"theme-topbar-light-{width}")
    btn.click()  # back to system (dark media)
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert not page.errors
