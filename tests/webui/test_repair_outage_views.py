"""Finished job views of a file-system repair and of a sync with a library outage: what the
head says, the fsck output, no "0 files could not be written"."""

from __future__ import annotations

import json

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_plan_flow import device  # noqa: F401

pytestmark = [pytest.mark.ui, pytest.mark.slow]

REPAIR = {
    "id": "j1",
    "kind": "repair",
    "name": "repair stick",
    "device_id": "stick",
    "status": "succeeded",
    "progress": 1.0,
    "message": "errors corrected",
    "finished_at": "2026-10-26T15:00:00Z",
    "log": [],
    "result": {
        "fixed": True,
        "output": "fsck.fat 4.2 (2021-01-31)\n/MUSIC/A.MP3: Start cluster beyond limit (12345 > 1000). Truncating file.\nPerform changes ? (y/n) y\n/dev/loop3: 120 files, 900/1000 clusters",
    },
}
SYNC = {
    "id": "j2",
    "kind": "sync",
    "name": "sync stick",
    "device_id": "stick",
    "status": "succeeded",
    "progress": 1.0,
    "message": "",
    "finished_at": "2026-10-26T15:00:00Z",
    "log": [],
    "result": {
        "device_id": "stick",
        "status": "partial",
        "counts": {"copied": 3},
        "postponed": 7,
        "failures": [],
        "warnings": ["Аудіокниги ABS unreachable - 7 items postponed to the next sync"],
        "message": "3 copied, 7 postponed",
    },
}

EXPECT = {
    "j1": [
        "Check and repair · Stick",
        "File system errors corrected",
        "Start cluster beyond limit",
    ],
    "j2": ["7 items were postponed", "Аудіокниги ABS unreachable"],
}


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("job", [REPAIR, SYNC], ids=["repair", "outage"])
def test_finished_job_view(device, chrome, width, job):  # noqa: F811
    url, _app, _stick = device
    page = open_page(chrome, url + "/#/", width)
    page.route(
        f"**/api/v1/jobs/{job['id']}",
        lambda r: r.fulfill(content_type="application/json", body=json.dumps(job)),
    )
    page.goto(f"{url}/#/devices/stick/sync/{job['id']}")
    report = page.locator("[data-report]")
    report.wait_for()
    if job["kind"] == "repair":
        page.locator("[data-repair-output] summary").click()
    text = page.locator("#content").inner_text()
    for want in EXPECT[job["id"]]:
        assert want in text
    assert "is synced" not in text and "0 files could not be written" not in text
    shot(page, f"job-{job['kind']}-{width}")
    assert no_horizontal_scroll(page)
    assert page.errors == []
