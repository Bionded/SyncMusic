"""Dashboard: "Needs you" - pending review → the right device's plan, unknown drive →
Register -, active jobs with live progress and Cancel in place, automatic updates as quiet rows, successful updates
left out of Recent jobs, the gentle-mode suggestion, library health, the empty state."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from syncmusic.core.detection import ConnectedDisk
from syncmusic.core.models import DeviceConfig, DeviceIdentity, RuleSet
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


async def _seed(app, music):
    for dev_id, name in (("zorvex", "Zorvex"), ("classic", "iPod Classic")):
        await app.store.put_device(
            DeviceConfig(
                id=dev_id,
                type="flashdrive",
                name=name,
                identity=DeviceIdentity(usb_serial=f"S-{dev_id}"),
                rule_set=RuleSet(template_ids=["default.music.all"]),
            )
        )
    await app.sync.load()
    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz", serial="S-zorvex", tran="usb")
    app.sync.detector.connected["sdz"] = ConnectedDisk(
        disk=disk, device_id="zorvex", data_partition="/dev/sdz1"
    )
    app.sync._waiting["zorvex"] = {"reason": "on_connect: ask", "plan_id": "plan-123"}
    unknown = BlockDeviceInfo(name="sdx", path="/dev/sdx", vendor="Apple", model="iPod", tran="usb")
    app.sync.detector.connected["sdx"] = ConnectedDisk(disk=unknown)
    release = asyncio.Event()

    async def job(ctx):
        ctx.progress(0.4, "copying 4 of 10")
        await release.wait()
        return {}

    info = app.jobs.submit("sync", "sync iPod Classic", job, device_id="classic")
    return info.id, release


def test_dashboard(web_server, chrome, tmp_path):
    url, app = web_server
    music = tmp_path / "music"
    music.mkdir()
    lib = {
        "id": "music",
        "type": "folder",
        "name": "Music folder",
        "settings": {"root": str(music)},
    }
    assert httpx.post(f"{url}/api/v1/libraries", json=lib).status_code == 201
    job_id, release = run_on(app, _seed(app, music))
    try:
        page = open_page(chrome, url)
        page.wait_for_selector("[data-review]")
        assert (
            page.locator("[data-review]").get_attribute("href") == "#/devices/zorvex/plan/plan-123"
        )
        assert (
            page.locator("[data-register]").get_attribute("href")
            == "#/devices/register?disk=%2Fdev%2Fsdx"
        )
        row = page.locator(f'[data-job="{job_id}"]')
        assert row.locator(".bar").get_attribute("aria-valuenow") == "40"
        assert "copying 4 of 10" in row.inner_text()
        page.wait_for_selector('[data-health="music"] .status.ok')
        needs = page.locator("[data-needs]").inner_text()
        assert "Zorvex: plan ready to review" in needs and "Unknown drive: Apple iPod" in needs

        def progress():  # live update without a reload
            run_on(app, _progress(app, job_id))

        progress()
        page.wait_for_function(
            f"document.querySelector('[data-job=\"{job_id}\"] .bar').getAttribute('aria-valuenow') === '80'"
        )
        assert (
            page.locator('[data-stat="jobs"] small').inner_text().startswith("80%")
        )  # follows the job
        shot(page, "dashboard-1280")
        # Cancel in place: asks, then the row says Cancelling… (the job stops at its next safe point)
        row.locator("[data-action=cancelJob]").click()
        # dismiss keeps the job, the confirm button says Stop
        page.locator("#overlay").get_by_role("button", name="Keep running", exact=True).click()
        assert "Cancelling" not in row.locator("[data-action=cancelJob]").inner_text()
        row.locator("[data-action=cancelJob]").click()
        assert page.locator("#overlay button.danger").inner_text() == "Stop"
        page.locator("#overlay button.danger").click()
        page.wait_for_selector(f'[data-job="{job_id}"] [data-action=cancelJob][disabled]')
        assert "Cancelling" in row.locator("[data-action=cancelJob]").inner_text()
        page.locator("[data-review]").click()
        page.wait_for_function("location.hash === '#/devices/zorvex/plan/plan-123'")
        # a seeded plan id: the plan screen says it is outdated (404)
        page.wait_for_selector("text=This plan is outdated")
        page.set_viewport_size({"width": 375, "height": 800})
        page.goto(url + "/#/")
        page.wait_for_selector("[data-review]")
        page.wait_for_selector('[data-health="music"] .status.ok')
        shot(page, "dashboard-375")
        assert no_horizontal_scroll(page)
        assert [e for e in page.errors if "404 (Not Found)" not in e] == []
    finally:
        app.test_loop.call_soon_threadsafe(release.set)


async def _progress(app, job_id):
    app.bus.publish(
        "job.progress", job_id=job_id, device_id="classic", progress=0.8, message="copying 8 of 10"
    )


@pytest.mark.parametrize("width", [1280, 390])
def test_dashboard_v2_rows(web_server, chrome, tmp_path, width):
    """Round 2: a running update is a quiet row without a bar (not an active job, not in the Devices
    badge); a successful update is left out of Recent jobs; a full sync stopped by the device's storage offers
    'Turn on gentle mode' (with Undo) in Needs you."""
    from syncmusic.core.models import SyncReport

    url, app = web_server
    (tmp_path / "music").mkdir()
    httpx.post(
        f"{url}/api/v1/libraries",
        json={
            "id": "music",
            "type": "folder",
            "name": "Music",
            "settings": {"root": str(tmp_path / "music")},
        },
    ).raise_for_status()
    ids = {}
    for did, name in (("stick", "Флешка"), ("other", "Other")):
        (tmp_path / did).mkdir()
        r = httpx.post(
            f"{url}/api/v1/devices/register",
            json={"name": name, "mount_path": str(tmp_path / did), "type": "flashdrive"},
        )
        r.raise_for_status()
        ids[did] = r.json()["id"]  # the Cyrillic name gives a slug id
    release = asyncio.Event()

    async def seed():
        await app.store.put_report(
            SyncReport(
                id="r1",
                device_id=ids["stick"],
                status="failed",
                suggest_gentle=True,
                message="I/O error",
            )
        )

        async def quick(ctx):
            return {"kind": "update", "status": "succeeded"}

        async def slow(ctx):
            ctx.progress(0.5, "positions")
            await release.wait()

        done = app.jobs.submit("update", "Update Флешка", quick, device_id=ids["stick"])
        await app.jobs.wait(done.id)
        app.jobs.submit("update", "Update Other", slow, device_id=ids["other"])

    run_on(app, seed())
    try:
        page = open_page(chrome, url + "/#/", width=width)
        page.wait_for_selector('[data-update-row="other"]')
        assert (
            page.locator('[data-update-row="other"] [data-update-line][data-state=running]').count()
            == 1
        )
        assert (
            page.locator("#dashJobs [data-job]").count() == 0
        )  # an update has no job row with a bar
        assert page.locator('[data-stat="jobs"] strong').inner_text() == "0"
        assert page.locator("[data-recent-job]").count() == 0  # the successful update is not news
        if width == 390:  # the phone card calls an update an update, not a sync
            pill = page.locator('[data-phone-device="other"] .dash-phone-name')
            assert "Updating" in pill.inner_text() and "Syncing" not in pill.inner_text()
        gentle = page.locator(f'[data-need="gentle"][data-device="{ids["stick"]}"]')
        assert "Флешка: full sync stopped" in gentle.inner_text()
        shot(page, f"dashboard-v2-{width}")
        assert no_horizontal_scroll(page)
        gentle.locator("[data-action=gentleOn]").click()
        page.wait_for_selector('[data-need="gentle"]', state="detached")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.toast')].some((t) => t.innerText.includes('Gentle write mode is on'))"
        )
        cfg = httpx.get(f"{url}/api/v1/devices/{ids['stick']}").json()["config"]
        assert cfg["sync"]["gentle_write"] is True
        assert page.errors == []
    finally:
        app.test_loop.call_soon_threadsafe(release.set)


def test_dashboard_empty_state(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url + "/#/")
    page.wait_for_selector("[data-dash-empty]")
    # the first-run wizard first, Add a library beside it
    assert page.locator("[data-dash-empty] a.btn.primary").get_attribute("href") == "#/welcome"
    assert page.locator('[data-dash-empty] a[href="#/libraries/new"]').count() == 1
    assert page.locator("[data-stat]").count() == 0
    shot(page, "dashboard-empty-1280")
    assert page.errors == []


PHONE_DEVICES = [
    {"id": "syncing", "type": "ipod", "name": "iPod Classic", "connected": True, "busy": True},
    {
        "id": "waits",
        "type": "rockbox",
        "name": "Плеєр з дуже довгою назвою Rockbox",
        "connected": True,
        "waiting": True,
    },
    {
        "id": "broken",
        "type": "ftp",
        "name": "FTP",
        "connected": True,
        "error": "login failed: 530 Login incorrect",
    },
    {"id": "clock", "type": "ipod", "name": "Nano", "connected": True, "setting_clock": True},
    {"id": "off", "type": "flashdrive", "name": "Offline stick", "connected": False},
]
PHONE_JOB = {
    "id": "j1",
    "kind": "sync",
    "name": "Sync iPod Classic",
    "status": "running",
    "device_id": "syncing",
    "progress": 0.42,
    "eta_s": 600,
    "message": "Copying",
    "created_at": "2026-10-24T10:00:00Z",
    "started_at": "2026-10-24T10:00:00Z",
}


@pytest.mark.parametrize("width", [390, 1280])
def test_phone_dashboard_device_cards(web_server, chrome, width):
    """on a phone the connected devices come first with big buttons whose primary follows the
    state; on a desktop the cards are hidden."""
    url, _ = web_server
    page = chrome.new_page(viewport={"width": width, "height": 900})
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    devices = [{"update": None, **d} for d in PHONE_DEVICES]
    page.route(
        "**/api/v1/devices", lambda r: r.fulfill(json={"registered": devices, "unknown": []})
    )
    page.route("**/api/v1/jobs", lambda r: r.fulfill(json=[PHONE_JOB]))
    page.goto(url + "/#/")
    section = page.locator("[data-phone-devices]")
    section.wait_for(state="attached")
    if width > 760:
        assert not section.is_visible()
        assert page.errors == []
        return
    cards = section.locator("[data-phone-device]")
    assert cards.evaluate_all("els => els.map(e => e.dataset.phoneDevice)") == [
        "syncing",
        "waits",
        "broken",
        "clock",
    ]
    primary = lambda d: section.locator(f'[data-phone-device="{d}"] .btn.primary')  # noqa: E731
    assert primary("syncing").inner_text().strip() == "Show progress"
    assert "42%" in section.locator('[data-phone-device="syncing"] [data-phone-msg]').inner_text()
    assert primary("waits").inner_text().strip() == "Review & sync"
    assert primary("waits").get_attribute("href") == "#/devices/waits/plan"
    assert primary("broken").inner_text().strip() == "Open settings"
    assert primary("clock").is_disabled()
    assert section.locator('[data-phone-device="syncing"] [data-action=phoneEject]').is_disabled()
    box = primary("waits").bounding_box()
    assert box["height"] >= 44
    no_horizontal_scroll(page)
    assert page.errors == []
    shot(page, f"t325-phone-{width}")


@pytest.mark.parametrize("width", [1280, 390])
def test_needs_attention_rows(web_server, chrome, tmp_path, width):
    """a failed last sync, items that did not fit and an available update join "Needs you", most
    severe first; the update and the failed sync can be dismissed (remembered across reloads)."""
    from syncmusic.core.models import SyncReport

    url, app = web_server
    ids = {}
    for did, name in (("a", "Плеєр"), ("b", "Stick")):
        (tmp_path / did).mkdir()
        r = httpx.post(
            f"{url}/api/v1/devices/register",
            json={"name": name, "mount_path": str(tmp_path / did), "type": "flashdrive"},
        )
        r.raise_for_status()
        ids[did] = r.json()["id"]

    async def seed():
        await app.store.put_report(
            SyncReport(id="rb", device_id=ids["b"], status="partial", skipped_for_space=["x", "y"])
        )
        await app.store.put_report(
            SyncReport(id="ra", device_id=ids["a"], status="failed", message="library gone")
        )
        app.updates.available, app.updates.latest = True, "abc1234"

    run_on(app, seed())
    page = open_page(chrome, url + "/#/", width=width)
    page.wait_for_selector('[data-need="update"]')
    kinds = page.locator("[data-needs] [data-need]").evaluate_all(
        "rows => rows.map(r => r.dataset.need)"
    )
    for k in ("sync-failed", "space", "update"):
        assert k in kinds
    assert kinds.index("sync-failed") < kinds.index("space") < kinds.index("update")
    failed = page.locator('[data-need="sync-failed"]')
    assert "Плеєр: last sync failed" in failed.inner_text()
    assert "library gone" in failed.inner_text()
    # the sidebar's Dashboard badge counts the device rows: both devices have no templates
    if width == 1280:
        assert page.locator("[data-nav=dashboard] [data-needs-badge]").inner_text() == "2"
        # its label says it counts device problems, not the whole list
        assert "device problems" in page.locator(
            "[data-nav=dashboard] [data-needs-badge]"
        ).get_attribute("title")
    assert "Stick: 2 items did not fit" in page.locator('[data-need="space"]').inner_text()
    shot(page, f"dashboard-attention-{width}")
    assert no_horizontal_scroll(page)
    page.locator('[data-need="update"] [data-action=dismissNeed]').click()
    page.wait_for_selector('[data-need="update"]', state="detached")
    page.reload()
    page.wait_for_selector('[data-need="sync-failed"]')
    assert page.locator('[data-need="update"]').count() == 0
    assert page.errors == []


def test_device_card_shows_the_sync_step(web_server, chrome, tmp_path):
    """a running sync's card names its step with files n/m, and the speed once it moved."""
    url, app = web_server
    (tmp_path / "s").mkdir()
    r = httpx.post(
        f"{url}/api/v1/devices/register",
        json={"name": "Stick", "mount_path": str(tmp_path / "s"), "type": "flashdrive"},
    )
    r.raise_for_status()
    dev_id = r.json()["id"]
    release = asyncio.Event()

    async def job(ctx):
        for i in range(1, 40):
            ctx.progress(
                i / 40, f"file {i}", phase="copy", files_done=i, files_total=40, bytes=i * 5_000_000
            )
            await asyncio.sleep(0.25)
        await release.wait()

    page = open_page(chrome, url + "/#/", width=1280)
    run_on(app, _submit(app, job, dev_id))
    try:
        step = page.locator(f'[data-device-card="{dev_id}"] [data-card-step]')
        step.wait_for()
        page.wait_for_function(
            "() => /MB\\/s/.test(document.querySelector('[data-card-step]')?.innerText || '')",
            timeout=15000,
        )
        shot(page, "sidebar-sync-step")
        assert "Copying" in step.inner_text() and "/40" in step.inner_text()
        assert page.errors == []
    finally:
        app.test_loop.call_soon_threadsafe(release.set)


