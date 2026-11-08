"""Plan review → apply → progress → report: the first sync needs keep/adopt/wipe and the choice is
stored; a plan that does not fit offers Force, which needs one confirmation to plan with priority trimming and a
second one to apply; the report shows the read-back verification and Eject only once the job has finished."""

from __future__ import annotations

import csv
from pathlib import Path

import httpx
import pytest

from syncmusic.devices._filedevice import FileDeviceMixin
from tests.media import make_music
from tests.webui.conftest import link_rules, no_horizontal_scroll, open_page, run_on, shot
from tests.webui.test_libraries import _add_to

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.fixture
def device(web_server, tmp_path):
    url, app = web_server
    music = tmp_path / "music"
    for artist, title in (("Скрябін", "Група крові"), ("Скрябін", "Зозуля"), ("Тартак", "Орбіт")):
        make_music(music / artist / f"{title}.mp3", title=title, artist=artist, album="A")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
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
    return url, app, tmp_path / "stick"


def test_first_sync_choice_apply_progress_report(device, chrome):
    url, app, stick = device
    page = open_page(chrome, url + "/#/devices/stick/plan")
    page.wait_for_function("/#\\/devices\\/stick\\/plan\\/.+/.test(location.hash)")
    page.wait_for_selector('[data-count="add"] strong')
    assert page.locator('[data-count="add"] strong').inner_text() == "3"
    assert page.locator("[data-first-sync]").is_visible()
    assert (
        page.locator('input[name="firstSync"]:checked').count() == 0
    )  # nothing chosen for the user
    shot(page, "plan-1280")
    page.get_by_role("button", name="Apply").click()
    page.wait_for_selector(".toast:has-text('Choose keep, adopt or wipe')")
    assert "/plan/" in page.evaluate("location.hash")  # nothing applied
    page.check('input[name="firstSync"][value="adopt"]')
    page.get_by_role("button", name="Apply").click()
    page.wait_for_function("location.hash.includes('/sync/')")
    page.wait_for_selector("[data-report]", timeout=60_000)
    report = page.locator("[data-report]").inner_text()
    assert page.locator("[data-report]").get_attribute("data-outcome") == "succeeded"
    assert "Verified on the device after flushing: 3 items" in report
    # a folder device has nothing to eject
    assert page.locator('[data-action="eject"]').count() == 0
    shot(page, "report-1280")
    cfg = _api(url, "GET", "/devices/stick")
    assert (
        cfg["config"]["first_sync_choice"] == "adopt"
        and cfg["last_report"]["first_sync"] == "adopt"
    )
    assert len(list(stick.rglob("*.mp3"))) == 3
    page.set_viewport_size({"width": 375, "height": 800})
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_selector('[data-count="keep"] strong')
    assert page.locator('[data-count="keep"] strong').inner_text() == "3"
    assert page.locator("[data-first-sync]").count() == 0  # not a first sync any more
    assert no_horizontal_scroll(page)

    # a removal is never applied by one click
    # no longer selected (a source gone from the library would be an orphan and stay)
    excl = {"id": "x", "name": "No Тартак", "criteria": {"is": {"artist": "Тартак"}}}
    every = {"id": "all", "name": "All", "criteria": {"all": []}}
    link_rules(url, "stick", {"rules": [every], "excludes": [excl]})
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_selector('[data-count="remove"] strong')
    page.wait_for_function(
        "document.querySelector('[data-count=\"remove\"] strong')?.textContent === '1'"
    )
    # below the delete threshold (1 of 3 at 50 %) a removal is plain info, Apply needs no tick
    _api(url, "PATCH", "/devices/stick", json={"delete_threshold_pct": 50})
    page.goto(url + "/#/devices/stick")
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_selector("[data-remove-info]")
    assert "1 file will be removed" in page.locator("[data-remove-info]").inner_text()
    assert page.locator('[data-warn="remove"], [data-apply-bar] [data-plan-ok]').count() == 0
    assert page.locator('[data-apply-bar] [data-action="apply"]').is_enabled()
    shot(page, "plan-remove-below-threshold-1280")
    _api(url, "PATCH", "/devices/stick", json={"delete_threshold_pct": 20})
    page.goto(url + "/#/devices/stick")
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_selector('[data-warn="remove"]')
    # the design: the removal is listed with its reason, and Apply waits for the bar's box
    assert "1 file will be removed" in page.locator('[data-warn="remove"]').inner_text()
    assert page.locator('[data-count="remove"]').get_attribute("aria-pressed") == "true"
    assert page.locator("[data-plan-item] [data-reason]").all_inner_texts() == [
        "Excluded by “No Тартак”"
    ]
    apply = page.locator('[data-apply-bar] [data-action="apply"]')
    assert apply.is_disabled()
    assert "Yes, remove 1 file from Stick" in page.locator("[data-apply-bar]").inner_text()
    assert len(list(stick.rglob("*.mp3"))) == 3
    page.check("[data-plan-ok]")
    assert apply.is_enabled()
    apply.click()
    page.wait_for_selector("[data-report]", timeout=60_000)
    assert len(list(stick.rglob("*.mp3"))) == 2
    assert page.errors == []


