"""Navigation per the new prototype: a burger drawer on phones instead of the bottom bar, the Libraries
split control (label toggles a link per library, the button next to it opens All libraries, no libraries → All
libraries with Add library), Add library as the last item, one card per connected device that opens it and follows
its job's progress."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot
from tests.webui.test_devices import _disk_device

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=30, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _open_menu(page, phone: bool) -> None:
    if not phone:
        return
    page.locator("#nav").wait_for(state="hidden")  # the drawer is closed (after its slide-out)
    page.locator("#burger").click()
    page.wait_for_function("document.body.classList.contains('menu-open')")
    assert page.locator("#burger").get_attribute("aria-expanded") == "true"
    page.locator("#nav").wait_for(state="visible")


def _route(page, path: str) -> None:
    page.wait_for_selector(f"#content[data-route='{path}']")


def _closed(page) -> bool:
    return not page.evaluate("document.body.classList.contains('menu-open')")


@pytest.mark.parametrize("width", [1280, 390])
def test_libraries_split_control_and_add_library_in_the_dropdown(
    web_server, chrome, tmp_path, width
):
    url, _ = web_server
    phone = width < 760
    page = open_page(chrome, url, width)
    _route(page, "/")
    if phone:
        assert page.locator("#burger").is_visible()
        assert page.locator(".mobilebar").count() == 0  # no bottom navigation any more
    else:
        assert not page.locator("#burger").is_visible()

    # no Add library item at the end of the menu: with no libraries the dropdown offers it
    _open_menu(page, phone)
    assert page.locator("#nav [data-nav=add-library]").count() == 0
    toggle = page.locator("#nav [data-libtoggle]")
    assert toggle.get_attribute("aria-expanded") == "false"
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true"
    assert page.locator("#libLinks a").all_inner_texts() == ["Add library"]
    assert page.locator('#libLinks a svg[data-icon="plus"]').count() == 1
    shot(page, f"nav-empty-libraries-{width}")
    page.locator("#libLinks [data-lib-add]").click()
    _route(page, "/libraries/new")
    assert _closed(page)
    page.goto(url + "/#/")
    _route(page, "/")

    for lid, name in (("alpha", "Alpha"), ("beta", "Beta")):
        (tmp_path / lid).mkdir()
        _api(
            url,
            "POST",
            "/libraries",
            json={
                "id": lid,
                "type": "folder",
                "name": name,
                "settings": {"root": str(tmp_path / lid)},
            },
        )
    page.reload()
    _route(page, "/")

    # the label shows and hides a link per library
    _open_menu(page, phone)
    toggle = page.locator("#nav [data-libtoggle]")
    assert toggle.get_attribute("aria-expanded") == "false"
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true"
    assert page.locator("#libLinks a").all_inner_texts() == ["Alpha", "Beta"]  # no Add library
    shot(page, f"nav-libraries-{width}")
    page.locator("#libLinks a", has_text="Beta").click()
    _route(page, "/libraries/beta/browse")
    assert _closed(page)
    _open_menu(page, phone)
    assert page.locator("#libLinks a[aria-current=page]").inner_text() == "Beta"
    page.locator("#nav [data-libtoggle]").click()
    assert page.locator("#libLinks").count() == 0

    # the button next to the label opens All libraries
    page.locator("#nav [data-nav-all]").click()
    _route(page, "/libraries")
    _open_menu(page, phone)
    assert page.locator("#nav [data-nav-all]").get_attribute("aria-current") == "page"

    assert no_horizontal_scroll(page)
    assert page.errors == []


def test_burger_closes_with_escape_and_backdrop(web_server, chrome):
    width = 390
    url, _ = web_server
    page = open_page(chrome, url, width)
    _route(page, "/")
    _open_menu(page, True)
    page.keyboard.press("Escape")
    page.wait_for_function("!document.body.classList.contains('menu-open')")
    assert page.evaluate("document.activeElement.id") == "burger"
    _open_menu(page, True)
    page.mouse.click(width - 10, 400)  # the backdrop right of the drawer
    page.wait_for_function("!document.body.classList.contains('menu-open')")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_device_card_opens_the_device_and_shows_progress(web_server, chrome, width):
    url, app = web_server
    phone = width < 760
    run_on(app, _disk_device(app, []))
    release = asyncio.Event()

    async def job(ctx):
        ctx.progress(0.42, "Copying 3 of 7")
        await release.wait()

    async def start():
        return app.jobs.submit("sync", "Sync Zorvex", job, device_id="zorvex")

    page = open_page(chrome, url, width)
    _route(page, "/")
    _open_menu(page, phone)
    card = page.locator("#sideBottom [data-device-card=zorvex]")
    # this device has no templates: the short 648.1 badge instead of "Connected"
    assert "Zorvex" in card.inner_text() and "No templates" in card.inner_text()

    run_on(app, start())
    page.wait_for_function(
        "document.querySelector('[data-device-card=zorvex]')?.innerText.includes('42%')"
    )
    # the card shows the time left, not the current file (40249e0); this fake job has no ETA yet
    assert "Estimating the time left" in card.inner_text()
    assert "Copying 3 of 7" not in card.inner_text()
    assert card.locator("[role=progressbar]").get_attribute("aria-valuenow") == "42"

    async def finish():
        release.set()

    run_on(app, finish())
    page.wait_for_function(
        "!document.querySelector('[data-device-card=zorvex]')?.innerText.includes('%')"
    )
    card.click()
    _route(page, "/devices/zorvex")
    assert _closed(page)
    assert no_horizontal_scroll(page)
    # the fake disk cannot be measured (the guard refuses /dev/sdz): that 403 is expected, like test_devices.py
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_two_devices_sync_at_once_with_their_own_progress(web_server, chrome, tmp_path):
    """two folder devices sync at the same time, a third waits for a free slot (limit 2); every card says
    its state in words, the Devices item counts the running syncs, and a finished job leaves synced / failed."""
    url, app = web_server
    (
        tmp_path / "music"
    ).mkdir()  # devices with a library: one without says "no libraries", not "synced"
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "music",
            "type": "folder",
            "name": "Music",
            "settings": {"root": str(tmp_path / "music")},
        },
    )
    for did in ("a", "b", "c"):
        (tmp_path / did).mkdir()
        _api(
            url,
            "POST",
            "/devices/register",
            json={
                "name": f"Dev {did.upper()}",
                "type": "flashdrive",
                "mount_path": str(tmp_path / did),
                "rule_set": {"template_ids": ["default.music.all"]},
            },
        )
    _api(url, "PATCH", "/settings", json={"max_device_syncs": 2})
    gates = {did: asyncio.Event() for did in "abc"}

    def job(did, pct):
        async def run(ctx):
            ctx.progress(pct, f"Copying on {did}")
            await gates[did].wait()
            if did == "b":
                raise RuntimeError("disk full")

        return run

    page = open_page(chrome, url, 1280)
    _route(page, "/")

    async def start():
        for did, pct in (("a", 0.25), ("b", 0.6), ("c", 0.1)):
            app.jobs.submit("sync", f"Sync {did}", job(did, pct), device_id=f"dev-{did}")

    run_on(app, start())
    card = {did: page.locator(f"#sideBottom [data-device-card=dev-{did}]") for did in "abc"}
    page.wait_for_function(
        "document.querySelector('[data-device-card=dev-a]')?.innerText.includes('25%')"
        " && document.querySelector('[data-device-card=dev-b]')?.innerText.includes('60%')"
    )
    assert (
        card["a"].get_attribute("data-state") == "running"
        and "Estimating the time left"
        in card["a"].inner_text()  # time left, not the file (40249e0)
    )
    assert card["b"].get_attribute("data-state") == "running"
    page.wait_for_selector("[data-device-card=dev-c][data-state=queued]")
    assert "Waiting: Waiting for another device sync to finish" in card["c"].inner_text()
    assert "%" not in card["c"].inner_text()
    badge = page.locator("#nav [data-nav=devices] [data-devices-badge]")
    assert badge.inner_text() == "3" and badge.get_attribute("title") == "3 devices syncing"

    async def release(did):
        gates[did].set()

    run_on(app, release("a"))  # c gets a's slot
    page.wait_for_selector("[data-device-card=dev-a][data-state=done]")
    assert "synced" in card["a"].inner_text()
    page.wait_for_function(
        "document.querySelector('[data-device-card=dev-c]')?.innerText.includes('10%')"
    )
    run_on(app, release("b"))
    page.wait_for_selector("[data-device-card=dev-b][data-state=failed]")
    page.wait_for_function(  # the state attribute comes first, the card's text with its next draw
        "document.querySelector('[data-device-card=dev-b]')?.innerText.includes('Last sync failed')"
    )
    page.wait_for_function(
        "document.querySelector('#nav [data-nav=devices] [data-devices-badge]')?.innerText === '10%'"
    )
    assert badge.get_attribute("title") == "Dev C: 10%"
    run_on(app, release("c"))
    page.wait_for_selector("[data-device-card=dev-c][data-state=done]")
    assert badge.count() == 0

    page.reload()  # the outcomes survive a reload (taken from /jobs)
    page.wait_for_selector("[data-device-card=dev-b][data-state=failed]")
    assert card["a"].get_attribute("data-state") == "done"
    assert page.errors == []
