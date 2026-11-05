"""In the browser: register an FTP server from the registration page (address and login in the
type's settings form), then its device page names the server and offers no Eject and no Format. Against the
in-process FTP server of the contract harness."""

from __future__ import annotations

import httpx
import pytest

from tests.contracts.harness import _ftp_server
from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def test_register_an_ftp_server(web_server, chrome):
    url, _ = web_server
    root, port = _ftp_server()
    (root / "ui-nas").mkdir(exist_ok=True)
    page = open_page(chrome, url + "/#/devices/register")
    page.check('input[name="target"][value="network:"]')
    page.wait_for_selector("#regNetwork:not(.hide) input")
    # two network types since the SMB share device: FTP first and chosen; Android (MTP) last
    # when libmtp is installed on the host
    types = page.locator('input[name="regType"]')
    offered = [types.nth(i).get_attribute("value") for i in range(types.count())]
    assert offered in (["ftp", "smb"], ["ftp", "smb", "android"])
    assert page.locator('input[name="regType"]:checked').get_attribute("value") == "ftp"
    # the type comes before its settings form
    assert page.evaluate(
        "document.querySelector('#regDetails').compareDocumentPosition("
        "document.querySelector('#regNetwork')) & Node.DOCUMENT_POSITION_FOLLOWING"
    )
    # the SMB form asks for /share/folder
    page.locator('label.reg-type:has(input[value="smb"])').click()
    page.wait_for_selector('#regNetwork [id$="-n-remote_path"]')
    page.locator('label.reg-type:has(input[value="ftp"])').click()
    page.wait_for_selector('#regNetwork [id$="-n-base_path"]')
    page.fill("#regName", "Home NAS")
    form = page.locator("#regNetwork")
    assert "Server *" in form.inner_text() and "Folder on the server" in form.inner_text()
    form.locator('[id$="-n-host"]').fill("127.0.0.1")
    form.locator('[id$="-n-port"]').fill(str(port))
    form.locator('[id$="-n-username"]').fill("sd")
    form.locator('[id$="-n-password"]').fill("secret")
    form.locator('[id$="-n-base_path"]').fill("/ui-nas")
    assert no_horizontal_scroll(page)
    page.select_option("#regProcTemplate", "builtin-ftp")
    shot(page, "register-ftp")
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.startsWith('#/devices/home-nas/overview')")
    assert page.locator('[data-action="eject"]').count() == 0
    page.goto(url + "/#/devices/home-nas/settings")  # the danger zone is in Device settings
    page.wait_for_selector(".dangerzone")
    assert "nothing to format" in page.locator(".dangerzone").inner_text()
    assert page.locator('[data-action="eject"]').count() == 0
    assert f"127.0.0.1:{port}/ui-nas" in page.locator("[data-identity] .mono").inner_text()
    cfg = httpx.get(f"{url}/api/v1/devices/home-nas").json()["config"]
    assert cfg["type"] == "ftp" and cfg["settings"]["password"] == "********"
    page.close()


def test_ftp_test_and_save(web_server, chrome):
    """The FTP connection card's Test and save logs in first - a refused login
    shows the server's reason and stores nothing, a working one is saved; the path layout is one field per media
    kind (no JSON) and a changed music layout is stored."""
    url, _ = web_server
    root, port = _ftp_server()
    (root / "ui-nas2").mkdir(exist_ok=True)
    body = {
        "name": "NAS 2",
        "type": "ftp",
        "settings": {
            "host": "127.0.0.1",
            "port": port,
            "base_path": "/ui-nas2",
            "username": "sd",
            "password": "secret",
        },
    }
    r = httpx.post(f"{url}/api/v1/devices/register", json=body, timeout=60)
    assert r.status_code == 201, r.text
    dev = r.json()["id"]
    for width in (1280, 390):
        page = open_page(chrome, url + f"/#/devices/{dev}/settings", width=width)
        card = page.locator('#typeCard[data-type-card="ftp"]')
        card.wait_for()
        assert "FTP connection" in card.inner_text()
        assert page.locator("#filesCard textarea").count() == 0  # no JSON
        assert page.locator('#filesCard input[id$="d-layout-music"]').count() == 1
        assert no_horizontal_scroll(page)
        shot(page, f"ftp-settings-{width}")
        page.close()
    page = open_page(chrome, url + f"/#/devices/{dev}/settings")
    card = page.locator('#typeCard[data-type-card="ftp"]')
    card.wait_for()
    card.locator('[id$="d-password"]').fill("wrong")
    card.get_by_role("button", name="Test and save").click()
    page.wait_for_selector("[data-ftp-result][data-state=error]")
    assert "cannot reach the server" in page.locator("[data-ftp-result]").inner_text()
    stored = httpx.get(f"{url}/api/v1/devices/{dev}").json()["config"]["settings"]
    assert stored["password"] == "********"  # unchanged (masked)

    card.locator('[id$="d-password"]').fill("secret")
    page.locator('#filesCard input[id$="d-layout-music"]').fill("Music/{artist}/{title}")
    card.get_by_role("button", name="Test and save").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    settings = httpx.get(f"{url}/api/v1/devices/{dev}").json()["config"]["settings"]
    assert settings["layout"]["music"] == "Music/{artist}/{title}"
    assert settings["layout"]["podcast"].startswith("Podcasts/")
    assert [
        e for e in page.errors if "Failed to load resource" not in e
    ] == []  # the refused test: 409


@pytest.mark.parametrize("width", [1280, 390])
def test_offline_server_says_since_when(web_server, chrome, width):
    """a network device whose server did not answer an automatic start says so on the Devices list
    and its page (the next slot tries again)."""
    import datetime as dt

    url, app = web_server
    root, port = _ftp_server()
    (root / f"ui-off-{width}").mkdir(exist_ok=True)
    body = {
        "name": "Дача NAS",
        "type": "ftp",
        "settings": {"host": "127.0.0.1", "port": port, "base_path": f"/ui-off-{width}"},
    }
    r = httpx.post(f"{url}/api/v1/devices/register", json=body, timeout=60)
    assert r.status_code == 201, r.text
    dev = r.json()["id"]
    app.sync._offline[dev] = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
    page = open_page(chrome, url + "/#/devices", width=width)
    row = page.locator(f'[data-device-row="{dev}"]')
    row.wait_for()
    assert "Not reachable since" in row.inner_text()
    assert no_horizontal_scroll(page)
    shot(page, f"devices-offline-{width}")
    page.goto(url + f"/#/devices/{dev}/overview")
    line = page.locator("[data-offline-since]")
    line.wait_for()
    assert "has not answered since" in line.inner_text()
    assert no_horizontal_scroll(page)
    shot(page, f"device-offline-{width}")
    assert page.errors == []
