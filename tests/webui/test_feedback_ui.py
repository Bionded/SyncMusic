"""In the browser: listening data that did not reach its library - a Needs you row on the dashboard,
a chip on the device header, the Not delivered drawer (waiting rows with the error and last try, given-up rows,
Clear list), at 1280 and 390 px with long Cyrillic titles."""

from __future__ import annotations

import httpx
import pytest

from syncmusic.infra.store import PendingRow
from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.fixture
def seeded(web_server, tmp_path):
    url, app = web_server
    music = tmp_path / "music"
    title = "Дуже довга назва пісні, яка не вміщається в один рядок на телефоні"
    make_music(music / "Скрябін" / "a.mp3", title=title, artist="Скрябін", album="A")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Музика", "settings": {"root": str(music)}},
    )
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Stick",
        "type": "flashdrive",
        "mount_path": str(tmp_path / "stick"),
        "library_ids": ["music"],
    }
    dev = _api(url, "POST", "/devices/register", json=body)["id"]
    plan = _api(url, "POST", f"/devices/{dev}/plan", json={})
    ref = next(a["ref"] for a in plan["actions"] if a["item"])
    key = f"{ref['library_id']}/{ref['item_id']}"
    play = {"ref": ref, "played_at": "2026-10-24T07:10:00+00:00", "source_device": dev}

    async def seed():
        await app.store.capture_feedback(
            dev, [("play", play), ("rating", {"ref": key, "rating": 4})], [], None
        )
        rows = await app.store.pending_feedback(dev)
        await app.store.retry_failed(rows[0].id, "Navidrome answered 503", count=False)
        await app.store.drop_pending(
            PendingRow(rows[1].id, "rating", rows[1].data, 9, "x"), dev, "song not found"
        )

    run_on(app, seed())
    return url, dev, title


@pytest.mark.parametrize("width", [1280, 390])
def test_not_delivered_is_visible(seeded, chrome, width):
    url, dev, title = seeded
    page = open_page(chrome, url + "/#/", width=width)
    row = page.locator('[data-need="feedback"]')
    row.wait_for()
    assert "1 play, rating or position waits for its library" in row.inner_text()
    assert "1 given up" in row.inner_text()
    row.get_by_role("link", name="Show").click()
    drawer = page.locator('[data-drawer="feedback"]')
    drawer.wait_for()
    page.wait_for_selector('[data-fb-row="pending"]')
    pending = page.locator('[data-fb-row="pending"]').inner_text()
    assert (
        title in pending
        and "Музика" in pending
        and "Navidrome answered 503" in pending
        and "last try" in pending
        and "tried once" in pending  # every failed try counts, not only refusals
    )
    dropped = page.locator('[data-fb-row="dropped"]').inner_text()
    assert "Rating · 4 ★" in dropped and "song not found" in dropped and "10 refusals" in dropped
    assert no_horizontal_scroll(page)
    shot(page, f"feedback-drawer-{width}")
    assert page.locator("[data-feedback-chip]").inner_text().strip() == "2 not delivered"
    if width == 1280:
        drawer.get_by_role("button", name="Clear list").click()
        page.wait_for_selector(".toast:has-text('given-up items is empty')")
        assert page.locator('[data-fb-row="dropped"]').count() == 0
        assert _api(url, "GET", f"/devices/{dev}/feedback")["dropped"] == []
        page.wait_for_function(
            "document.querySelector('[data-feedback-chip]')?.innerText.includes('1 not delivered')"
        )
    assert page.errors == []


def test_report_line_in_history(seeded, chrome):
    """The History row of a sync whose reverse sync left data waiting says what waits for which library."""
    import json as _json
    import re

    url, dev, _ = seeded
    report = {
        "id": "r1",
        "device_id": dev,
        "status": "succeeded",
        "kind": "full",
        "started_at": "2026-10-24T08:00:00Z",
        "finished_at": "2026-10-24T08:02:00Z",
        "counts": {"add": 1},
        "reverse": {"pending": 3, "pending_by": {"music": {"play": 2, "rating": 1}}, "dropped": 1},
    }
    page = open_page(chrome, url + "/#/")
    page.route(
        re.compile(r"/api/v1/sync-reports\?device="),
        lambda route: route.fulfill(content_type="application/json", body=_json.dumps([report])),
    )
    page.goto(url + f"/#/devices/{dev}/history")
    page.wait_for_selector("[data-report]")
    line = page.locator("[data-feedback-line]")
    line.wait_for()
    text = line.inner_text()
    assert "2 plays and 1 rating waiting for Музика" in text and "1 given up" in text, text
    line.get_by_role("button", name="Show what is not delivered").click()
    page.wait_for_selector('[data-drawer="feedback"] [data-fb-row]')
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_reverse_job_with_waiting_data_does_not_say_nothing_to_send(seeded, chrome):
    """a reverse sync that delivered nothing because the library is down says 'Nothing delivered yet' and
    what waits, not 'Nothing to send back'."""
    import json as _json

    url, dev, _ = seeded
    report = {
        "id": "r2",
        "device_id": dev,
        "status": "succeeded",
        "kind": "full",
        "plan_id": None,
        "reverse": {"plays_found": 3, "pending": 3, "pending_by": {"music": {"play": 3}}},
    }
    job = {
        "id": "jrev",
        "kind": "reverse",
        "name": "reverse Stick",
        "status": "succeeded",
        "device_id": dev,
        "started_at": "2026-10-24T08:00:00Z",
        "finished_at": "2026-10-24T08:00:03Z",
        "result": report,
        "log": [],
    }
    page = open_page(chrome, url + "/#/")
    page.route(
        "**/api/v1/jobs/jrev",
        lambda route: route.fulfill(content_type="application/json", body=_json.dumps(job)),
    )
    page.goto(url + f"/#/devices/{dev}/sync/jrev")
    page.wait_for_selector("[data-report] .job-reverse")
    text = page.locator("[data-report]").inner_text()
    assert "Nothing to send back" not in text and "Nothing delivered yet." in text, text
    assert "3 plays waiting for Музика" in text, text
    assert page.errors == []
