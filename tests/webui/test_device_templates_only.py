"""a device gets templates (and own rules) only; they decide its libraries. No Libraries tab, no
library step when registering; an own rule chooses the libraries it draws from."""

from __future__ import annotations

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import (
    add_templates,
    no_horizontal_scroll,
    open_page,
    remove_template,
    save_templates,
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
    # a long Cyrillic library name: its group head must not widen the tab on the phone
    other = "Папка Тест - дуже довга назва бібліотеки"
    for lid, name, artists in (
        ("music", "Music", ("Скрябін", "Тартак")),
        ("other", other, ("ДахаБраха",)),
    ):
        root = tmp_path / lid
        for artist in artists:
            make_music(root / artist / "a.mp3", title=f"{artist} song", artist=artist, album="A")
        body = {"id": lid, "type": "folder", "name": name, "settings": {"root": str(root)}}
        _api(url, "POST", "/libraries", json=body)
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Stick",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "rule_set": {"template_ids": ["default.music.all"]},
    }
    _api(url, "POST", "/devices/register", json=body)
    return url


def _matches(page, n):
    page.wait_for_function(
        f"document.querySelector('[data-preview-matches]')?.innerText.startsWith('Matches {n} item')"
    )


@pytest.mark.parametrize("width", [1280, 390])
def test_templates_decide_the_libraries(device, chrome, width):
    url = device
    page = open_page(chrome, url + "/#/devices/register", width=width)
    page.wait_for_selector("#regTemplates")
    assert page.locator('input[name="lib"]').count() == 0  # no library step
    assert "Libraries" not in page.locator("#content form").inner_text()

    page.goto(url + "/#/devices/stick")
    page.wait_for_selector(".tabs")
    assert page.locator('.tabs a:has-text("Libraries")').count() == 0
    assert page.locator("#content [data-no-libraries]").count() == 0

    page.locator('.tabs a:has-text("Templates")').click()
    page.wait_for_selector("[data-preview-matches]")
    _matches(page, 2)
    # linking the Other library's default template brings its items
    add_templates(page, "default.other.all")
    _matches(page, 3)
    save_templates(page)
    assert _api(url, "GET", "/devices/stick")["libraries"] == ["music", "other"]

    # templates only: no rule builder on the device, each template has an Edit link
    page.reload()
    page.wait_for_selector("[data-preview-matches]")
    assert page.locator("[data-rules] [data-rb]").count() == 0
    assert page.get_by_role("button", name="Add a rule").count() == 0
    edit = page.locator('[data-tpl-row="default.other.all"] a')
    assert edit.get_attribute("href") == "#/templates/default.other.all"
    # one group per library, the global priority across them
    assert page.locator("[data-rules] [data-group]").count() == 2
    assert page.locator("[data-rules] .dt-rank b").all_inner_texts() == ["1", "2"]
    assert no_horizontal_scroll(page)
    # the rows keep the page margin on the right like the cards below
    right = page.evaluate(
        "Math.max(...[...document.querySelectorAll('[data-rules] .dt-row')].map(e => e.getBoundingClientRect().right))"
    )
    assert right <= width - 16 + 0.5, right
    shot(page, f"device-templates-only-{width}")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_the_picker_adds_removes_and_saves(device, chrome, width):
    """Add template opens a searchable drawer grouped by library with the
    linked ones greyed; × removes with Undo; nothing is stored before Save; Discard brings the saved list back."""
    url = device
    page = open_page(chrome, url + "/#/devices/stick/rules", width=width)
    page.wait_for_selector('[data-tpl-row="default.music.all"]')
    assert page.locator(".savebar").is_hidden()
    page.locator('[data-tpls] [data-action="rulesAdd"]').first.click()
    drawer = page.locator('[data-drawer="tplpick"]')
    drawer.wait_for()
    assert drawer.locator('[data-pick="default.music.all"]').is_disabled()  # linked already
    assert "Added" in drawer.locator('[data-pick="default.music.all"]').inner_text()
    page.fill("#tplPickQ", "zzz")
    drawer.locator(".dt-pick-none").wait_for()
    page.fill("#tplPickQ", "")
    assert drawer.locator('[data-pick-group="other"]').count() == 1
    assert no_horizontal_scroll(page)
    shot(page, f"device-templates-picker-{width}")
    drawer.locator('[data-pick="default.other.all"]').click()
    assert drawer.locator('[data-pick="default.other.all"]').is_disabled()
    drawer.locator("[data-pick-done]").click()
    _matches(page, 3)
    assert page.locator(".savebar").is_visible()
    assert _api(url, "GET", "/devices/stick")["config"]["rule_set"]["template_ids"] == [
        "default.music.all"
    ]  # nothing stored yet
    # × with Undo
    remove_template(page, "default.music.all")
    _matches(page, 1)
    page.locator(".toast [data-toast-undo]").last.click()
    page.wait_for_selector('[data-rules] [data-tpl-row="default.music.all"]')
    _matches(page, 3)
    # Discard: the saved list again
    page.locator('[data-savebar="discard"]').click()
    page.wait_for_selector('[data-rules] [data-tpl-row="default.other.all"]', state="detached")
    _matches(page, 2)
    add_templates(page, "default.other.all")
    save_templates(page)
    rs = _api(url, "GET", "/devices/stick")["config"]["rule_set"]
    assert rs["template_ids"] == ["default.music.all", "default.other.all"]
    assert no_horizontal_scroll(page)
    shot(page, f"device-templates-{width}")
    assert page.errors == []