def test_force_needs_two_confirmations(device, chrome, monkeypatch):
    url, app, _ = device
    space = {"free": 10**12}

    async def free_space(self):
        return space["free"]

    async def capacity(self):
        return 10**12

    monkeypatch.setattr(FileDeviceMixin, "free_space", free_space)
    monkeypatch.setattr(FileDeviceMixin, "capacity", capacity)
    full = _api(url, "POST", "/devices/stick/plan")
    space["free"] = (
        full["space"]["reserve"] + full["space"]["needed"] // 2 - full["space"]["reserve"] // 2
    )  # about half fits
    page = open_page(chrome, url + "/#/devices/stick/plan")
    page.wait_for_selector("[data-nofit]")
    assert page.get_by_role("button", name="Apply").is_disabled()
    first_url = page.evaluate("location.hash")
    page.get_by_role("button", name="Force: leave items off by priority…").click()
    page.locator(".modal").get_by_role("button", name="Cancel").click()  # 1st confirmation refused
    assert page.evaluate("location.hash") == first_url
    page.get_by_role("button", name="Force: leave items off by priority…").click()
    page.locator(".modal").get_by_role("button", name="Plan with priority trimming").click()
    page.wait_for_selector("[data-excluded]")
    assert page.evaluate("location.hash") != first_url
    page.check('input[name="firstSync"][value="keep"]')
    page.get_by_role("button", name="Apply").click()
    page.wait_for_selector(".modal:has-text('Apply the forced plan?')")  # 2nd confirmation
    page.locator(".modal").get_by_role("button", name="Cancel").click()
    assert "/plan/" in page.evaluate("location.hash") and _api(url, "GET", "/sync-reports") == []
    page.get_by_role("button", name="Apply").click()
    page.locator(".modal").get_by_role("button", name="Apply and leave them off").click()
    page.wait_for_function("location.hash.includes('/sync/')")
    page.wait_for_selector("[data-report]", timeout=60_000)
    report = _api(url, "GET", "/devices/stick")["last_report"]
    assert report["counts"]["add"] < 3
    assert page.errors == []


def test_plan_review_polish(device, chrome, monkeypatch):
    """per-rule rows in priority order (the alphabetically later rule has the higher priority), the left-off
    list is not labelled 'Changes', and a first sync on an empty device does not claim it holds other files."""
    url, app, _ = device
    rule_set = {
        "rules": [
            {"id": "aa", "name": "Aa Скрябін", "criteria": {"contains": {"artist": "Скрябін"}}},
            {"id": "zz", "name": "Zz Тартак", "criteria": {"contains": {"artist": "Тартак"}}},
        ],
        "priority": ["zz", "aa"],
    }
    link_rules(url, "stick", rule_set)
    space = {"free": 10**12}

    async def free_space(self):
        return space["free"]

    async def capacity(self):
        return 10**12

    monkeypatch.setattr(FileDeviceMixin, "free_space", free_space)
    monkeypatch.setattr(FileDeviceMixin, "capacity", capacity)
    full = _api(url, "POST", "/devices/stick/plan")
    space["free"] = full["space"]["needed"] - 1  # not everything fits
    page = open_page(chrome, url + "/#/devices/stick/plan")
    page.wait_for_selector("[data-nofit]")
    assert (
        page.locator("[data-first-text]")
        .inner_text()
        .startswith("The device holds no media files yet")
    )
    rows = page.locator("[data-nofit] tbody tr td:first-child").all_inner_texts()
    assert rows == ["Zz Тартак", "Aa Скрябін"]
    page.get_by_role("button", name="Force: leave items off by priority…").click()
    page.locator(".modal").get_by_role("button", name="Plan with priority trimming").click()
    page.wait_for_selector("[data-excluded]")
    assert page.locator("[data-excluded] tbody tr td:first-child").all_inner_texts() == rows
    # the left-out items are the list's Left out group, each with its reason
    page.locator("[data-excluded]").get_by_role("button", name="Show them").click()
    assert page.locator('[role=tab][data-group="left"]').get_attribute("aria-selected") == "true"
    n = int(page.locator('[data-count="left"] strong').inner_text())
    reasons = page.locator("[data-plan-item] [data-reason]").all_inner_texts()
    assert n > 0 and reasons == ["No space after higher-priority templates"] * n, reasons
    assert page.errors == []


def test_itunesdb_size_shown_and_over_the_limit_needs_its_own_confirmation(
    web_server, chrome, tmp_path, monkeypatch
):
    """the iPod's Overview and Device settings show the iTunesDB size of 63 MiB; a plan over the limit is
    marked and applying it needs a confirmation that names the risk."""
    from syncmusic.devices.iPod.ipod import ITUNESDB, IPod
    from tests.contracts.harness import ipod_tree

    url, app = web_server
    music = tmp_path / "music"
    make_music(music / "Скрябін" / "Зозуля.mp3", title="Зозуля", artist="Скрябін", album="A")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    mount = ipod_tree(tmp_path / "pod")
    _api(
        url,
        "POST",
        "/devices/register",
        json={
            "name": "Pod",
            "mount_path": str(mount),
            "type": "ipod",
            "library_ids": ["music"],
        },
    )
    page = open_page(chrome, url + "/#/devices/pod")
    page.wait_for_selector(".dbsize")
    line = page.locator(".dbsize").inner_text()
    assert "iTunesDB:" in line and "of 63 MiB" in line and "Within the limit" in line
    page.goto(url + "/#/devices/pod/settings")
    page.wait_for_selector("#dbSize .dbsize")

    monkeypatch.setattr(IPod, "db_size_limit", (mount / ITUNESDB).stat().st_size + 100)
    page.goto(url + "/#/devices/pod/plan")
    page.wait_for_selector("#planDb [data-db-level=over]")
    assert "Over the limit" in page.locator("#planDb").inner_text()
    assert "The iTunesDB would exceed its limit" in page.locator('[data-warn="db"]').inner_text()
    page.check('input[name="firstSync"][value="keep"]')
    if page.locator("[data-plan-ok]").count():  # the confirmation reasons (the bar's box)
        page.check("[data-plan-ok]")
    page.get_by_role("button", name="Apply").click()
    page.wait_for_selector(".modal:has-text('The iTunesDB would exceed its limit')")
    assert "Transcoding does not make it smaller" in page.locator(".modal").inner_text()
    page.locator(".modal").get_by_role("button", name="Cancel").click()
    assert "/plan/" in page.evaluate("location.hash") and not app.jobs.jobs()  # nothing applied
    page.get_by_role("button", name="Apply").click()
    page.locator(".modal").get_by_role("button", name="Apply despite the risk").click()
    page.wait_for_function("location.hash.includes('/sync/')")
    page.wait_for_selector("[data-report]", timeout=60_000)
    assert page.locator("[data-report]").get_attribute("data-outcome") == "succeeded"
    assert page.errors == []


