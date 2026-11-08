"""Overview → Storage health: Run integrity check, what it found, Delete broken files."""

from __future__ import annotations

import threading

import pytest

from syncmusic.core import integrity
from tests.webui.conftest import no_horizontal_scroll, open_page, poll, run_on, shot
from tests.webui.test_plan_flow import _api, device  # noqa: F401 - fixture

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_integrity_check_card(device, chrome, tmp_path, width):  # noqa: F811
    url, app, _stick = device
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    files = sorted((tmp_path / "stick").rglob("*.mp3"))
    files[0].write_bytes(b"short")  # broken
    (tmp_path / "stick" / "Чуже - дуже довга назва файлу від іншої програми.mp3").write_bytes(
        b"x" * 50
    )

    page = open_page(chrome, url + "/#/devices/stick", width)
    card = page.locator("[data-health]")
    card.wait_for()
    assert "No integrity check yet" in card.inner_text()
    card.get_by_role("button", name="Run integrity check").click()
    page.wait_for_selector("[data-integrity-summary]", timeout=30000)
    card = page.locator("[data-health]")
    assert "1 broken" in card.locator("[data-integrity-summary]").inner_text()
    assert (
        card.locator('[data-integrity="foreign"] summary').inner_text() == "Not from SyncMusic · 1"
    )
    card.locator('[data-integrity="broken"] summary').click()
    assert "size 5 bytes" in card.locator('[data-integrity="broken"]').inner_text()
    shot(page, f"integrity-{width}")
    assert no_horizontal_scroll(page)
    card.get_by_role("button", name="Delete 1 broken file").click()
    poll(page, lambda: not files[0].exists(), what="the repair to delete the broken file")
    # the card follows the fix - the deleted file is missing now, no second delete
    page.wait_for_selector('[data-health] [data-integrity="missing"]', timeout=15000)
    card = page.locator("[data-health]")
    assert card.locator('[data-integrity="broken"]').count() == 0
    assert card.get_by_role("button", name="Delete 1 broken file").count() == 0
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_integrity_check_is_no_sync(device, chrome, monkeypatch, width):  # noqa: F811
    """while the check runs the header and the sidebar say "Checking files…", not "Syncing"; its job view
    is an integrity check (what it found) - never "is synced" / "Not verified"."""
    url, app, _stick = device
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    go = threading.Event()
    real = integrity.check

    def slow(*a, **kw):
        go.wait(30)
        return real(*a, **kw)

    monkeypatch.setattr(integrity, "check", slow)
    try:
        page = open_page(chrome, url + "/#/devices/stick", width)
        page.locator("[data-health]").get_by_role("button", name="Run integrity check").click()
        page.locator("[data-device-head]").get_by_text("Checking files…").wait_for(timeout=15000)
        head = page.locator("[data-device-head]").inner_text()
        assert "Syncing" not in head and "Updates wait until the check ends" in head
        if width == 1280:
            assert "Checking files…" in page.locator('[data-device-card="stick"]').inner_text()
        page.locator("[data-device-head] [data-sync]").click()  # Show progress
        page.wait_for_selector("[data-live]")
        assert page.locator("h1").inner_text().startswith("Integrity check · Stick")
        assert page.locator("[data-phases], [data-tally]").count() == 0
        shot(page, f"integrity-running-{width}")
    finally:
        go.set()
    page.wait_for_selector("[data-report]", timeout=30000)
    report = page.locator("[data-report]").inner_text()
    assert "Integrity check finished" in report and "3 files checked" in report
    assert "Synced" not in page.locator(".job-line").inner_text()
    assert "synced" not in report and "Not verified" not in report and "eject" not in report
    assert page.locator("[data-counts] .count-tile").first.inner_text().split("\n") == [
        "3",
        "Checked",
    ]
    shot(page, f"integrity-done-{width}")
    assert no_horizontal_scroll(page)
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_health_vitals_and_warning(device, chrome, width):  # noqa: F811
    """I/O errors in 30 days, the write-speed trend and the warning with its advice."""
    url, app, _stick = device
    for mb, secs in ((100, 10), (100, 10), (100, 12), (100, 11)):
        run_on(app, app.store.add_health("stick", "write", mb * 1_000_000, secs), 10)
    page = open_page(chrome, url + "/#/devices/stick", width)
    page.wait_for_selector("[data-health] [data-speed] .dev-trend")
    assert page.locator("[data-speed] b").inner_text() == "9.09 MB/s · usually 10 MB/s"
    assert page.locator("[data-health-warning]").count() == 0
    cfg = run_on(app, app.sync.config("stick"), 10)
    run_on(app, app.sync._record_health(cfg, error="[Errno 5] Input/output error"), 10)
    page.reload()
    page.wait_for_selector("[data-health-warning]")
    assert page.locator("[data-io-errors]").get_attribute("data-io-errors") == "1"
    assert "Storage may be failing" in page.locator("[data-health-warning]").inner_text()
    shot(page, f"health-{width}")
    assert no_horizontal_scroll(page)
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_dashboard_needs_you_row(device, chrome):  # noqa: F811
    url, app, _stick = device
    cfg = run_on(app, app.sync.config("stick"), 10)
    run_on(app, app.sync._record_health(cfg, error="[Errno 5] Input/output error"), 10)
    page = open_page(chrome, url + "/#/", 390)
    row = page.locator('[data-need="health"]')
    row.wait_for()
    assert "Stick: storage may be failing" in row.inner_text()
    assert "1 I/O error in the last 30 days" in row.inner_text()
    row.get_by_role("button", name="Acknowledge").click()
    toast = page.locator(".toast", has_text="acknowledged")
    toast.wait_for()
    assert page.locator('[data-need="health"]').count() == 0
    toast.locator("[data-toast-undo]").click()  # Undo brings it back
    page.locator('[data-need="health"]').wait_for()