def test_a_device_without_templates_says_so(device, chrome):
    url = device
    _api(url, "PATCH", "/devices/stick", json={"rule_set": {"template_ids": []}})
    page = open_page(chrome, url + "/#/devices/stick")
    page.wait_for_selector("#content [data-no-libraries]")
    assert "No template takes media" in page.inner_text("#content [data-no-libraries]")
    assert page.locator('#content [data-no-libraries] a[href="#/devices/stick/rules"]').count() == 1
    card = page.locator('[data-device-card="stick"]')
    page.wait_for_selector('[data-device-card="stick"][data-state="no-libraries"]')
    assert "No templates" in card.inner_text()  # the short 648.1 badge
    page.goto(url + "/#/devices/stick/rules")
    page.wait_for_selector("[data-preview]")
    settle(page)
    assert page.errors == []  # the preview of nothing is no 400
    # after the first template is saved the header follows at once (badge gone, Sync enabled)
    head = page.locator("[data-device-head]")
    assert "No templates" in head.inner_text()
    add_templates(page, "default.music.all")
    save_templates(page)
    page.wait_for_function(
        "!document.querySelector('[data-device-head]').innerText.includes('No templates')"
    )
    assert head.locator('a[data-sync][href="#/devices/stick/plan"]').count() == 1
    assert page.errors == []


def test_picks_show_as_the_devices_own_template(device, chrome):
    """ "Add to device" puts the picks into "<Device> - own", linked and marked Own on the Rules tab; the
    priority list names its Manual selection rule; Save keeps it linked."""
    url = device
    items = _api(url, "GET", "/libraries/other/items")["items"]
    _api(
        url,
        "POST",
        "/devices/stick/manual-selection",
        json={"library_id": "other", "ids": [items[0]["item_id"]]},
    )
    page = open_page(chrome, url + "/#/devices/stick/rules")
    row = page.locator('[data-tpl-row="own-stick"]')
    row.wait_for()
    assert "Stick - own" in row.inner_text() and "Own" in row.inner_text()
    assert "Picked for this device or removed from it" in row.inner_text()
    _matches(page, 3)  # "All music" 2 + the pick from Other
    assert (
        row.locator(".dt-rank b").inner_text() == "2"
    )  # after "All music", in its own library's group
    assert _api(url, "GET", "/devices/stick")["config"]["rule_set"]["template_ids"] == [
        "default.music.all",
        "own-stick",
    ]
    shot(page, "device-own-template-1280")
    page.set_viewport_size({"width": 390, "height": 800})
    page.reload()
    row.wait_for()
    assert no_horizontal_scroll(page)
    shot(page, "device-own-template-390")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_the_templates_list_shows_device_picks_apart(device, chrome, width):
    """the device's own template is listed under "Device picks" with a badge to its device - not as an
    "All libraries" template - has no Copy, and its delete dialog says what deleting it does."""
    url = device
    items = _api(url, "GET", "/libraries/other/items")["items"]
    body = {"library_id": "other", "ids": [items[0]["item_id"]]}
    _api(url, "POST", "/devices/stick/manual-selection", json=body)
    page = open_page(chrome, url + "/#/templates", width=width)
    row = page.locator('[data-tpl="own-stick"]')
    row.wait_for()
    group = page.locator('[data-tpl-group="picks"]')
    assert (
        "Device picks" in group.text_content()
        and group.locator('[data-tpl="own-stick"]').count() == 1
    )
    text = row.inner_text()
    assert "All libraries" not in text and "Picks of Stick" in text
    assert "added to or removed from Stick by hand" in text
    assert row.locator('[data-action="copy"]').count() == 0
    assert row.locator('a[href="#/devices/stick"]').count() == 1
    assert no_horizontal_scroll(page)
    shot(page, f"templates-device-picks-{width}")
    row.locator('[data-action="remove"]').click()
    assert "come back at its next sync" in page.locator(".dialog, [role=dialog]").first.inner_text()
    assert page.errors == []


def test_picks_already_in_a_template_above_are_not_no_matches(device, chrome):
    """the device's own picks below All music of the same library say they are already brought by a
    template above, not "No matches"."""
    url = device
    items = _api(url, "GET", "/libraries/music/items")
    rows = items["items"] if isinstance(items, dict) else items
    first = next(r for r in rows if r.get("item_id"))
    _api(
        url,
        "POST",
        "/devices/stick/manual-selection",
        json={"library_id": "music", "ids": [first["item_id"]]},
    )
    assert "own-stick" in _api(url, "GET", "/devices/stick")["config"]["rule_set"]["template_ids"]
    page = open_page(chrome, url + "/#/devices/stick/rules")
    page.wait_for_function(
        "document.querySelector('[data-tpl-sum=\"own-stick\"]')?.textContent.includes('already brought by a template above')"
    )
    assert page.locator('[data-tpl-sum="own-stick"]').text_content().startswith("1 item")
    assert "No matches" not in page.locator('[data-tpl-sum="own-stick"]').text_content()
    assert page.errors == []
