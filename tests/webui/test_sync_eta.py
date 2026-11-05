"""the card of a syncing device opens the sync status; the status, the card (short form) and Active jobs
show the time left ("About N min left · done at HH:MM"), "Estimating…" before there is enough data, live; an idle card
still opens the device page. A fake sync job stands in (the ETA's minimum sample time is shortened)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from syncmusic.infra import jobs
from tests.webui.conftest import open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.mark.parametrize("width", [1280, 390])
def test_busy_card_opens_the_status_with_eta(web_server, chrome, tmp_path, monkeypatch, width):
    url, app = web_server
    monkeypatch.setattr(jobs.Eta, "MIN_S", 0.6)
    (tmp_path / "music").mkdir()
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "music",
            "type": "folder",
            "name": "Music",
            "settings": {"root": str(tmp_path / "music")},
        },
    )
    (tmp_path / "stick").mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={
            "name": "Stick",
            "mount_path": str(tmp_path / "stick"),
            "type": "flashdrive",
            "library_ids": ["music"],
        },
    )
    page = open_page(chrome, url + "/#/", width=width)
    if width < 600:
        page.locator("#burger").click()
    card = page.locator("#sideBottom [data-device-card=stick]")
    card.wait_for()
    assert card.get_attribute("href") == "#/devices/stick"
    gate = asyncio.Event()
    started = {}

    async def run(ctx):
        ctx.progress(0.0, "Starting")
        await asyncio.sleep(0.4)  # nothing estimable yet
        for i in range(1, 400):  # 0.25 % per 0.1 s: ~40 s for the whole job
            ctx.progress(i / 400, f"Copying {i}")
            if i == 20:
                await gate.wait()
            ctx.check_cancelled()
            await asyncio.sleep(0.1)

    async def start():
        started["job"] = app.jobs.submit("sync", "Sync Stick", run, device_id="stick")

    run_on(app, start())
    job_id = started["job"].id
    page.wait_for_selector(
        f'#sideBottom [data-device-card=stick][href="#/devices/stick/sync/{job_id}"]'
    )
    # the dashboard's Active jobs: "Estimating…" first, then the time left
    page.wait_for_selector(f'[data-job="{job_id}"] [data-job-eta]:has-text("Estimating")')
    run_on(app, _set(gate))
    page.wait_for_selector(f'[data-job="{job_id}"] [data-job-eta]:has-text("left")', timeout=15_000)
    page.wait_for_selector(
        "#sideBottom [data-device-card=stick] [data-card-left]:has-text('min left')", timeout=15_000
    )
    shot(page, f"sidebar-syncing-{width}")
    card.click()
    page.wait_for_function(f"location.hash === '#/devices/stick/sync/{job_id}'")
    eta = page.locator("[data-live] [data-eta]")
    eta.wait_for()
    page.wait_for_function(
        "() => /About .* left · done at|Less than a minute left/.test(document.querySelector('[data-eta]').innerText)",
        timeout=15_000,
    )
    run_on(app, _cancel(app, job_id))
    page.wait_for_selector(
        "#content [data-report], #content .status", timeout=15_000
    )  # the finished view replaces the live one
    assert page.errors == []


async def _cancel(app, job_id):
    app.jobs.cancel(job_id)
    await app.jobs.wait(job_id)


async def _set(event):
    event.set()


def test_finish_time_names_the_day_when_it_is_not_today(web_server, chrome):
    """ "done at 14:05" today, "done tomorrow at 05:27", later the weekday and date."""
    url, _ = web_server
    page = open_page(chrome, url + "/#/")
    page.wait_for_selector("#content h1")
    texts = page.evaluate(
        """async () => {
        const c = await import('/js/views/common.js');
        const now = new Date(2026, 9, 25, 4, 38);  // Sun 25 Oct 2026, 04:38
        return [c.etaText(3600, false, now), c.etaText(24 * 3600 + 49 * 60, false, now),
                c.etaText(19 * 3600, false, now), c.etaText(50 * 3600, false, now),
                c.etaText(50 * 3600, true, now)];
    }"""
    )
    today, tomorrow, late_tonight, later, short = texts
    assert today.startswith("About 1 h 0 min left · done at ") and "05" in today
    assert tomorrow.startswith("About 24 h 49 min left · done tomorrow at ") and "05" in tomorrow
    assert "done at " in late_tonight and "tomorrow" not in late_tonight  # 23:38 the same day
    assert "tomorrow" not in later and "27" in later and "Oct" in later  # Tue 27 Oct, 06:38
    assert short == "~50 h 0 min"
    assert page.errors == []
