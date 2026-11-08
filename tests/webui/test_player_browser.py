"""Player contents as a library browser: Tracks / Albums / Artists / Playlists / Audiobooks /
Podcasts tabs (kinds the device cannot hold hidden), search across tabs, items other tools put there marked and
removable, an orphan of a removed library keeps its tags, ▶ plays - at 1280 and 390 px. Temp folders only."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _stick(url, app, tmp_path):
    """A synced stick: 3 songs from a folder library (Скрябін ×2 on one album, Тартак), plus a song, a podcast episode
    and a playlist another tool put there."""
    music = tmp_path / "music"
    make_music(
        music / "k1.mp3",
        title="Група крові",
        artist="Скрябін",
        album="Група крові",
        tracknumber="1",
    )
    make_music(
        music / "k2.mp3", title="Зозуля", artist="Скрябін", album="Група крові", tracknumber="2"
    )
    make_music(music / "s.mp3", title="Орбіт", artist="Тартак", album="Гранатовий альбом")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
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
    make_music(
        stick / "Other" / "x.mp3", title="Чужа", artist="Воплі Відоплясова", album="Радіо Африка"
    )
    make_music(stick / "Podcasts" / "Show" / "ep.mp3", title="Episode 1", album="Show")
    (stick / "Playlists").mkdir(exist_ok=True)
    (stick / "Playlists" / "Mix.m3u8").write_text("#EXTM3U\n../Other/x.mp3\n", encoding="utf-8")
    return stick


def _tabs(page):
    """The tab names without their counts ("Tracks 4" → "Tracks")."""
    return [
        x.split()[0]
        for x in page.locator('#content [role="tablist"] >> nth=1')
        .locator('[role="tab"]')
        .all_inner_texts()
    ]


@pytest.mark.parametrize("width", [1280, 390])
def test_player_contents_tabs_search_and_foreign_items(web_server, chrome, tmp_path, width):
    url, app = web_server
    stick = _stick(url, app, tmp_path)
    page = open_page(chrome, url + "/#/devices/stick/contents", width=width)
    page.wait_for_selector('input[name="pcItem"]')
    # a flash drive takes video too
    tabs = ["Tracks", "Albums", "Artists", "Playlists", "Audiobooks", "Podcasts", "Videos"]
    assert _tabs(page) == tabs
    assert "Items on the device: 5" in page.locator("[data-pc-count]").inner_text()
    counts = {
        t: page.locator(f'[data-pc-tab="{t}"] .pc-n').inner_text()
        for t in ("tracks", "albums", "artists", "playlists", "podcasts")
    }
    assert counts == {
        "tracks": "4",
        "albums": "3",
        "artists": "3",
        "playlists": "1",
        "podcasts": "1",
    }
    # Tracks: music only, the foreign song marked
    assert page.locator('input[name="pcItem"]').count() == 4
    row = page.locator('tr:has(input[aria-label="Чужа"])')
    assert "Not from SyncMusic" in row.inner_text() and "Воплі Відоплясова" in row.inner_text()
    # Albums: a cover grid; "2 tracks" opens the Скрябін album's tracks in order
    page.get_by_role("tab", name="Albums").click()
    page.wait_for_selector(".pc-grid [data-pc-group]")
    assert page.locator("[data-pc-group]").count() == 3
    album = page.locator('[data-pc-group="al|Скрябін|Група крові"]')
    assert "Скрябін" in album.inner_text() and "2 tracks" in album.inner_text()
    foreign_card = page.locator('[data-pc-group="al|Воплі Відоплясова|Радіо Африка"]')
    assert "Not from SyncMusic" in foreign_card.inner_text()
    shot(page, f"pc-flash-albums-{width}")
    album.get_by_role("link", name="2 tracks").click()
    page.wait_for_selector("[data-pc-count]:has-text('Shown: 2 of 5')")
    assert [x.strip() for x in page.locator("tbody tr td:nth-child(3)").all_inner_texts()] == [
        "1. Група крові",
        "2. Зозуля",
    ]
    # Managed by: not from SyncMusic
    page.goto(url + "/#/devices/stick/contents?tab=tracks")
    page.wait_for_selector('input[name="pcItem"]')
    page.get_by_label("Managed by").select_option("foreign")
    page.wait_for_selector("[data-pc-count]:has-text('Shown: 1 of 5')")
    assert page.locator('input[name="pcItem"]').count() == 1
    assert page.locator('[data-pc-tab="tracks"] .pc-n').inner_text() == "1"
    page.get_by_role("tab", name="Albums").click()
    page.wait_for_selector(".pc-grid [data-pc-group]")
    assert page.locator("[data-pc-group]").count() == 1  # the filter stays across tabs
    page.goto(url + "/#/devices/stick/contents?tab=tracks")
    page.wait_for_selector('input[name="pcItem"]')
    # Artists, Playlists, Podcasts
    page.get_by_role("tab", name="Artists").click()
    page.wait_for_selector('[data-pc-group="ar|Тартак"]')
    assert page.locator("[data-pc-group]").count() == 3
    page.get_by_role("tab", name="Playlists").click()
    page.wait_for_selector('[data-pc-group="pl|Mix"]')
    assert page.locator('[data-pc-group="pl|Mix"] input[name="pcItem"]').count() == 1
    page.get_by_role("tab", name="Podcasts").click()
    page.wait_for_selector('[data-pc-group="al|Unknown artist|Show"]')
    # a flash drive keeps no bookmarks: the spoken-word tabs say so
    assert "keeps no listening positions" in page.locator("[data-pc-no-bookmarks]").inner_text()
    shot(page, f"pc-flash-podcasts-{width}")
    page.get_by_role("tab", name="Audiobooks").click()
    page.wait_for_selector(".empty")
    # search narrows and survives a tab change
    page.get_by_role("tab", name="Tracks").click()
    page.wait_for_selector('input[name="pcItem"]')
    page.fill("#pcQ", "скрябін")
    page.locator('form[data-action="pcSearch"] button').click()
    page.wait_for_selector("[data-pc-count]:has-text('Shown: 2 of 5')")
    page.get_by_role("tab", name="Artists").click()
    page.wait_for_selector('[data-pc-group="ar|Скрябін"]')
    assert page.locator("[data-pc-group]").count() == 1
    assert no_horizontal_scroll(page)
    # ▶ on the foreign song plays its file
    page.goto(url + "/#/devices/stick/contents?tab=playlists")
    page.wait_for_selector('[data-pc-group="pl|Mix"]')
    page.get_by_role("button", name="Play Чужа").click()
    src = page.locator("#pcAudio").get_attribute("src")
    assert httpx.get(url + src, timeout=30).content == (stick / "Other" / "x.mp3").read_bytes()
    # remove the playlist's foreign song + the Тартак album
    page.locator('input[aria-label="Select Mix"]').check()
    page.goto(url + "/#/devices/stick/contents?tab=albums")
    page.wait_for_selector("[data-pc-group]")
    page.locator('input[aria-label="Select Гранатовий альбом"]').check()
    page.locator('input[aria-label="Select Радіо Африка"]').check()
    assert page.locator("[data-pc-selected]").inner_text() == "2 selected"
    page.locator('[data-action="pcClear"]').click()
    assert page.locator("[data-pc-selbar]").is_hidden()
    page.locator('input[aria-label="Select Гранатовий альбом"]').check()
    page.locator('input[aria-label="Select Радіо Африка"]').check()
    page.locator('[data-action="pcRemove"]').click()
    modal = page.locator(".modal")
    modal.wait_for()
    assert "Remove 2 from Stick?" in modal.inner_text()
    assert modal.locator("#pcExclude").is_checked()
    shot(page, f"pc-flash-remove-{width}")
    modal.get_by_role("button", name="Remove 2").click()
    page.wait_for_function("location.hash.includes('/sync/')")
    page.wait_for_selector("[data-report]", timeout=60_000)
    assert page.locator("[data-report]").get_attribute("data-outcome") == "succeeded"
    assert not (stick / "Other" / "x.mp3").exists()
    titles = {r["title"] for r in _api(url, "GET", "/devices/stick/contents")}
    assert titles == {"Група крові", "Зозуля", "Episode 1"}
    assert page.errors == []


def test_rockbox_hides_kinds_it_cannot_hold(web_server, chrome, tmp_path):
    """A Rockbox player takes no podcasts / audiobooks in v1: those tabs are hidden while none are on it."""
    url, _ = web_server
    rb = tmp_path / "rb"
    (rb / ".rockbox").mkdir(parents=True)
    make_music(rb / "Music" / "a.mp3", title="A", artist="X", album="Y")
    _api(
        url,
        "POST",
        "/devices/register",
        json={"name": "RB", "mount_path": str(rb), "type": "rockbox"},
    )
    page = open_page(chrome, url + "/#/devices/rb/contents")
    page.wait_for_selector('input[name="pcItem"]')
    assert _tabs(page) == ["Tracks", "Albums", "Artists", "Playlists"]
    assert page.errors == []


def test_orphan_of_a_removed_library_keeps_its_tags(web_server, chrome, tmp_path):
    """after the library is detached and removed, the songs are listed under their artist and album (file
    tags), not 'Unknown artist' / 'No album' with the file name."""
    url, app = web_server
    _stick(url, app, tmp_path)
    # detached: no template draws from the library any more
    _api(url, "PATCH", "/devices/stick", json={"rule_set": {"template_ids": []}})
    _api(url, "DELETE", "/libraries/music")
    page = open_page(chrome, url + "/#/devices/stick/contents?tab=artists")
    page.wait_for_selector('[data-pc-group="ar|Скрябін"]')
    card = page.locator('[data-pc-group="ar|Скрябін"]')
    assert "Група крові" in card.inner_text()
    assert card.locator(".status", has_text="Orphan").count() == 2
    assert "Unknown artist" not in page.locator("#content").inner_text().replace(
        "Unknown artist - Show", ""
    )
    assert ".mp3" not in card.inner_text()
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_ipod_podcast_shows_where_it_stands(web_server, chrome, tmp_path, monkeypatch, width):
    """on a device with bookmarks a podcast episode shows its position (and no 'no positions' note)."""
    from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
    from tests.contracts.harness import ipod_tree
    from tests.fake_abs import FakeABS
    from tests.media import make_track

    url, app = web_server
    abs_ = FakeABS()
    abs_.add_podcast("p1", "Подкаст", [("e1", "Епізод 1")])
    data = make_track(tmp_path / "src" / "e1.m4a", seconds=3.0, title="Епізод 1").read_bytes()
    abs_.files[("p1", "p1-e1")] = data
    abs_.items["p1"]["media"]["episodes"][0]["audioFile"]["metadata"]["size"] = len(data)
    abs_.set_progress("p1", 1.0, episode="e1")
    monkeypatch.setattr(AudiobookshelfLibrary, "transport", abs_)
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "abs",
            "type": "audiobookshelf",
            "name": "ABS",
            "settings": {"url": "http://abs.test", "api_token": "tok"},
        },
    )
    mount = ipod_tree(tmp_path / "ipod")
    rules = [{"id": "p", "name": "Spoken", "kind": ["podcast"], "criteria": {"all": []}}]
    tpl = _api(
        url, "POST", "/templates", json={"name": "Spoken", "library_ids": ["abs"], "rules": rules}
    )
    body = {
        "name": "iPod",
        "mount_path": str(mount),
        "type": "ipod",
        "rule_set": {"template_ids": [tpl["id"]]},
    }
    _api(url, "POST", "/devices/register", json=body)
    plan = _api(url, "POST", "/devices/ipod/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    page = open_page(chrome, url + "/#/devices/ipod/contents?tab=podcasts", width=width)
    page.wait_for_selector('input[name="pcItem"]')
    cell = page.locator('tr:has(input[aria-label="Епізод 1"]) td:nth-child(6)').inner_text()
    assert cell.startswith("0:01 of 0:03"), cell
    assert page.locator("[data-pc-no-bookmarks]").count() == 0
    shot(page, f"pc-ipod-podcasts-{width}")
    assert no_horizontal_scroll(page)
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_player_contents_and_browse_present_videos(web_server, chrome, tmp_path, width):
    """a video-only stick opens on Videos (badge = items), grouped by show and season with movies apart,
    video placeholders; the library's Videos tab sorts by show, not "Artist A–Z"."""
    from tests.media import make_video

    url, app = web_server
    vid = tmp_path / "vid"
    make_video(vid / "Мультики" / "Сезон 1" / "Ну постривай S01E01.mp4", size="160x120")
    make_video(vid / "Мультики" / "Сезон 1" / "Ну постривай S01E02.mp4", size="160x120")
    make_video(vid / "Фільм.mkv", size="160x120")
    lib = {
        "id": "vid",
        "type": "folder",
        "name": "Відео",
        "settings": {"root": str(vid), "default_kind": "video"},
    }
    _api(url, "POST", "/libraries", json=lib)
    job = _api(url, "POST", "/libraries/vid/refresh")
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    stick = tmp_path / "stick"
    stick.mkdir()
    body = {"name": "Stick", "mount_path": str(stick), "type": "flashdrive", "library_ids": ["vid"]}
    _api(url, "POST", "/devices/register", json=body)
    cfg = _api(url, "GET", "/devices/stick")["config"]
    rs = {**cfg["rule_set"], "template_ids": ["default.vid.all"]}
    _api(url, "PATCH", "/devices/stick", json={"rule_set": rs})
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 120).status == "succeeded"

    page = open_page(chrome, url + "/#/devices/stick/contents", width=width)
    page.wait_for_selector('input[name="pcItem"]')
    assert page.locator('[data-pc-tab="videos"]').get_attribute("aria-selected") == "true"
    assert page.locator('[data-pc-tab="videos"] .pc-n').inner_text() == "3"
    groups = page.locator("[data-pc-group]")
    assert groups.count() == 2
    assert "Мультики" in groups.nth(0).inner_text() and "Season 1" in groups.nth(0).inner_text()
    assert (
        "S01E01" in groups.nth(0).inner_text()
        and "Unknown artist" not in groups.nth(0).inner_text()
    )
    assert "Movies" in groups.nth(1).inner_text()
    assert page.locator("tbody .thumb.video").count() == 3
    # no Show/Episode columns (the group is the show), nothing cut off at phone width, video wording
    assert "Show" not in page.locator("thead").first.inner_text()
    assert page.locator("#pcQ").get_attribute("placeholder") == "Search title, show"
    overflow = page.evaluate(
        """() => [...document.querySelectorAll('[data-pc-group] .table-wrap')]
             .filter((w) => w.scrollWidth > w.clientWidth + 1).length"""
    )
    assert overflow == 0
    # the show and Movies tables share one column grid
    lefts = page.evaluate(
        """() => [...document.querySelectorAll('[data-pc-group] table')]
             .map((t) => [...t.querySelectorAll('thead th')].map((th) => Math.round(th.getBoundingClientRect().left)))"""
    )
    assert len(lefts) == 2 and lefts[0] == lefts[1], lefts
    no_horizontal_scroll(page)
    shot(page, f"pc-videos-{width}")
    page.get_by_role("tab", name="Tracks").click()
    page.wait_for_selector(".empty")
    assert "Nothing of this kind" in page.locator(".empty").inner_text()

    page.goto(url + "/#/libraries/vid/browse")
    page.wait_for_selector("#sort")
    assert page.locator("#sort").input_value() == "show"
    assert "Artist" not in page.locator("#sort").inner_text()
    shot(page, f"lib-videos-{width}")


