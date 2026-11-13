"""Device header + Overview: capacity split by media kind with Sync beside the legend, the Last
full sync card with count tiles and the gentle-mode suggestion, the header's update line with Update now, the progress
strip of a running sync, the clock state that disables Sync and Player contents, and the danger zone in Device
settings. States the fixture cannot reach (a running job, setting the clock, a storage failure) are routed."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _synced_stick(url, app, tmp_path) -> str:
    for i in range(3):
        make_music(tmp_path / "music" / f"{i}.mp3", title=f"Пісня {i}", artist="Океан Ельзи")
    body = {"id": "music", "type": "folder", "name": "Музика"}
    _api(url, "POST", "/libraries", json={**body, "settings": {"root": str(tmp_path / "music")}})
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Довга назва флешки Океан Ельзи",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "rule_set": {"template_ids": ["default.music.all"]},
    }
    dev = _api(url, "POST", "/devices/register", json=body)["id"]
    plan = _api(url, "POST", f"/devices/{dev}/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    run_on(app, app.jobs.wait(job["id"]), 60)
    return dev


def _patch_live(page, dev, **fields):
    """GET /devices with this device's live state changed (busy, setting_clock, …)."""

    def handle(route):
        r = route.fetch()
        data = r.json()
        for d in data["registered"]:
            if d["id"] == dev:
                d.update(fields)
        route.fulfill(response=r, json=data)

    page.route(re.compile(r"/api/v1/devices$"), handle)


@pytest.mark.parametrize("width", [1280, 390])
def test_overview_capacity_last_sync_and_header(web_server, chrome, tmp_path, width):
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, f"{url}/#/devices/{dev}", width=width)
    head = page.locator("[data-device-head]")
    head.wait_for()
    assert "Довга назва флешки Океан Ельзи" in head.locator("h1").inner_text()
    assert head.locator("[data-sync]").inner_text().strip() == "Sync"
    # capacity: music from the manifest, the rest 'Other', free hatched; Sync at the legend's right
    page.locator("[data-capacity]").wait_for()
    legend = page.locator(".cap-legend").inner_text()
    assert "Music" in legend and "Free" in legend
    assert page.locator('.cap-bar [data-kind="music"]').count() == 1
    assert page.locator(".cap-legend [data-cap-sync]").count() == 1
    # the last full sync: tiles and Full report → the job
    card = page.locator("[data-last-sync]")
    assert card.get_attribute("data-last-sync") == "succeeded"
    tiles = card.locator(".count-tile").all_inner_texts()
    assert [x.split("\n")[1] for x in tiles] == ["Added", "Replaced", "Tags", "Removed"]
    assert tiles[0].startswith("+3")
    assert "/sync/" in card.locator("[data-full-report]").get_attribute("href")
    # the tabs in the design's order, Templates on the v1 route
    tabs = page.locator(".dev-tabs .tab").all_inner_texts()
    assert tabs == [
        "Overview",
        "Templates",
        "Processors",
        "Player contents",
        "Settings",
        "History",
    ]
    # Update now starts the automatic update
    events = []
    page.on("request", lambda r: r.method == "POST" and events.append(r.url))
    page.locator('[data-action="updateNow"]').click()
    page.wait_for_selector(".toast:has-text('Updating')")
    assert any(u.endswith(f"/devices/{quote(dev)}/update") for u in events), events
    assert no_horizontal_scroll(page)
    shot(page, f"device-overview-{width}")
    # the danger zone lives in Device settings
    assert page.locator(".dangerzone").count() == 0
    page.goto(f"{url}/#/devices/{dev}/settings")
    zone = page.locator("#formatZone")
    zone.wait_for()
    assert zone.locator('[data-action="remove"]').count() == 1
    assert str(tmp_path / "stick") in zone.locator("[data-identity]").inner_text()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_running_sync_shows_the_progress_strip(web_server, chrome, tmp_path):
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, url, 1280)
    _patch_live(page, dev, busy="jX")
    job = {"id": "jX", "kind": "sync", "name": "Sync", "device_id": dev, "status": "running",
           "progress": 0.38, "message": "Converting Пісня 1", "eta_s": 760, "log": []}  # fmt: skip
    page.route("**/api/v1/jobs/jX", lambda r: r.fulfill(json=job))
    page.goto(f"{url}/#/devices/{dev}")
    strip = page.locator("[data-progress-strip]")
    strip.wait_for()
    assert strip.inner_text().startswith("38% · Converting Пісня 1")
    assert strip.get_attribute("href").endswith(f"/devices/{quote(dev)}/sync/jX")
    head = page.locator("[data-device-head]")
    assert head.locator("[data-sync]").inner_text().strip() == "Show progress"
    assert "Updates wait until the full sync ends" in head.inner_text()
    assert page.locator("[data-capacity]").count() == 0  # capacity while idle only
    shot(page, "device-overview-syncing")


