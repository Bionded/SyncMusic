"""Processors tab → Preview on one track: pick a FLAC, Run the chain, the before / after table
(changed values bold), Download result; it runs the chain on screen, also before Save."""

from __future__ import annotations

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot
from tests.webui.test_plan_flow import _api, device  # noqa: F401 - fixture

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_preview_on_one_track(device, chrome, tmp_path, width):  # noqa: F811
    url, app, _stick = device
    make_music(
        tmp_path / "music" / "Воплі Відоплясова" / "Місто - дуже довга назва пісні.flac",
        title="Місто - дуже довга назва пісні",
        artist="Воплі Відоплясова",
        album="Трикутник",
    )
    job = _api(url, "POST", "/libraries/music/refresh")
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    _api(url, "PATCH", "/devices/stick", json={"quality_profile": "weak"})

    page = open_page(chrome, url + "/#/devices/stick/processors", width)
    aside = page.locator("[data-chain-preview]")
    aside.wait_for()
    # the search field finds any track; nothing found says so
    aside.locator("#chainSearch").fill("місто")
    aside.locator("#chainSearch").press("Enter")
    page.wait_for_function(
        "[...document.querySelectorAll('#chainTrack option')].every(o => o.textContent.includes('Місто'))"
    )
    aside.locator("#chainSearch").fill("nothing like this")
    aside.locator("#chainSearch").press("Enter")
    page.wait_for_selector("#chainTrack option:has-text('No track matches')", state="attached")
    aside.locator("#chainSearch").fill("")
    aside.locator("#chainSearch").press("Enter")
    page.wait_for_function("document.querySelectorAll('#chainTrack option').length > 1")
    options = aside.locator("#chainTrack option").all_inner_texts()
    flac = next(o for o in options if o.endswith("(FLAC)"))
    aside.locator("#chainTrack").select_option(label=flac)
    aside.get_by_role("button", name="Run the chain").click()
    fmt = aside.locator('[data-pv-row="Format"]')
    fmt.wait_for(timeout=30000)
    assert fmt.get_attribute("data-changed") is not None
    assert "FLAC" in fmt.inner_text() and fmt.locator("b").inner_text().startswith("MP3")
    title = aside.locator('[data-pv-row="Title"]')
    assert title.get_attribute("data-changed") is None and title.locator("b").count() == 0
    href = aside.locator("[data-chain-download]").get_attribute("href")
    r = httpx.get(url + href, timeout=30)
    assert r.status_code == 200 and len(r.content) > 1000
    shot(page, f"chain-preview-{width}")
    assert no_horizontal_scroll(page)
    # a new search (another track) clears the result shown for the one before
    aside.locator("#chainSearch").fill("місто")
    aside.locator("#chainSearch").press("Enter")
    page.wait_for_function("!document.querySelector('[data-pv-row=\"Format\"]')")
    aside.locator("#chainTrack").select_option(index=0)
    aside.get_by_role("button", name="Run the chain").click()
    fmt.wait_for(timeout=30000)

    # the chain on screen counts, saved or not: Convert switched off → the FLAC stays FLAC
    page.locator("[data-proc] [data-proc-enabled]").first.uncheck()
    aside.get_by_role("button", name="Run the chain").click()
    page.wait_for_function(
        "document.querySelector('[data-pv-row=\"Format\"]') && "
        "!document.querySelector('[data-pv-row=\"Format\"]').hasAttribute('data-changed')",
        timeout=30000,
    )
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
