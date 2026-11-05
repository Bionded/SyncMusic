"""Files on the device: the single-disc and the multi-disc music template, each with a live
example of where a file lands."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_plan_flow import device  # noqa: F401 - fixture

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_layout_examples(device, chrome, width):  # noqa: F811
    url, _app, _stick = device
    page = open_page(chrome, url + "/#/devices/stick/settings", width=width)
    single = page.locator('#filesCard [data-path-example="music"]')
    multi = page.locator('#filesCard [data-path-example="music_multi_disc"]')
    single.wait_for()
    assert single.inner_text() == "e.g. Music/Miles Davis/1959 - Kind of Blue/0101 - So What.mp3"
    assert multi.inner_text() == "e.g. Music/Pink Floyd/1979 - The Wall/CD02/01 - Hey You.mp3"
    assert "Music, multi-disc" in page.locator("#filesCard").inner_text()
    page.locator('#filesCard input[id$="d-layout-music_multi_disc"]').fill(
        "Music/{album} ({disc_total} discs)/Disc {disc}/{track:02} {title}"
    )
    assert multi.inner_text() == "e.g. Music/The Wall (2 discs)/Disc 2/01 Hey You.mp3"
    page.locator('#filesCard input[id$="d-layout-music"]').fill(
        "Music/{genre}/{year} - {nope}/{title}"
    )
    assert single.inner_text() == "e.g. Music/Jazz/1959/So What.mp3"
    assert no_horizontal_scroll(page)
    page.locator("#filesCard").scroll_into_view_if_needed()
    shot(page, f"path-layout-{width}")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_flash_base_folder_picker(device, chrome, width):  # noqa: F811
    """the flash drive's base folder is chosen in the picker dialog on the mounted stick (read-only;
    a new folder is only a name) and saved with the settings."""
    import httpx

    url, _app, stick = device
    (stick / "Плеєр").mkdir(exist_ok=True)
    page = open_page(chrome, url + "/#/devices/stick/settings", width=width)
    card = page.locator("#filesCard")
    card.get_by_role("button", name="Browse…").click()
    dlg = page.locator(".modal")
    dlg.locator(".found-row", has_text="Плеєр").click()
    dlg.locator("[aria-current=location]", has_text="Плеєр").wait_for()
    assert dlg.get_by_role("button", name="Up one level").is_enabled()
    dlg.locator("#pickNewName").fill("Музика")
    dlg.get_by_role("button", name="New folder").click()
    shot(page, f"flash-base-folder-{width}")
    assert no_horizontal_scroll(page)
    dlg.get_by_role("button", name="Use this folder").click()
    assert card.locator('[id$="-base_folder"]').input_value() == "Плеєр/Музика"
    assert not (stick / "Плеєр" / "Музика").exists()  # nothing written by browsing
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".toast:has-text('saved')")
    cfg = httpx.get(f"{url}/api/v1/devices/stick").json()["config"]
    assert cfg["settings"]["base_folder"] == "Плеєр/Музика"
    assert page.errors == []
