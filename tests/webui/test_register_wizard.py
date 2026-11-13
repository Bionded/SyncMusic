# SPDX-License-Identifier: GPL-3.0-or-later
"""Register step by step: one step at a time, Next checks the shown step, Enter goes on, Back keeps what was
entered, the summary shows what Register creates, nothing exists before Register; "Show all steps on one page"
gives the single-page form and is remembered."""

from __future__ import annotations

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _devices(url) -> list:
    return httpx.get(f"{url}/api/v1/devices", timeout=30).json()["registered"]


@pytest.mark.parametrize("width", [1280, 390])
def test_register_step_by_step(web_server, chrome, tmp_path, width):
    url, _ = web_server
    target = tmp_path / "стік"
    target.mkdir()
    page = open_page(chrome, "about:blank", width=width)
    page.add_init_script("localStorage.removeItem('sm.regSteps')")
    page.goto(url + "/#/devices/register")
    page.wait_for_selector("#regPath:visible")
    steps = page.locator("form [data-step]")
    shown = lambda: [i for i in range(steps.count()) if steps.nth(i).is_visible()]  # noqa: E731
    assert shown() == [0]
    assert page.locator('button[type="submit"]').is_hidden()
    page.fill("#regPath", str(target))
    page.locator("#regPath").dispatch_event("change")
    page.locator(".reg-type", has_text="USB flash drive").click()
    page.fill("#regName", "")
    page.get_by_role("button", name="Next").click()
    assert shown() == [0], "Next without a name"
    page.fill("#regName", "Стік у машині з дуже довгою назвою")
    shot(page, f"register-wizard-device-{width}")
    page.locator("#regName").press("Enter")  # Enter = Next, not Register
    assert shown() == [1]
    page.get_by_role("button", name="Back").click()
    assert shown() == [0] and page.locator("#regName").input_value().startswith("Стік")
    for want in (1, 2):
        page.get_by_role("button", name="Next").click()
        assert shown() == [want]
    page.get_by_role("button", name="Next").click()
    assert shown() == [2], "Next without a processor choice"
    page.locator("label", has_text="Only this device's processors").first.click()
    for want in (3, 4):
        page.get_by_role("button", name="Next").click()
        assert shown() == [want]
    summary = page.locator("#regSummary")
    assert "Стік у машині" in summary.inner_text()
    assert "USB flash drive" in summary.inner_text()
    assert page.locator('button[type="submit"]').is_visible()
    assert no_horizontal_scroll(page)
    shot(page, f"register-wizard-summary-{width}")
    assert not [d for d in _devices(url) if d["name"].startswith("Стік")], "saved too early"
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.includes('/overview')")
    assert [d for d in _devices(url) if d["name"].startswith("Стік")]
    assert page.errors == []


def test_all_steps_on_one_page_is_remembered(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, "about:blank")
    page.add_init_script("localStorage.removeItem('sm.regSteps')")
    page.goto(url + "/#/devices/register")
    page.wait_for_selector("#regPath:visible")
    page.get_by_role("button", name="Show all steps on one page").click()
    assert page.locator("form [data-step]:visible").count() == 4  # every step, no summary
    assert page.locator('button[type="submit"]').is_visible()
    assert page.get_by_role("button", name="Next").is_hidden()
    assert page.evaluate("localStorage.getItem('sm.regSteps')") == "all"
    page.get_by_role("button", name="Step by step").click()
    assert page.locator("form [data-step]:visible").count() == 1
