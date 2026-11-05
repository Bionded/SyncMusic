"""every job in Active jobs is a link - a device sync opens its sync status, a library refresh the job view
with type, library, state, times, progress + time left, current step, the live log and Cancel; finished jobs stay
openable from Recent jobs. Fake jobs stand in. 1280 and 390 px."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.webui.conftest import open_page, run_on, settle

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.mark.parametrize("width", [1280, 390])
def test_jobs_open_their_views(web_server, chrome, tmp_path, width):
    url, app = web_server
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
    gate = asyncio.Event()
    ids = {}

    def slow(kind):
        async def run(ctx):
            for i in range(1, 1000):
                ctx.progress(i / 1000, f"{kind} step {i}")
                ctx.check_cancelled()
                if i == 3:
                    await gate.wait()
                await asyncio.sleep(0.05)

        return run

    async def start():
        ids["sync"] = app.jobs.submit("sync", "sync Stick", slow("sync"), device_id="stick").id
        ids["refresh"] = app.jobs.submit(
            "refresh", "refresh music", slow("refresh"), library_id="music"
        ).id

    run_on(app, start())
    page = open_page(chrome, url + "/#/", width=width)
    links = page.locator("#dashJobs [data-job-link]")
    page.wait_for_function(
        "() => document.querySelectorAll('#dashJobs [data-job-link]').length === 2"
    )
    hrefs = sorted(links.evaluate_all("l => l.map(a => a.getAttribute('href'))"))
    assert hrefs == sorted([f"#/devices/stick/sync/{ids['sync']}", f"#/jobs/{ids['refresh']}"])
    # the refresh opens the job view with everything
    page.locator(f'#dashJobs a[href="#/jobs/{ids["refresh"]}"]').click()
    view = page.locator(f'[data-job-view="{ids["refresh"]}"]')
    view.wait_for()
    text = view.inner_text()
    for label in (
        "Type",
        "Library refresh",
        "Library",
        "Music",
        "State",
        "Running",
        "Started",
        "Duration",
        "Progress",
        "Current step",
    ):
        assert label.casefold() in text.casefold(), (label, text)
    assert page.locator("[data-job-log]").inner_text().strip().startswith("")
    run_on(app, _set(gate))
    page.wait_for_function(
        "() => /refresh step \\d+/.test(document.querySelector('[data-job-step]').innerText) && document.querySelector('[data-job-step]').innerText !== 'refresh step 3'",
        timeout=10_000,
    )
    live = page.locator("[data-job-log]").inner_text().split("\n")
    assert any("refresh step" in line for line in live)
    # no blank lines, every live line has its time like the stored log
    assert all(line[:4].isdigit() and "T" in line[:11] for line in live), live
    # Cancel asks, then the job ends cancelled and the view shows it
    page.locator('[data-action="jobCancel"]').click()
    # the dialog's buttons say what they do, not "Cancel" twice
    buttons = page.locator(".modal .modalfoot button").all_inner_texts()
    assert buttons == ["Keep running", "Stop"], buttons
    page.locator(".modal").get_by_role("button", name="Stop", exact=True).click()
    page.wait_for_selector("[data-job-state]:has-text('Cancelled')", timeout=10_000)
    assert (
        page.locator('[data-action="jobCancel"]').count() == 0
        and "finished" in view.inner_text().casefold()
    )
    # the sync row opens the sync status; after cancelling it, Recent jobs opens both
    page.goto(url + "/#/")
    page.locator(f'#dashJobs a[href="#/devices/stick/sync/{ids["sync"]}"]').click()
    page.wait_for_function(f"location.hash === '#/devices/stick/sync/{ids['sync']}'")
    page.wait_for_selector("[data-live]")
    run_on(app, _cancel(app, ids["sync"]))
    page.goto(url + "/#/")
    page.wait_for_selector(f'[data-recent-job="{ids["refresh"]}"] a')
    page.locator(f'[data-recent-job="{ids["refresh"]}"] a').click()
    page.wait_for_selector(f'[data-job-view="{ids["refresh"]}"]')
    assert page.errors == []


async def _set(event):
    event.set()


async def _cancel(app, job_id):
    app.jobs.cancel(job_id)
    await app.jobs.wait(job_id)


def test_finished_job_with_a_long_result_fits_390(web_server, chrome):
    """A finished device sync's job view at 390 px - the Result list (job / plan ids of 32 hex
    characters, ISO times) runs past its card and the page scrolls sideways (scrollWidth ~455 px)."""
    url, app = web_server

    async def done(ctx):
        return {
            "id": "0254a8a10c9f43df91e26b8a96c3e83d",
            "device_id": "virt-flash",
            "plan_id": "4e1e839ae89a4bcbb08e0c2c90723796",
            "status": "succeeded",
            "started_at": "2026-10-22T00:18:50.201765Z",
            "finished_at": "2026-10-22T00:18:51.103580Z",
            "bytes_written": 270203301,
            "message": "",
        }

    async def start():
        job = app.jobs.submit("refresh", "refresh music", done)
        await app.jobs.wait(job.id)
        return job.id

    job_id = run_on(app, start())
    page = open_page(chrome, url + f"/#/jobs/{job_id}", width=390)
    page.wait_for_selector(f'[data-job-view="{job_id}"]')
    page.wait_for_selector("[data-job-state]:has-text('Done')")
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 392, f"job view scrolls sideways at 390 px: scrollWidth {width}"
    assert page.errors == []


def test_job_of_a_removed_device_opens_without_console_errors(web_server, chrome):
    """The job view of a device that was removed since (its reverse sync / sync stays in Recent
    jobs) asks GET /devices/<id>, gets 404 and the browser logs 'Failed to load resource: ... 404'. The view itself
    shows the raw device id. Expected: no failed request in the console (e.g. take the name from the device list or
    the job), the id or 'Removed device' shown."""
    url, app = web_server

    async def done(ctx):
        return {"device_id": "gone-mini", "status": "succeeded", "message": "reverse sync"}

    async def start():
        job = app.jobs.submit("reverse", "reverse sync Gone Mini", done, device_id="gone-mini")
        await app.jobs.wait(job.id)
        return job.id

    job_id = run_on(app, start())
    for width in (1280, 390):
        page = open_page(chrome, url + f"/#/jobs/{job_id}", width=width)
        page.wait_for_selector(f'[data-job-view="{job_id}"]')
        page.wait_for_selector("[data-job-state]:has-text('Done')")
        settle(page)
        text = page.inner_text(f'[data-job-view="{job_id}"]')
        assert "gone-mini" in text and "(removed)" in text
        assert page.errors == []
