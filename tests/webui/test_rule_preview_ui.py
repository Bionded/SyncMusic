"""Live rule preview on the device Rules tab: linking templates and the device limit update the counts,
size and space within ~1 s. A device has templates only: rules are edited in the template editor, which
has the presets."""

from __future__ import annotations

import os
import shutil
import time

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import (
    add_templates,
    discard_templates,
    no_horizontal_scroll,
    open_page,
    remove_template,
    settle,
    shot,
)

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
    for name, criteria in (
        ("All", {"all": []}),
        ("Скрябін", {"all": [{"is": {"artist": "Скрябін"}}]}),
    ):
        rules = [{"id": name.lower(), "name": name, "criteria": criteria}]
        _api(
            url, "POST", "/templates", json={"name": name, "library_ids": ["music"], "rules": rules}
        )
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Stick",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "rule_set": {"template_ids": ["all"]},
    }
    _api(url, "POST", "/devices/register", json=body)
    return url, tmp_path / "stick"


def _matches(page):
    return page.locator("[data-preview-matches]").inner_text()


def test_preview_follows_the_edits(device, chrome):
    url, stick = device
    page = open_page(chrome, url + "/#/devices/stick/rules")
    page.wait_for_selector("[data-preview-matches]")
    assert _matches(page).startswith("Matches 3 items")
    assert "Free after the sync" in page.locator("[data-preview-space]").inner_text()
    assert "reserved for the device database" in page.locator("[data-preview-reserve]").inner_text()
    assert page.locator("[data-preview-badge]").inner_text().endswith("fits")
    # linking another template and unlinking "All": the preview follows
    add_templates(page, "скрябін")
    t0 = time.monotonic()
    remove_template(page, "all")
    page.wait_for_function(
        "document.querySelector('[data-preview-matches]')?.textContent.startsWith('Matches 2 items')",
        timeout=3000,
    )
    assert time.monotonic() - t0 < 1.5
    assert page.locator("[data-rules] [data-tpl-row]").evaluate_all(
        "rs => rs.map((r) => r.dataset.tplRow)"
    ) == ["скрябін"]
    # the device limit is part of the preview
    page.fill("#limitTracks", "1")
    page.wait_for_selector("[data-preview-left-out]:has-text('1 left out by the device limit')")
    # what the template loses to the limit, on its row; the device after the sync by kind
    page.wait_for_selector("[data-tpl-cut]:visible:has-text('left out by the device limit')")
    assert "Music" in page.locator("[data-preview-bar]").inner_text()
    assert _matches(page).startswith("Matches 1 item ")
    page.locator("[data-preview] summary").click()
    shot(page, "rule-preview-1280")
    assert _api(url, "GET", "/devices/stick")["config"]["rule_set"]["template_ids"] == [
        "all"
    ]  # nothing saved
    page.set_viewport_size({"width": 390, "height": 800})
    discard_templates(page)  # a reload would ask about the unsaved changes
    page.reload()
    page.wait_for_selector("[data-preview-matches]")
    page.locator("[data-preview]").scroll_into_view_if_needed()
    if folder := os.environ.get("SYNCMUSIC_SHOTS"):
        page.screenshot(path=os.path.join(folder, "rule-preview-390.png"))
    assert no_horizontal_scroll(page)
    # unplugged: the library's numbers, no space
    shutil.rmtree(stick)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.fill("#limitTracks", "1")
    page.wait_for_selector("[data-preview-badge]:has-text('device not connected')")
    # Discard dropped the unsaved links ("All" again); offline the device limit does not count
    assert _matches(page).startswith("Matches 3 items")
    assert page.errors == []


def test_the_first_matches_stay_open(device, chrome):
    """Opening "First N matches" must not trigger a new preview that re-renders the card and closes the list."""
    url, _ = device
    page = open_page(chrome, url + "/#/devices/stick/rules")
    page.wait_for_selector("[data-preview-matches]")
    settle(page)  # the first preview has settled
    page.locator("[data-preview] summary").click()
    settle(page)  # past the 500 ms debounce and a request it would start
    assert page.locator("[data-preview] details").evaluate("d => d.open")
    assert page.errors == []


