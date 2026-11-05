"""Logs page in a browser: filters, a live entry appears while the page is open, the download holds the
matching lines, a report's Log link opens the page filtered to its job; phone and desktop width."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from syncmusic.infra.log import KVFormatter
from tests.unit.test_logs_page import _format
from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _seed(app) -> None:
    path = app.config.data_dir / "logs" / "syncmusic.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = KVFormatter()
    try:
        raise OSError("disk full")
    except OSError:
        exc = sys.exc_info()
    lines = [
        _format(f, "app started"),
        _format(f, "copied Зозуля", job="j1", device="classic"),
        _format(
            f, "copy failed: disk full", level=logging.ERROR, exc=exc, job="j1", device="classic"
        ),
        _format(f, "mini mounted", job="j2", device="mini"),
    ]
    path.write_text("\n".join(lines) + "\n")


def _msgs(page) -> list[str]:
    return page.locator("#logList .log-text").all_inner_texts()


@pytest.mark.parametrize("width", [1280, 390])
def test_filter_live_and_download(web_server, chrome, width, tmp_path):
    url, app = web_server
    _seed(app)
    page = open_page(chrome, url + "/#/logs", width)
    page.wait_for_selector("#content[data-route='/logs'] #logList")
    assert _msgs(page) == [  # newest first
        "mini mounted",
        "copy failed: disk full",
        "copied Зозуля",
        "app started",
    ]
    # the error's traceback opens below its line with Copy and Open job
    err = page.locator('#logList .log-row[data-level="ERROR"]')
    err.locator("summary").click()
    assert "OSError: disk full" in err.locator("pre").inner_text()
    assert err.get_by_role("button", name="Copy").is_visible()
    assert err.get_by_role("link", name="Open job").get_attribute("href") == "#/jobs/j1"
    shot(page, f"logs-v2-{width}")

    page.select_option("#logLevel", "ERROR")  # applies at once
    page.wait_for_function("location.hash.includes('level=ERROR')")
    page.wait_for_function("document.querySelectorAll('#logList .log-row').length === 1")
    assert _msgs(page) == ["copy failed: disk full"]

    page.select_option("#logLevel", "INFO")
    page.wait_for_function("!location.hash.includes('level=ERROR')")
    page.wait_for_function("document.querySelectorAll('#logList .log-row').length === 4")
    page.wait_for_selector('#logJob option[value="j2"]', state="attached")
    page.select_option("#logJob", "j2")
    page.wait_for_function("location.hash.includes('job=j2')")
    page.wait_for_function("document.querySelectorAll('#logList .log-row').length === 1")
    assert _msgs(page) == ["mini mounted"]

    page.select_option("#logJob", "")
    page.wait_for_function("!location.hash.includes('job=')")
    page.wait_for_function("document.querySelectorAll('#logList .log-row').length === 4")
    page.fill("#logQuery", "зозуля")
    page.press("#logQuery", "Enter")
    page.wait_for_function("location.hash.includes('query=')")
    page.wait_for_function("document.querySelectorAll('#logList .log-row').length === 1")
    page.wait_for_function("document.querySelector('#logList').innerText.includes('Зозуля')")
    assert _msgs(page) == ["copied Зозуля"]

    with page.expect_download() as dl:
        page.locator("#logDownload").click()
    dl.value.save_as(tmp_path / "dl.log")
    text = (tmp_path / "dl.log").read_text()
    assert "copied" in text and "mini" not in text and text.count("\n") == 1

    # live: a record logged now appears without a reload when it matches, not when it does not
    page.fill("#logQuery", "")
    page.press("#logQuery", "Enter")
    page.wait_for_function("!location.hash.includes('query=')")
    page.wait_for_function("document.querySelectorAll('#logList .log-row').length === 4")
    logging.getLogger("syncmusic.test.live").warning("live line arrived")
    logging.getLogger("syncmusic.test.live").debug("below the level")
    page.wait_for_function(
        "document.querySelector('#logList').innerText.includes('live line arrived')"
    )
    assert "below the level" not in page.locator("#logList").inner_text()
    assert no_horizontal_scroll(page)
    assert page.errors == []


def test_report_log_link_opens_the_job(web_server, chrome, tmp_path):
    import httpx

    from tests.webui.test_device_overview import _synced_stick

    url, app = web_server
    dev_id = _synced_stick(url, app, tmp_path)
    job = httpx.post(f"{url}/api/v1/devices/{dev_id}/update", timeout=30).json()
    page = open_page(chrome, url + f"/#/devices/{dev_id}/history")
    link = page.locator("[data-report-log]").first
    link.wait_for()
    assert job["id"] in link.get_attribute("href")
    link.click()
    page.wait_for_selector("#content[data-route='/logs']")
    assert page.locator("#logJob").input_value() == job["id"]
    assert page.locator("#logLevel").input_value() == "DEBUG"


def test_step_filter_and_column(web_server, chrome):
    """lines a sync job logged in a step show 'device · step' and the step select keeps only them."""
    url, app = web_server
    path = app.config.data_dir / "logs" / "syncmusic.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = KVFormatter()
    lines = [
        _format(f, "plan ready", job="j1", device="mini", step="plan"),
        _format(f, "copied Зозуля", job="j1", device="mini", step="copy"),
    ]
    path.write_text("\n".join(lines) + "\n")
    page = open_page(chrome, url + "/#/logs")
    page.wait_for_selector("#content[data-route='/logs'] #logList .log-row")
    assert [
        x.replace("\n", "") for x in page.locator("#logList .log-where").all_text_contents()
    ] == ["mini · copy", "mini · plan"]
    # a long device name is cut, the step stays visible
    page.evaluate(
        "document.querySelectorAll('#logList .lw-dev').forEach(e => e.textContent = 'Стік a - Довга кирилична назва пристрою, ще довша')"
    )
    box = page.locator("#logList .log-where").first.bounding_box()
    step = page.locator("#logList .lw-src").first.bounding_box()
    assert step["x"] + step["width"] <= box["x"] + box["width"] + 0.5 and step["width"] > 20
    page.select_option("#logStep", "copy")
    page.wait_for_function("location.hash.includes('step=copy')")
    page.wait_for_function("document.querySelectorAll('#logList .log-row').length === 1")
    assert _msgs(page) == ["copied Зозуля"]
    assert "step=copy" in page.locator("#logDownload").get_attribute("href")
    assert page.errors == []


def test_server_lines_and_job_names(web_server, chrome):
    """uvicorn's INFO lines are labelled 'server', not 'error'; two jobs with the same name differ by their
    time in the Job filter."""
    from syncmusic.infra.log import ContextFilter

    url, app = web_server
    path = app.config.data_dir / "logs" / "syncmusic.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = KVFormatter()
    rec = logging.getLogger("uvicorn.error").makeRecord(
        "uvicorn.error", logging.INFO, __file__, 1, "Application startup complete.", (), None
    )
    ContextFilter().filter(rec)
    path.write_text(f.format(rec) + "\n")
    page = open_page(chrome, url + "/#/logs")
    page.wait_for_selector("#content[data-route='/logs'] #logList .log-row")
    where = page.locator("#logList .log-where").first.text_content()
    assert where.endswith("· server") and "error" not in where, where
    labels = page.locator("#logJob option").all_text_contents()
    assert len(labels) == len(set(labels)), labels
    # two jobs of the same name started in the same minute (or second) still read differently
    jobs = [
        {"id": "aaaaaa11", "name": "sync Стік a", "started_at": "2026-10-24T10:02:05Z"},
        {"id": "bbbbbb22", "name": "sync Стік a", "started_at": "2026-10-24T10:02:41Z"},
        {"id": "cccccc33", "name": "sync Стік a", "started_at": "2026-10-24T10:02:41Z"},
        {"id": "dddddd44", "name": "update Стік a", "started_at": "2026-10-24T10:02:41Z"},
    ]
    page.route(
        "**/api/v1/jobs",
        lambda route: route.fulfill(content_type="application/json", body=json.dumps(jobs)),
    )
    page.reload()
    page.wait_for_selector('#logJob option[value="dddddd44"]', state="attached")
    labels = page.locator("#logJob option").all_text_contents()[1:]
    assert len(labels) == len(set(labels)) == 4, labels
    assert "#" not in labels[0] and labels[0].count(":") == 1, (
        labels
    )  # unique already: minutes only
    assert labels[3].count(":") == 2 and "#" not in labels[3], (
        labels
    )  # aaaaaa11: the seconds tell it apart
    assert labels[1].endswith("#cccccc") and labels[2].endswith("#bbbbbb"), labels  # newest first
    assert page.errors == []