@pytest.mark.parametrize("width", [1280, 390])
def test_clock_state_disables_sync_and_player_contents(web_server, chrome, tmp_path, width):
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, url, width)
    _patch_live(page, dev, setting_clock=True)
    page.goto(f"{url}/#/devices/{dev}")
    note = page.locator("[data-clock-note]")
    note.wait_for()
    assert "Sync and Player contents unlock then" in note.inner_text()
    assert page.locator("[data-device-head] [data-sync]").get_attribute("aria-disabled") == "true"
    contents = page.locator('.dev-tabs .tab:has-text("Player contents")')
    assert (
        contents.get_attribute("aria-disabled") == "true" and contents.get_attribute("href") is None
    )
    assert no_horizontal_scroll(page)
    shot(page, f"device-overview-clock-{width}")


def test_storage_failure_offers_gentle_mode_on_the_overview(web_server, chrome, tmp_path):
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, url, 390)

    def handle(route):
        r = route.fetch()
        data = r.json()
        if "last_report" not in data:  # GET /devices/diagnostics …
            route.fulfill(response=r)
            return
        data["last_report"].update(
            status="failed",
            suggest_gentle=True,
            counts={"add": 86},
            failures=[{"ref": "x", "item": "Океан Ельзи - Без бою", "step": "put", "error": "[Errno 5] I/O error"}],
        )  # fmt: skip
        route.fulfill(response=r, body=json.dumps(data), content_type="application/json")

    page.route(re.compile(r"/api/v1/devices/[^/?]+$"), handle)
    page.goto(f"{url}/#/devices/{dev}")
    card = page.locator('[data-last-sync="failed"]')
    card.wait_for()
    assert "put · Океан Ельзи - Без бою: [Errno 5] I/O error" in card.inner_text()
    assert card.locator(".count-tile").last.inner_text().split("\n") == ["1", "Failed"]
    card.get_by_role("button", name="Turn on gentle write mode").click()
    page.wait_for_selector(".toast.ok")
    assert _api(url, "GET", f"/devices/{dev}")["config"]["sync"]["gentle_write"] is True
    assert no_horizontal_scroll(page)
    shot(page, "device-overview-gentle")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


BOOKS = [
    {
        "key": "abs/b1:1",
        "library_id": "abs",
        "title": "Дуже довга назва аудіокниги, що не вміщається в один рядок на телефоні",
        "author": "Ліна Костенко",
        "position_s": 4200.0,
        "library_position_s": 300.0,
        "duration_s": 36000.0,
        "state": "device_ahead",
        "diff_s": 3900.0,
    },
    {
        "key": "abs/b2:1",
        "library_id": "abs",
        "title": "Кобзар",
        "author": "",
        "position_s": 710.0,
        "library_position_s": 700.0,
        "duration_s": 1000.0,
        "state": "in_sync",
        "diff_s": 0.0,
    },
    {
        "key": "abs/b3:1",
        "library_id": "abs",
        "title": "Book",
        "author": "A",
        "position_s": 10.0,
        "library_position_s": 500.0,
        "duration_s": 1000.0,
        "state": "library_ahead",
        "diff_s": 490.0,
    },
]


