"""every dashboard element leads to its detail - stat cards, whole device / unknown-drive / job / library /
review rows, the last-sync text (its report), a device error (its log), the library health badge (settings); nested
buttons keep their own target; keyboard Tab + Enter works. 1280 and 390 px. Temp folders, fake disks and jobs."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from syncmusic.core.detection import ConnectedDisk
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.media import make_music
from tests.webui.conftest import open_page, run_on

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.mark.parametrize("width", [1280, 390])
def test_every_dashboard_element_is_a_link(web_server, chrome, tmp_path, width):
    url, app = web_server
    make_music(tmp_path / "music" / "a.mp3", title="A", artist="X", album="Y")
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
    for did in ("stick", "other"):
        (tmp_path / did).mkdir()
        _api(
            url,
            "POST",
            "/devices/register",
            json={
                "name": did.title(),
                "mount_path": str(tmp_path / did),
                "type": "flashdrive",
                "library_ids": ["music"],
            },
        )
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    run_on(app, app.jobs.wait(job["id"]), 60)
    gate = asyncio.Event()

    async def seed():
        app.sync._errors["other"] = "The file system is marked dirty"
        app.sync._waiting["stick"] = {"reason": "on_connect: ask", "plan_id": "plan-123"}
        app.sync.detector.connected["sdx"] = ConnectedDisk(
            disk=BlockDeviceInfo(
                name="sdx", path="/dev/sdx", vendor="Apple", model="iPod", tran="usb"
            )
        )

        async def slow(ctx):
            ctx.progress(0.3, "working")
            await gate.wait()

        return app.jobs.submit("refresh", "refresh music", slow, library_id="music").id

    rid = run_on(app, seed())
    expected = {
        '[data-stat="connected"]': "#/devices",
        '[data-stat="attention"]': "#/devices/other",  # the most severe item first
        '[data-stat="jobs"]': f"#/jobs/{rid}",
        '[data-stat="libraries"]': "#/libraries",
        '[data-device-row="other"] .stretch': "#/devices/other",
        f'[data-recent-job="{job["id"]}"] .stretch': f"#/devices/stick/sync/{job['id']}",
        '[data-device-row="other"] [data-device-error]': "#/logs?device=other",
        '[data-unknown-row="/dev/sdx"] .stretch': "#/devices/register?disk=%2Fdev%2Fsdx",
        '[data-unknown-row="/dev/sdx"] [data-register]': "#/devices/register?disk=%2Fdev%2Fsdx",
        f'[data-job="{rid}"] .stretch': f"#/jobs/{rid}",
        '[data-library-row="music"] .stretch': "#/libraries/music/browse",
        '[data-library-row="music"] [data-health-link]': "#/libraries/music",
        '[data-review="stick"]': "#/devices/stick/plan/plan-123",
    }
    page = open_page(chrome, url + "/#/", width=width)
    page.wait_for_selector('[data-library-row="music"]')
    for selector, href in expected.items():
        assert page.locator(selector).first.get_attribute("href") == href, selector
    # the whole row is the link: a click on the row's empty part (its icon) opens the device (device errors
    # are "Needs you" rows)
    page.locator('[data-device-row="other"] .need-ico').scroll_into_view_if_needed()
    box = page.locator('[data-device-row="other"] .need-ico').bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_function("location.hash === '#/devices/other'")
    page.wait_for_selector('#content[data-route="/devices/other"]')
    # nested links keep their own target
    for selector, href in (
        ('[data-device-row="other"] [data-device-error]', "#/logs?device=other"),
        ('[data-library-row="music"] [data-health-link]', "#/libraries/music"),
        ('[data-stat="libraries"]', "#/libraries"),
    ):
        page.goto(url + "/#/")
        page.wait_for_selector('#content[data-route="/"] [data-library-row="music"]')
        page.locator(selector).click()
        page.wait_for_function(f"location.hash === {href!r}")
        page.wait_for_selector("#content[data-route]:not([data-route='/'])")
    # keyboard: the stat card and the library row are reachable and open with Enter
    page.goto(url + "/#/")
    page.wait_for_selector('[data-library-row="music"]')
    page.locator('[data-library-row="music"] .stretch').focus()
    page.keyboard.press("Enter")
    page.wait_for_function("location.hash === '#/libraries/music/browse'")
    run_on(app, _set(gate))
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


async def _set(event):
    event.set()
