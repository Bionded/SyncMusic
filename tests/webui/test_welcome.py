"""First-run wizard `#/welcome`: from an empty install to a registered device
with a template, a quality profile, a plan and a running first sync. The plugged-in drive is a fake unknown disk;
its registration is redirected to a folder (`mount_path`) so the rest runs for real."""

from __future__ import annotations

import json

import httpx
import pytest

from syncmusic.core.detection import ConnectedDisk
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


async def _plug(app):
    disk = BlockDeviceInfo(
        name="sdx",
        path="/dev/sdx",
        vendor="SanDisk",
        model="Ultra Fit",
        tran="usb",
        size=32 * 10**9,
    )
    app.sync.detector.connected["sdx"] = ConnectedDisk(disk=disk)
    app.bus.publish("device.unknown", disk="/dev/sdx")


@pytest.mark.parametrize("width", [1280, 390])
def test_first_run_wizard(web_server, chrome, tmp_path, width):
    url, app = web_server
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="Зоряна ніч", artist="Океан Ельзи", album="Земля")
    make_music(music / "b.mp3", title="Обійми", artist="Океан Ельзи", album="Земля")
    stick = tmp_path / "stick"
    stick.mkdir()
    page = open_page(chrome, url + "/#/", width=width)
    page.locator("[data-start-wizard]").click()
    page.wait_for_selector('[data-wizard][data-step="library"]')
    assert page.locator(".wiz-bar li").count() == 6
    # 1 Library: Folder, its root
    page.locator('[data-action="wizLibType"][data-type="folder"]').click()
    page.wait_for_selector('[data-type="folder"][aria-checked="true"]')
    # the Add library form: Local / SMB / FTP switch, no server fields for a local folder, Advanced closed
    assert page.locator("[data-folder-source] input[name=libSource]").count() == 3
    assert page.locator('input[name="libSource"][value="local"]').is_checked()
    assert not page.locator("#libAddress").is_visible()
    assert page.locator('input[id$="-host"], input[id$="-port"]').count() == 0
    assert not page.locator("[data-advanced]").get_attribute("open")
    page.locator('input[name="libSource"][value="ftp"] + span').click()
    assert page.locator("#libAddress").is_visible()
    page.locator('input[name="libSource"][value="local"] + span').click()
    page.fill("#wizLibName", "Музика")
    page.locator('input[id$="-root"]').fill(str(music))
    shot(page, f"wiz-library-{width}")
    page.locator("[data-wiz-next]").click()
    # 2 Device: waits, then the plugged-in drive shows up by itself
    page.wait_for_selector("[data-wiz-waiting]")
    shot(page, f"wiz-waiting-{width}")
    run_on(app, _plug(app))
    page.wait_for_selector('input[name="wizDisk"][value="/dev/sdx"]')
    # vendor and model as the title and the suggested name, the pill sized to its text on the same line
    opt = page.locator(".wiz-opt").first
    assert opt.locator("b").inner_text() == "SanDisk Ultra Fit"
    assert page.input_value("#wizDevName") == "SanDisk Ultra Fit"
    card, pill = opt.bounding_box(), opt.locator(".status").bounding_box()
    title = opt.locator("b").bounding_box()
    assert pill["width"] < 0.4 * card["width"], (pill, card)
    assert pill["y"] < title["y"] + title["height"] and title["y"] < pill["y"] + pill["height"]
    shot(page, f"wiz-device-{width}")
    page.fill("#wizDevName", "Флешка")

    def to_folder(route):  # the fake disk cannot be mounted: register the folder instead
        body = json.loads(route.request.post_data)
        body.pop("disk")
        route.continue_(
            post_data=json.dumps({**body, "mount_path": str(stick), "type": "flashdrive"})
        )

    page.route("**/api/v1/devices/register", to_folder)
    page.locator("[data-wiz-next]").click()
    # 3 Templates: nothing preselected, Continue refuses until one is picked
    page.wait_for_selector("[data-wiz-templates]")
    assert "Флешка" in page.locator(".wiz h1").inner_text()
    assert page.locator('input[name="wizTpl"]:checked').count() == 0
    page.locator("[data-wiz-next]").click()
    page.wait_for_selector(".toast:has-text('Pick at least one template')")
    page.locator('.wiz-opt:has-text("All music") input').check()
    assert page.locator("[data-wiz-picked]").inner_text() == "1 selected"
    shot(page, f"wiz-templates-{width}")
    page.locator("[data-wiz-next]").click()
    # 4 Quality: the three profiles, the first one chosen
    page.wait_for_selector('input[name="wizProfile"]')
    assert page.locator('input[name="wizProfile"]').count() == 3
    page.locator(
        '[data-action="wizBack"]'
    ).click()  # Back keeps the picks; Continue changes the device
    page.wait_for_selector('input[name="wizTpl"]:checked')
    page.locator("[data-wiz-next]").click()
    page.wait_for_selector('input[name="wizProfile"]')
    page.locator('input[name="wizProfile"][value="space"]').check()
    shot(page, f"wiz-quality-{width}")
    page.locator("[data-wiz-next]").click()
    # 5 Review: the plan's counts
    page.wait_for_selector("[data-wiz-counts]", timeout=60_000)
    assert "2" in page.locator(".wiz-count").first.inner_text()
    shot(page, f"wiz-review-{width}")
    assert no_horizontal_scroll(page)
    page.locator("[data-wiz-next]").click()
    # 6 Done: the first sync runs
    page.wait_for_selector("[data-wiz-done]", timeout=60_000)
    assert page.locator('[data-action="wizSkip"]').count() == 0  # nothing left to skip
    shot(page, f"wiz-done-{width}")
    (dev,) = run_on(app, app.store.devices())
    assert dev.name == "Флешка" and dev.quality_profile == "space"
    assert len(dev.rule_set.template_ids) == 1
    page.locator('a:has-text("Show progress")').click()
    page.wait_for_selector("[data-report]", timeout=60_000)
    assert len(list(stick.rglob("*.m4a")) + list(stick.rglob("*.mp3"))) == 2
    assert page.errors == []


def test_wizard_skip_and_existing_library(web_server, chrome, tmp_path):
    url, app = web_server
    music = tmp_path / "music"
    music.mkdir()
    lib = {"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}}
    assert httpx.post(f"{url}/api/v1/libraries", json=lib).status_code == 201
    page = open_page(chrome, url + "/#/welcome")
    page.wait_for_selector("[data-wiz-have]")
    assert "You have 1 library" in page.locator("[data-wiz-have]").inner_text()
    page.locator('[data-action="wizSkip"]').click()
    page.wait_for_function("location.hash === '#/'")
    # a reload in the middle starts again from what exists
    page.goto(url + "/#/welcome?step=4")
    page.wait_for_selector('[data-wizard][data-step="library"]')
    assert page.errors == []