@pytest.mark.parametrize("width", [1280, 390])
def test_overview_audiobooks_in_progress(web_server, chrome, tmp_path, width):
    """the card lists the books with a bar and who is ahead; absent when the device has none."""
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, f"{url}/#/devices/{dev}", width=width)
    page.locator("[data-last-sync]").wait_for()
    assert page.locator("[data-books]").count() == 0  # a flash drive keeps no bookmarks
    page.route(re.compile(r"/api/v1/devices/[^/]+/positions$"), lambda r: r.fulfill(json=BOOKS))
    page.reload()
    card = page.locator("[data-books]")
    card.wait_for()
    assert "Audiobooks in progress" in card.locator("h2").inner_text()
    states = card.locator(".dev-book-state").all_inner_texts()
    assert states == ["Device ahead · 1 h 5 min", "In sync · 71%", "Library ahead · will update"]
    title = card.locator(".dev-book-title").first
    # never wrapped: one line at every width; on a phone the long title is cut with an ellipsis (at 1280 the
    # full-width card has room for it)
    one_line = "e => e.getClientRects().length === 1 && e.offsetHeight < 2 * parseFloat(getComputedStyle(e).lineHeight || 20)"
    assert title.evaluate(one_line)
    if width < 600:
        assert title.evaluate("e => e.scrollWidth > e.clientWidth")
    no_horizontal_scroll(page)
    shot(page, f"t420-books-{width}")