@pytest.mark.parametrize("width", [1280, 390])
def test_health_card_lists_acknowledges_and_clears_errors(device, chrome, width):  # noqa: F811
    url, app, _stick = device
    cfg = run_on(app, app.sync.config("stick"), 10)
    for n in range(3):
        run_on(
            app,
            app.sync._record_health(cfg, error=f"integrity check: /x/{n}.mp3: Input/output error"),
            10,
        )
    page = open_page(chrome, url + "/#/devices/stick", width)
    page.wait_for_selector("[data-health-warning]")
    lst = page.locator("[data-io-list]")
    lst.locator("summary").click()
    assert lst.locator("li").count() == 3 and "/x/2.mp3" in lst.locator("li").first.inner_text()
    shot(page, f"health-errors-{width}")
    page.locator("[data-health-warning]").get_by_role("button", name="Acknowledge").click()
    page.wait_for_selector("[data-health-acked]")
    assert page.locator("[data-health-warning]").count() == 0
    page.locator("[data-io-list] summary").click()
    page.locator("[data-io-list]").get_by_role("button", name="Clear error history").click()
    page.locator(".modal, dialog").get_by_role("button", name="Clear error history").click()
    page.wait_for_selector('[data-io-errors="0"]')
    assert page.locator("[data-io-list]").count() == 0
    assert no_horizontal_scroll(page)
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_plan_warns_about_failing_storage(device, chrome):  # noqa: F811
    url, app, _stick = device
    cfg = run_on(app, app.sync.config("stick"), 10)
    run_on(app, app.sync._record_health(cfg, error="[Errno 5] Input/output error"), 10)
    page = open_page(chrome, url + "/#/devices/stick/plan", 390)
    warn = page.locator('[data-warn="health"]')
    warn.wait_for()
    assert "storage may be failing" in warn.inner_text()
    assert "1 I/O error in the last 30 days." in warn.inner_text()
    assert no_horizontal_scroll(page)
