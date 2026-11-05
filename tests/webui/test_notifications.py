"""The notifications centre in a browser: the bell's unread count (also live), the list with
words per event, mark one / all read, the unread tab, a linked notification opens its page, the state survives a
reload, Clear all; phone and desktop width, no console errors."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


async def _seed(app) -> None:
    await app.store.add_notification(
        "sync.failed",
        "error",
        {"device_id": "x", "error": "the device did not open"},
        device_id="x",
        device_name="Classic",
        link="/logs",
    )
    await app.store.add_notification(
        "device.unknown", "info", {"vendor": "Zorvex", "model": "FlashStick 3.0"}, link=None
    )


async def _live(app) -> None:
    # what the notifications service does for a bus event
    from syncmusic.infra.events import Event

    async def name(_):
        return "Флешка в машині з дуже довгою назвою для перевірки переносу"

    app.notifications.device_name = name
    await app.notifications.store_entry(
        app.store,
        Event(
            type="sync.finished",
            data={"device_id": "y", "job_id": "j", "status": "partial", "failed": 2},
        ),
    )


def _count(page) -> str:
    return page.get_attribute("#bell", "data-unread")


@pytest.mark.parametrize("width", [1280, 390])
def test_bell_list_read_clear(web_server, chrome, width):
    url, app = web_server
    run_on(app, _seed(app))
    page = open_page(chrome, url + "/#/", width)
    page.wait_for_function("document.querySelector('#bell').dataset.unread === '2'")
    assert page.inner_text("[data-bell-count]") == "2"

    run_on(app, _live(app))  # announced while the page is open
    page.wait_for_function("document.querySelector('#bell').dataset.unread === '3'")

    # the design: the bell opens a drawer with the newest; Escape closes it
    page.click("#bell")
    drawer = page.locator("#bellDrawer")
    drawer.locator("[data-drawer-item]").first.wait_for()
    assert drawer.locator("[data-drawer-item]").count() == 3
    assert drawer.locator(".drawer-dot").count() == 3
    assert "3 unread" in drawer.locator("[data-drawer-count]").inner_text()
    assert no_horizontal_scroll(page)
    shot(page, f"notifications-drawer-{width}")
    page.keyboard.press("Escape")
    page.locator("#bellDrawer").wait_for(state="detached")
    page.click("#bell")
    drawer.locator("[data-drawer-item]").first.wait_for()
    page.locator("#bellDrawer [data-drawer-all]").click()
    page.wait_for_selector("#content[data-route='/notifications'] [data-notification]")
    page.locator("#bellDrawer").wait_for(state="detached")
    rows = page.locator("[data-notification]")
    assert rows.count() == 3
    texts = rows.all_inner_texts()
    assert (
        "Флешка в машині з дуже довгою назвою для перевірки переносу: sync finished, 2 items failed"
        in texts[0]
    )
    assert "An unknown device was connected: Zorvex FlashStick 3.0" in texts[1]
    assert "Classic: sync failed" in texts[2] and "the device did not open" in texts[2]
    assert (
        page.locator(
            "[data-notification][data-level='error'] .notif-ico[data-tone='error']"
        ).count()
        == 1
    )
    # the design: each item's own action, named by what it opens
    assert (
        rows.nth(1).get_by_role("link", name="Register").get_attribute("href")
        == "#/devices/register"
    )
    assert rows.nth(2).get_by_role("link", name="Open logs").count() == 1
    assert "3 unread · kept 7 days" in page.inner_text("[data-notif-head]")
    assert no_horizontal_scroll(page)
    shot(page, f"notifications-{width}")

    page.get_by_role("tab", name="Unread").click()
    page.wait_for_function("document.querySelectorAll('[data-notification]').length === 3")
    page.get_by_role("tab", name="Problems").click()  # warnings and errors
    page.wait_for_function("document.querySelectorAll('[data-notification]').length === 2")
    page.get_by_role("tab", name="All").click()
    page.wait_for_function("document.querySelectorAll('[data-notification]').length === 3")

    # a linked notification opens its page and counts as read
    page.locator("[data-notification][data-level='error']").get_by_role(
        "link", name="Open logs"
    ).click()
    page.wait_for_selector("#content[data-route='/logs']")
    page.wait_for_function("document.querySelector('#bell').dataset.unread === '2'")

    # an item in the drawer opens its subject and counts as read (the unknown drive: Register)
    run_on(app, app.store.mark_notifications_read(None))
    run_on(app, app.store.mark_notifications_read([2], read=False))
    page.reload()
    page.wait_for_function("document.querySelector('#bell').dataset.unread === '1'")
    page.click("#bell")
    page.locator("#bellDrawer [data-drawer-item][data-read='false']").first.click()
    page.wait_for_function("document.querySelector('#bell').dataset.unread === '0'")
    page.wait_for_selector("#content[data-route='/devices/register']")
    page.goto(url + "/#/notifications")
    page.wait_for_selector("#content[data-route='/notifications'] [data-notification]")
    # × clears one notification (read or not), Clear all every one
    assert page.locator("[data-notification]").count() == 3
    page.locator("[data-notification][data-level='info']").get_by_role(
        "button", name="Clear"
    ).click()
    page.wait_for_function("document.querySelectorAll('[data-notification]').length === 2")
    assert len(run_on(app, app.store.notifications())) == 2
    run_on(app, app.store.mark_notifications_read([1], read=False))
    page.reload()
    page.wait_for_selector("#content[data-route='/notifications'] [data-notification]")
    shot(page, f"notifications-clear-{width}")
    page.get_by_role("button", name="Clear all").click()
    page.wait_for_selector("text=No notifications yet.")
    page.wait_for_function("document.querySelector('#bell').dataset.unread === '0'")
    assert page.locator("[data-bell-count]").count() == 0
    assert run_on(app, app.store.notifications()) == []
    assert page.get_by_role("button", name="Clear all").is_disabled()
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_bell_drawer_clears_all(web_server, chrome, width):
    """ "Clear all" in the bell's drawer deletes every notification; disabled when empty."""
    url, app = web_server
    run_on(app, _seed(app))
    page = open_page(chrome, url + "/#/", width)
    page.wait_for_function("document.querySelector('#bell').dataset.unread === '2'")
    page.click("#bell")
    drawer = page.locator("#bellDrawer")
    drawer.locator("[data-drawer-item]").first.wait_for()
    button = drawer.locator("[data-drawer-clear-all]")
    assert button.is_enabled() and button.inner_text() == "Clear all"
    assert no_horizontal_scroll(page)
    # let the slide-in finish, or the shot shows the page through a half-opaque drawer
    drawer.evaluate("el => Promise.all(el.getAnimations({subtree: true}).map(a => a.finished))")
    shot(page, f"bell-clear-all-{width}")
    button.click()
    page.wait_for_function("document.querySelector('#bell').dataset.unread === '0'")
    drawer.get_by_text("No notifications yet.").wait_for()
    assert button.is_disabled() and drawer.locator("[data-drawer-count]").inner_text() == ""
    assert drawer.locator("[data-drawer-item]").count() == 0
    assert page.locator("[data-bell-count]").count() == 0
    assert run_on(app, app.store.notifications()) == []
    shot(page, f"bell-clear-all-done-{width}")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_sidebar_notifications_item(web_server, chrome, width):
    """a Notifications item right below Settings with the bell's unread count (live), current on
    #/notifications; at 390 px it is in the ☰ drawer."""
    url, app = web_server
    run_on(app, _seed(app))
    page = open_page(chrome, url + "/#/", width)
    page.wait_for_selector("#nav a[data-nav='notifications']", state="attached")
    navs = page.eval_on_selector_all("#nav [data-nav]", "els => els.map(e => e.dataset.nav)")
    assert navs[navs.index("settings") + 1] == "notifications", navs
    page.wait_for_function(
        "document.querySelector('[data-notif-badge]')?.textContent === '2'", timeout=10_000
    )
    run_on(app, _live(app))
    page.wait_for_function("document.querySelector('[data-notif-badge]')?.textContent === '3'")
    if width < 760:
        page.click("#burger")
    page.click("#nav a[data-nav='notifications']")
    page.wait_for_selector("#content[data-route='/notifications'] [data-notification]")
    assert page.get_attribute("#nav a[data-nav='notifications']", "aria-current") == "page"
    assert no_horizontal_scroll(page)
    shot(page, f"sidebar-notifications-{width}")