@pytest.mark.parametrize("width", [1280, 390])
def test_dashboard_and_sync_page_show_step_and_prepares(web_server, chrome, tmp_path, width):
    """the dashboard's job row / phone card name the sync step, and the sync page lists what
    is being prepared (merges, conversions)."""
    url, app = web_server
    (tmp_path / "s").mkdir()
    r = httpx.post(
        f"{url}/api/v1/devices/register",
        json={"name": "Stick", "mount_path": str(tmp_path / "s"), "type": "flashdrive"},
    )
    r.raise_for_status()
    dev_id = r.json()["id"]
    release = asyncio.Event()
    prep = ["Combining “Хрещення вогнем” - 1.0 of 13.0 h, ~45 min left", "Converting 3 FLAC → ALAC"]

    async def job(ctx):
        for i in range(1, 80):
            ctx.progress(
                i / 80,
                f"file {i}",
                phase="copy",
                files_done=i,
                files_total=80,
                bytes=i * 5_000_000,
                preparing=prep,
            )
            await asyncio.sleep(0.25)
        await release.wait()

    page = open_page(chrome, url + "/#/", width=width)
    run_on(app, _submit(app, job, dev_id))
    try:
        sel = (
            f'[data-phone-device="{dev_id}"] [data-phone-step]'
            if width == 390
            else "[data-job] [data-job-step]"
        )
        page.wait_for_function(
            f"() => /Copying \\d+\\/80/.test(document.querySelector('{sel}')?.innerText || '')",
            timeout=15000,
        )
        shot(page, f"dashboard-sync-step-{width}")
        assert no_horizontal_scroll(page)
        job_id = app.jobs.running_for_device(dev_id).id
        page.goto(f"{url}/#/devices/{dev_id}/sync/{job_id}")
        box = page.locator("[data-preparing]")
        box.wait_for()
        page.wait_for_function(
            "() => /FLAC → ALAC/.test(document.querySelector('[data-prep-list]')?.innerText || '')"
        )
        assert "Хрещення вогнем" in box.inner_text()
        shot(page, f"sync-preparing-{width}")
        assert no_horizontal_scroll(page)
        assert page.errors == []
    finally:
        app.test_loop.call_soon_threadsafe(release.set)


