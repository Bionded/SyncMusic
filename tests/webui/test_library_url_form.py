"""Library form: a server URL typed without a scheme shows http:// after leaving the field and is saved
like that; "Verify TLS certificate" is greyed out with a hint while the URL is http:// and usable for https://
(its value kept); Test connection runs for both. Nothing is reachable at the URLs used."""

from __future__ import annotations

import httpx
import pytest

from tests.webui.conftest import open_page

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_url_scheme_and_tls_checkbox(web_server, chrome, width):
    url, _ = web_server
    page = open_page(chrome, url + "/#/libraries/new?type=subsonic", width=width)
    field = page.locator("[data-server-url]")
    field.wait_for()
    tls = page.locator("[data-verify-tls]")
    hint = page.locator("[data-tls-hint]")
    assert (
        tls.is_checked() and tls.is_enabled() and hint.is_hidden()
    )  # empty URL: nothing greyed out
    field.fill("127.0.0.1:9")
    page.locator("#libName").click()  # leave the field
    assert field.input_value() == "http://127.0.0.1:9"
    assert tls.is_disabled() and tls.is_checked() and hint.is_visible()
    assert "Only used for HTTPS URLs." in hint.inner_text()
    field.fill("https://127.0.0.1:9")
    assert tls.is_enabled() and hint.is_hidden()
    field.fill("http://127.0.0.1:9")
    assert tls.is_disabled()
    page.fill("#libName", "ND")
    for name in ("username", "password"):
        page.locator(f"[id$='-{name}']").fill("x")
    page.locator('form[data-action="add"] button[type="submit"]').click()
    # the form itself is #/libraries/new: wait for the page of the library it created
    page.wait_for_function(
        "location.hash.startsWith('#/libraries/') && !location.hash.startsWith('#/libraries/new')",
        timeout=30_000,
    )
    libs = httpx.get(f"{url}/api/v1/libraries", timeout=30).json()
    nd = next(x for x in libs if x["name"] == "ND")
    assert (
        nd["settings"]["url"] == "http://127.0.0.1:9"
        and nd["settings"].get("verify_tls", True) is True
    )
    # edit: the stored http URL greys the box out at once; Test connection answers for http and https
    page.goto(url + f"/#/libraries/{nd['id']}")
    page.locator("[data-verify-tls]").wait_for()
    assert page.locator("[data-verify-tls]").is_disabled()
    page.get_by_role("button", name="Test connection").click()
    page.wait_for_function(
        "document.getElementById('libCheck').innerText.trim().length > 0", timeout=30_000
    )
    page.locator("[data-server-url]").fill("https://127.0.0.1:9")
    assert page.locator("[data-verify-tls]").is_enabled()
    page.get_by_role("button", name="Test connection").click()
    page.wait_for_function(
        "document.getElementById('libCheck').innerText.includes('127.0.0.1') || document.getElementById('libCheck').innerText.length > 3",
        timeout=30_000,
    )
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
