"""the iTunesDB size as a bar only in a stock iPod's Device settings - width and aria from size / limit,
coloured by the service's level with the worded badge (ok / warn / over), an empty bar for "no database yet"; the
device overview keeps the text only; a flash drive has none."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.contracts.harness import ipod_tree
from tests.webui.conftest import open_page

pytestmark = [pytest.mark.ui, pytest.mark.slow]

LIMIT = 63 * 1024 * 1024


def test_itunesdb_bar_in_ipod_settings(web_server, chrome, tmp_path):
    url, _ = web_server
    mount = ipod_tree(tmp_path / "pod")
    r = httpx.post(
        f"{url}/api/v1/devices/register",
        json={"name": "Pod", "mount_path": str(mount), "type": "ipod"},
        timeout=30,
    )
    assert r.status_code == 201, r.text
    page = open_page(chrome, url + "/#/devices/pod/settings")
    bar = page.locator("#dbSize [data-db-bar]")
    bar.wait_for()
    real = httpx.get(f"{url}/api/v1/devices/pod/space", timeout=30).json()["db"]
    assert int(bar.get_attribute("aria-valuenow")) == round(real["size"] / real["limit"] * 100)
    assert page.locator("#dbSize").get_by_text("Within the limit").is_visible()
    for size, level, pct, word in (
        (int(LIMIT * 0.95), "warn", 95, "Close to the limit"),
        (LIMIT + 1, "over", 100, "Over the limit"),
        (None, "ok", 0, "no database yet"),
    ):
        page.unroute("**/api/v1/devices/pod/space")
        body = json.dumps(
            {
                "capacity": 10**10,
                "free": 10**9,
                "planned": None,
                "db": {"size": size, "limit": LIMIT, "level": level},
            }
        )
        page.route("**/api/v1/devices/pod/space", _answer(body))
        page.reload()
        bar.wait_for()
        assert int(bar.get_attribute("aria-valuenow")) == pct, level
        assert f"lvl-{level}" in bar.get_attribute("class")
        assert word in page.locator("#dbSize").inner_text()
        width = bar.locator("i").evaluate("el => el.style.width")
        assert width == f"{pct}%"
    page.unroute("**/api/v1/devices/pod/space")
    # the overview: the text, no bar
    page.goto(url + "/#/devices/pod/overview")
    page.wait_for_selector('#content[data-route="/devices/pod/overview"] [data-db-level]')
    assert page.locator("[data-db-bar]").count() == 0
    # a flash drive: neither
    (tmp_path / "stick").mkdir()
    httpx.post(
        f"{url}/api/v1/devices/register",
        json={"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"},
        timeout=30,
    )
    page.goto(url + "/#/devices/stick/settings")
    page.wait_for_selector('#content[data-route="/devices/stick/settings"]')
    assert page.locator("[data-db-bar], [data-db-level]").count() == 0
    assert page.errors == []


def _answer(body):
    return lambda route: route.fulfill(status=200, content_type="application/json", body=body)