def test_player_contents_search_select_album_remove(device, chrome):
    """Player contents lists the device's items, search narrows them, ticking an album ticks its tracks,
    the confirmation names the device and says the originals stay, the items are gone after the job; ▶ plays the
    file on the device."""
    url, app, stick = device
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    page = open_page(chrome, url + "/#/devices/stick/contents")
    page.wait_for_selector('input[name="pcItem"]')
    assert page.locator('input[name="pcItem"]').count() == 3
    assert "Items on the device: 3" in page.locator("[data-pc-count]").inner_text()

    page.fill("#pcQ", "скрябін")
    page.locator('form[data-action="pcSearch"] button').click()
    page.wait_for_selector("[data-pc-count]:has-text('Shown: 2 of 3')")
    page.locator('[aria-label="Play Група крові"]').click()
    src = page.locator("#pcAudio").get_attribute("src")
    assert httpx.get(url + src, timeout=30).status_code == 200

    # the Albums tab keeps the search; ticking the album ticks its tracks
    page.get_by_role("tab", name="Albums").click()
    page.wait_for_selector('[data-pc-group] input[aria-label="Select A"]')
    assert page.locator("#pcQ").input_value() == "скрябін"
    page.locator('input[aria-label="Select A"]').check()
    assert page.locator('input[name="pcItem"]:checked').count() == 2
    page.locator('[data-action="pcRemove"]').click()
    modal = page.locator(".modal")
    modal.wait_for()
    assert "Remove 2 from Stick?" in modal.inner_text()
    assert "Your libraries are not touched" in modal.inner_text()
    modal.get_by_role("button", name="Remove 2").click()
    page.wait_for_function("location.hash.includes('/sync/')")
    page.wait_for_selector("[data-report]", timeout=60_000)
    assert page.locator("[data-report]").get_attribute("data-outcome") == "succeeded"
    page.goto(url + "/#/devices/stick/contents")
    page.wait_for_selector("[data-pc-count]:has-text('Items on the device: 1')")
    assert page.locator('input[name="pcItem"]').count() == 1
    assert len(list(stick.rglob("*.mp3"))) == 1
    assert no_horizontal_scroll(page)
    assert page.errors == []


def test_manual_item_survives_template_change_and_orphans_are_removed(device, chrome):
    """an item added from the library browser stays after the rules change and a sync; an item gone from
    its library stays as an orphan (marked in Player contents) until Device settings → Remove orphans."""
    url, app, stick = device
    music = stick.parent / "music"
    # only the manual selection: no rules, Тартак added from the library browser
    _api(url, "PATCH", "/devices/stick", json={"rule_set": {"template_ids": []}})
    page = open_page(chrome, url + "/#/libraries/music/browse")
    page.wait_for_selector("text=3 items")
    page.fill("#q", "тартак")
    page.locator('form[data-action="search"] button').click()
    page.wait_for_selector("text=1 item")
    page.locator('input[name="sel"]').first.check()
    _add_to(page, "Stick", files=1)
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    # the rules change (everything, then nothing again): the manual item (the own template) is still on the device
    for rules in ([{"id": "all", "name": "All", "criteria": {"all": []}}], []):
        link_rules(url, "stick", {"rules": rules})
        plan = _api(url, "POST", "/devices/stick/plan", json={})
        job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"confirm": True})
        assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    assert [p.name for p in stick.rglob("*.mp3")] == ["Орбіт.mp3"]

    # the source goes: an orphan, kept by the next sync (with `remove_deleted` off)
    _api(url, "PATCH", "/devices/stick", json={"remove_deleted": False})
    (music / "Тартак" / "Орбіт.mp3").unlink()
    _api(url, "POST", "/libraries/music/refresh")
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_function("/#\\/devices\\/stick\\/plan\\/.+/.test(location.hash)")
    page.wait_for_selector("text=1 item on the device is gone from its library (orphan)")
    page.get_by_role("button", name="Apply").click()
    page.wait_for_selector("[data-report]", timeout=60_000)
    assert len(list(stick.rglob("*.mp3"))) == 1
    page.goto(url + "/#/devices/stick/contents")
    page.wait_for_selector("text=Orphan: source gone")

    page.goto(url + "/#/devices/stick/settings")
    card = page.locator("#orphansCard")
    card.wait_for()
    assert "1 orphan on the device" in card.inner_text()
    shot(page, "orphans-1280")
    card.get_by_role("button", name="Remove orphans…").click()
    modal = page.locator(".modal")
    modal.wait_for()
    assert "Remove 1 orphan from Stick?" in modal.inner_text() and "Орбіт" in modal.inner_text()
    modal.get_by_role("button", name="Remove 1").click()
    page.wait_for_function("location.hash.includes('/sync/')")
    page.wait_for_selector("[data-report]", timeout=60_000)
    assert page.locator("[data-report]").get_attribute("data-outcome") == "succeeded"
    assert not list(stick.rglob("*.mp3"))
    page.goto(url + "/#/devices/stick/settings")
    page.wait_for_selector("#orphansCard:has-text('No orphans')")
    page.set_viewport_size({"width": 390, "height": 800})
    assert no_horizontal_scroll(page)
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_transcode_to_fit(web_server, chrome, tmp_path, monkeypatch):
    """a plan that does not fit offers converting (MP3 320 first) with an estimate; another target
    changes the estimate; accepting sets the device's transcode rule and the new plan fits."""
    url, app = web_server
    music = tmp_path / "music"
    for i in (1, 2, 3):
        make_music(
            music / "A" / f"0{i}.flac", seconds=20, title=f"Lossless {i}", artist="A", album="X"
        )
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "m", "type": "folder", "name": "M", "settings": {"root": str(music)}},
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
            "library_ids": ["m"],
            "rule_set": {"template_ids": ["default.m.all"]},
            "processors": [],
        },
    )
    space = {
        "free": 10**9
    }  # 1 GB: the safety margin is its minimum (100 MiB), the MB figures stay readable

    async def free_space(self):
        return space["free"]

    async def capacity(self):
        return 10**9

    monkeypatch.setattr(FileDeviceMixin, "free_space", free_space)
    monkeypatch.setattr(FileDeviceMixin, "capacity", capacity)
    full = _api(url, "POST", "/devices/stick/plan")
    # 70 % of the FLACs' size: too small for them, enough for MP3 320 (the noise FLACs are ~520 kbit/s)
    space["free"] = (
        full["space"]["reserve"] + (full["space"]["needed"] - full["space"]["reserve"]) * 7 // 10
    )
    page = open_page(chrome, url + "/#/devices/stick/plan")
    card = page.locator("[data-fit]")
    card.wait_for()
    assert page.locator("#fitTarget option").first.inner_text() == "MP3 320k"
    first = page.locator("#fitEstimate").inner_text()
    assert "3 files converted" in first and "fits" in first
    page.select_option("#fitTarget", label="MP3 192k")
    page.wait_for_function(
        f"document.getElementById('fitEstimate').innerText !== {first!r} && !document.getElementById('fitEstimate').innerText.startsWith('…')"
    )
    assert page.locator("#fitEstimate").inner_text() != first
    shot(page, "transcode-to-fit-1280")
    card.get_by_role("button", name="Convert and plan again").click()
    page.wait_for_selector(".toast:has-text('converts to MP3 192k')")
    page.wait_for_function(
        "!document.querySelector('[data-nofit]') && !!document.querySelector('[data-action=\"apply\"]:not([disabled])')"
    )
    procs = _api(url, "GET", "/devices/stick")["config"]["processors"]
    assert procs[0]["settings"]["rules"][0] == {
        "from": [],
        "to": "mp3",
        "when": "always",
        "quality": {"bitrate_kbps": 192},
        "above_target_only": True,
    }
    page.set_viewport_size({"width": 390, "height": 800})
    assert no_horizontal_scroll(page)
    assert page.errors == []


