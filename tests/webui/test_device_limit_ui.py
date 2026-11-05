"""Device limit with a fill order: the Rules tab saves 'at most N tracks / GB' and the fill order, the
plan lists what the limit left out."""

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


@pytest.fixture
def device(web_server, tmp_path):
    url, _ = web_server
    music = tmp_path / "music"
    for artist, title in (("Скрябін", "Група крові"), ("Скрябін", "Зозуля"), ("Тартак", "Орбіт")):
        make_music(music / artist / f"{title}.mp3", title=title, artist=artist, album="A")
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
            "library_ids": ["music"],  # links "All music"
        },
    )
    return url


def test_limit_saved_on_the_rules_tab_and_shown_in_the_plan(device, chrome):
    url = device
    page = open_page(chrome, url + "/#/devices/stick/rules")
    page.wait_for_selector("[data-limit]")
    assert page.input_value("#limitTracks") == "" and page.input_value("#fillOrder") == "priority"
    page.fill("#limitTracks", "2")
    page.fill("#limitGb", "1.5")
    page.select_option("#fillOrder", "random")
    shot(page, "device-limit-rules-1280")
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector(".toast:has-text('Saved')")
    rs = _api(url, "GET", "/devices/stick")["config"]["rule_set"]
    assert (rs["limit_tracks"], rs["limit_bytes"], rs["fill_order"]) == (
        2,
        1_500_000_000,
        ["random"],
    )
    page.reload()
    page.wait_for_selector("[data-limit]")
    assert page.input_value("#limitTracks") == "2" and page.input_value("#limitGb") == "1.5"
    assert page.input_value("#fillOrder") == "random"

    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_selector("[data-limited]")
    assert page.locator('[data-count="add"] strong').inner_text() == "2"
    card = page.locator("[data-limited]").inner_text()
    assert "Left out by the device limit (1)" in card and "2 tracks" in card
    assert "Filled first with: Random (stable per device)" in card  # the Rules tab label
    page.locator("[data-limited]").get_by_role("button", name="Show them").click()
    assert page.locator("[data-plan-item] [data-reason]").all_inner_texts() == [
        "Left out by the device limit"
    ]
    shot(page, "device-limit-plan-1280")
    page.set_viewport_size({"width": 390, "height": 800})
    page.reload()
    page.wait_for_selector("[data-limited]")
    shot(page, "device-limit-plan-390")
    assert no_horizontal_scroll(page)

    # clearing the fields removes the limit
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(url + "/#/devices/stick/rules")
    page.wait_for_selector("[data-limit]")
    page.fill("#limitTracks", "")
    page.fill("#limitGb", "")
    page.select_option("#fillOrder", "priority")
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector(".toast:has-text('Saved')")
    rs = _api(url, "GET", "/devices/stick")["config"]["rule_set"]
    assert (rs["limit_tracks"], rs["limit_bytes"], rs["fill_order"]) == (None, None, [])
    assert page.errors == []
