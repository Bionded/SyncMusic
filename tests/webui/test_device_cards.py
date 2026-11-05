"""Sidebar device cards: used space of an idle device ("used / total" + a thin bar), refreshed after a
sync, replaced by the progress bar during a job; a device without libraries says "Connected · no libraries" even
when its last sync succeeded. 1280 and 390 px. Temp folders only."""

from __future__ import annotations

import os
import shutil

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import open_page, poll, run_on, settle, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return unit
        n /= 1024


@pytest.mark.parametrize("width", [1280, 390])
def test_cards_show_space_and_no_libraries(web_server, chrome, tmp_path, width):
    url, app = web_server
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="A", artist="X", album="Y")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Stick",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "library_ids": ["music"],
    }
    _api(url, "POST", "/devices/register", json=body)
    page = open_page(chrome, url + "/#/", width=width)
    if width < 600:
        page.locator("#burger").click()
    card = page.locator("#sideBottom [data-device-card=stick]")
    card.locator("[data-space]").wait_for(state="attached")  # hidden with the sidebar on a phone
    total = shutil.disk_usage(tmp_path / "stick").total
    assert (
        " / " in card.locator("[data-space]").inner_text()
        and _human(total) in card.locator("[data-space]").inner_text()
    )
    assert (
        card.locator(".space-bar").count() == 1
        and card.locator('[role="progressbar"]').count() == 0
    )
    # a sync writes files: the numbers are asked again after the job (the text itself may not change: a folder
    # device shows its file system's usage, rounded)
    asked: list[str] = []
    page.on(
        "request", lambda r: asked.append(r.url) if r.url.endswith("/devices/stick/space") else None
    )
    big = tmp_path / "music" / "big.mp3"
    make_music(big, title="Big", artist="X", album="Y", seconds=3.0)
    with open(big, "ab") as f:
        f.write(os.urandom(30 * 1024 * 1024))
    _api(url, "POST", "/libraries/music/refresh")
    for _ in range(50):
        plan = _api(url, "POST", "/devices/stick/plan", json={})
        if plan["counts"]["add"] == 2:
            break
    asked.clear()  # from here: the sync and what the page asks after it
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    poll(page, lambda: asked, what="the card to ask for the space again after the sync")
    card.locator("[data-space]").wait_for(state="attached")  # hidden with the sidebar on a phone
    assert "Connected · synced" in card.inner_text()
    # no templates: says so, not "synced"
    _api(url, "PATCH", "/devices/stick", json={"rule_set": {"template_ids": []}})
    page.wait_for_selector("[data-device-card=stick][data-state=no-libraries]", timeout=15_000)
    assert "No templates" in card.inner_text() and "synced" not in card.inner_text()
    assert page.errors == []


def test_partial_card_stays_inside_the_sidebar(web_server, chrome, tmp_path):
    """A long state line ("Last sync partial · some items failed") must wrap inside the card, not widen it past
    the sidebar's right edge (seen in: card 243 px in a 238 px sidebar)."""
    url, app = web_server
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="A", artist="X", album="Y")
    make_music(music / "b.mp3", title="B", artist="X", album="Y")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Stick",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "rule_set": {"template_ids": ["default.music.all"]},  # templates only
    }
    _api(url, "POST", "/devices/register", json=body)
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    # a source that becomes unreadable after the plan makes one item fail → "partial"
    os.chmod(music / "b.mp3", 0)
    try:
        job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
        run_on(app, app.jobs.wait(job["id"]), 60)
    finally:
        os.chmod(music / "b.mp3", 0o644)
    page = open_page(chrome, url + "/#/", width=1280)
    page.wait_for_selector("[data-device-card=stick][data-state=partial]", timeout=15_000)
    settle(page)  # the card is drawn again when its answers arrive: measure the one that stays
    card = page.locator("#sideBottom [data-device-card=stick]").bounding_box()
    side = page.locator(".sidebar").bounding_box()
    assert card and side
    assert card["x"] + card["width"] <= side["x"] + side["width"]


