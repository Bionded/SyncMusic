"""The sidebar's Libraries split button: one bordered control with rounded outer corners and a thin
divider; the label shows/hides the per-library links, the list icon opens All libraries; both parts have accessible names,
hover/focus states and the same height - on the wide sidebar and in the phone menu."""

from __future__ import annotations

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import open_page

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_libraries_split_button(web_server, chrome, tmp_path, width):
    url, _ = web_server
    make_music(tmp_path / "m" / "a.mp3", title="A")
    r = httpx.post(
        f"{url}/api/v1/libraries",
        json={
            "id": "m",
            "type": "folder",
            "name": "Моя музика",
            "settings": {"root": str(tmp_path / "m")},
        },
        timeout=30,
    )
    assert r.status_code == 201, r.text
    page = open_page(chrome, url + "/#/", width=width)
    if width < 600:
        page.locator("#burger").click()
    split = page.locator(".nav-split")
    split.wait_for()
    # the phone drawer slides in: measure once it stands still
    page.wait_for_function(
        """() => new Promise(ok => { const x = document.querySelector('.nav-split').getBoundingClientRect().left;
             setTimeout(() => ok(x >= 0 && x === document.querySelector('.nav-split').getBoundingClientRect().left), 150); })"""
    )
    label = split.get_by_role("button", name="Libraries")
    all_libs = split.get_by_role("link", name="All libraries")
    box, a, b = split.bounding_box(), label.bounding_box(), all_libs.bounding_box()
    # one control: the parts fill it side by side, same height, the divider between them
    assert abs(a["y"] - b["y"]) < 1 and abs(a["height"] - b["height"]) < 1
    assert (
        abs(a["x"] + a["width"] - b["x"]) < 1.5
        and abs(b["x"] + b["width"] - (box["x"] + box["width"])) < 2
    )
    style = split.evaluate(
        """el => { const s = getComputedStyle(el), l = getComputedStyle(el.querySelector('.nav-library')),
              r = getComputedStyle(el.querySelector('.nav-all'));
              return [s.borderTopWidth, s.borderTopLeftRadius, l.borderTopLeftRadius, r.borderTopRightRadius,
                      r.borderLeftWidth, l.borderTopWidth, r.borderTopWidth]; }"""
    )
    # the design: no frame around it, rounded ends, the divider between the parts
    assert style == ["0px", "8px", "8px", "8px", "1px", "0px", "0px"], style
    split.screenshot(path=str(tmp_path / f"split-{width}.png"))
    # hover changes the part under the mouse only
    before = all_libs.evaluate("el => getComputedStyle(el).backgroundColor")
    all_libs.hover()
    assert all_libs.evaluate("el => getComputedStyle(el).backgroundColor") != before
    # the label toggles the library links
    assert label.get_attribute("aria-expanded") == "false"
    label.click()
    page.wait_for_selector("#libLinks a:has-text('Моя музика')")
    assert (
        page.locator(".nav-split")
        .get_by_role("button", name="Libraries")
        .get_attribute("aria-expanded")
        == "true"
    )
    # keyboard: both parts take focus with a visible ring
    page.locator(".nav-split .nav-library").focus()
    page.keyboard.press("Tab")
    focused = page.evaluate("() => document.activeElement.getAttribute('aria-label')")
    assert focused == "All libraries"
    ring = page.evaluate("() => getComputedStyle(document.activeElement).outlineStyle")
    assert ring != "none"
    # the list icon opens All libraries
    page.keyboard.press("Enter")
    page.wait_for_function("location.hash === '#/libraries'")
    assert page.errors == []
