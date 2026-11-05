"""Libraries: add with a connection test, edit (secret stays masked and stored), refresh, browse with
search / kind filter, add selected items to a device, remove (refused while attached). Temp folders only."""

from __future__ import annotations

import time

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _add_to(page, target, files=None, undo=False, text=None):
    """The sticky bar's "Add to device…" → pick `target` (a device or template name) → Add rule → the toast
    (with "`files` file…" or `text` in it)."""
    page.get_by_role("button", name="Add to device…").click()
    modal = page.locator(".modal")
    modal.wait_for()
    modal.locator(".target", has_text=target).click()
    modal.get_by_role("button", name="Add rule").click()
    # an earlier toast may still be shown: wait for this one
    needle = text or f"{files} file"
    toast = page.locator(f".toast:has-text('Rule added to {target}'):has-text('{needle}')").last
    toast.wait_for()
    if undo:
        toast.get_by_role("button", name="Undo").click()
        page.wait_for_selector(".toast:has-text('Rule removed')")


def test_folder_library_add_browse_add_to_device_remove(web_server, chrome, tmp_path):
    url, app = web_server
    music = tmp_path / "music"
    for artist, title in (("Скрябін", "Група крові"), ("Скрябін", "Зозуля"), ("Тартак", "Орбіт")):
        make_music(
            music / artist / f"{title}.mp3", title=title, artist=artist, album=f"{artist} album"
        )
    page = open_page(chrome, url + "/#/libraries")
    page.get_by_role("link", name="Add a library").first.click()
    page.wait_for_selector('[data-lib-type="folder"]')
    page.locator('[data-lib-type="folder"]').click()
    page.wait_for_function("location.hash.includes('type=folder')")
    page.wait_for_selector('input[id$="root"]')
    page.fill("#libName", "Моя музика")
    page.fill('input[id$="root"]', str(music))
    page.get_by_role("button", name="Save and scan").click()
    page.wait_for_selector(".toast:has-text('added and reachable')")
    # the design: the first scan starts, the created default templates are links
    saved = page.locator("[data-lib-saved]")
    saved.wait_for()
    assert saved.locator("[data-created-template]").count() >= 1
    shot(page, "library-saved-1280")
    saved.get_by_role("link", name="Back to libraries").click()
    page.wait_for_selector("[data-health] .status.ok")
    lib = _api(url, "GET", "/libraries")[0]
    assert lib["name"] == "Моя музика" and lib["settings"]["root"] == str(music)
    shot(page, "libraries-1280")

    # refresh → the index count arrives as a toast; the button stays disabled while the
    # first scan runs, which can take long on a loaded Pi: wait for it through the
    # API (flaky in the full slow suite), then the page follows within its 3 s poll
    deadline = time.monotonic() + 240
    while busy := [
        j
        for j in _api(url, "GET", "/jobs")
        if j["kind"] == "refresh" and j["status"] in ("queued", "running")
    ]:
        assert time.monotonic() < deadline, f"first scan still not done: {busy}"
        time.sleep(1)
    refresh = page.locator('[data-action="refresh"]:not([disabled])').first
    refresh.wait_for(
        timeout=90_000
    )  # the page polls every 3 s; slower while other workers load the host
    refresh.click()
    page.wait_for_selector(".toast:has-text('3 tracks')")

    # browse, search, add to a device
    (tmp_path / "stick").mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"},
    )
    page.get_by_role("link", name="Browse").first.click()
    page.wait_for_selector("text=3 items")
    page.fill("#q", "скрябін")
    page.locator('form[data-action="search"] button').click()
    page.wait_for_selector("text=2 items")
    # the Albums tab: ticking the album adds an include rule for it
    page.get_by_role("tab", name="Albums").click()
    page.wait_for_selector("text=Скрябін album")
    page.locator('input[name="sel"][aria-label="Скрябін album"]').check()
    shot(page, "library-browse-1280")
    _add_to(page, "Stick", files=2)
    own = _api(url, "GET", "/templates/own-stick")  # the device's own template
    assert "own-stick" in _api(url, "GET", "/devices/stick")["config"]["rule_set"]["template_ids"]
    manual = next(r for r in own["rules"] if r["id"] == "manual-selection")
    assert manual["libraries"] == [lib["id"]]  # it draws from that library
    assert "album_id" in str(manual["criteria"])
    page.goto(f"{url}/#/libraries/{lib['id']}/browse")
    page.wait_for_selector("text=3 items")
    page.set_viewport_size({"width": 375, "height": 800})
    assert no_horizontal_scroll(page)

    # remove: refused while the device uses it, then allowed
    page.goto(f"{url}/#/libraries/{lib['id']}")
    page.wait_for_selector('[data-action="remove"]')
    page.locator('[data-action="remove"]').click()
    assert "Stick" in page.locator(".modal [data-remove-users]").inner_text()  # who uses it
    page.fill("#typedName", "Моя музика")
    page.locator(".modal").get_by_role("button", name="Remove library").click()
    page.wait_for_selector(".toast.error:has-text('change those rules first')")
    _api(url, "PATCH", "/devices/stick", json={"rule_set": {}})
    _api(url, "DELETE", "/templates/own-stick")  # its picks name the library
    page.locator('[data-action="remove"]').click()
    page.fill("#typedName", "Моя музика")
    page.locator(".modal").get_by_role("button", name="Remove library").click()
    page.wait_for_function("location.hash === '#/libraries'")
    assert _api(url, "GET", "/libraries") == []
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_unreachable_server_is_added_with_the_error_and_the_password_stays_masked(
    web_server, chrome
):
    url, app = web_server
    page = open_page(chrome, url + "/#/libraries/new?type=subsonic")
    page.wait_for_selector('input[id$="password"]')
    page.fill("#libName", "Home server")
    page.fill('input[id$="url"]', "http://127.0.0.1:9")  # nothing listens there
    page.fill('input[id$="username"]', "me")
    page.fill('input[id$="password"]', "s3cret")
    page.get_by_role("button", name="Save and scan").click()
    page.wait_for_selector(".toast.error:has-text('connection test failed')")
    page.wait_for_function("location.hash === '#/libraries/home-server'")
    page.wait_for_selector('input[id$="password"]')
    assert page.locator('input[id$="password"]').get_attribute("type") == "password"
    assert (
        page.locator('input[id$="password"]').input_value() == "********"
    )  # never the secret itself
    page.fill('input[id$="username"]', "me2")
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector(".toast:has-text('Saved')")
    # saving the mask back keeps the stored password
    cfg = run_on(app, app.store.library("home-server"))
    assert cfg.settings["password"] == "s3cret" and cfg.settings["username"] == "me2"
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_deep_links_show_shared_strings_not_raw_keys(web_server, chrome, tmp_path):
    """a fresh page load straight at a library or a user template (Devices never visited) shows the shared
    strings ('Remove this library', 'Delete'), not raw keys."""
    url, _ = web_server
    (tmp_path / "m").mkdir()
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "m", "type": "folder", "name": "M", "settings": {"root": str(tmp_path / "m")}},
    )
    tpl = _api(url, "POST", "/templates", json={"name": "Mine", "rules": []})
    for path, sel, text in (
        ("/#/libraries/m", ".danger-row b", "Remove this library"),
        # the v2 template editor's Delete sits in its action bar
        (f"/#/templates/{tpl['id']}", "[data-tpl-actions] [data-action='remove']", "Delete"),
    ):
        page = open_page(chrome, url + path)
        page.wait_for_selector(sel)
        assert page.locator(sel).inner_text() == text, path
        assert (
            "dev." not in page.locator("#content").inner_text()
            and "common." not in page.locator("#content").inner_text()
        )
        assert page.errors == []