def test_plan_of_an_interrupted_sync_says_it_resumes(device, chrome):
    """after a sync stopped at a checkpoint, the plan says how far it got (1280 and 390 px)."""
    from syncmusic.core import manifest as manifest_io
    from syncmusic.core.models import Interruption

    url, app, stick = device
    _api(url, "POST", "/devices/stick/plan", json={})  # nothing applied: write a manifest by hand
    m = manifest_io.read(stick)
    if m is None:
        from syncmusic.core.models import Manifest, ManifestHeader

        uid = _api(url, "GET", "/devices/stick")["config"]["identity"]["manifest_device_id"]
        m = Manifest(header=ManifestHeader(device_id="stick", device_uid=uid))
    m.header.interrupted = Interruption(plan_id="old", done=1, total=3)
    (stick / manifest_io.STATE_DIR).mkdir(exist_ok=True)
    manifest_io.write(stick, m)
    for width, name in ((1280, "plan-resuming-1280"), (390, "plan-resuming-390")):
        page = open_page(chrome, url + "/#/devices/stick/plan", width=width)
        page.wait_for_selector("[data-resuming]")
        text = page.locator("[data-resuming]").inner_text()
        assert "Resuming an interrupted sync" in text and "after 1 of 3 files" in text
        assert (
            page.locator('[data-warn="confirm"]').count() == 0
        )  # our own manifest: nothing to confirm
        assert no_horizontal_scroll(page)
        shot(page, name)
        page.close()


def test_a_failed_item_is_named_in_the_report(device, chrome, monkeypatch):
    """the report lists a failed track as artist – album – title with the step, error and its ref."""
    from syncmusic.devices.flashdrive import FlashDrive

    url, app, stick = device
    real = FlashDrive.put_track

    async def put(self, s, item):
        if item.track.title == "Зозуля":
            raise OSError(36, "File name too long")
        return await real(self, s, item)

    monkeypatch.setattr(FlashDrive, "put_track", put)
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(
        url, "POST", f"/plans/{plan['id']}/apply", json={"confirm": True, "first_sync": "keep"}
    )
    run_on(app, app.jobs.wait(job["id"]))
    for width, name in ((1280, "report-failure-1280"), (390, "report-failure-390")):
        page = open_page(chrome, url + "/#/devices/stick", width=width)
        # the Overview names the first failure; Full report opens the job's report with all of them
        note = page.locator("[data-last-note]")
        note.wait_for()
        assert "put · Скрябін – A – Зозуля" in note.inner_text(), note.inner_text()
        page.locator("[data-full-report]").click()
        page.wait_for_selector("[data-report] [data-failure]")
        text = page.locator("[data-failure]").first.inner_text()
        assert "Скрябін – A – Зозуля" in text and "put" in text and "File name too long" in text, (
            text
        )
        # the sidebar card does not call a partial sync "synced"
        card = page.locator('[data-device-card="stick"]')
        page.wait_for_function(
            "document.querySelector('[data-device-card=\"stick\"]')?.dataset.state === 'partial'"
        )
        assert "partial" in card.text_content() and "synced" not in card.text_content()
        assert no_horizontal_scroll(page)
        shot(page, name)
        page.close()


