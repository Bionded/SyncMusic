"""Web UI shell: served next to the API, five destinations, phone + desktop layouts, keyboard focus,
shared dialogs that cannot be confirmed by accident."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from syncmusic.webui import STATIC_DIR, web_ui
from tests.webui.conftest import no_horizontal_scroll, open_page

DESTINATIONS = ["dashboard", "libraries", "devices", "templates", "settings"]


async def test_ui_is_served_and_api_paths_stay_json(config):
    app = Application(config)
    await app.start()
    api = create_api(AppFacade(app), manage_lifecycle=False, ui=web_ui())
    try:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
            r = await c.get("/")
            assert r.status_code == 200 and "<title>SyncMusic</title>" in r.text
            r = await c.get("/js/app.js")
            assert r.status_code == 200 and "javascript" in r.headers["content-type"]
            r = await c.get("/api/v1/nope")
            assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
            assert (await c.get("/api/v1/health")).json()["status"] == "ok"
            for missing in (
                "/nosuch.js",
                "/settings",
            ):  # was a 500 with a traceback in the log
                r = await c.get(missing)
                assert r.status_code == 404 and r.text == "Not Found", missing
    finally:
        await app.stop()


async def test_static_files_are_revalidated_on_every_load():
    """no heuristic caching - after an update the browser must not run new JS with an old stylesheet."""
    async with AsyncClient(transport=ASGITransport(app=web_ui()), base_url="http://t") as c:
        for path in ("/", "/app.css", "/js/app.js"):
            r = await c.get(path)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
            again = await c.get(path, headers={"if-none-match": r.headers["etag"]})
            assert again.status_code == 304, path


def test_every_destination_has_a_view_module():
    for name in DESTINATIONS:
        assert (STATIC_DIR / "js" / "views" / f"{name}.js").is_file(), name


def test_no_external_resources():
    """The UI works offline on the LAN: no CDN fonts or scripts."""
    for f in STATIC_DIR.rglob("*"):
        if f.suffix in (".html", ".css", ".js"):
            text = f.read_text().replace(
                "http://www.w3.org/2000/svg", ""
            )  # a namespace, not a request
            # the help popovers' "Read more" target: a link the user opens, not a resource
            text = text.replace("https://github.com/Bionded/SyncMusic/blob/main/", "")
            assert "https://" not in text and "http://" not in text, f


@pytest.mark.ui
@pytest.mark.slow
@pytest.mark.parametrize("width", [1280, 375])
def test_shell_navigation_at_desktop_and_phone_width(web_server, chrome, width):
    url, _ = web_server
    page = open_page(chrome, url, width)
    phone = width < 760
    page.wait_for_selector("#nav a", state="attached")  # in the closed drawer on phones
    for name in DESTINATIONS:
        if phone:  # a burger drawer instead of the bottom bar
            page.locator("#burger").click()
            page.wait_for_function("document.body.classList.contains('menu-open')")
        sel = "#nav [data-nav-all]" if name == "libraries" else f"#nav [data-nav={name}]"
        page.locator(sel).click()
        page.wait_for_function(
            f"document.querySelector('{sel}').getAttribute('aria-current') === 'page'"
        )
        # the new screen, not the previous one (a navigation shows "Loading" until it is drawn)
        page.wait_for_selector("#content[data-route] h1")
        assert page.locator("#content h1").first.is_visible()
        assert no_horizontal_scroll(page), name
    assert page.locator("#livePill").inner_text().strip() == "Live"
    assert page.errors == []


@pytest.mark.ui
@pytest.mark.slow
@pytest.mark.parametrize("width", [1280, 390])
def test_top_bar_breadcrumbs_and_reconnect_banner(web_server, chrome, tmp_path, width):
    """Shell: breadcrumbs (section link / screen) on wide screens, the page title alone on
    phones; while the event stream is down the pill says Reconnecting… and a banner offers Retry now."""
    url, _ = web_server
    (tmp_path / "music").mkdir()
    httpx.post(
        url + "/api/v1/libraries",
        json={
            "id": "music",
            "type": "folder",
            "name": "Музика",
            "settings": {"root": str(tmp_path / "music")},
        },
    ).raise_for_status()
    page = open_page(chrome, url + "/#/libraries/music", width)
    page.wait_for_selector("#content[data-route='/libraries/music'] h1")
    page.wait_for_function("document.querySelector('#pageTitle').innerText === 'Музика'")
    if width >= 760:
        assert page.locator("#crumbs a").all_inner_texts() == ["Libraries"]
        assert page.locator("#crumbs [aria-current=page]").inner_text() == "Музика"
        assert not page.locator("#pageTitle").is_visible()
        page.locator("#crumbs a").click()
        page.wait_for_selector("#content[data-route='/libraries'] h1")
        assert page.locator("#crumbs a").count() == 0  # a section's own page: one crumb
    else:
        assert page.locator("#pageTitle").is_visible() and not page.locator("#crumbs").is_visible()
    assert page.locator("#liveBanner").is_hidden()

    # the stream breaks: the last state stays, a banner says so; Retry now opens it again
    page.route("**/api/v1/events", lambda route: route.abort())
    page.evaluate("import('/js/api.js').then((m) => m.reconnectEvents())")
    page.wait_for_selector("#liveBanner [data-live-retry]")
    assert page.locator("#livePill").get_attribute("data-live-state") == "off"
    assert page.locator("#content h1").is_visible() and no_horizontal_scroll(page)
    page.unroute("**/api/v1/events")
    page.locator("#liveBanner [data-live-retry]").click()
    page.wait_for_selector("#liveBanner", state="hidden")
    assert page.locator("#livePill").get_attribute("data-live-state") == "on"
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.ui
@pytest.mark.slow
def test_keyboard_focus_is_visible(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url)
    # the first screen is drawn (the nav is redrawn with it: a focus before that would be on a removed link)
    page.wait_for_selector("#content[data-route='/'] h1")
    page.wait_for_selector("#nav a")
    page.keyboard.press("Tab")  # skip link
    page.keyboard.press("Tab")  # the brand (opens the dashboard)
    assert page.evaluate("document.activeElement.id") == "brand"
    page.keyboard.press("Tab")  # first nav link
    focused = page.evaluate("document.activeElement.dataset.nav || document.activeElement.tagName")
    assert focused == "dashboard", focused
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert outline not in ("none", ""), outline


@pytest.mark.ui
@pytest.mark.slow
def test_typed_confirmation_needs_the_exact_name(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url)
    page.wait_for_selector("#content h1")
    page.evaluate(
        """async () => {
        const ui = await import('/js/ui.js');
        window.__typed = ui.typedConfirm({title: 'Format', message: 'm', name: 'iPod Classic', ok: 'Format'});
    }"""
    )
    page.locator("#typedName").fill("ipod classic")
    page.get_by_role("button", name="Format").click()
    assert page.locator("#typedName").is_visible()  # still open: the name did not match
    page.locator("#typedName").fill("iPod Classic")
    page.get_by_role("button", name="Format").click()
    assert page.evaluate("window.__typed") is True
    page.evaluate(
        "async () => { const ui = await import('/js/ui.js'); window.__c = ui.confirm({title: 'X', message: 'y', ok: 'Do', danger: true}); }"
    )
    page.keyboard.press("Escape")
    assert page.evaluate("window.__c") is False
    assert page.errors == []


def test_live_server_fixture_works(web_server):
    url, _ = web_server
    assert httpx.get(f"{url}/api/v1/health").json()["status"] == "ok"
