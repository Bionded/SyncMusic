"""Overlays and states: the typed confirmation's button stays disabled until the name matches,
the right-hand drawer (Esc / backdrop / focus return), toasts with Undo, the sticky Save / Discard bar with its
leave guard, skeleton / empty / problem / capacity components, and round 2's gentle-mode suggestion with Undo."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.webui.conftest import animations_done, no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _ui(page, script: str):
    """Run `script` with `ui` = the ui.js module (async allowed)."""
    return page.evaluate(f"async () => {{ const ui = await import('/js/ui.js'); {script} }}")


def _ready(page) -> None:
    page.wait_for_selector("#content[data-route='/'] h1")


def test_typed_confirmation_button_waits_for_the_exact_name(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url)
    _ready(page)
    _ui(
        page,
        "window.__typed = ui.typedConfirm({title: 'Wipe Мій плеєр?', message: 'Rockbox stays.', name: 'Мій плеєр', ok: 'Wipe'});",
    )
    ok = page.locator(".modal").get_by_role("button", name="Wipe")
    assert page.locator(".modal .modal-tone svg[data-icon='warn']").count() == 1
    assert page.evaluate("document.activeElement.id") == "typedName"  # focus lands on the input
    assert ok.get_attribute("data-ready") == "false"
    page.locator("#typedName").fill("мій плеєр")
    shot(page, "overlays-typed-confirm")
    assert ok.get_attribute("data-ready") == "false"
    ok.click()  # does nothing but say why
    assert page.locator("#typedName").is_visible()
    page.locator("#typedName").fill("Мій плеєр")
    assert ok.get_attribute("data-ready") == "true"
    ok.click()
    assert page.evaluate("window.__typed") is True
    assert page.errors == []


def test_drawer_traps_focus_closes_and_returns_focus(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url)
    _ready(page)
    page.locator("#bell").focus()
    _ui(
        page,
        """const {raw} = await import('/js/dom.js');
           window.__d = ui.drawer({title: 'Сповіщення', label: 't',
             body: raw('<button class="btn" id="d1">One</button> <button class="btn" id="d2">Two</button>')});
           window.__d.closed.then(() => { window.__closed = (window.__closed || 0) + 1; });""",
    )
    page.wait_for_selector(".drawer[data-drawer='t']")
    assert page.evaluate("document.activeElement.id") == "d1"
    animations_done(page)  # the slide-in animation
    shot(page, "overlays-drawer")
    for _ in range(4):  # Tab cycles inside: d1 → d2 → close → d1 …
        page.keyboard.press("Tab")
        assert page.evaluate("!!document.activeElement.closest('.drawer')")
    page.keyboard.press("Escape")
    page.wait_for_selector(".drawer", state="detached")
    assert page.evaluate("document.activeElement.id") == "bell"
    assert page.evaluate("window.__closed") == 1
    # the backdrop closes it too
    _ui(page, "ui.drawer({title: 'X', body: 'text'});")
    page.mouse.click(20, 400)
    page.wait_for_selector(".drawer", state="detached")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_drawer_closes_on_back_and_on_navigation(web_server, chrome, width):
    """Back closes an open drawer and stays on the page; a screen change closes it; closing it with × leaves no
    extra history entry (one Back then leaves the page)."""
    url, _ = web_server
    page = open_page(chrome, url + "/#/devices", width=width)
    page.wait_for_selector("#content[data-route='/devices'] h1")
    page.locator("#bell").click()
    page.wait_for_selector(".drawer[data-drawer='bell']")
    page.go_back()
    page.wait_for_selector(".drawer", state="detached")
    assert page.evaluate("location.hash") == "#/devices"
    assert page.locator("#content[data-route='/devices'] h1").is_visible()

    page.locator("#bell").click()
    page.wait_for_selector(".drawer[data-drawer='bell']")
    page.evaluate("location.hash = '#/settings'")  # what a sidebar link does
    page.wait_for_selector("#content[data-route='/settings'] h1")
    assert page.locator(".drawer").count() == 0

    page.goto(url + "/#/libraries")
    page.wait_for_selector("#content[data-route='/libraries'] h1")
    page.evaluate("location.hash = '#/devices'")
    page.wait_for_selector("#content[data-route='/devices'] h1")
    page.locator("#bell").click()
    page.wait_for_selector(".drawer[data-drawer='bell']")
    page.locator(".drawer [data-drawer-close]").click()
    page.wait_for_selector(".drawer", state="detached")
    page.wait_for_timeout(200)
    assert page.evaluate("location.hash") == "#/devices"
    page.go_back()
    page.wait_for_selector("#content[data-route='/libraries'] h1")
    assert page.errors == []


def test_toast_with_undo(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url)
    _ready(page)
    _ui(
        page, "ui.toast('Rule added to iPod Classic.', 'ok', {undo: () => { window.__undo = 1; }});"
    )
    t = page.locator(".toast.ok")
    assert t.locator("svg[data-icon='ccheck']").count() == 1
    t.get_by_role("button", name="Undo").click()
    page.wait_for_selector(".toast.ok", state="detached")
    assert page.evaluate("window.__undo") == 1
    _ui(page, "ui.toast('Nope', 'error');")
    assert page.locator(".toast.error svg[data-icon='xcircle']").count() == 1
    assert page.errors == []


def test_unsaved_bar_and_leave_guard(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url)
    _ready(page)
    _ui(
        page,
        """const c = document.querySelector('#content');
           c.insertAdjacentHTML('beforeend', '<form id="f"><input id="name" value="a"></form>');
           window.__saved = 0; window.__discarded = 0;
           window.__bar = ui.unsavedBar(c.querySelector('#f'), {
             save: async () => { window.__saved++; }, discard: () => { window.__discarded++; } });""",
    )
    bar = page.locator(".savebar")
    assert bar.is_hidden()
    page.locator("#name").fill("Назва")
    assert bar.is_visible() and "Unsaved changes" in bar.inner_text()
    # leaving asks; "Cancel" keeps the screen and the address
    page.evaluate("location.hash = '#/devices'")
    dialog = page.locator(".modal")
    dialog.wait_for()
    assert "Leave without saving?" in dialog.inner_text()
    dialog.get_by_role("button", name="Cancel").click()
    page.wait_for_function("location.hash === '#/' || location.hash === ''")
    assert page.locator("#name").input_value() == "Назва" and bar.is_visible()
    # Save: the bar goes, leaving no longer asks
    bar.get_by_role("button", name="Save").click()
    page.wait_for_function("window.__saved === 1")
    assert bar.is_hidden()
    # Discard
    page.locator("#name").fill("b")
    bar.get_by_role("button", name="Discard").click()
    assert page.evaluate("window.__discarded") == 1 and bar.is_hidden()
    # dirty again, leave and confirm: the next screen is shown
    page.locator("#name").fill("c")
    page.evaluate("location.hash = '#/devices'")
    page.locator(".modal").get_by_role("button", name="Discard changes").click()
    page.wait_for_selector("#content[data-route='/devices'] h1")
    page.evaluate("location.hash = '#/'")  # no guard left
    _ready(page)
    assert page.locator(".modal").count() == 0
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_state_components(web_server, chrome, width, theme):
    url, _ = web_server
    page = open_page(chrome, url, width)
    page.evaluate(f"localStorage.setItem('syncmusic.theme', '{theme}')")
    page.reload()
    _ready(page)
    _ui(
        page,
        """const {html, setHtml} = await import('/js/dom.js');
           setHtml(document.querySelector('#content'), html`<div class="grid" id="demo">
             ${ui.skeleton(3)}
             ${ui.emptyState('Plug a player into the Pi.', {title: 'No devices yet', action: html`<a class="btn" href="#/devices">Devices</a>`})}
             ${ui.problemBox({message: 'The FTP server refused the login (530). Nothing was changed.'})}
             ${ui.problemBox({kind: 'warn', message: '38 files will be removed - more than the threshold.', action: html`<button class="btn small">Review</button>`})}
             ${ui.suggestionBox({iconName: 'shield', message: 'Дуже довге речення українською про те, що щадний режим запису може допомогти плеєрам, які зависають.', action: html`<button class="btn small">Turn on gentle write mode</button>`})}
             ${ui.indetBar('Setting the clock')}
             ${ui.capacityBar({capacity: 64e9, parts: {music: 33e9, audiobook: 5e9, podcast: 2.5e9, video: 6e9}})}
           </div>`);""",
    )
    assert page.locator("[data-skeleton] span").count() == 3
    assert page.locator("[data-empty]").inner_text().startswith("No devices yet")
    assert page.locator(".problem.error svg[data-icon='xcircle']").count() == 1
    assert page.locator(".problem.warn svg[data-icon='warn']").count() == 1
    legend = page.locator(".cap-legend").inner_text()
    for word in (
        "Music 33.0 GB",
        "Audiobooks 5.0 GB",
        "Podcasts 2.5 GB",
        "Video 6.0 GB",
        "Free 17.5 GB",
    ):
        assert word in legend
    assert page.locator(".cap-bar").get_attribute("aria-label") == "46.5 GB used of 64.0 GB"
    assert no_horizontal_scroll(page)
    shot(page, f"overlays-states-{width}-{theme}")
    assert page.errors == []


def test_gentle_mode_suggestion_with_undo(web_server, chrome, tmp_path, width=390):
    url, _ = web_server
    (tmp_path / "stick").mkdir()
    r = httpx.post(
        f"{url}/api/v1/devices/register",
        json={"name": "Старий iPod", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"},
        timeout=30,
    )
    assert r.status_code < 400, r.text
    dev = r.json()["id"]
    page = open_page(chrome, url, width)
    # a finished full sync that failed on the device's storage (the report the backend writes)
    job = {
        "id": "j1",
        "kind": "sync",
        "name": "sync Старий iPod",
        "device_id": dev,
        "status": "succeeded",
        "progress": 1.0,
        "message": "",
        "finished_at": "2026-10-23T20:00:00Z",
        "log": [],
        "result": {
            "device_id": dev,
            "status": "failed",
            "counts": {},
            "failures": [],
            "message": "I/O error",
            "suggest_gentle": True,
        },
    }
    page.route(
        "**/api/v1/jobs/j1",
        lambda route: route.fulfill(content_type="application/json", body=json.dumps(job)),
    )
    page.goto(f"{url}/#/devices/{dev}/sync/j1")
    box = page.locator("[data-suggest-gentle]")
    box.wait_for()
    assert box.locator("svg[data-icon='shield']").count() == 1
    box.get_by_role("button", name="Turn on gentle write mode").click()
    page.wait_for_selector(".toast.ok")
    cfg = httpx.get(f"{url}/api/v1/devices/{dev}", timeout=30).json()["config"]
    assert cfg["sync"]["gentle_write"] is True
    assert box.count() == 0
    assert no_horizontal_scroll(page)
    shot(page, "overlays-gentle-toast")
    page.locator(".toast.ok").get_by_role("button", name="Undo").click()
    page.wait_for_selector("text=Gentle write mode is off again")
    cfg = httpx.get(f"{url}/api/v1/devices/{dev}", timeout=30).json()["config"]
    assert cfg["sync"]["gentle_write"] is False
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_unsaved_bar_floats_at_the_bottom_of_the_screen(web_server, chrome, width):
    """pinned to the viewport bottom over the main column, the page keeps room for it."""
    url, _ = web_server
    page = open_page(chrome, url)
    page.set_viewport_size({"width": width, "height": 800})
    _ready(page)
    _ui(
        page,
        """const c = document.querySelector('#content');
           c.insertAdjacentHTML('afterbegin', '<form id="f"><input id="name" value="a"></form>');
           ui.unsavedBar(c.querySelector('#f'), { save: async () => {}, discard: () => {} });""",
    )
    page.locator("#name").fill("Назва")
    bar = page.locator(".savebar")
    box = bar.bounding_box()
    assert box is not None and 800 - 30 <= box["y"] + box["height"] <= 800
    assert bar.evaluate("e => getComputedStyle(e).position") == "fixed"
    if width == 1280:
        assert box["x"] >= 256  # not under the sidebar
    else:
        assert box["x"] <= 16 and box["x"] + box["width"] >= width - 16
    assert page.evaluate("document.body.classList.contains('has-savebar')")
    assert no_horizontal_scroll(page)
    shot(page, f"savebar-{width}")
    bar.get_by_role("button", name="Save").click()
    page.wait_for_function("!document.body.classList.contains('has-savebar')")
    assert page.errors == []