def test_folder_library_of_each_media_type(web_server, chrome, tmp_path):
    """the add form asks a folder library's media type (music / audiobooks / podcasts); the per-subfolder
    mapping sits under Advanced; the list shows the type."""
    url, app = web_server
    for kind, label in (("music", "Music"), ("audiobook", "Audiobooks"), ("podcast", "Podcasts")):
        root = tmp_path / kind
        make_music(root / "x" / "01.mp3", title=f"{kind} 1", artist="A", album="B")
        page = open_page(chrome, url + "/#/libraries/new?type=folder")
        page.wait_for_selector('select[id$="default_kind"]')
        field = page.locator('select[id$="default_kind"]')
        assert (
            page.locator(f'label[for="{field.get_attribute("id")}"]')
            .inner_text()
            .startswith("Media type")
        )
        assert {o.inner_text() for o in field.locator("option").all()} >= {
            "Music",
            "Audiobooks",
            "Podcasts",
        }
        assert not page.locator(
            '[data-advanced] [id$="subfolder_kinds"]'
        ).is_visible()  # under Advanced
        page.fill("#libName", f"Folder {label}")
        page.fill('input[id$="root"]', str(root))
        field.select_option(kind)
        page.get_by_role("button", name="Save and scan").click()
        page.wait_for_selector(".toast:has-text('added and reachable')")
        lib = next(x for x in _api(url, "GET", "/libraries") if x["name"] == f"Folder {label}")
        assert lib["settings"]["default_kind"] == kind
        items = _api(url, "GET", f"/libraries/{lib['id']}/items")["items"]
        assert {i["kind"] for i in items} == {kind}
        assert page.errors == []
    page.goto(url + "/#/libraries")
    page.wait_for_selector("text=Folder Audiobooks")
    text = page.locator(".lib-grid").inner_text()
    assert all(f"Folder library · {k}" in text for k in ("Audiobooks", "Podcasts", "Music"))
    shot(page, "libraries-media-types-1280")

    # a device with only folder libraries: reverse sync is shown as not available
    (tmp_path / "pod").mkdir()
    from tests.contracts.harness import ipod_tree

    ipod_tree(tmp_path / "pod")
    lib_ids = [x["id"] for x in _api(url, "GET", "/libraries")]
    _api(
        url,
        "POST",
        "/devices/register",
        json={
            "name": "Pod",
            "mount_path": str(tmp_path / "pod"),
            "type": "ipod",
            "library_ids": lib_ids,
        },
    )
    page.goto(url + "/#/devices/pod/settings")
    page.wait_for_selector('#reverseCard [data-no-reverse="library"]')


