"""Device History: filter chips All / Full syncs / Updates / Sent back with counts, one row per
report (badge, when, kind · duration, what changed, errors) that expands to its failures with the step, the gentle
suggestion after a storage failure, Open report and Log. A storage failure is routed (the fixture has none)."""

from __future__ import annotations

import re

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot
from tests.webui.test_device_overview import _api, _synced_stick

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_history_filters_rows_and_failures(web_server, chrome, tmp_path, width):
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    run_on(app, app.jobs.wait(_api(url, "POST", f"/devices/{dev}/update")["id"]), 60)
    page = open_page(chrome, url, width)

    def reports(route):
        r = route.fetch()
        data = r.json()
        full = next(x for x in data if x["kind"] == "full")
        failed = {
            **full,
            "id": "broken",
            "status": "failed",
            "suggest_gentle": True,
            "failures": [{"ref": "x", "item": "Пісня 1", "step": "put", "error": "[Errno 5] I/O"}],
        }
        route.fulfill(response=r, json=[failed, *data])

    page.route(re.compile(r"/api/v1/sync-reports"), reports)
    page.goto(f"{url}/#/devices/{dev}/history")
    rows = page.locator("[data-history] .hist-row")
    rows.first.wait_for()
    chips = [" ".join(x.split()) for x in page.locator("[data-hist-filter]").all_inner_texts()]
    assert chips == ["All 3", "Full syncs 2", "Updates 1", "Sent back 0"]
    # the newest row starts open: its failure with the step, the gentle suggestion, Open report and Log
    first = rows.first
    assert first.get_attribute("open") is not None and first.get_attribute("data-kind") == "full"
    fail = first.locator("[data-failure]").inner_text()
    assert "Пісня 1" in fail and "Step: put" in fail and "[Errno 5] I/O" in fail
    assert first.locator("[data-suggest-gentle]").count() == 1
    assert first.locator("[data-open-report]").count() == 1
    assert "1 failed" in first.locator("summary").inner_text()
    ok = page.locator('.hist-row[data-kind="full"]').nth(1).locator("summary").inner_text()
    assert "+3" in ok and "verified" in ok and "No errors" in ok
    assert no_horizontal_scroll(page)
    shot(page, f"device-history-{width}")
    # a filter keeps only its kind; closed rows open on a click
    page.locator('[data-hist-filter="update"]').click()
    page.wait_for_function("document.querySelectorAll('[data-history] .hist-row').length === 1")
    row = page.locator("[data-history] .hist-row")
    assert row.get_attribute("data-kind") == "update"
    assert "Updated" in row.locator("summary").inner_text()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_history_empty(web_server, chrome, tmp_path):
    url, _ = web_server
    (tmp_path / "stick").mkdir()
    body = {"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"}
    _api(url, "POST", "/devices/register", json=body)
    page = open_page(chrome, f"{url}/#/devices/stick/history", 390)
    page.wait_for_selector("[data-empty]")
    assert "No syncs yet." in page.locator("[data-empty]").inner_text()
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_history_shows_what_went_back(web_server, chrome, tmp_path, width):
    """an update that only sent listening data back says so (not "Nothing changed"), and the row
    lists what went back: plays, ratings, positions, finished episodes, waiting, errors."""
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    run_on(app, app.jobs.wait(_api(url, "POST", f"/devices/{dev}/update")["id"]), 60)
    page = open_page(chrome, url, width)

    def reports(route):
        r = route.fetch()
        data = r.json()
        upd = next(x for x in data if x["kind"] == "update")
        upd["counts"] = {}
        base = {
            "at": upd["started_at"],
            "device_id": dev,
            "report_id": upd["id"],
            "sync_kind": "update",
        }
        upd["reverse"] = {
            "plays_found": 3,
            "plays_submitted": 2,
            "plays_duplicate": 1,
            "ratings_pushed": 1,
            "stars_pushed": 1,
            "positions_pushed": 2,
            "finished_pushed": 1,
            "pending": 4,
            "errors": ["Аудіокниги: timed out"],
        }
        # entries name where they went; "Sent back" keeps only the device → library ones
        upd["activity"] = [
            {**base, "kind": "sent", "counts": {"plays": 2}, "target": "Navidrome"},
            {
                **base,
                "kind": "playlist_written",
                "name": "Mostarda",
                "counts": {"songs": 1442},
                "target": "Stick",
            },
        ]
        route.fulfill(response=r, json=[upd, *[x for x in data if x is not upd]])

    page.route(re.compile(r"/api/v1/sync-reports"), reports)
    page.goto(f"{url}/#/devices/{dev}/history")
    row = page.locator('[data-history] .hist-row[data-kind="update"]').first
    row.wait_for()
    summary = row.locator("summary").inner_text()
    assert (
        "Sent back: 2 plays, 1 rating, 1 heart, 2 positions" in summary
        and "Nothing changed" not in summary
    )
    back = row.locator("[data-hist-back]").inner_text()
    for text in (
        "3 plays found on the device",
        "2 plays sent",
        "1 already in the library",
        "1 heart added or removed",
        "1 episode marked finished",
        "4 waiting for the library",
        "Аудіокниги: timed out",
    ):
        assert text in back, back
    assert no_horizontal_scroll(page)
    shot(page, f"device-history-back-{width}")
    # "Sent back" keeps only the reports whose reverse step sent or kept something
    chip = page.locator('[data-hist-filter="back"]')
    assert " ".join(chip.inner_text().split()) == "Sent back 1"
    chip.click()
    page.wait_for_function("document.querySelectorAll('[data-history] .hist-row').length === 1")
    assert page.locator("[data-history] .hist-row").get_attribute("data-kind") == "update"
    acts = page.locator("[data-history] .hist-row [data-hist-activity]").inner_text()
    assert "Sent 2 plays to Navidrome" in acts and "Mostarda" not in acts, acts
    page.locator('[data-hist-filter="all"]').click()
    page.wait_for_function("document.querySelectorAll('[data-history] .hist-row').length > 1")
    acts = page.locator(
        '[data-history] .hist-row[data-kind="update"] [data-hist-activity]'
    ).first.inner_text()
    assert "Wrote playlist “Mostarda” (1442 songs) to Stick" in acts and "to Navidrome" in acts, (
        acts
    )
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