@pytest.mark.parametrize("width", [1280, 390])
def test_player_contents_grid_list_sort_and_range_select(web_server, chrome, tmp_path, width):
    """list/grid toggle per tab (remembered), sort by album/size, shift-click ticks a range."""
    url, app = web_server
    _stick(url, app, tmp_path)
    page = open_page(chrome, f"{url}/#/devices/stick/contents?tab=albums", width=width)
    page.locator(".pc-grid").wait_for()  # albums: grid by default

    def names():
        return page.locator(
            "[data-pc-group] .pc-title, [data-pc-group] .card-head b"
        ).all_inner_texts()

    page.locator("[data-change=pcSort]").select_option("album")
    first = page.locator("[data-pc-group] .pc-title").first
    expect(first).to_have_text("Гранатовий альбом")
    assert names() == ["Гранатовий альбом", "Група крові", "Радіо Африка"]
    page.locator("[data-change=pcSort]").select_option("size")
    expect(first).to_have_text("Група крові")  # two songs: the biggest album
    shot(page, f"contents-grid-sorted-{width}")
    # the list view, remembered after a reload
    page.locator('[data-action=pcView][data-view="list"]').click()
    page.locator(".card-head [data-change=pcSelectGroup]").first.wait_for()
    assert page.locator(".pc-grid").count() == 0
    page.reload()
    page.locator(".card-head [data-change=pcSelectGroup]").first.wait_for()
    # shift-click: the first and the third album tick the second too
    boxes = page.locator("[data-change=pcSelectGroup]")
    boxes.nth(0).click()
    boxes.nth(2).click(modifiers=["Shift"])
    assert [boxes.nth(i).is_checked() for i in range(3)] == [True, True, True]
    assert page.locator("[data-pc-selected]").inner_text() == "4 selected"
    assert no_horizontal_scroll(page)
    shot(page, f"contents-list-selected-{width}")
    page.locator('[data-action=pcView][data-view="grid"]').click()
    page.locator(".pc-grid").wait_for()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_other_installations_items_are_read_only(web_server, chrome, tmp_path, width):
    """Items another installation put there: filtered by "Managed by", their rows cannot be selected for removal."""
    url, app = web_server
    _stick(url, app, tmp_path)
    items = _api(url, "GET", "/devices/stick/contents")
    mine = next(i for i in items if not i["foreign"] and i["kind"] == "music")
    other = {**mine, "key": "ext:L9/x1", "title": "Від іншої", "other_instance": True}
    page = open_page(chrome, url + "/#/", width)
    page.route(
        "**/api/v1/devices/stick/contents",
        lambda route: route.fulfill(json=[*items, other]),
    )
    page.goto(url + "/#/devices/stick/contents?tab=tracks")
    row = page.locator('tr[data-key="ext:L9/x1"]')
    row.wait_for()
    assert "Another installation" in row.inner_text()
    assert row.get_attribute("data-read-only") is not None
    assert row.locator('input[name="pcItem"]').is_disabled()
    page.get_by_label("Select all").check()
    assert not row.locator('input[name="pcItem"]').is_checked()
    page.get_by_label("Select all").uncheck()
    page.get_by_label("Managed by").select_option("others")
    page.wait_for_selector("[data-pc-count]:has-text('Shown: 1 of')")
    assert page.locator('input[name="pcItem"]').count() == 1
    page.get_by_label("Managed by").select_option("mine")
    page.wait_for_selector("[data-pc-count]:has-text('Shown: 3 of')")
    shot(page, f"contents-managed-{width}")
    assert no_horizontal_scroll(page)
