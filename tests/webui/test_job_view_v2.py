"""Sync progress and job view: the running view (big percent, phases checklist, "Now:" with
"file N of M", counters) moved in place by `job.progress`, and the finished report per outcome - a partial sync with
its failed files and Sync again, an update ("No files copied"). Running and partial jobs are routed."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot
from tests.webui.test_device_overview import _api, _synced_stick

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _job(dev, **kw):
    return {"id": "jV", "kind": "sync", "name": "sync", "device_id": dev, "log": [],
            "created_at": "2026-10-24T00:10:00Z", "started_at": "2026-10-24T00:10:00Z", **kw}  # fmt: skip


async def _publish(app, **data):
    app.bus.publish("job.progress", job_id="jV", **data)


@pytest.mark.parametrize("width", [1280, 390])
def test_running_view_moves_in_place(web_server, chrome, tmp_path, width):
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    stats = {
        "phase": "copy",
        "files_done": 141,
        "files_total": 372,
        "bytes": 2_900_000_000,
        "failed": 1,
    }
    job = _job(dev, status="running", progress=0.38, eta_s=760, message="Пісня 1", stats=stats)
    page = open_page(chrome, url, width)
    page.route("**/api/v1/jobs/jV", lambda r: r.fulfill(json=job))
    page.goto(f"{url}/#/devices/{dev}/sync/jV")
    live = page.locator("[data-live]")
    live.wait_for()
    assert live.locator("[data-pct]").inner_text() == "38%"
    assert live.locator('[aria-current="step"]').get_attribute("data-phase-step") == "copy"
    assert live.locator("[data-file-n]").inner_text() == "file 141 of 372"
    assert live.locator("[data-t-written]").inner_text() == "141"
    assert "2.9 GB written" in live.locator("[data-bytes]").inner_text()
    assert page.locator("[data-following]").count() == 1
    assert no_horizontal_scroll(page)
    shot(page, f"job-running-{width}")
    stats = {**stats, "phase": "database", "files_done": 372}
    # the event stream may reconnect under load (a lost event, a redraw): publish until the view shows it
    page.wait_for_selector('#livePill[data-live-state="on"]', state="attached")
    for _ in range(20):
        run_on(
            app, _publish(app, progress=0.97, message="writing the database", eta_s=20, stats=stats)
        )
        try:
            page.wait_for_function(
                "document.querySelector('[data-pct]')?.innerText === '97%'", timeout=1000
            )
            break
        except Exception:  # noqa: BLE001 - not there yet: publish again
            continue
    assert live.locator('[aria-current="step"]').get_attribute("data-phase-step") == "database"
    assert live.locator('[data-phase-step="copy"]').get_attribute("class") == "done"
    assert live.locator("[data-file-n]").inner_text() == "file 372 of 372"
    assert live.locator("[data-phase]").inner_text() == "writing the database"
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_partial_report_lists_failed_files_and_offers_sync_again(web_server, chrome, tmp_path):
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    fail = {
        "ref": "music/x",
        "item": "Христина Соловій - Пробач мені - Шукала",
        "step": "convert",
        "error": "bad frame",
    }
    report = {"device_id": dev, "kind": "full", "plan_id": "p1", "status": "partial", "counts": {"add": 2},
              "failures": [fail], "message": "partial: 2 synced, 0 skipped for space, 1 failed"}  # fmt: skip
    job = _job(
        dev, status="succeeded", progress=1, finished_at="2026-10-24T00:19:00Z", result=report
    )
    page = open_page(chrome, url, 390)
    page.route("**/api/v1/jobs/jV", lambda r: r.fulfill(json=job))
    page.goto(f"{url}/#/devices/{dev}/sync/jV")
    rep = page.locator("[data-report]")
    rep.wait_for()
    assert rep.get_attribute("data-outcome") == "partial"
    assert "Partly synced" in rep.inner_text()
    row = rep.locator("[data-failure]")
    assert (
        "Христина Соловій - Пробач мені - Шукала" in row.inner_text()
        and "Step: convert · bad frame" in row.inner_text()
    )
    assert rep.locator("[data-sync-again]").get_attribute("href").endswith("/plan")
    assert "Partial" in page.locator(".job-line").inner_text()
    assert no_horizontal_scroll(page)
    shot(page, "job-partial-390")


def test_update_job_opens_the_sync_view(web_server, chrome, tmp_path):
    url, app = web_server
    dev = _synced_stick(url, app, tmp_path)
    job = _api(url, "POST", f"/devices/{dev}/update")
    run_on(app, app.jobs.wait(job["id"]), 60)
    page = open_page(chrome, f"{url}/#/devices/{dev}/sync/{job['id']}", 1280)
    rep = page.locator("[data-report]")
    rep.wait_for()
    text = rep.inner_text()
    assert ("Update finished" in text and "No files copied" in text) or "Update skipped" in text, (
        text
    )
    assert page.locator(".job-meta h1").inner_text().startswith("Update · ")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
