# SPDX-License-Identifier: GPL-3.0-or-later
"""A player another SyncMusic instance is home of: Register offers Continue / Start fresh (off: not home) / Keep
files; the device's danger zone names the home and makes this instance home after a confirmation."""

from __future__ import annotations

import httpx
import pytest

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import InstanceInfo, Manifest, ManifestHeader
from tests.contracts.harness import rockbox_tree
from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _synced_elsewhere(tmp_path):
    mount = rockbox_tree(tmp_path / "mini")
    home = InstanceInfo(id="other-instance", name="Kitchen box")
    header = ManifestHeader(device_id="mini", device_uid="0f3c9a1e", home=home, last_writer=home)
    manifest_io.write(mount, Manifest(header=header))
    return mount


@pytest.mark.parametrize("width", [1280, 390])
def test_register_offers_continuity_and_device_page_rebinds(web_server, chrome, tmp_path, width):
    url, _ = web_server
    mount = _synced_elsewhere(tmp_path)
    page = open_page(chrome, url + "/#/devices/register", width=width)
    page.wait_for_selector("#regPath:visible")
    page.fill("#regPath", str(mount))
    page.dispatch_event("#regPath", "change")
    box = page.locator("#regContinuity")
    box.wait_for(state="visible")
    assert "Kitchen box" in box.inner_text()
    assert page.locator("input[name=continuity][value=continue]").is_checked()
    assert page.locator("input[name=continuity][value=fresh]").is_disabled()
    assert page.locator("#regFirst").is_hidden()
    shot(page, f"register-continuity-{width}")
    assert no_horizontal_scroll(page)

    # registered (by API, the rest of the form is covered elsewhere): it continues with the device's identity
    body = {"name": "Mini", "type": "rockbox", "mount_path": str(mount)}
    r = httpx.post(f"{url}/api/v1/devices/register", json=body, timeout=60)
    assert r.status_code < 400, r.text
    dev = r.json()["id"]
    assert r.json()["identity"]["manifest_device_id"] == "0f3c9a1e"

    page = open_page(chrome, url + f"/#/devices/{dev}/settings", width=width)
    row = page.locator("[data-dz=home]")
    row.wait_for()
    assert "Kitchen box" in row.inner_text()
    assert page.locator("[data-action=wipe]").is_disabled()
    shot(page, f"device-home-other-{width}")
    row.locator("[data-action=makeHome]").click()
    page.locator(".modalfoot .btn.primary").click()
    page.wait_for_selector(".toast:has-text('now the home')")
    page.wait_for_function(
        "() => !document.querySelector('[data-action=makeHome]')"
        " && document.querySelector('[data-action=wipe]') && !document.querySelector('[data-action=wipe]').disabled"
    )
    assert manifest_io.read(mount).header.home.id != "other-instance"
    shot(page, f"device-home-this-{width}")
    assert not page.errors, page.errors