@pytest.mark.parametrize("width", [1280, 390])
def test_card_states_of_design_v2(web_server, chrome, width):
    """An automatic update shows a compact 'Updating…' line and no bar, a failed one
    'Update failed'; while the clock is set: the 'Setting the clock…' badge, an indeterminate bar, 'Ready in a few
    seconds'; an error shows its reason on one line. The device list is faked (no real states needed)."""
    url, _ = web_server
    base = {
        "connected": True,
        "disk": None,
        "mounted_at": None,
        "busy": None,
        "ejecting": False,
        "setting_clock": False,
        "ejected": False,
        "error": None,
        "waiting": None,
        "no_libraries": False,
        "libraries": [],
        "kind": "folder",
        "update": {"state": "ok"},
    }
    devices = [
        {
            **base,
            "id": "upd",
            "name": "Плеєр з дуже довгою назвою для перевірки",
            "type": "flashdrive",
            "busy": "j1",
            "update": {"state": "running"},
        },
        {
            **base,
            "id": "bad",
            "name": "Failed update",
            "type": "flashdrive",
            "update": {"state": "failed"},
        },
        {**base, "id": "clk", "name": "Classic", "type": "ipod", "setting_clock": True},
        {
            **base,
            "id": "err",
            "name": "Broken",
            "type": "flashdrive",
            "error": "mount failed: wrong fs type on /dev/sdz1",
        },
    ]
    page = chrome.new_page(viewport={"width": width, "height": 900})
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.route(
        "**/api/v1/devices", lambda r: r.fulfill(json={"registered": devices, "unknown": []})
    )
    page.route(
        "**/api/v1/jobs/j1",
        lambda r: r.fulfill(
            json={"id": "j1", "kind": "update", "status": "running", "progress": 0.5}
        ),
    )
    page.goto(url + "/#/settings")
    if width < 760:
        page.locator("#burger").click()
        page.wait_for_function("document.querySelector('#sidebar').getBoundingClientRect().x === 0")
    page.wait_for_selector("[data-device-card=err]")
    upd = page.locator("[data-device-card=upd]")
    # an update opens the device, not a job page
    assert upd.get_attribute("href") == "#/devices/upd"
    assert upd.locator("[data-update-line][data-state=running]").inner_text() == "Updating…"
    assert upd.locator(".bar, .space-bar, [data-card-pct]").count() == 0
    bad = page.locator("[data-device-card=bad]")
    assert bad.locator("[data-update-line][data-state=failed]").inner_text() == "Update failed"
    clk = page.locator("[data-device-card=clk]")
    assert clk.get_attribute("data-state") == "clock" and clk.locator(".bar.indet").count() == 1
    assert "Setting the clock…" in clk.inner_text() and "Ready in a few seconds" in clk.inner_text()
    err = page.locator("[data-device-card=err]")
    assert (
        "Error" in err.inner_text()
        and "wrong fs type" in err.locator("[data-card-error]").inner_text()
    )
    # nothing in the cards widens the sidebar; the Devices badge ignores an update
    # measured in one go on the cards in the page: they re-render on device / job events (like 463)
    page.wait_for_function(
        """() => {
          const side = document.querySelector('#sidebar').getBoundingClientRect();
          const cards = [...document.querySelectorAll('#sideBottom [data-device-card]')];
          return cards.length > 0 && cards.every((c) => c.getBoundingClientRect().right <= side.right + 0.5);
        }"""
    )
    assert page.locator("[data-devices-badge]").count() == 0
    shot(page, f"sidebar-states-{width}")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_card_update_now_starts_an_update(web_server, chrome, tmp_path, width):
    """an idle connected card has an "Update now" button beside its link; it starts the device's
    update job (not a navigation) and says so."""
    url, app = web_server
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="A", artist="X", album="Y")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Флешка",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "library_ids": ["music"],
    }
    dev = _api(url, "POST", "/devices/register", json=body)
    page = open_page(chrome, url + "/#/", width=width)
    if width < 600:
        page.locator("#burger").click()
    btn = page.locator(f"#sideBottom [data-update-now={dev['id']}]")
    btn.wait_for()
    shot(page, f"card-update-now-{width}")
    btn.click()
    page.locator(".toast", has_text="Update of Флешка started").wait_for(timeout=10_000)
    assert "#/devices" not in page.url  # the button is not the card's link
    jobs = [j for j in app.jobs.jobs() if j.kind == "update" and j.device_id == dev["id"]]
    assert jobs
    run_on(app, app.jobs.wait(jobs[0].id), 60)
    assert page.errors == []
