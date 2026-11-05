"""Folder picker: Browse… next to a local library root, an smb:// / ftp:// address and a network
device's target folder lists the folders with a preview, opens and leaves folders, and "Use this folder" fills the
field. Local browsing reads a real temp tree; the server side is faked (tests/integration/test_browse.py covers it)."""

from __future__ import annotations

import pytest

from syncmusic.infra import discovery
from syncmusic.libraries import browse
from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.fixture
def fake_servers(web_server, monkeypatch):
    calls: list[tuple] = []

    def browse_remote(source, *, host, port, username, password, path, tls=False, timeout_s=15):
        calls.append((source, host, port, username, path))
        if path.endswith("Музика"):
            return browse.Listing(
                path=path,
                parent=path.rsplit("/", 1)[0] or "/",
                folders=["Бумбокс"],
                files=3,
                media_files=2,
                preview_folders=1,
                preview_media=14,
                partial=False,
            )
        return browse.Listing(
            path=path,
            parent=None if path == "/" else "/",
            folders=["Музика", "Книги"],
            files=0,
            media_files=0,
            preview_folders=5,
            preview_media=2,
            partial=True,
        )

    async def list_shares(host, port=445, username="", password="", timeout_s=15.0):
        calls.append(("shares", host, port, username))
        return [discovery.Share(name="music"), discovery.Share(name="public")]

    monkeypatch.setattr(browse, "browse_remote", browse_remote)
    monkeypatch.setattr(discovery, "list_shares", list_shares)
    return web_server[0], calls


@pytest.mark.parametrize("width", [1280, 390])
def test_local_and_smb_library_root(fake_servers, chrome, tmp_path, width):
    """the picker is a dialog with a breadcrumb; Cancel leaves the field alone."""
    url, calls = fake_servers
    (tmp_path / "Музика" / "Довга назва альбому, яка не влазить у рядок телефона").mkdir(
        parents=True
    )
    (tmp_path / "Музика" / "a.mp3").write_bytes(b"x")
    page = open_page(chrome, url + "/#/libraries/new?type=folder", width=width)
    page.wait_for_selector("[data-folder-source]")
    page.locator('[id$="-src-root"]').fill(str(tmp_path))
    local = page.locator('[data-src="local"]')
    dlg = page.locator(".modal")
    root = page.locator('[id$="-src-root"]').bounding_box()
    btn = local.get_by_role("button", name="Browse…").bounding_box()
    assert btn["x"] >= root["x"] + root["width"] and abs(btn["y"] - root["y"]) < 4, (root, btn)
    local.get_by_role("button", name="Browse…").click()
    dlg.locator(".found-row", has_text="Музика").click()
    dlg.locator("[aria-current=location]", has_text="Музика").wait_for()
    assert "1 folder · 1 media file" in dlg.locator("[data-pick-preview]").inner_text()
    assert dlg.locator("[data-pick-new]").count() == 0  # a library root is read-only: no New folder
    shot(page, f"picker-local-{width}")
    assert no_horizontal_scroll(page)
    dlg.get_by_role("button", name="Up one level").click()
    dlg.locator("[data-pick-path]", has_text=str(tmp_path)).wait_for(state="attached")
    dlg.locator(".found-row", has_text="Музика").click()
    dlg.locator("[aria-current=location]", has_text="Музика").wait_for()
    # the breadcrumb goes back to any level
    dlg.locator(".picker-crumbs button", has_text=tmp_path.name).click()
    dlg.locator("[aria-current=location]", has_text=tmp_path.name).wait_for()
    dlg.locator(".found-row", has_text="Музика").click()
    dlg.locator("[aria-current=location]", has_text="Музика").wait_for()
    dlg.get_by_role("button", name="Use this folder").click()
    assert page.locator('[id$="-src-root"]').input_value() == f"{tmp_path}/Музика"
    assert dlg.count() == 0
    # Cancel leaves the chosen folder alone
    local.get_by_role("button", name="Browse…").click()
    dlg.locator(".found-row").first.wait_for()
    dlg.get_by_role("button", name="Cancel").click()
    assert dlg.count() == 0
    assert page.locator('[id$="-src-root"]').input_value() == f"{tmp_path}/Музика"

    # SMB: the server's shares first, then the share's folders
    page.locator('input[name="libSource"][value="smb"]').check(force=True)
    remote = page.locator('[data-src="remote"]')
    remote.get_by_role("button", name="Browse…").click()
    remote.locator("[data-pick-error]:text('Enter the server address first.')").wait_for()
    page.locator("#libAddress").fill("smb://nas.local/")
    remote.get_by_role("button", name="Browse…").click()
    dlg.locator(".found-row", has_text="music").wait_for()
    assert dlg.get_by_role("button", name="Use this folder").is_disabled()
    shot(page, f"picker-shares-{width}")
    dlg.locator(".found-row", has_text="music").click()
    dlg.locator(".found-row", has_text="Музика").click()
    dlg.locator("[data-pick-path]", has_text="/music/Музика").wait_for(state="attached")
    dlg.get_by_role("button", name="Use this folder").click()
    assert (
        page.locator("#libAddress").input_value()
        == "smb://nas.local/music/%D0%9C%D1%83%D0%B7%D0%B8%D0%BA%D0%B0/"
    )
    assert calls[0] == ("shares", "nas.local", 445, "") and calls[-1][-1] == "/music/Музика"
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_ftp_device_target_folder(fake_servers, chrome, width):
    url, calls = fake_servers
    page = open_page(chrome, url + "/#/devices/register", width=width)
    page.check('input[name="target"][value="network:"]')
    page.wait_for_selector("#regNetwork:not(.hide) input")
    form = page.locator("#regNetwork")
    form.locator('[id$="-n-host"]').fill("ftp.local")
    # the button sits on the field's row, at its right end (icon only on a phone)
    field = form.locator('[id$="-n-base_path"]').bounding_box()
    btn = form.get_by_role("button", name="Browse…").bounding_box()
    assert btn["x"] >= field["x"] + field["width"] and abs(btn["y"] - field["y"]) < 4, (field, btn)
    assert abs(btn["height"] - field["height"]) < 3
    form.get_by_role("button", name="Browse…").click()
    dlg = page.locator(".modal")
    dlg.locator(":text('at least 5 folders')").wait_for()
    dlg.locator(".found-row", has_text="Музика").click()
    dlg.locator("[data-pick-path]", has_text="/Музика").wait_for(state="attached")
    # New folder on a device target: a name only, the first sync creates it
    dlg.locator("#pickNewName").fill("Плеєр")
    dlg.get_by_role("button", name="New folder").click()
    dlg.locator(":text('created by the first sync')").wait_for()
    shot(page, f"picker-ftp-device-{width}")
    assert no_horizontal_scroll(page)
    dlg.get_by_role("button", name="Use this folder").click()
    assert form.locator('[id$="-n-base_path"]').input_value() == "/Музика/Плеєр"
    assert calls[0][:2] == ("ftp", "ftp.local")