def test_a_failed_verification_is_not_synced_on_the_card(device, chrome, monkeypatch):
    """a sync job that succeeded with a `failed` report says 'Last sync failed' on the sidebar card and
    'Failed' in Recent jobs, not 'synced' / 'Done'."""
    from syncmusic.devices.flashdrive import FlashDrive

    url, app, _stick = device

    async def missing(self, manifest):
        return {next(iter(manifest.items))} if manifest.items else {"(none)"}

    monkeypatch.setattr(FlashDrive, "missing", missing)
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(
        url, "POST", f"/plans/{plan['id']}/apply", json={"confirm": True, "first_sync": "keep"}
    )
    run_on(app, app.jobs.wait(job["id"]))
    info = _api(url, "GET", f"/jobs/{job['id']}")
    assert info["status"] == "succeeded" and info["result"]["status"] == "failed", info
    for width, name in ((1280, "report-verify-failed-1280"), (390, "report-verify-failed-390")):
        page = open_page(chrome, url + "/#/", width=width)
        page.wait_for_function(
            "document.querySelector('[data-device-card=\"stick\"]')?.dataset.state === 'failed'"
        )
        card = page.locator('[data-device-card="stick"]').text_content()
        assert "Last sync failed" in card and "synced" not in card, card
        row = page.locator(f'[data-recent-job="{job["id"]}"]')
        assert row.get_attribute("data-status") == "failed"
        assert "Failed" in row.text_content() and "Done" not in row.text_content()
        assert no_horizontal_scroll(page)
        shot(page, name)
        page.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_the_plan_says_why_and_downloads_the_list(device, chrome, width):
    """every item of the plan says why (the rule that selects it), and the plan downloads as CSV."""
    url, _app, _stick = device
    page = open_page(chrome, url + "/#/devices/stick/plan", width=width)
    page.wait_for_selector('[data-count="add"][aria-pressed="true"]')
    page.locator('[data-count="add"]').click()
    reasons = page.locator("[data-plan-item] [data-reason]").all_inner_texts()
    # the folder library's "All" default (linked by register's library_ids)
    assert len(reasons) == 3 and all(
        r == "Selected by “Every file of the folder”" for r in reasons
    ), reasons
    # the filter narrows the list as you type
    page.fill("[data-plan-q]", "зозуля")
    assert page.locator("[data-plan-item] .prow-title").all_inner_texts() == ["Зозуля"]
    page.fill("[data-plan-q]", "")
    with page.expect_download() as info:
        page.locator(".plan-head").get_by_role("button", name="Download list").click()
    # every item with its reason, as CSV from the server: the page has only a sample
    rows = list(csv.DictReader(Path(info.value.path()).read_text(encoding="utf-8").splitlines()))
    assert [r["group"] for r in rows].count("add") == 3, rows
    kuk = next(r for r in rows if r["title"] == "Зозуля")
    assert kuk["reason"] == "selected by “Every file of the folder”", kuk
    assert no_horizontal_scroll(page)
    shot(page, f"plan-reasons-{width}")
    assert page.errors == []


def test_gentle_warnings_in_the_plan(device, chrome):
    """With gentle write mode on the plan says the sync is slower; after a full sync
    that dropped off USB without it, the plan names the failing track and offers to turn it on (with Undo)."""
    import json

    url, _app, _stick = device
    failed = {
        "id": "r1",
        "device_id": "stick",
        "status": "failed",
        "kind": "full",
        "suggest_gentle": True,
        "started_at": "2026-10-24T08:00:00Z",
        "finished_at": "2026-10-24T08:01:00Z",
        "failures": [
            {
                "ref": "music/x",
                "error": "I/O error",
                "item": "Океан Ельзи – Модель – Без бою",
                "step": "put",
            }
        ],
    }
    page = open_page(chrome, url + "/#/", width=390)

    def with_report(route):
        resp = route.fetch()
        body = resp.json()
        body["last_report"] = failed
        route.fulfill(response=resp, body=json.dumps(body))

    page.route("**/api/v1/devices/stick", with_report)
    page.goto(url + "/#/devices/stick/plan")
    box = page.locator('[data-warn="usb"]')
    box.wait_for()
    text = box.inner_text()
    assert "dropped off USB" in text and "Put · Океан Ельзи – Модель – Без бою" in text, text
    assert page.locator('[data-warn="gentle"]').count() == 0
    assert no_horizontal_scroll(page)
    shot(page, "plan-usb-390")
    box.get_by_role("button", name="Turn on gentle mode").click()
    page.wait_for_selector(".toast:has-text('Gentle write mode is on for Stick')")
    assert _api(url, "GET", "/devices/stick")["config"]["sync"]["gentle_write"] is True
    assert page.locator('[data-warn="usb"]').count() == 0
    page.locator(".toast").get_by_role("button", name="Undo").click()
    page.wait_for_selector(".toast:has-text('Gentle write mode is off again')")
    assert _api(url, "GET", "/devices/stick")["config"]["sync"]["gentle_write"] is False

    page.unroute("**/api/v1/devices/stick")
    cfg = _api(url, "GET", "/devices/stick")["config"]
    _api(
        url,
        "PATCH",
        "/devices/stick",
        json={
            "sync": {
                **cfg["sync"],
                "gentle_write": True,
                "gentle_batch_files": 1,
                "gentle_pause_s": 30,
            }
        },
    )
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(url + "/#/devices/stick/plan")
    gentle = page.locator('[data-warn="gentle"]')
    gentle.wait_for()
    # 3 files, one per batch, 30 s pauses: about 2 min (no full sync yet to tell the speed)
    assert "batches of 1 with 30 s pauses" in gentle.inner_text()
    assert "add about 2 min" in gentle.inner_text(), gentle.inner_text()
    assert page.locator('[data-warn="usb"]').count() == 0
    shot(page, "plan-gentle-1280")
    # a few seconds of pauses are said in seconds, not rounded up to a minute
    sync = {**cfg["sync"], "gentle_write": True, "gentle_batch_files": 10, "gentle_pause_s": 2}
    _api(url, "PATCH", "/devices/stick", json={"sync": sync})
    page.goto(url + "/#/")
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_selector('[data-warn="gentle"]:has-text("batches of 10")')
    text = page.locator('[data-warn="gentle"]').inner_text()
    assert "add only about 2 s" in text and "min" not in text, text
    assert page.errors == []


def test_apply_bar_has_no_empty_row_on_the_phone(device, chrome):
    """without a confirmation tick the sticky bar is one row of buttons at 390 px."""
    url, app, _stick = device
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    page = open_page(chrome, url + "/#/devices/stick/plan", width=390)
    bar = page.locator("[data-apply-bar]")
    bar.wait_for()
    assert page.locator("[data-apply-bar] [data-plan-ok]").count() == 0
    assert bar.bounding_box()["height"] < 80, bar.bounding_box()
    assert no_horizontal_scroll(page)
    shot(page, "plan-bar-390")
    assert page.errors == []


def _bytes(text: str) -> float:
    """'1.2 MB used' → bytes (the UI's units, dom.js bytes(): steps of 1000)."""
    n, unit = text.split(" ")[:2]
    return float(n) * 1000 ** ["B", "KB", "MB", "GB", "TB"].index(unit)