def test_hiby_overview_says_reboot_after_unplugging(web_server, chrome, tmp_path):
    """a HiBy's Last full sync card tells to reboot the player after unplugging; others do not."""
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, url, 390)
    page.goto(f"{url}/#/devices/{dev}")
    page.locator('[data-last-sync="succeeded"]').wait_for()
    assert page.locator("[data-hiby-reboot]").count() == 0

    def handle(route):
        r = route.fetch()
        data = r.json()
        if "config" in data:
            data["config"]["type"] = "hiby"
        route.fulfill(response=r, body=json.dumps(data), content_type="application/json")

    page.route(re.compile(r"/api/v1/devices/[^/?]+$"), handle)
    page.reload()
    hint = page.locator('[data-last-sync="succeeded"] [data-hiby-reboot]')
    hint.wait_for()
    assert "Reboot the player after unplugging" in hint.inner_text()
    assert no_horizontal_scroll(page)
    shot(page, "device-overview-hiby-reboot")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_hiby_tips_card_collapses_and_remembers(web_server, chrome, tmp_path, width):
    """a HiBy's Overview has the Tips card (open first, stays collapsed once closed), the Settings tab links to it;
    a flash drive has neither."""
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, url, width)
    page.goto(f"{url}/#/devices/{dev}")
    page.locator('[data-last-sync="succeeded"]').wait_for()
    assert page.locator("[data-device-tips]").count() == 0

    def handle(route):
        r = route.fetch()
        data = r.json()
        if "config" in data:
            data["config"]["type"] = "hiby"
        route.fulfill(response=r, body=json.dumps(data), content_type="application/json")

    page.route(re.compile(r"/api/v1/devices/[^/?]+$"), handle)
    page.reload()
    card = page.locator('[data-device-tips="hiby"]')
    card.wait_for()
    assert card.get_attribute("open") is not None
    assert card.locator("[data-tip]").count() == 5
    assert "Detect changes" in card.inner_text()
    # the reboot hint lives in the Last full sync card only
    assert "Reboot" not in card.inner_text()
    assert "sync report warns" in card.inner_text()
    assert "04-devices.md#tips-for-the-hiby-r1" in card.locator("a").get_attribute("href")
    assert no_horizontal_scroll(page)
    shot(page, f"device-overview-hiby-tips-{width}")
    card.locator("summary").click()
    page.wait_for_function(
        "JSON.parse(localStorage.getItem('deviceTipsClosed') || '[]').includes('hiby')"
    )
    page.reload()
    page.locator('[data-device-tips="hiby"]').wait_for()
    assert page.locator('[data-device-tips="hiby"]').get_attribute("open") is None
    page.goto(f"{url}/#/devices/{dev}/settings")
    page.locator("[data-tips-link]").click()
    page.locator('[data-device-tips="hiby"]').wait_for()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_compas_tips_card(web_server, chrome, tmp_path, width):
    """a HiBy R1 with Compás has its own Tips card (storage mode, library update after unplugging, plugins)."""
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, url, width)

    def handle(route):
        r = route.fetch()
        data = r.json()
        if "config" in data:
            data["config"]["type"] = "hiby_compas"
        route.fulfill(response=r, body=json.dumps(data), content_type="application/json")

    page.route(re.compile(r"/api/v1/devices/[^/?]+$"), handle)
    page.goto(f"{url}/#/devices/{dev}")
    card = page.locator('[data-device-tips="hiby_compas"]')
    card.wait_for()
    assert card.locator("[data-tip]").count() == 4
    text = card.inner_text()
    assert "Update Music Database" in text and "Podcasts plugin" in text and "tips." not in text
    assert "04-devices.md#hiby-r1-compás" in card.locator("a").get_attribute("href")
    assert (
        page.locator("[data-hiby-reboot]").count() == 0
    )  # the SonixOS reboot hint is not for Compás
    assert no_horizontal_scroll(page)
    shot(page, f"device-overview-compas-tips-{width}")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_hiby_split_genres_setting_with_live_example(web_server, chrome, tmp_path, width):
    """a HiBy's Settings tab has the Player library card: Split genres follows the player by default, the
    separators show only for On, with a live example that follows the checkboxes."""
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, url, width)

    def handle(route):
        r = route.fetch()
        data = r.json()
        if "config" in data:
            data["config"]["type"] = "hiby"
            data["config"]["settings"] = {}
        route.fulfill(response=r, body=json.dumps(data), content_type="application/json")

    page.route(re.compile(r"/api/v1/devices/[^/?]+$"), handle)
    page.goto(f"{url}/#/devices/{dev}/settings")
    card = page.locator('[data-type-card="hiby"]')
    card.wait_for()
    mode = card.locator("[data-field=split_genres] select")
    assert mode.input_value() == "player"
    seps = card.locator("[data-field=genre_separators]")
    assert seps.is_hidden()
    mode.select_option("on")
    seps.wait_for()
    example = seps.locator("[data-split-example]")
    assert "Rock · Pop · Indie & Folk · Jazz" in example.inner_text()
    seps.get_by_label("& ampersand").check()
    assert "Rock · Pop · Indie · Folk · Jazz" in example.inner_text()
    seps.get_by_label("; semicolon").uncheck()
    assert "Rock; Pop · Indie · Folk · Jazz" in example.inner_text()
    assert no_horizontal_scroll(page)
    card.scroll_into_view_if_needed()
    shot(page, f"device-settings-hiby-split-{width}")
    mode.select_option("off")
    assert seps.is_hidden()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_space_map_and_left_off(web_server, chrome, tmp_path, width):
    """the space map by library and rule, the rest of the used space explained, and what the last
    plan left off with its advice (routed: the fixture's stick has room for everything)."""
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    page = open_page(chrome, url, width)
    left = {
        "does_not_fit": True, "short_by": 2_500_000_000, "dropped": [], "dropped_total": 0, "dropped_bytes": 0,
        "limit_left_out": 0, "limit_left_out_bytes": 0,
        "per_rule": [{"rule_id": "r1", "name": "Уся музика з дуже довгою назвою правила", "items": 3,
                      "bytes": 9_000_000_000, "dropped_items": 0, "dropped_bytes": 0}],
        "suggestions": ["convert lossless files to AAC 256k when space is needed (3 items, 9.0 GB)"],
    }  # fmt: skip

    def handle(route):
        r = route.fetch()
        route.fulfill(response=r, json={**r.json(), "left_off": left})

    page.route(re.compile(r"/api/v1/devices/[^/]+/space$"), handle)
    page.goto(f"{url}/#/devices/{dev}")
    smap = page.locator("[data-space-map]")
    smap.wait_for()
    smap.locator("summary").click()
    lib = smap.locator('[data-space-row="music"]')
    assert "Музика" in lib.inner_text() and "3 items" in lib.inner_text()
    assert smap.locator("h3").all_inner_texts() == ["By library", "By rule"]
    assert "device database" in smap.locator("[data-space-rest]").inner_text()
    box = page.locator("[data-left-off]")
    assert "needs 2.5 GB more" in box.inner_text()
    assert "9.0 GB wanted" in box.locator('[data-left-rule="r1"]').inner_text()
    assert "AAC 256k" in box.locator("[data-left-advice]").inner_text()
    assert no_horizontal_scroll(page)
    shot(page, f"device-space-map-{width}")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
