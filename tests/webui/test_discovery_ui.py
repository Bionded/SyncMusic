"""Add library → Folder → SMB / FTP: "Find servers on the network" lists what discovery found (only when pressed), a pick fills the address, "List shares" with the login lists the shares and a pick adds the share;
errors show in place. Discovery itself is faked here (tests/integration/test_discovery.py covers the real one)."""

from __future__ import annotations

import pytest

from syncmusic.infra import discovery
from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.fixture
def fake_network(web_server, monkeypatch):
    calls: list[tuple] = []

    async def find_servers(kinds, timeout_s=3.0):
        calls.append(("find", tuple(kinds)))
        servers = [
            discovery.Server(
                kind="smb",
                name="Домашній NAS",
                host="nas.local",
                address="10.0.0.20",
                port=445,
                source="mdns",
            ),
            discovery.Server(
                kind="smb",
                name="10.0.0.79",
                host="10.0.0.79",
                address="10.0.0.79",
                port=4445,
                source="wsd",
            ),
            discovery.Server(
                kind="ftp",
                name="Player FTP",
                host="ftp.local",
                address="10.0.0.30",
                port=2121,
                source="mdns",
            ),
        ]
        return [s for s in servers if s.kind in kinds]

    async def list_shares(host, port=445, username="", password="", timeout_s=15.0):
        calls.append(("shares", host, port, username))
        if username == "bad":
            raise discovery.DiscoveryError(
                f"cannot list the shares of {host}:{port}: wrong user name or password"
            )
        return [discovery.Share(name="music", comment="Музика"), discovery.Share(name="public")]

    monkeypatch.setattr(discovery, "find_servers", find_servers)
    monkeypatch.setattr(discovery, "list_shares", list_shares)
    return web_server[0], calls


@pytest.mark.parametrize("width", [1280, 390])
def test_find_servers_and_shares(fake_network, chrome, width):
    url, calls = fake_network
    page = open_page(chrome, url + "/#/libraries/new?type=folder", width=width)
    page.wait_for_selector("[data-folder-source]")
    page.locator('input[name="libSource"][value="smb"]').check(force=True)
    assert calls == []  # nothing is searched before the button is pressed
    page.get_by_role("button", name="Find servers on the network").click()
    page.wait_for_selector("[data-found] .found-row")
    rows = page.locator("[data-found] .found-row")
    assert rows.count() == 2 and calls == [("find", ("smb",))]
    assert "nas.local · 10.0.0.20" in rows.nth(0).inner_text()
    assert ":4445" in rows.nth(1).inner_text()
    rows.nth(0).click()
    assert page.locator("#libAddress").input_value() == "smb://nas.local/"

    # a login the server refuses says why, in place
    page.locator("#libGuest").uncheck()
    page.locator('[data-login] [id$="-src-username"]').fill("bad")
    page.get_by_role("button", name="List shares").click()
    page.wait_for_selector("[data-shares] :text('wrong user name or password')")
    page.locator('[data-login] [id$="-src-username"]').fill("samba")
    page.get_by_role("button", name="List shares").click()
    page.wait_for_selector("[data-shares] .found-row")
    assert calls[-1] == ("shares", "nas.local", 445, "samba")
    shot(page, f"discovery-shares-{width}")
    page.locator("[data-shares] .found-row", has_text="music").click()
    assert page.locator("#libAddress").input_value() == "smb://nas.local/music/"
    assert no_horizontal_scroll(page)

    # FTP: its own servers, no shares
    page.locator('input[name="libSource"][value="ftp"]').check(force=True)
    assert (
        page.locator("[data-shares-box]").is_hidden()
        and page.locator("[data-found] .found-row").count() == 0
    )
    page.get_by_role("button", name="Find servers on the network").click()
    page.wait_for_selector("[data-found] .found-row")
    page.locator("[data-found] .found-row").first.click()
    assert page.locator("#libAddress").input_value() == "ftp://ftp.local:2121/"
    shot(page, f"discovery-ftp-{width}")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_register_an_ftp_device_from_the_search(fake_network, chrome):
    url, calls = fake_network
    page = open_page(chrome, url + "/#/devices/register")
    page.check('input[name="target"][value="network:"]')
    page.wait_for_selector("#regNetwork:not(.hide) input")
    assert calls == []
    form = page.locator("#regNetwork")
    form.get_by_role("button", name="Find servers on the network").click()
    form.locator("[data-found] .found-row").first.wait_for()
    assert calls == [("find", ("ftp",))] and form.locator("[data-found] .found-row").count() == 1
    form.locator("[data-found] .found-row").first.click()
    assert form.locator('[id$="-n-host"]').input_value() == "ftp.local"
    assert form.locator('[id$="-n-port"]').input_value() == "2121"
    assert no_horizontal_scroll(page)
    shot(page, "register-ftp-find")