def test_a_preset_is_added_in_one_click(device, chrome):
    """ "From a preset" in the template editor adds a ready rule (with its parameter); "These artists or
    albums" goes through the library picker; Save stores them in the template."""
    url, _ = device
    page = open_page(chrome, url + "/#/templates/скрябін")
    page.wait_for_selector('[data-rule="rules:0"]')
    # a chip: its parameter first
    page.locator("[data-preset-chip='new']").click()
    dlg = page.locator(".modal")
    dlg.locator("[data-preset='new'] input[type=radio]").check()
    days = dlg.get_by_label("New in the library: Days")
    days.fill("0")
    dlg.get_by_role("button", name="Add the rule").click()
    assert days.get_attribute("aria-invalid") is not None  # refused, the dialog stays
    days.fill("30")
    shot(page, "rule-presets-1280")
    dlg.get_by_role("button", name="Add the rule").click()
    page.wait_for_selector('[data-rule="rules:1"]')
    assert page.locator('[data-rule="rules:1"] [data-k="name"]').input_value() == (
        "Added in the last 30 days"
    )
    page.locator('[data-rule="rules:1"] [data-action="rbRemoveRule"]').click()
    page.locator("[data-preset-chip='picked']").click()
    page.locator(".modal [data-preset='picked'] input[type=radio]").check()
    page.get_by_role("button", name="Add the rule").click()
    page.wait_for_selector("[data-picker] .pick-row:has-text('Тартак')")
    page.locator("[data-picker] .pick-row:has-text('Тартак') input").check()
    page.get_by_role("button", name="Add 1 pick").click()
    page.wait_for_selector('[data-rule="rules:1"]')
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    rules = _api(url, "GET", "/templates/скрябін")["rules"]
    assert rules[1]["name"] == "These artists or albums"
    assert rules[1]["criteria"] == {"any": [{"is": {"artist": "Тартак"}}]}
    assert rules[1]["kind"] == ["music"]
    page.goto(url + "/#/templates/скрябін")
    page.wait_for_selector('[data-rule="rules:0"]')
    page.set_viewport_size({"width": 390, "height": 800})
    page.locator("[data-preset-chip='new']").click()
    page.wait_for_selector(".modal .preset-list")
    assert no_horizontal_scroll(page)
    if folder := os.environ.get("SYNCMUSIC_SHOTS"):
        page.screenshot(path=os.path.join(folder, "rule-presets-390.png"))
    assert page.errors == []


def test_the_latest_episodes_preset_can_be_edited_in_the_builder(device, chrome):
    """the dialog promises "change it there like any other": the latestEpisodes rule is a builder row
    whose number of episodes can be changed and saved."""
    url, _ = device
    page = open_page(chrome, url + "/#/templates/all")
    page.wait_for_selector('[data-rule="rules:0"]')
    page.locator("[data-preset-chip='latest-episodes']").click()
    page.locator(".modal [data-preset='latest-episodes'] input[type=radio]").check()
    page.get_by_role("button", name="Add the rule").click()
    page.wait_for_selector('[data-rule="rules:1"]')
    assert page.locator('[data-rule="rules:1"] .warningbox').count() == 0
    row = page.locator('[data-rule="rules:1"] [data-cond]')
    assert row.locator('[data-c="field"]').input_value() == "latest_episodes"
    assert "episodes of every podcast" in row.inner_text()
    row.locator('[data-c="value"]').fill("7")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    rules = _api(url, "GET", "/templates/all")["rules"]
    assert rules[1]["criteria"] == {"all": [{"latestEpisodes": {"per_series": 7}}]}, rules[1]
    # "not played yet" rides along and is read back ticked
    page.goto(url + "/#/templates/all")
    row = page.locator('[data-rule="rules:1"] [data-cond]')
    row.locator('[data-c="unplayed"]').check()
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    rules = _api(url, "GET", "/templates/all")["rules"]
    assert rules[1]["criteria"] == {
        "all": [{"latestEpisodes": {"per_series": 7, "unplayed": True}}]
    }
    page.goto(url + "/#/templates/all")
    assert page.locator('[data-rule="rules:1"] [data-cond] [data-c="unplayed"]').is_checked()
    assert no_horizontal_scroll(page)
    shot(page, "latest-episodes-row-1280")
    assert page.errors == []


_OVERLAPS = """() => {
  const p = document.querySelector('.rules-preview').getBoundingClientRect();
  return [...document.querySelectorAll('.rules-main > .card')].filter((c) => {
    const b = c.getBoundingClientRect();
    return b.left < p.right && b.right > p.left && b.top < p.bottom && b.bottom > p.top;
  }).length;
}"""


@pytest.mark.parametrize("width", [1440, 1280, 390])
def test_preview_never_covers_a_card(device, chrome, width):
    """the preview was a sticky card at the bottom that floated over the template list. Wide: a column
    beside the cards that stays in view; narrower: the last card."""
    url, _ = device
    page = open_page(chrome, url + "/#/devices/stick/rules", width, 700)
    page.wait_for_selector("[data-preview-matches]")
    for y in (0, 300, 800, 5000):
        page.evaluate(f"window.scrollTo(0, {y})")
        settle(page, quiet_ms=0)  # the sticky preview has followed the scroll
        assert page.evaluate(_OVERLAPS) == 0, f"scrolled to {y}"
    if width >= 1360:  # still in view at the bottom of the page
        top = page.evaluate("document.querySelector('.rules-preview').getBoundingClientRect().top")
        assert 0 <= top < 100
    assert no_horizontal_scroll(page)
    shot(page, f"rules-preview-{width}")
    page.close()