def test_browse_tabs_by_library_type_and_send_exact_files(web_server, chrome, tmp_path):
    """the browse tabs follow the library type (music: Songs / Albums / Artists / Playlists, audiobooks:
    Books, Audiobookshelf: Books / Podcasts); a playlist or a book is confirmed with its exact files, then the
    plan review opens."""
    url, app = web_server
    music = tmp_path / "music"
    for artist, title in (("Скрябін", "Група крові"), ("Скрябін", "Зозуля"), ("Тартак", "Орбіт")):
        make_music(
            music / artist / f"{title}.mp3", title=title, artist=artist, album=f"{artist} album"
        )
    (music / "Дорога.m3u").write_text("Тартак/Орбіт.mp3\nСкрябін/Зозуля.mp3\n")
    for n in (1, 2):  # a podcast subfolder gets its own tab
        make_music(music / "Podcasts" / "Радіо" / f"{n}.mp3", title=f"Випуск {n}", artist="Радіо")
    books = tmp_path / "books"
    for n in (1, 2):
        make_music(books / "Тигролови" / f"0{n}.mp3", title=f"Розділ {n}", artist="Багряний")
    plain = tmp_path / "plain"
    make_music(plain / "Тартак" / "Орбіт.mp3", title="Орбіт", artist="Тартак", album="Альбом")
    for lid, root, kind in (
        ("music", music, "music"),
        ("books", books, "audiobook"),
        ("plain", plain, "music"),
    ):
        subfolders = {"Podcasts": "podcast"} if lid == "music" else {}
        _api(
            url,
            "POST",
            "/libraries",
            json={
                "id": lid,
                "type": "folder",
                "name": lid,
                "settings": {
                    "root": str(root),
                    "default_kind": kind,
                    "subfolder_kinds": subfolders,
                },
            },
        )
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "abs",
            "type": "audiobookshelf",
            "name": "ABS",
            "settings": {"url": "http://127.0.0.1:9", "api_token": "t"},
        },
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
            "rule_set": {},  # nothing but what is sent from the library
        },
    )

    (tmp_path / "rb" / ".rockbox").mkdir(parents=True)
    _api(
        url,
        "POST",
        "/devices/register",
        json={
            "name": "Mini",
            "mount_path": str(tmp_path / "rb"),
            "type": "rockbox",
            "rule_set": {},
        },
    )

    def tabs(page):
        return [x.get_attribute("data-tab") for x in page.locator(".tabs .tab").all()]

    def wait_tabs(page, want):
        # after a hash navigation the old view's tabs may still be up (flaky under load)
        page.wait_for_function(
            "want => JSON.stringify([...document.querySelectorAll('.tabs .tab')]"
            ".map(x => x.dataset.tab)) === JSON.stringify(want)",
            arg=want,
        )

    page = open_page(chrome, url + "/#/libraries/music/browse")
    page.wait_for_selector("text=3 items")
    assert tabs(page) == ["songs", "albums", "artists", "playlists", "podcasts"]
    # the tabs carry their counts
    assert page.locator('.tabs [data-tab="artists"] .tab-n').inner_text() == "2"
    assert page.locator('.tabs [data-tab="playlists"] .tab-n').inner_text() == "1"
    assert page.locator('.tabs [data-tab="podcasts"] .tab-n').inner_text() == "2"
    page.get_by_role("tab", name="Podcasts").click()
    page.wait_for_selector('input[name="sel"][aria-label="Випуск 2"]').check()
    shot(page, "library-tabs-podcasts-1280")
    _add_to(page, "Stick", files=1)
    assert _api(url, "POST", "/devices/stick/plan", json={})["counts"]["add"] == 1
    page.get_by_role("tab", name="Songs").click()
    page.wait_for_selector("text=3 items")
    page.get_by_role("tab", name="Artists").click()
    page.wait_for_selector("text=2 items")
    page.get_by_role("tab", name="Playlists").click()
    page.wait_for_selector('input[name="sel"][aria-label="Дорога"]').check()
    _add_to(page, "Stick", files=2)  # the playlist's two files
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    assert plan["counts"]["add"] == 3  # the episode and the playlist's two files

    # a music folder without playlists or other kinds: no empty tabs
    page.goto(url + "/#/libraries/plain/browse")
    page.wait_for_selector("text=1 item")
    wait_tabs(page, ["songs", "albums", "artists"])

    # a books library: the Rockbox player is offered but disabled (music only)
    page.goto(url + "/#/libraries/books/browse")
    page.wait_for_selector("text=1 item")
    wait_tabs(page, ["books"])
    page.locator('input[name="sel"][aria-label="Тигролови"]').check()
    page.get_by_role("button", name="Add to device…").click()
    modal = page.locator(".modal")
    modal.wait_for()
    mini = modal.locator(".target", has_text="Mini")
    assert "Rockbox players get music only" in mini.inner_text()
    assert mini.locator("input").is_disabled()
    modal.get_by_role("button", name="Cancel").click()

    page.goto(url + "/#/libraries/abs/browse")
    page.wait_for_selector(".tabs .tab:has-text('Podcasts')")
    assert tabs(page) == ["books", "podcasts"]
    # on a phone the tab row scrolls: the open tab is brought into sight
    page = open_page(chrome, url + "/#/libraries/music/browse?tab=podcasts", width=390)
    page.wait_for_selector('input[name="sel"]')
    box = page.locator('.tabs [data-tab="podcasts"]').bounding_box()
    assert box and box["x"] + box["width"] <= 390
    shot(page, "library-tabs-podcasts-390")
    for width in (390, 1280):
        page = open_page(chrome, url + "/#/libraries/music/browse?tab=albums", width=width)
        page.wait_for_selector('input[name="sel"]')
        assert no_horizontal_scroll(page)
        shot(page, f"library-tabs-{width}")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_library_browser_v2(web_server, chrome, tmp_path, width):
    """Library browser: grid / list (remembered), the sticky selection bar with Clear, adding to a
    template with Undo in the toast, "On devices" chips after a sync, sort, the no-match text; light theme too."""
    url, app = web_server
    music = tmp_path / "music"
    long = "Дуже довга назва альбому, яка не вміщається в один рядок картки"
    for artist, album, title in (
        ("Скрябін", "Група крові", "Група крові"),
        ("Скрябін", "Група крові", "Зозуля"),
        ("ДахаБраха", long, "Це все"),
    ):
        make_music(music / artist / album / f"{title}.mp3", title=title, artist=artist, album=album)
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Музика", "settings": {"root": str(music)}},
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
            "rule_set": {},
        },
    )
    tpl = _api(url, "POST", "/templates", json={"name": "Дорога", "library_ids": ["music"]})

    page = open_page(chrome, url + "/#/libraries/music/browse?tab=albums", width=width)
    page.wait_for_selector("[data-browse-grid]")  # albums open as artwork cards
    assert page.locator(".bcard").count() == 2
    # selecting shows the bar; Clear empties it
    bar = page.locator("[data-selbar]")
    assert bar.is_hidden()
    page.locator('.bcard-art:has(input[aria-label="Група крові"])').click()
    assert bar.is_visible() and bar.locator("[data-sel-count]").inner_text() == "1 selected"
    bar.get_by_role("button", name="Clear").click()
    assert bar.is_hidden()
    assert no_horizontal_scroll(page)
    shot(page, f"library-browser-grid-{width}")

    # to a template, then Undo: only what was new goes again
    # (no device uses it yet: the toast promises no plan)
    page.locator('input[name="sel"][aria-label="Група крові"]').check()
    _add_to(page, "Дорога", undo=True, text="No device uses this template yet.")
    # the template is as it was before the add (no empty Manual selection rule)
    assert _api(url, "GET", f"/templates/{tpl['id']}")["rules"] == []
    # used by the stick: the toast names its plan
    _api(url, "PATCH", "/devices/stick", json={"rule_set": {"template_ids": [tpl["id"]]}})
    page.locator('input[name="sel"][aria-label="Група крові"]').check()
    _add_to(page, "Дорога", undo=True, text="2 files will be in the next plan of its 1 device.")
    _api(url, "PATCH", "/devices/stick", json={"rule_set": {"template_ids": []}})

    # to the device, synced: the chip shows where the album is
    page.locator('input[name="sel"][aria-label="Група крові"]').check()
    _add_to(page, "Stick", files=2)
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    page.reload()
    card = page.locator('.bcard:has(input[aria-label="Група крові"])')
    card.locator(".ondev").wait_for()
    assert card.locator(".ondev").inner_text() == "Stick"
    assert page.locator(f'.bcard:has(input[aria-label="{long}"]) .ondev').count() == 0

    # half an album on a device: a "1/2" chip; a long device name ends in "…" inside the card
    _api(url, "PATCH", "/devices/stick", json={"name": "Флешка з дуже довгою назвою для перевірки"})
    items = _api(url, "GET", "/libraries/music/items")["items"]
    kuku = next(i["item_id"] for i in items if i["title"] == "Зозуля")
    job = _api(url, "POST", "/devices/stick/contents/remove", json={"keys": [f"music/{kuku}"]})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    page.reload()
    chip = card.locator(".ondev[data-partly]")
    chip.wait_for()
    assert chip.locator(".ondev-n").inner_text() == "1/2"
    # cut, with the ellipsis - measured in one evaluate on a connected node: an update after the removal job may
    # re-render the card between two calls, and a detached node has no computed style
    page.wait_for_function(
        """() => { const e = document.querySelector('.ondev[data-partly] .ondev-name');
          return !!e && e.isConnected && e.scrollWidth > e.clientWidth
            && getComputedStyle(e).textOverflow === 'ellipsis'; }""",
        timeout=10000,
    )
    # inside its card (measured in one go: the browser may redraw the grid meanwhile)
    assert chip.evaluate(
        "e => e.getBoundingClientRect().right <= e.closest('.bcard').getBoundingClientRect().right + 0.5"
    )
    shot(page, f"library-browser-partly-{width}")

    # the list view is remembered; sort and search keep the tab
    page.get_by_role("button", name="List").click()
    page.wait_for_selector("[data-browse-list]")
    page.goto(url + "/#/libraries/music/browse?tab=artists")
    # a hash change re-renders later: wait for the artists view, not the old albums list
    page.wait_for_selector('.tab[data-tab="artists"][aria-selected="true"]')
    page.wait_for_selector("[data-browse-list]")
    page.evaluate("document.querySelector('#sort').dataset.stale = '1'")
    page.select_option("#sort", "year")
    page.wait_for_function(
        "location.hash.includes('sort=year') && location.hash.includes('tab=artists')"
    )
    page.wait_for_selector("#sort:not([data-stale])")  # re-rendered: #q is the new input
    page.fill("#q", "немає такого")
    page.locator('form[data-action="search"] button').click()
    page.wait_for_selector("[data-browse-empty]")
    assert (
        "Nothing matches “немає такого” in artists."
        in page.locator("[data-browse-empty]").inner_text()
    )
    # light theme, a chip on the song still on the device (Зозуля was removed)
    page.evaluate("localStorage.setItem('syncmusic.theme', 'light')")
    page.goto(url + "/#/libraries/music/browse?tab=songs")
    page.reload()
    page.wait_for_selector("[data-browse-list] .ondev")
    assert page.locator("[data-browse-list] .ondev").count() == 1
    assert no_horizontal_scroll(page)
    shot(page, f"library-browser-list-light-{width}")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_library_cards_v2(web_server, chrome, tmp_path, width):
    """List: the empty state; one card per library with what it holds per kind (from the loaded
    snapshot), an unreachable library's reason on its card, a running refresh's progress bar that follows the job,
    and Refresh all."""
    import asyncio

    url, app = web_server
    page = open_page(chrome, url + "/#/libraries", width=width)
    page.wait_for_selector("[data-libs-empty] a[href='#/libraries/new']")
    make_music(tmp_path / "music" / "a.mp3", title="A", artist="X", album="Y")
    make_music(tmp_path / "music" / "b.mp3", title="B", artist="X", album="Y")
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "music",
            "type": "folder",
            "name": "Музика",
            "settings": {"root": str(tmp_path / "music")},
        },
    )
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "nd",
            "type": "subsonic",
            "name": "Navidrome",
            "settings": {"url": "http://127.0.0.1:9", "username": "u", "password": "p"},
        },
    )
    job = _api(url, "POST", "/libraries/music/refresh")
    run_on(app, app.jobs.wait(job["id"]), 60)
    gate = asyncio.Event()

    async def scan():
        async def slow(ctx):
            ctx.progress(0.25, "scanning")
            await gate.wait()

        return app.jobs.submit("refresh", "refresh Музика", slow, library_id="music").id

    page.reload()
    page.wait_for_selector('[data-library="music"] [data-count="tracks"]')
    assert page.locator('[data-library="music"] [data-count="tracks"]').inner_text() == "2 tracks"
    nd = page.locator('[data-library="nd"]')
    page.wait_for_selector('[data-library="nd"] .lib-problem')
    assert "not reachable" in nd.locator(".lib-problem").inner_text()
    assert nd.locator("[data-health]").inner_text() == "Unreachable"
    sid = run_on(app, scan())
    bar = page.locator(f'[data-library="music"] [data-scan="{sid}"]')
    bar.wait_for()
    run_on(app, _progress(app, sid))
    page.wait_for_function(
        f"document.querySelector('[data-scan=\"{sid}\"] .bar').getAttribute('aria-valuenow') === '60'"
    )
    assert page.locator('[data-library="music"] [data-action="refresh"]').is_disabled()
    shot(page, f"libraries-v2-{width}")
    assert no_horizontal_scroll(page)
    run_on(app, _set(gate))
    page.wait_for_selector(f'[data-scan="{sid}"]', state="detached")
    page.locator('[data-action="refreshAll"]').click()
    page.wait_for_selector(".toast:has-text('Refreshing 2 libraries')")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