def test_space_numbers_of_a_removal_plan(device, chrome):
    """a plan that only removes says what it frees (no negative 'Needs'), and the device Overview's 'After
    the planned sync' reads like the plan's After line, not the bytes needed."""
    url, app, _stick = device
    plan = _api(url, "POST", "/devices/stick/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    splin = [
        {"id": "tartak", "name": "Тартак", "criteria": {"any": [{"is": {"artist": "Тартак"}}]}}
    ]
    link_rules(url, "stick", {"rules": splin})  # only Тартак stays: two removals, nothing added
    page = open_page(chrome, url + "/#/devices/stick/plan")
    page.wait_for_selector('[data-count="remove"] strong')
    assert page.locator('[data-count="remove"] strong').inner_text() == "2"
    assert page.locator('[data-count="add"] strong').inner_text() == "0"
    needed = page.locator(".plan-space [data-needed]").inner_text()
    assert needed.startswith("Frees ") and "-" not in needed, needed
    after = page.locator(".plan-space [data-free-after]").inner_text()
    # the removed rows: titles and artist - album, the template by its name
    page.locator('[data-count="remove"]').click()
    rows = page.locator("[data-plan-item]")
    assert sorted(rows.locator(".prow-title").all_inner_texts()) == ["Група крові", "Зозуля"]
    assert rows.locator(".prow-sub").all_inner_texts() == ["Скрябін - A", "Скрябін - A"]
    for reason in rows.locator("[data-reason]").all_inner_texts():
        assert reason.startswith("No template selects it any more (was in “"), reason
        assert "default." not in reason, reason
    # on the phone the chip wraps instead of cutting off the template name
    page.set_viewport_size({"width": 390, "height": 844})
    for chip in rows.locator("[data-reason] > span").all():
        assert chip.evaluate("e => e.scrollWidth <= e.clientWidth"), chip.inner_text()
    assert no_horizontal_scroll(page)
    shot(page, "plan-remove-390")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(url + "/#/devices/stick/overview")
    line = page.locator("[data-planned]")
    line.wait_for()
    # "After the planned sync: 1.2 MB used · 3 GB free" = the plan's "1.2 MB used · 3 GB free · fits"; the free
    # space is measured again and parallel tests share the file system: within 150 MB, one rounding step of "3.0 GB"
    overview = line.inner_text().removeprefix("After the planned sync: ").split(" · ")
    for shown, planned_ in zip(overview, after.rsplit(" · ", 1)[0].split(" · "), strict=True):
        assert shown.split(" ")[2:] == planned_.split(" ")[2:]  # "used" / "free"
        assert abs(_bytes(shown) - _bytes(planned_)) <= 150e6, (shown, planned_)
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_no_cache_suggestion_in_the_plan(device, chrome, width):
    """a plan converting far more than the cache holds offers to sync without the cache."""
    import json

    url, _app, _stick = device
    page = open_page(chrome, url + "/#/", width=width)
    plan_ids = []

    def big(route):
        resp = route.fetch()
        body = resp.json()
        plan_ids.append(body.get("id"))
        body["processed_bytes"] = 120 * 1000**3  # 6× the default 20 GB limit
        route.fulfill(response=resp, body=json.dumps(body))

    page.route("**/api/v1/plans/*", big)
    page.goto(url + "/#/devices/stick/plan")
    box = page.locator('[data-warn="nocache"]')
    box.wait_for()
    assert "120 GB of converted files against a 20.0 GB cache" in box.inner_text()
    assert no_horizontal_scroll(page)
    shot(page, f"plan-nocache-{width}")
    box.get_by_role("button", name="Sync without the cache").click()
    page.wait_for_selector(".toast:has-text('Stick now syncs without the conversion cache')")
    assert _api(url, "GET", "/devices/stick")["config"]["sync"]["no_cache"] is True
    assert page.locator('[data-warn="nocache"]').count() == 0
    # the plan on screen is still valid: Apply needs no new plan
    assert plan_ids[-1] and _api(url, "GET", f"/plans/{plan_ids[-1]}")["id"] == plan_ids[-1]
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_plan_review_suggests_processors_for_the_plan(device, chrome, width):
    """The plan review judges the processors on the plan's items; a better template than the
    device's is offered with its reason, and using it plans again with it."""
    url, _app, _stick = device
    tpl = {"name": "Тільки теги", "processors": [{"type": "retag"}]}
    other = _api(url, "POST", "/processor-templates", json=tpl)["id"]
    _api(url, "PATCH", "/devices/stick", json={"processor_template_id": other})
    page = open_page(chrome, url + "/#/devices/stick/plan", width=width)
    page.wait_for_function("/#\\/devices\\/stick\\/plan\\/.+/.test(location.hash)")
    box = page.locator("[data-plan-suggest]")
    box.wait_for()
    assert "Processors for this plan:" in box.inner_text() and "3 files" in box.inner_text()
    suggested = box.get_attribute("data-plan-suggest")
    assert suggested != other
    assert no_horizontal_scroll(page)
    shot(page, f"plan-suggest-{width}")
    first = page.evaluate("location.hash")
    box.locator('[data-action="planUseSuggested"]').click()
    page.wait_for_function(f"location.hash !== {first!r} && location.hash.includes('/plan/')")
    page.wait_for_selector('[data-count="add"] strong')
    assert _api(url, "GET", "/devices/stick")["config"]["processor_template_id"] == suggested
    assert page.locator("[data-plan-suggest]").count() == 0
    assert page.errors == []


