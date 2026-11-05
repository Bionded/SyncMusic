"""a library's extra accounts are managed in its settings (add, edit with the secret masked,
refuse removal while a device uses one); a device picks one in Device settings, also via "+ Add account…"."""

from __future__ import annotations

import time

import httpx
import pytest

from tests.webui.conftest import link_rules, no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _library(url):
    settings = {"url": "http://127.0.0.1:9", "username": "owner", "password": "main-pw"}
    body = {"id": "home", "type": "subsonic", "name": "Home server", "settings": settings}
    _api(url, "POST", "/libraries", json=body)


def _account_dialog(page, name, user, password):
    modal = page.locator(".modal")
    modal.wait_for()
    modal.locator("#acctName").fill(name)
    modal.locator('input[id$="username"]').fill(user)
    modal.locator('input[id$="password"]').fill(password)
    modal.get_by_role("button", name="Save").click()
    page.wait_for_selector(f".toast:has-text('Account “{name}” saved')")
    modal.wait_for(state="detached")


def test_library_accounts_and_the_device_pick(web_server, chrome, tmp_path):
    url, app = web_server
    _library(url)
    page = open_page(chrome, url + "/#/libraries/home")
    page.wait_for_selector("#libAccounts")
    page.get_by_role("button", name="Add account").click()
    _account_dialog(page, "Anna", "anna", "anna-pw")
    row = page.locator('[data-lib-account="anna"]')
    row.wait_for()
    assert "No device uses it" in row.inner_text() and "anna" in row.inner_text()
    stored = run_on(app, app.store.library("home"))
    assert stored.accounts["anna"].settings == {"username": "anna", "password": "anna-pw"}
    # edit: the password comes back masked and saving the mask keeps it
    row.get_by_role("button", name="Edit").click()
    modal = page.locator(".modal")
    modal.wait_for()
    assert modal.locator('input[id$="password"]').input_value() == "********"
    modal.locator("#acctName").fill("Anna K.")
    modal.get_by_role("button", name="Save").click()
    page.wait_for_selector(".toast:has-text('Account “Anna K.” saved')")
    stored = run_on(app, app.store.library("home"))
    assert stored.accounts["anna"].name == "Anna K."
    assert stored.accounts["anna"].settings["password"] == "anna-pw"
    shot(page, "688-library-accounts")

    # a device on this library picks the account
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    _api(url, "POST", "/devices/register", json=body)
    link_rules(url, "stick", {"rules": [{"id": "r1"}]}, library_ids=("home",))
    page = open_page(chrome, url + "/#/devices/stick/settings")
    pick = page.locator('[data-account-pick="home"]')
    pick.wait_for()
    assert pick.input_value() == ""
    pick.select_option("anna")
    page.get_by_role("button", name="Save").first.click()
    page.wait_for_selector(".toast:has-text('saved')")
    assert run_on(app, app.store.device("stick")).library_accounts == {"home": "anna"}
    # "+ Add account…" opens the library's dialog; the new account is picked
    pick.select_option("__add")
    _account_dialog(page, "Ben", "ben", "ben-pw")
    page.wait_for_function("document.querySelector('[data-account-pick=home]')?.value === 'ben'")
    for _ in range(50):  # stored by the dialog's follow-up PATCH
        if run_on(app, app.store.device("stick")).library_accounts == {"home": "ben"}:
            break
        time.sleep(0.1)
    assert run_on(app, app.store.device("stick")).library_accounts == {"home": "ben"}
    page.wait_for_function("document.querySelector('[data-account-pick=home]')?.value === 'ben'")
    page.wait_for_function(  # the device card links to the library browsed as its account
        "document.querySelector('[data-account=home] [data-browse-as]')?.getAttribute('href')"
        " === '#/libraries/home/browse?as=ben'"
    )
    assert pick.input_value() == "ben"
    shot(page, "688-device-account-pick")
    assert no_horizontal_scroll(page)

    # the library refuses to remove an account in use
    page = open_page(chrome, url + "/#/libraries/home")
    row = page.locator('[data-lib-account="anna"]')
    row.wait_for()
    assert "No device uses it" in row.inner_text()
    ben = page.locator('[data-lib-account="ben"]')
    assert "Used by Stick" in ben.inner_text()
    ben.get_by_role("button", name="Remove").click()
    page.wait_for_selector(".toast.error:has-text('Stick sign in as this account')")
    row.get_by_role("button", name="Remove").click()
    page.locator(".modal").get_by_role("button", name="Remove").click()
    page.wait_for_selector(".toast:has-text('Account “Anna K.” removed')")
    assert set(run_on(app, app.store.library("home")).accounts) == {"ben"}
    page = open_page(chrome, url + "/#/libraries/home", width=390)
    page.locator('[data-lib-account="ben"]').wait_for()
    shot(page, "688-library-accounts-phone")
    assert no_horizontal_scroll(page)
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_browse_the_library_as_an_account(web_server, chrome, tmp_path):
    """the browser's "View as" lists the library's accounts with the devices using them and shows that
    user's library."""
    from tests.media import make_music

    url, app = web_server
    kid_root = tmp_path / "kid"
    make_music(kid_root / "Kid" / "01 Lullaby.mp3", title="Lullaby", artist="Kiddo")
    main_root = tmp_path / "main"
    make_music(main_root / "Grown" / "01 Anthem.mp3", title="Anthem", artist="Grown")
    lib = {"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(main_root)}}
    _api(url, "POST", "/libraries", json=lib)
    acc = {"kid": {"name": "Kid", "settings": {"root": str(kid_root)}}}
    _api(url, "PATCH", "/libraries/music", json={"accounts": acc})
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    _api(url, "POST", "/devices/register", json=body)
    link_rules(url, "stick", {"rules": [{"id": "r1"}]})
    _api(url, "PATCH", "/devices/stick", json={"library_accounts": {"music": "kid"}})

    page = open_page(chrome, url + "/#/libraries/music/browse?tab=songs")
    sel = page.locator("[data-view-as]")
    sel.wait_for()
    assert "Kid - Stick" in sel.inner_text()
    page.locator("text=Anthem").first.wait_for()
    assert page.locator("text=Lullaby").count() == 0
    sel.select_option("kid")
    page.wait_for_function("location.hash.includes('as=kid')")
    page.locator("text=Lullaby").first.wait_for()
    shot(page, "687-browse-as")
    page.set_viewport_size({"width": 390, "height": 800})
    assert no_horizontal_scroll(page)

    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_template_preview_and_pick_as_an_account(web_server, chrome, tmp_path):
    """the template editor's "View as" makes the preview read that user's library and preselects the
    account in Pick from library; a template whose devices sign in as different users says so."""
    from tests.media import make_music

    url, app = web_server
    main_root, kid_root = tmp_path / "main", tmp_path / "kid"
    make_music(main_root / "Grown" / "01 Anthem.mp3", title="Anthem", artist="Grown")
    make_music(kid_root / "Kiddo" / "01 Lullaby.mp3", title="Lullaby", artist="Kiddo")
    make_music(kid_root / "Kiddo" / "02 Nap.mp3", title="Nap", artist="Kiddo")
    lib = {"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(main_root)}}
    _api(url, "POST", "/libraries", json=lib)
    acc = {"kid": {"name": "Kid", "settings": {"root": str(kid_root)}}}
    _api(url, "PATCH", "/libraries/music", json={"accounts": acc})
    for name in ("Stick", "Pod"):
        (tmp_path / name).mkdir()
        body = {"name": name, "mount_path": str(tmp_path / name), "type": "flashdrive"}
        _api(url, "POST", "/devices/register", json=body)
        rs = {"template_ids": ["default.music.all"]}
        _api(url, "PATCH", f"/devices/{name.lower()}", json={"rule_set": rs})
    _api(url, "PATCH", "/devices/stick", json={"library_accounts": {"music": "kid"}})

    page = open_page(chrome, url + "/#/templates/default.music.all")
    page.locator("[data-preview-headline]").wait_for()
    assert page.locator("[data-preview-headline]").inner_text().startswith("1 item")
    note = page.locator("[data-tpl-accounts-differ]")
    assert "Stick - Kid" in note.inner_text() and "Pod - main account" in note.inner_text()
    page.locator("[data-tpl-as]").select_option("music:kid")
    page.wait_for_function(
        "document.querySelector('[data-preview-headline]')?.textContent.startsWith('2 items')"
    )
    shot(page, "687-template-preview-as")
    page.get_by_role("tab", name="Pick from library").click()
    page.locator('[data-action="tplPick"]').click()
    page.wait_for_selector("[data-picker] .pick-row")
    assert page.locator("#pickAs").input_value() == "kid"
    assert page.locator(".pick-name", has_text="Kiddo").count() == 1
    assert page.locator(".pick-name", has_text="Grown").count() == 0
    shot(page, "687-pick-as")
    page.locator("#pickAs").select_option("")
    page.locator(".pick-name", has_text="Grown").wait_for()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
