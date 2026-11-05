"""Device profile card: sources per field, an override saved and kept over a reload, Reset."""

from __future__ import annotations

import httpx
import pytest

from tests.contracts.harness import rockbox_tree
from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.fixture
def device(web_server, tmp_path):
    url, _ = web_server
    r = httpx.post(
        f"{url}/api/v1/devices/register",
        json={"name": "Mini", "type": "rockbox", "mount_path": str(rockbox_tree(tmp_path / "rb"))},
        timeout=60,
    )
    assert r.status_code == 201, r.text
    return url


def test_profile_card_override_and_reset(device, chrome):
    url = device
    page = open_page(chrome, url + "/#/devices/mini/settings")
    page.wait_for_selector("#profileCard")
    assert "iPod mini 2nd generation" in page.inner_text("#profileModel")
    rate = '.pf-row[data-field="max_sample_rate"]'
    assert page.get_attribute(rate, "data-source") == "known_model"
    assert page.get_attribute('.pf-row[data-field="has_rtc"]', "data-source") == "detected"
    assert page.locator('.pf-row[data-field="supports_podcasts"]').count() == 0  # music only
    shot(page, "device-profile-1280")

    tracks = '.pf-row[data-field="max_tracks"]'
    page.click(f'{tracks} [data-action="profileEdit"]')
    assert page.is_visible('form.pf-edit[data-field="max_tracks"]')
    page.click('form.pf-edit[data-field="max_tracks"] [data-action="profileCancel"]')
    assert page.is_hidden('form.pf-edit[data-field="max_tracks"]')

    page.click(f'{rate} [data-action="profileEdit"]')
    page.select_option("#pf-max_sample_rate", "96000")
    page.click('form.pf-edit[data-field="max_sample_rate"] button[type="submit"]')
    page.wait_for_selector(f'{rate}[data-source="override"]')
    assert "96 kHz" in page.inner_text(rate)
    over = httpx.get(f"{url}/api/v1/devices/mini", timeout=30).json()["config"]
    assert over["capability_overrides"] == {"max_sample_rate": 96000}

    page.reload()
    page.wait_for_selector(f'{rate}[data-source="override"]')
    page.set_viewport_size({"width": 390, "height": 800})
    assert no_horizontal_scroll(page)
    assert not page.errors
    shot(page, "device-profile-390")
    page.click(f'{rate} [data-action="profileReset"]')
    page.wait_for_selector(f'{rate}[data-source="known_model"]')
    assert page.locator('[data-action="profileResetAll"]').count() == 0

    page.click(f'{tracks} [data-action="profileEdit"]')
    page.fill("#pf-max_tracks", "500")
    page.click('form.pf-edit[data-field="max_tracks"] button[type="submit"]')
    page.wait_for_selector(f'{tracks}[data-source="override"]')
    page.click('[data-action="profileResetAll"]')
    page.wait_for_selector(f'{tracks}[data-source="default"]')
    cfg = httpx.get(f"{url}/api/v1/devices/mini", timeout=30).json()["config"]
    assert cfg["capability_overrides"] == {}
