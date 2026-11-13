"""Settings → Wi-Fi clients: allow clients, the Add client dialog shows a QR code, the code and a countdown and turns
into "paired" when a client uses the code; rename and revoke from the list; closing an unused dialog cancels its code."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, poll, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _pair(url: str, code: str, name: str) -> str:
    r = httpx.post(
        f"{url}/api/client/v1/pair",
        json={"code": code, "client": {"uid": str(uuid.uuid4()), "name": name, "os": "Android 15"}},
    )
    assert r.status_code == 201, r.text
    return r.json()["device_id"]


@pytest.mark.parametrize("width", [1280, 390])
def test_pair_rename_revoke(chrome, web_server, width):
    url, _ = web_server
    page = open_page(chrome, f"{url}/#/settings", width=width)
    box = page.locator("[data-clients]")
    box.scroll_into_view_if_needed()
    assert page.locator("[data-action=clientAdd]").is_disabled()
    page.locator("#cl-allow").check()
    page.locator("[data-clients='on']").wait_for()

    # an unused code is cancelled when the dialog closes
    page.locator("[data-action=clientAdd]").click()
    page.locator("[data-cl-qr] svg").wait_for()
    assert "Valid for" in page.locator("[data-cl-left]").inner_text()
    page.get_by_role("button", name="Close", exact=True).last.click()
    poll(
        page,
        lambda: httpx.get(f"{url}/api/v1/clients/pairings").json() == [],
        what="code cancelled",
    )

    page.locator("[data-action=clientAdd]").click()
    code = page.locator("[data-cl-code]").inner_text().strip()
    assert no_horizontal_scroll(page)
    shot(page, f"clients-dialog-{width}")
    device_id = _pair(url, code, "Телефон Олени")
    page.locator("[data-cl-done]").wait_for(timeout=10_000)
    assert "Телефон Олени" in page.locator("[data-cl-done]").inner_text()
    page.get_by_role("button", name="Close", exact=True).last.click()
    row = page.locator(f"[data-client='{device_id}']")
    row.wait_for()
    assert "Android 15" in row.inner_text()

    row.locator("[data-action=clientRename]").click()
    page.locator("#cl-name").fill("Kitchen phone")
    page.get_by_role("button", name="Save", exact=True).last.click()
    page.locator(f"[data-client='{device_id}']", has_text="Kitchen phone").wait_for()

    page.locator(f"[data-client='{device_id}'] [data-action=clientRevoke]").click()
    page.get_by_role("button", name="Revoke", exact=True).last.click()
    page.locator(f"[data-client='{device_id}']", has_text="Revoked").wait_for()
    assert no_horizontal_scroll(page)
    shot(page, f"clients-settings-{width}")
    assert page.errors == []


def test_client_device_page_shows_the_client_and_its_progress(chrome, web_server):
    url, app = web_server
    httpx.patch(f"{url}/api/v1/settings", json={"clients_allowed": True}).raise_for_status()
    code = httpx.post(f"{url}/api/v1/clients/pairings").json()["code"]
    device_id = _pair(url, code, "Телефон Олени")
    page = open_page(chrome, f"{url}/#/devices/{device_id}/overview")
    card = page.locator(f"[data-client-card='{device_id}']")
    card.wait_for()
    assert "Android 15" in card.inner_text()

    async def progress():
        app.bus.publish(
            "client.progress",
            device_id=device_id,
            done=1,
            total=4,
            bytes_done=1000,
            bytes_total=4000,
        )

    page.wait_for_timeout(500)  # the event stream is connected
    run_on(app, progress())
    page.locator("[data-client-sync]", has_text="file 1 of 4").wait_for()
    httpx.post(f"{url}/api/v1/clients/{device_id}/revoke").raise_for_status()
    page.locator("[data-client-revoked]").wait_for()
    assert no_horizontal_scroll(page)
    assert page.errors == []