async def _progress(app, job_id):
    app.bus.publish(
        "job.progress", job_id=job_id, device_id=None, progress=0.6, message="scanning", eta_s=120
    )


async def _set(event):
    event.set()


@pytest.mark.parametrize("width", [1280, 390])
def test_jellyfin_add_form(web_server, chrome, width):
    """Jellyfin in the type choice; its form asks for the address, user, password / API key, and
    secrets stay masked after saving."""
    url, app = web_server
    page = open_page(chrome, url + "/#/libraries/new?type=jellyfin", width=width)
    page.wait_for_selector('input[id$="api_key"]')
    for field in ("url", "username", "password", "api_key"):
        assert page.locator(f'input[id$="{field}"]').count() == 1, field
    assert page.locator('input[id$="password"]').get_attribute("type") == "password"
    page.fill("#libName", f"Jellyfin {width}")
    page.fill('input[id$="url"]', "127.0.0.1:9")  # nothing listens there
    page.fill('input[id$="username"]', "me")
    page.fill('input[id$="api_key"]', "k3y")
    assert no_horizontal_scroll(page)
    shot(page, f"library-add-jellyfin-{width}")
    page.get_by_role("button", name="Save and scan").click()
    page.wait_for_selector(".toast.error:has-text('connection test failed')")
    cfg = run_on(app, app.store.library(f"jellyfin-{width}"))
    assert cfg.type == "jellyfin" and cfg.settings["api_key"] == "k3y"
    assert cfg.settings["url"] == "http://127.0.0.1:9"
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_plex_is_not_offered(web_server, chrome):
    """Plex is not in the type choice of this version."""
    url, _ = web_server
    page = open_page(chrome, url + "/#/libraries/new", width=1280)
    page.wait_for_selector("[data-lib-type]")
    types = page.locator("[data-lib-type]").evaluate_all("els => els.map(e => e.dataset.libType)")
    assert "plex" not in types and {"subsonic", "folder", "audiobookshelf", "jellyfin"} <= set(
        types
    )
    # Jellyfin (video only, one way) is the one library type in beta
    beta = page.locator("[data-lib-type]:has([data-beta])").evaluate_all(
        "els => els.map(e => e.dataset.libType)"
    )
    assert beta == ["jellyfin"]
    assert "Movies and episodes" in page.locator('[data-lib-type="jellyfin"]').inner_text()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