def test_plan_review_offers_no_template_the_device_already_runs(device, chrome):
    """A device with its own chain equal to the suggested template's (defaults spelled out, not linked) gets no
    suggestion: offering the same processing again is noise."""
    url, _app, _stick = device
    page = open_page(chrome, url + "/#/devices/stick/plan", width=1280)
    page.wait_for_function("/#\\/devices\\/stick\\/plan\\/.+/.test(location.hash)")
    page.wait_for_selector('[data-count="add"] strong')
    plan_id = page.evaluate("location.hash.split('/').pop()")
    sg = _api(url, "POST", "/devices/suggest-processor-template", json={"plan_id": plan_id})
    tpl = next(t for t in _api(url, "GET", "/processor-templates") if t["id"] == sg["template_id"])
    own = [{"type": p["type"], "settings": p.get("settings", {})} for p in tpl["processors"]]
    _api(url, "PATCH", "/devices/stick", json={"processor_template_id": None, "processors": own})
    page.goto(url + "/#/devices")
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_function(
        f"location.hash.includes('/plan/') && !location.hash.endsWith({plan_id!r})"
    )
    page.wait_for_selector('[data-count="add"] strong')
    page.wait_for_timeout(500)
    assert page.locator("[data-plan-suggest]").count() == 0
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_a_big_plan_shows_a_sample_and_loads_the_rest(device, chrome, width):
    """the page shows the plan's totals, not the length of its sample; a group whose sample is not
    all of it is listed from GET /plans/{id}/items (also when filtering), and the full list is a download."""
    import json

    url, _app, _stick = device
    page = open_page(chrome, url + "/#/", width=width)
    asked = []

    def big(route):
        resp = route.fetch()
        body = resp.json()
        body["totals"]["add"], body["bytes"]["add"], body["sampled"] = 12_671, 9 * 1000**3, True
        route.fulfill(response=resp, body=json.dumps(body))

    def items(route):
        asked.append(route.request.url)
        resp = route.fetch()
        body = resp.json()
        body["total"] = 12_671
        route.fulfill(response=resp, body=json.dumps(body))

    page.route("**/api/v1/plans/*", big)
    page.route("**/api/v1/plans/*/items?*", items)
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_selector('[data-count="add"][aria-pressed="true"]')
    assert page.locator('[data-count="add"] strong').inner_text() == "12,671"
    assert "9.0 GB" in page.locator('[data-count="add"] .pc-sub').inner_text()
    more = page.locator("[data-plan-more]")
    more.wait_for()
    assert "Showing 3 of 12,671" in more.inner_text(), more.inner_text()
    assert asked and "group=add" in asked[0]
    page.fill("[data-plan-q]", "зозуля")
    page.wait_for_function("document.querySelectorAll('[data-plan-item]').length === 1")
    assert any("q=%D0%B7" in u for u in asked), asked  # the filter ("з…") asks the server
    page.fill("[data-plan-q]", "")
    with page.expect_download() as info:
        more.get_by_role("button", name="Download list").click()
    assert info.value.suggested_filename == "plan-stick.csv"
    assert no_horizontal_scroll(page)
    shot(page, f"plan-sample-{width}")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_plan_metadata_tab(device, chrome, width):
    """a separate Metadata tab with its count, "field: old → new" per item in both directions,
    a conflict says who wins, filters by direction and change, search; the list's other tabs are unchanged."""
    import json

    url, _app, _stick = device
    page = open_page(chrome, url + "/#/", width=width)

    def with_meta(route):
        resp = route.fetch()
        body = resp.json()
        a, b = body["actions"][0], body["actions"][1]
        a["changes"] = [
            {
                "field": "title",
                "old": "Група крові",
                "new": "Група крові (Remastered 2026, дуже довга назва)",
                "direction": "to_device",
                "conflict": "",
            },
            {"field": "rating", "old": "3", "new": "4", "direction": "to_device", "conflict": ""},
        ]
        b["changes"] = [
            {
                "field": "played",
                "old": "unplayed",
                "new": "played",
                "direction": "to_library",
                "conflict": "",
            },
            {
                "field": "position",
                "old": "750",
                "new": "6",
                "direction": "to_library",
                "conflict": "both moved: the newer position wins (the device's when a time is unknown)",
            },
        ]
        body["meta"] = {
            "total": 2,
            "conflicts": 1,
            "actions": [a, b],
            "changes": {
                "to_device:title": 1,
                "to_device:rating": 1,
                "to_library:played": 1,
                "to_library:position": 1,
            },
        }
        route.fulfill(response=resp, body=json.dumps(body))

    page.route("**/api/v1/plans/*", with_meta)
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_selector('[data-count="add"][aria-pressed="true"]')
    tab = page.locator('[role="tab"][data-group="meta"]')
    assert tab.locator("[data-meta-count]").inner_text() == "2"
    tab.click()
    page.wait_for_selector("[data-meta-item]")
    assert page.locator("[data-meta-item]").count() == 2
    text = page.locator("[data-plan-body]").inner_text()
    assert "Rating: ★★★ → ★★★★" in text and "Resume position: 12:30 → 0:06" in text, text
    assert "Played: unplayed → played" in text
    assert page.locator(".mchg.conflict small").inner_text().startswith("both moved")
    shot(page, f"plan-meta-{width}")
    page.select_option("[data-meta-dir]", "to_library")
    assert page.locator("[data-meta-item]").count() == 1
    assert page.locator("[data-meta-item] [data-change]").count() == 2
    page.select_option("[data-meta-dir]", "")
    page.select_option("[data-meta-field]", "rating")
    assert page.locator("[data-meta-item] [data-change]").all_inner_texts()[0].startswith("Rating")
    page.select_option("[data-meta-field]", "")
    page.fill("[data-plan-q]", "зозуля")
    assert page.locator("[data-meta-item]").count() == 1
    assert no_horizontal_scroll(page)
    shot(page, f"plan-meta-filtered-{width}")
    page.locator('[role="tab"][data-group="add"]').click()
    page.fill("[data-plan-q]", "")
    assert page.locator("[data-plan-item]").count() == 3
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_playlist_conflicts_are_chosen_in_the_plan(device, chrome, monkeypatch, width):
    """the plan lists each playlist conflict with a choice; Apply sends the choices (not "ask")."""
    from syncmusic.core.models import PlaylistConflict
    from syncmusic.core.sync import SyncService

    url, app, stick = device
    real = SyncService.plan

    async def plan(self, device_id, **kw):
        pc = await real(self, device_id, **kw)
        pc.plan.playlist_conflicts = [
            PlaylistConflict(name="Мікс для дороги з дуже довгою назвою плейлиста", kind="reorder"),
            PlaylistConflict(name="Road", kind="name_taken"),
        ]
        return pc

    monkeypatch.setattr(SyncService, "plan", plan)
    page = open_page(chrome, url + "/#/devices/stick/plan", width=width)
    page.wait_for_selector("[data-pl-conflicts]")
    text = page.locator("[data-pl-conflicts]").inner_text()
    assert (
        "Reordered on the player and in the library" in text
        and "the library has a playlist" in text
    )
    page.select_option('[data-pl-choice="Road"]', "rename")
    assert no_horizontal_scroll(page)
    shot(page, f"plan-playlist-conflicts-{width}")
    page.check('input[name="firstSync"][value="keep"]')
    with page.expect_request(lambda r: r.url.endswith("/apply")) as req:
        page.locator('[data-apply-bar] [data-action="apply"]').click()
    assert req.value.post_data_json["playlist_choices"] == {"Road": "rename"}
    assert not page.errors


