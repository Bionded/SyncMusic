"""Planning progress on the plan page and the sidebar card: a stepper with the current step, a bar
(indeterminate while the share is unknown), items counted and the elapsed time; Cancel stops planning and goes back
to the device; leaving the page and coming back joins the running plan."""

from __future__ import annotations

import asyncio
import threading

import pytest

from syncmusic.core import planprogress
from tests.webui.conftest import animations_done, no_horizontal_scroll, open_page, shot
from tests.webui.test_plan_flow import device  # noqa: F401

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.fixture
def slow_plan(device):  # noqa: F811
    """The device's planning stops in the library step (half done, 1234 items) until `release` is set."""
    url, app, _ = device
    real, calls, release = app.sync.plan, [], threading.Event()

    async def plan(device_id, **kw):
        calls.append(device_id)
        planprogress.step("libraries", fraction=0.0, items=0, detail="Music")
        await asyncio.sleep(0.3)
        planprogress.step("libraries", fraction=0.5, items=1234, detail="Music")
        await asyncio.to_thread(release.wait)
        planprogress.step("device", detail="Stick")
        return await real(device_id, **kw)

    app.sync.plan = plan
    yield url, calls, release
    release.set()


def test_planning_progress_cancel_and_rejoin(slow_plan, chrome):
    url, calls, release = slow_plan
    page = open_page(chrome, url + "/#/devices/stick/plan")
    page.wait_for_selector("[data-planning] li.current[data-step='libraries']")
    page.wait_for_selector("[data-plan-step]:has-text('1,234')")
    step = page.locator("[data-plan-step]").inner_text()
    assert "Step 1 of 4" in step and "13 %" in step and "Music: 1,234 items so far" in step
    assert page.locator("[data-plan-bar]").get_attribute("aria-valuenow") == "13"
    assert page.locator("[data-plan-elapsed]").inner_text().startswith("Elapsed 0:")
    # the sidebar card says so too, and leads back to the planning page
    card = page.locator('[data-device-card="stick"]')
    assert card.get_attribute("data-state") == "planning" and "13%" in card.inner_text()
    shot(page, "planning-1280")

    # leaving and coming back joins the running plan (no second plan)
    page.goto(url + "/#/devices/stick")
    page.wait_for_selector("[data-route]")
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_selector("[data-planning] [data-plan-step]:has-text('1,234')")
    assert calls == ["stick"]

    page.set_viewport_size({"width": 390, "height": 844})
    animations_done(page)
    assert no_horizontal_scroll(page)
    shot(page, "planning-390")
    page.set_viewport_size({"width": 1280, "height": 800})

    # Cancel stops it and goes back to the device
    page.get_by_role("button", name="Cancel").click()
    page.wait_for_function("location.hash === '#/devices/stick'")
    page.wait_for_selector(".toast:has-text('Planning cancelled')")
    page.wait_for_function(
        '!document.querySelector(\'[data-device-card="stick"][data-state="planning"]\')'
    )

    # planning again finishes on the plan's own page
    release.set()
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_function("/#\\/devices\\/stick\\/plan\\/.+/.test(location.hash)", timeout=30_000)
    page.wait_for_selector('[data-count="add"] strong')
    assert len(calls) == 2
