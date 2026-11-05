"""Optional login in a browser: Settings → System → Access sets a password (login on), creates an
API token shown once; a new browser gets the login screen, a wrong password says so, the right one opens the app;
phone and desktop width."""

from __future__ import annotations

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]

PW = "hunter22pw"


@pytest.mark.parametrize("width", [1280, 390])
def test_access_login(web_server, chrome, width):
    url, _app = web_server
    page = open_page(chrome, f"{url}/#/settings", width=width)
    box = page.locator("[data-access]")
    box.wait_for()
    assert box.get_attribute("data-access") == "off"
    # turning it on without a password asks for one
    page.locator("#acc-on").click()
    page.locator("[data-acc-msg]").wait_for(state="visible", timeout=5_000)
    assert not page.locator("#acc-on").is_checked()
    page.fill("#acc-pw", PW)
    page.fill("#acc-pw2", PW)
    page.click("[data-action=accessPassword]")
    page.locator("[data-access='on']").wait_for()
    page.fill("#acc-token-name", "backup - тест")
    page.click("[data-action=accessToken]")
    token = page.locator("[data-acc-token]").input_value()
    assert token.startswith("smt_")
    assert page.locator("[data-token]").count() == 1
    assert no_horizontal_scroll(page)
    shot(page, f"access-on-{width}")
    r = httpx.get(f"{url}/api/v1/settings", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert httpx.get(f"{url}/api/v1/settings").status_code == 401
    assert page.errors == []

    other = open_page(chrome, f"{url}/#/settings", width=width)
    other.locator("[data-login]").wait_for()
    assert other.locator("#sidebar").count() == 0
    other.fill("#login-pw", "wrong-one")
    other.press("#login-pw", "Enter")
    other.locator("[data-login-error]:not([hidden])").wait_for()
    assert no_horizontal_scroll(other)
    shot(other, f"login-{width}")
    other.fill("#login-pw", PW)
    other.press("#login-pw", "Enter")
    other.locator("[data-access='on']").wait_for()
    # the only console error is the 401 of the wrong password
    assert all("401" in e for e in other.errors), other.errors


def test_access_revoke_and_sign_out(web_server, chrome):
    """Revoke a token (after the confirm), Sign out (login screen again), Sign out everywhere (other browsers too)."""
    url, _app = web_server
    page = open_page(chrome, f"{url}/#/settings")
    page.locator("[data-access]").wait_for()
    page.fill("#acc-pw", PW)
    page.fill("#acc-pw2", PW)
    page.click("[data-action=accessPassword]")
    page.locator("[data-access='on']").wait_for()
    page.fill("#acc-token-name", "ci")
    page.click("[data-action=accessToken]")
    token = page.locator("[data-acc-token]").input_value()
    page.click("[data-action=accessRevoke]")
    page.locator(".modal").get_by_role("button", name="Revoke").click()
    page.wait_for_function("document.querySelectorAll('[data-token]').length === 0")
    headers = {"Authorization": f"Bearer {token}"}
    assert httpx.get(f"{url}/api/v1/settings", headers=headers).status_code == 401

    page.click("[data-action=accessLogout]")
    page.locator("[data-login]").wait_for()
    page.fill("#login-pw", PW)
    page.press("#login-pw", "Enter")
    page.locator("[data-access='on']").wait_for()

    other = open_page(chrome, f"{url}/#/settings")
    other.locator("[data-login]").wait_for()
    other.fill("#login-pw", PW)
    other.press("#login-pw", "Enter")
    other.locator("[data-access='on']").wait_for()
    page.click("[data-action=accessEverywhere]")
    page.locator("[data-login]").wait_for()
    other.reload()
    other.locator("[data-login]").wait_for()
    assert page.errors == []
