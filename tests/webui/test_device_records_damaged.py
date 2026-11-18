# SPDX-License-Identifier: GPL-3.0-or-later
"""Device page with both sync-record copies unreadable: the damaged box offers the rebuild, the Capacity card
still shows the disk space with a calm note (no generic error) and no request fails. 1280 and 390 px."""

from __future__ import annotations

import httpx
import pytest

from syncmusic.core import manifest as manifest_io
from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, settle, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.mark.parametrize("width", [1280, 390])
def test_capacity_card_with_damaged_records(web_server, chrome, tmp_path, width):
    url, _app = web_server
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="A", artist="X", album="Y")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    stick = tmp_path / "stick"
    stick.mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={
            "name": "Stick",
            "mount_path": str(stick),
            "type": "flashdrive",
            "library_ids": ["music"],
        },
    )
    (stick / ".syncmusic").mkdir()
    for rel in (manifest_io.MANIFEST_PATH, manifest_io.BACKUP_PATH):
        (stick / rel).write_bytes(b"garbage\n")

    page = open_page(chrome, url + "/#/devices/stick", width=width)
    failed: list[str] = []
    page.on("response", lambda r: failed.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.reload()  # the listener sees every request of the page load
    page.locator("[data-space-records-damaged]").wait_for()
    page.locator("[data-manifest-damaged]").wait_for()
    settle(page)
    cap = page.locator(".dev-cap")
    assert cap.locator("[data-cap-line]").count() == 1
    assert "Something went wrong" not in cap.inner_text()
    assert "not valid JSON" not in cap.inner_text()
    assert not failed, failed
    assert no_horizontal_scroll(page)
    shot(page, f"device-records-damaged-{width}")
