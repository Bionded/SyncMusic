"""Global search: the top-bar button and Ctrl K open the overlay; it finds an album (opens the
library browser on it), a device and a job, Enter opens the first hit, Esc closes - at 1280 and 390 px."""

from __future__ import annotations

import httpx
import pytest

from syncmusic.core.models import DeviceConfig, DeviceIdentity, RuleSet
from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


async def _seed(app):
    await app.store.put_device(
        DeviceConfig(
            id="zorvex",
            type="flashdrive",
            name="Zorvex Ключ",
            identity=DeviceIdentity(usb_serial="S-zorvex"),
            rule_set=RuleSet(template_ids=["default.music.all"]),
        )
    )
    await app.sync.load()

    async def job(ctx):
        return {}

    info = app.jobs.submit("refresh", "refresh Zorba library", job)
    await app.jobs.wait(info.id)
    return info.id


@pytest.mark.parametrize("width", [1280, 390])
def test_search_overlay(web_server, chrome, tmp_path, width):
    url, app = web_server
    make_music(tmp_path / "music" / "a.mp3", title="Пісня", artist="Скрябін", album="Група крові")
    lib = {
        "id": "music",
        "type": "folder",
        "name": "Music",
        "settings": {"root": str(tmp_path / "music")},
    }
    assert httpx.post(f"{url}/api/v1/libraries", json=lib, timeout=60).status_code == 201
    httpx.get(f"{url}/api/v1/libraries/music/items", timeout=60)  # loads the browsing cache
    job_id = run_on(app, _seed(app))
    page = open_page(chrome, url + "/#/", width=width)
    page.wait_for_selector("#searchBtn svg")

    page.click("#searchBtn")
    box = page.locator("#searchInput")
    box.fill("група")
    hit = page.locator("[data-search-hit]", has_text="Група крові").first
    hit.wait_for()
    assert page.locator(".search-ghead", has_text="Albums").is_visible()
    assert no_horizontal_scroll(page)
    shot(page, f"search-album-{width}")
    box.press("Enter")  # the first hit: the album
    page.wait_for_selector('#content[data-route="/libraries/music/browse"]')
    assert "tab=albums" in page.url and "q=" in page.url
    assert page.locator(".search-panel").count() == 0

    page.keyboard.press("Control+k")
    page.locator("#searchInput").fill("ключ")
    page.locator('[aria-label="Devices"] [data-search-hit]', has_text="Zorvex Ключ").click()
    page.wait_for_selector('#content[data-route="/devices/zorvex"]')

    page.keyboard.press("Control+k")
    page.locator("#searchInput").fill("zorba")
    page.locator("[data-search-hit]", has_text="refresh Zorba library").wait_for()
    page.keyboard.press("Enter")
    page.wait_for_selector(f'#content[data-route="/jobs/{job_id}"]')

    page.keyboard.press("Control+k")
    page.locator("#searchInput").fill("nothing-like-this")
    page.locator(".search-empty", has_text="Nothing found").wait_for()
    page.keyboard.press("Escape")
    assert page.locator(".search-panel").count() == 0


def test_search_groups_non_music_by_kind(web_server, chrome):
    """video, audiobook and podcast hits get their own groups instead of 'Tracks'."""
    import json

    url, _app = web_server
    hit = {"library_id": "lib", "library": "Lib", "artist": "", "album": ""}
    body = {
        "artists": [],
        "albums": [],
        "tracks": [
            {**hit, "title": "Song X", "kind": "music"},
            {**hit, "title": "Movie X", "kind": "video"},
            {**hit, "title": "Book X", "kind": "audiobook"},
            {**hit, "title": "Episode X", "kind": "podcast"},
        ],
        "devices": [],
        "templates": [],
        "jobs": [],
        "not_loaded": [],
    }
    page = open_page(chrome, url + "/#/", width=1280)
    page.route(
        "**/api/v1/search?*",
        lambda r: r.fulfill(content_type="application/json", body=json.dumps(body)),
    )
    page.wait_for_selector("#searchBtn svg")
    page.click("#searchBtn")
    page.locator("#searchInput").fill("x")
    for group, title in [
        ("Tracks", "Song X"),
        ("Videos", "Movie X"),
        ("Audiobooks", "Book X"),
        ("Podcasts", "Episode X"),
    ]:
        box = page.locator(f'.search-group[aria-label="{group}"]')
        box.wait_for()
        assert (
            box.locator("[data-search-hit]").all_inner_texts() == [title]
            or title in box.inner_text()
        )
    assert "Movie X" not in page.locator('.search-group[aria-label="Tracks"]').inner_text()


@pytest.mark.parametrize("width", [1280, 390])
def test_command_palette(web_server, chrome, width):
    """pages and actions in the overlay - fuzzy match, Enter opens/runs, the used one comes first
    under Recent with no query; an action on a device runs its API call."""
    url, app = web_server
    run_on(app, _seed(app))
    page = open_page(chrome, url + "/#/", width=width)
    page.wait_for_selector("#searchBtn svg")
    page.keyboard.press("Control+k")
    page.locator('[aria-label="Pages"] [data-search-hit]', has_text="Open Activity").wait_for()
    assert page.get_attribute(".search-panel", "role") == "dialog"
    assert page.get_attribute("#searchResults", "role") == "listbox"
    page.locator("#searchInput").fill("opn sttngs")  # fuzzy
    page.wait_for_function(
        "document.querySelector('[aria-label=\\'Pages\\'] [data-search-hit]')?.innerText.trim() === 'Open Settings'"
    )
    shot(page, f"palette-{width}")
    assert no_horizontal_scroll(page)
    page.keyboard.press("Enter")
    page.wait_for_selector('#content[data-route="/settings"]')
    page.keyboard.press("Control+k")
    recent = page.locator('[aria-label="Recent"] [data-search-hit]').first
    recent.wait_for()
    assert "Open Settings" in recent.inner_text()
    page.locator("#searchInput").fill("sync zorvex")
    page.locator('[aria-label="Actions"] [data-search-hit]', has_text="Sync Zorvex Ключ").click()
    page.wait_for_selector('#content[data-route="/devices/zorvex/plan"]')
    page.keyboard.press("Control+k")
    page.locator("#searchInput").fill("sync all")
    page.locator(
        '[aria-label="Actions"] [data-search-hit]', has_text="Sync all connected devices"
    ).click()
    page.locator(".modal").get_by_role("button", name="Cancel").click()
    # settings by their explanation (637.2): opens Settings filtered to the row
    page.keyboard.press("Control+k")
    page.locator("#searchInput").fill("reads written files back")
    page.locator('[aria-label="Settings"] [data-search-hit]', has_text="Verify").click()
    page.wait_for_selector('#content[data-route="/settings"]')
    assert (
        page.input_value("[data-set-search]")
        == page.locator("[data-set-row]:visible").first.inner_text().split("\n")[0]
    )
    assert not [e for e in page.errors if "Failed to load resource" not in e]
