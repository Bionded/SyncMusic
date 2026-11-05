"""Inline help: a "?" next to the hard concepts opens a short popover with a "Read more" link to the
user docs; keyboard accessible (Enter opens, Escape closes and gives the focus back), a click outside closes it."""

from __future__ import annotations

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_help_tips(web_server, chrome, tmp_path, width):
    url, _ = web_server
    (tmp_path / "stick").mkdir()
    r = httpx.post(
        f"{url}/api/v1/devices/register",
        json={"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"},
        timeout=60,
    )
    assert r.status_code < 400, r.text

    page = open_page(chrome, url + "/#/devices/stick/settings", width=width)
    page.wait_for_selector("[data-sync-form]")
    for topic in ("update_full", "reverse"):
        assert page.locator(f".help-tip[data-help='{topic}']").count() == 1

    tip = page.locator(".help-tip[data-help='reverse']")
    tip.click()
    pop = page.locator(".help-pop")
    assert pop.is_visible()
    assert tip.get_attribute("aria-expanded") == "true"
    assert "play counts" in pop.inner_text()
    link = pop.locator("a")
    assert link.get_attribute("href").endswith("docs/01-overview.md#reverse-sync")
    assert link.get_attribute("target") == "_blank"
    box = pop.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    shot(page, f"help-tip-{width}")
    assert no_horizontal_scroll(page)

    # Escape closes and gives the focus back; Enter on the focused button opens it again
    page.keyboard.press("Escape")
    assert page.locator(".help-pop").count() == 0
    assert page.evaluate("document.activeElement.dataset.help") == "reverse"
    page.keyboard.press("Enter")
    assert page.locator(".help-pop").count() == 1
    assert page.evaluate(
        "document.activeElement.closest('.help-pop') !== null"
    )  # the link has the focus

    # a click outside closes; another tip replaces the open one
    page.locator("h1").first.click()
    assert page.locator(".help-pop").count() == 0
    page.locator(".help-tip[data-help='update_full']").click()
    page.locator(".help-tip[data-help='reverse']").click()
    assert page.locator(".help-pop").count() == 1
    assert "newer one wins" in page.locator(".help-pop").inner_text()

    # the other screens: rules and processors
    for route, topic in (("rules", "rules"), ("processors", "processors")):
        page.goto(f"{url}/#/devices/stick/{route}")
        page.wait_for_selector(f".help-tip[data-help='{topic}']")
        page.locator(f".help-tip[data-help='{topic}']").click()
        assert page.locator(".help-pop p").inner_text().strip()
        assert not page.locator(".help-pop").inner_text().startswith("help.")