async def _submit(app, job, dev_id):
    app.jobs.submit("sync", "sync Stick", job, device_id=dev_id)


@pytest.mark.parametrize("width", [1280, 390])
def test_setup_checklist(web_server, chrome, tmp_path, width):
    """after the first library the dashboard shows the getting-started checklist (library ticked,
    the next steps linked); dismissing it keeps it away after a reload."""
    url, _ = web_server
    (tmp_path / "m").mkdir()
    lib = {
        "id": "first",
        "type": "folder",
        "name": "Музика",
        "settings": {"root": str(tmp_path / "m")},
    }
    assert httpx.post(f"{url}/api/v1/libraries", json=lib, timeout=60).status_code == 201
    page = open_page(chrome, url + "/#/", width)
    card = page.wait_for_selector("[data-setup]")
    assert page.get_attribute('[data-setup-step="library"]', "data-done") == "true"
    assert page.get_attribute('[data-setup-step="device"]', "data-done") == "false"
    assert page.get_attribute('[data-setup-step="device"] a', "href") == "#/devices"
    assert " of 5 done" in card.inner_text()  # the optional system check may already be green
    shot(page, f"dashboard-setup-{width}")
    assert no_horizontal_scroll(page)
    page.click("[data-setup] [data-action=dismissNeed]")
    page.wait_for_selector("[data-setup]", state="detached")
    page.reload()
    page.wait_for_selector("[data-needs]")
    assert page.locator("[data-setup]").count() == 0
    assert not page.errors
