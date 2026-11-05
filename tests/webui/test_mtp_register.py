"""an Android player found on USB (MTP) is listed on the dashboard and Register with its type's icon,
a Beta badge and its USB serial (no size, no device path), and registers as `android` with the serial prefilled."""

from __future__ import annotations

import os

import httpx
import pytest

from syncmusic.devices.Android.mtp import AndroidMtp
from syncmusic.infra.blockdev.mtp import mtp_info
from tests.fake_mtp import FakeMtpBackend
from tests.unit.test_mtp_detection import PROPS, SERIAL
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.fixture
def config(config, tmp_path):
    """The shared test config with an allowlist that holds the made-up player of these tests."""
    allowlist = tmp_path / "hardware.toml"
    allowlist.write_text(f'[android_test]\nusb_serial = "{SERIAL}"\n')
    config.device_allowlist = allowlist
    return config


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=30, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.mark.parametrize("width", [1280, 390])
def test_found_mtp_player_registers_as_android(web_server, chrome, tmp_path, monkeypatch, width):
    url, app = web_server
    (tmp_path / "storage").mkdir()
    backend = FakeMtpBackend(tmp_path / "storage")
    monkeypatch.setattr(AndroidMtp, "backend_factory", staticmethod(lambda s, *_: backend))
    disk = mtp_info("1-1", PROPS)
    run_on(app, app._disk_added(disk))

    page = open_page(chrome, url + "/#/", width=width)
    row = page.locator(f'[data-unknown-row="{disk.path}"]')
    row.wait_for()
    assert row.locator("[data-beta]").count() == 1 and SERIAL in row.inner_text()
    assert no_horizontal_scroll(page)
    shot(page, f"mtp-dashboard-{width}")

    page.goto(url + f"/#/devices/register?disk={disk.path}")
    choice = page.locator(f'input[name="target"][value="{disk.path}"]').locator("xpath=..")
    choice.wait_for()
    text = choice.inner_text()
    assert "Unisoc Unisoc Phone" in text and SERIAL in text and "/sys/" not in text, text
    assert choice.locator("[data-beta]").count() == 1
    # only the MTP type is offered for it
    types = page.locator(".reg-type")
    assert types.count() == 1 and "Android player (MTP)" in types.inner_text()
    assert no_horizontal_scroll(page)
    shot(page, f"mtp-register-{width}")
    page.fill("#regName", f"Handheld {width}")
    page.check('input[name="procMode"][value="device"]')
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.includes('/overview')")
    dev = page.evaluate("decodeURIComponent(location.hash.split('/')[2])")
    cfg = _api(url, "GET", f"/devices/{dev}")["config"]
    assert cfg["type"] == "android" and cfg["settings"]["usb_serial"] == SERIAL
    page.locator(
        "[data-device-head] [data-action=eject]"
    ).wait_for()  # plugged in: ejectable, unlike a server
    assert (
        "Storage not shared" not in page.locator("[data-device-head]").inner_text()
    )  # no partition to wait for
    assert (
        page.locator("[data-integrity], .dev-health-vitals").count() == 0
    )  # no disk health for MTP
    shot(page, f"mtp-overview-{width}")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_found_mtp_player_register_form(web_server, chrome, tmp_path, monkeypatch, width):
    """the Android type preselected, the USB serial read-only, Storage from the player, the folder
    on the device picked over MTP (default ./), and these settings registered."""
    url, app = web_server
    storage = tmp_path / "storage"
    (storage / "Music" / "Альбоми").mkdir(parents=True)
    (storage / "Music" / "a.mp3").write_bytes(b"x")
    backend = FakeMtpBackend(storage, name="SD card")
    monkeypatch.setattr(AndroidMtp, "backend_factory", staticmethod(lambda s, *_: backend))
    disk = mtp_info("1-1", PROPS)
    run_on(app, app._disk_added(disk))

    page = open_page(chrome, url + f"/#/devices/register?disk={disk.path}", width=width)
    page.locator("#regMtp").wait_for()
    assert page.locator('input[name="regType"][value="android"]').is_checked()
    serial = page.locator("#regMtpSerial")
    assert serial.input_value() == SERIAL and serial.get_attribute("readonly") is not None
    page.wait_for_function("document.querySelector('#regMtpStorage option')?.value === 'SD card'")
    assert "SD card -" in page.locator("#regMtpStorage").inner_text()
    assert page.locator("#regMtpFolder").input_value() == "./"
    assert "Folder on the device" in page.locator("#regMtp").inner_text()
    shot(page, f"mtp-register-form-{width}")
    assert no_horizontal_scroll(page)

    page.locator("#regMtp").get_by_role("button", name="Browse…").click()
    dlg = page.locator(".modal")
    dlg.locator(".found-row", has_text="Music").click()
    dlg.locator("[aria-current=location]", has_text="Music").wait_for()
    assert "1 folder · 1 media file" in dlg.locator("[data-pick-preview]").inner_text()
    dlg.locator("#pickNewName").fill("SyncMusic")
    dlg.get_by_role("button", name="New folder").click()
    dlg.locator(":text('created by the first sync')").wait_for()
    shot(page, f"mtp-register-picker-{width}")
    assert no_horizontal_scroll(page)
    dlg.get_by_role("button", name="Use this folder").click()
    assert page.locator("#regMtpFolder").input_value() == "Music/SyncMusic"

    page.fill("#regName", f"Handheld {width}")
    page.check('input[name="procMode"][value="device"]')
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.includes('/overview')")
    dev = page.evaluate("decodeURIComponent(location.hash.split('/')[2])")
    s = _api(url, "GET", f"/devices/{dev}")["config"]["settings"]
    assert (s["usb_serial"], s["storage"], s["base_path"]) == (SERIAL, "SD card", "Music/SyncMusic")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_android_path_layout(web_server, chrome, tmp_path, monkeypatch, width):
    """an Android player's Settings offer the flash drive's 'Files on the device' block - the path
    templates with live examples, the playable formats, the base folder - and save a changed template."""
    url, _app = web_server
    (tmp_path / "storage").mkdir()
    backend = FakeMtpBackend(tmp_path / "storage")
    monkeypatch.setattr(AndroidMtp, "backend_factory", staticmethod(lambda s, *_: backend))
    reg = {"name": f"Handheld {width}", "type": "android", "settings": {"usb_serial": SERIAL}}
    dev = _api(url, "POST", "/devices/register", json=reg)["id"]
    page = open_page(chrome, url + f"/#/devices/{dev}/settings", width=width)
    card = page.locator("#filesCard")
    single = card.locator('[data-path-example="music"]')
    single.wait_for()
    assert single.inner_text() == "e.g. Music/Miles Davis/1959 - Kind of Blue/0101 - So What.mp3"
    text = card.inner_text()
    assert "Music, multi-disc" in text and "Playable formats" in text, text
    assert (
        "USB serial" in text
        and "Folder on the player" in text
        and "Folder on the server" not in text
    )  # not the FTP label
    card.locator('input[id$="d-layout-music"]').fill("Music/{artist}/{title}")
    assert single.inner_text() == "e.g. Music/Miles Davis/So What.mp3"
    card.scroll_into_view_if_needed()
    shot(page, f"android-layout-{width}")
    if os.environ.get("SYNCMUSIC_SHOTS"):
        card.screenshot(path=f"{os.environ['SYNCMUSIC_SHOTS']}/android-files-card-{width}.png")
    assert no_horizontal_scroll(page)
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".toast:has-text('saved')")
    cfg = _api(url, "GET", f"/devices/{dev}")["config"]
    assert cfg["settings"]["layout"]["music"] == "Music/{artist}/{title}"
    assert page.errors == []