@pytest.mark.parametrize("width", [1280, 390])
def test_plan_lists_playlist_changes(device, chrome, width):
    """the plan's Playlists card - created / updated / removed ones, where each comes from, and
    how many stay as they are."""
    import re

    url, _, _ = device
    changes = [
        {
            "name": "Pacific Drive OST",
            "change": "create",
            "songs": 58,
            "rule": True,
            "smart": False,
        },
        {"name": "Mix", "change": "update", "songs": 1, "rule": False, "smart": True},
        {"name": "Old", "change": "remove", "songs": 0, "rule": False, "smart": False},
        {"name": "Same", "change": "same", "songs": 3, "rule": False, "smart": False},
    ]
    page = open_page(chrome, url, width)

    def plan(route):
        r = route.fetch()
        route.fulfill(response=r, json={**r.json(), "playlist_changes": changes})

    page.route(re.compile(r"/api/v1/plans/[^/?]+$"), plan)
    page.goto(url + "/#/devices/stick/plan")
    card = page.locator("[data-plan-playlists]")
    card.wait_for()
    text = card.inner_text()
    assert "Create “Pacific Drive OST” (58 songs) · made by a rule" in text
    assert "Update “Mix” (1 song) · a smart playlist of the library" in text
    assert "Remove “Old” from the device" in text
    assert "1 playlist stays as it is." in text
    shot(page, f"plan-playlists-{width}")
    assert no_horizontal_scroll(page)


@pytest.mark.parametrize("width", [1280, 390])
def test_plan_list_groups_by_album(device, chrome, width):
    """the rows of one artist - album form a group with its count and size; a single item stays
    a plain row; the group collapses."""
    url, _app, _stick = device
    page = open_page(chrome, url + "/#/devices/stick/plan", width=width)
    page.wait_for_selector('[data-count="add"][aria-pressed="true"]')
    group = page.locator("[data-plan-group]")
    assert group.count() == 1
    summary = group.locator("summary")
    assert "Скрябін - A" in summary.inner_text() and "2 items" in summary.inner_text()
    assert group.locator("[data-plan-item] .prow-title").all_inner_texts() == [
        "Група крові",
        "Зозуля",
    ]
    single = page.locator("[data-plan-body] > [data-plan-item] .prow-title").all_inner_texts()
    assert single == ["Орбіт"]
    shot(page, f"plan-groups-{width}")
    summary.click()
    assert group.get_attribute("open") is None
    assert no_horizontal_scroll(page)
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_plan_list_filters_by_kind_and_rule(device, chrome, width):
    """library / kind / rule selects (from the plan's facets, shown when there is a choice) narrow
    the list; the library select stays hidden with one library."""
    import json

    url, _app, _stick = device
    page = open_page(chrome, url + "/#/", width=width)

    def tagged(route):
        resp = route.fetch()
        body = resp.json()
        for i, a in enumerate(body["actions"]):
            a["rules"] = ["fav"] if i == 0 else ["all"]
        body["facets"]["rule"] = {"fav": 1, "all": len(body["actions"]) - 1}
        body["facets"]["kind"]["podcast"] = 1
        route.fulfill(response=resp, body=json.dumps(body))

    page.route("**/api/v1/plans/*", tagged)
    page.goto(url + "/#/devices/stick/plan")
    page.wait_for_selector('[data-count="add"][aria-pressed="true"]')
    filters = page.locator("[data-list-filters]")
    filters.wait_for()
    assert filters.locator("[data-fl-library]").count() == 0
    assert page.locator("[data-plan-item]").count() == 3
    filters.locator("[data-fl-rule]").select_option("fav")
    page.wait_for_function("document.querySelectorAll('[data-plan-item]').length === 1")
    shot(page, f"plan-filters-{width}")
    page.locator("[data-fl-rule]").select_option("")
    page.locator("[data-fl-kind]").select_option("podcast")
    page.wait_for_function("document.querySelectorAll('[data-plan-item]').length === 0")
    assert "Nothing" in page.locator("[data-plan-body] .plan-empty").inner_text()
    assert no_horizontal_scroll(page)
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_a_group_with_mixed_reasons_says_how_many(device, chrome, width):
    """a group whose items are there for different reasons shows "2 reasons" (the reasons in its
    tooltip); each row keeps its own reason."""
    import json

    url, _app, _stick = device
    page = open_page(chrome, url + "/#/", width=width)

    def mixed(route):
        resp = route.fetch()
        body = resp.json()
        for a in body["actions"]:
            if a["title"] == "Зозуля":
                a["reason"] = "selected by “Favourites”"
        route.fulfill(response=resp, body=json.dumps(body))

    page.route("**/api/v1/plans/*", mixed)
    page.goto(url + "/#/devices/stick/plan")
    chip = page.locator("[data-plan-group] summary [data-reasons]")
    chip.wait_for()
    assert chip.inner_text() == "2 reasons"
    assert "Selected by “Favourites”" in (chip.get_attribute("title") or "")
    reasons = page.locator("[data-plan-group] [data-plan-item] [data-reason]").all_inner_texts()
    assert "Selected by “Favourites”" in reasons and len(set(reasons)) == 2
    shot(page, f"plan-reasons-{width}")
    assert no_horizontal_scroll(page)
    assert page.errors == []
