"""Add library → Folder: the Local path / SMB share / FTP server switch.
An FTP folder added with its address and anonymous login is scanned; editing it shows the address again; a wrong
address is refused in the form."""

from __future__ import annotations

import httpx
import pytest

from tests.contracts.harness import _ftp_server
from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_folder_on_an_ftp_server(web_server, chrome, tmp_path, width):
    url, _ = web_server
    root, port = _ftp_server()
    base = root / f"src-{tmp_path.name}"
    make_music(base / "Гурт" / "01 Пісня.mp3", title="Пісня", artist="Гурт", album="Альбом")
    page = open_page(chrome, url + "/#/libraries/new?type=folder", width=width)
    page.wait_for_selector("[data-folder-source]")
    assert page.locator("h1").inner_text() == "Add a library"  # the add-to dialog's title won
    assert page.locator('[data-src="remote"]').is_hidden()
    page.locator('input[name="libSource"][value="smb"]').check(force=True)
    assert page.locator("#libAddress").get_attribute("placeholder") == "smb://nas.local/music"
    assert "Guest access" in page.locator("[data-guest-label]").inner_text()
    shot(page, f"lib-source-smb-{width}")
    page.locator('input[name="libSource"][value="ftp"]').check(force=True)
    assert "Anonymous login" in page.locator("[data-guest-label]").inner_text()
    page.fill("#libName", "FTP музика")
    page.fill("#libAddress", "http://wrong")
    page.locator('form.lib-form button[type="submit"]').click()
    page.wait_for_selector(".toast:has-text('must look like ftp://')")
    page.fill("#libAddress", f"ftp://127.0.0.1:{port}/src-{tmp_path.name}")
    page.locator("#libGuest").check()
    assert page.locator("[data-login]").is_hidden()
    shot(page, f"lib-source-ftp-{width}")
    assert no_horizontal_scroll(page)
    page.locator('form.lib-form button[type="submit"]').click()
    page.wait_for_selector("[data-lib-saved]", timeout=60_000)
    (lib,) = [x for x in httpx.get(f"{url}/api/v1/libraries").json() if x["name"] == "FTP музика"]
    s = lib["settings"]
    assert (s["source"], s["host"], s["port"], s["remote_path"], s["username"]) == (
        "ftp",
        "127.0.0.1",
        port,
        f"/src-{tmp_path.name}",
        "",
    )
    assert page.locator(".saved-head .saved-ico.ok").count() == 1  # the connection check passed
    # editing shows the same address and the anonymous tick
    page.goto(f"{url}/#/libraries/{lib['id']}")
    page.wait_for_selector("#libAddress")
    assert (
        page.locator("#libAddress").input_value() == f"ftp://127.0.0.1:{port}/src-{tmp_path.name}"
    )
    assert page.locator("#libGuest").is_checked()
    assert page.errors == []
