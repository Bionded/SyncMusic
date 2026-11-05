"""Cover thumbnails next to each checkbox: in every library browser view and in Player contents a row has
a thumbnail or the placeholder; images load lazily; a library of 1000+ songs stays quick. Temp folders only."""

from __future__ import annotations

import time

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import open_page, run_on, settle

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=120, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


LOADED = "(img) => img.complete && img.naturalWidth === 80"


def test_library_rows_and_player_contents_have_thumbnails(web_server, chrome, tmp_path):
    url, app = web_server
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="With cover", artist="Скрябін", album="Зірка", cover=True)
    make_music(music / "b.mp3", title="No cover", artist="Тартак", album="Гранатовий")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    page = open_page(chrome, url + "/#/libraries/music/browse")
    # songs: a 40 px thumbnail per row (80 px image); albums / artists: artwork cards with the large cover
    for tab, img, size in (
        ("songs", ".thumb img", 80),
        ("albums", ".bcard-art img", 336),
        ("artists", ".bcard-art img", 336),
    ):
        page.goto(url + f"/#/libraries/music/browse?tab={tab}")
        page.wait_for_selector('input[name="sel"]')
        rows = page.locator("#content [data-row]")
        assert rows.count() == 2, tab
        # a folder library has no artist pictures: the artist row uses a song's cover
        covered = page.locator("#content [data-row]", has_text="Скрябін").locator(img)
        covered.wait_for()
        page.wait_for_function(
            f"() => [...document.querySelectorAll('#content {img}')].some(i => i.complete && i.naturalWidth === {size})"
        )
        # the other has no cover: the list row drops its image (204), a card never gets one
        page.wait_for_function(f"() => document.querySelectorAll('#content {img}').length === 1")
        assert page.locator("#content [data-row]", has_text="Тартак").locator(img).count() == 0
        assert page.locator(f"#content {img}").evaluate_all(
            "l => l.every(i => i.loading === 'lazy')"
        )
    # Player contents: the file's own picture, the placeholder for the other
    stick = tmp_path / "stick"
    stick.mkdir()
    body = {
        "name": "Stick",
        "mount_path": str(stick),
        "type": "flashdrive",
        "library_ids": ["music"],
    }
    _api(url, "POST", "/devices/register", json=body)
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    page.goto(url + "/#/devices/stick/contents")
    page.wait_for_selector('input[name="pcItem"]')
    assert page.locator("#content tbody tr .thumb").count() == 2
    img = page.locator('tr:has(input[aria-label="With cover"]) .thumb img')
    img.wait_for()
    page.wait_for_function(
        f"() => ({LOADED})(document.querySelector('tr:has(input[aria-label=\"With cover\"]) .thumb img'))"
    )
    # the 204 of the other removes its image: only the placeholder is left
    page.wait_for_function(
        "() => !document.querySelector('tr:has(input[aria-label=\"No cover\"]) .thumb img')"
    )
    assert page.errors == []


def test_big_library_stays_quick_and_loads_thumbnails_lazily(web_server, chrome, tmp_path):
    url, _ = web_server
    music = tmp_path / "music"
    for i in range(1100):
        make_music(
            music / f"{i // 10:03d}" / f"{i:04d}.mp3",
            title=f"Song {i:04d}",
            artist=f"Artist {i // 10:03d}",
            album=f"Album {i // 10:03d}",
            cover=True,
        )
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "big", "type": "folder", "name": "Big", "settings": {"root": str(music)}},
    )
    _api(url, "GET", "/libraries/big/items")  # the index is built once, before timing the page
    page = open_page(chrome, url + "/#/libraries/big/browse", width=1280, height=800)
    thumb_requests: list[str] = []
    page.on("request", lambda r: "/thumb?" in r.url and thumb_requests.append(r.url))
    start = time.monotonic()
    page.goto(url + "/#/libraries/big/browse?tab=songs")
    page.wait_for_selector('input[name="sel"]')
    assert time.monotonic() - start < 5
    assert "1100 items" in page.locator("#content .lb-count").inner_text()
    settle(page)
    page.wait_for_function(
        "[...document.querySelectorAll('#content [data-row] img')].every((i) => i.complete)"
    )
    shown = page.locator("#content [data-row] .thumb").count()
    # one page of rows, each asking for its own picture lazily - never the whole library (Chromium's lazy-load
    # distance covers a 50-row page, so all of them may come)
    assert shown == 50 and 0 < len(thumb_requests) <= shown, len(thumb_requests)
    assert page.locator("#content .thumb img").evaluate_all(
        "l => l.every(i => i.loading === 'lazy')"
    )
    page.mouse.wheel(0, 20000)
    page.wait_for_function(
        "() => [...document.querySelectorAll('#content .thumb img')].every(i => i.complete && i.naturalWidth === 80)",
        timeout=30000,
    )
    # the next page is quick too (thumbnails are made on demand, not for the whole library)
    start = time.monotonic()
    page.goto(url + "/#/libraries/big/browse?tab=songs&page=21")
    page.wait_for_selector('input[name="sel"]')
    assert time.monotonic() - start < 3
    assert page.locator("#content [data-row]").count() == 50
