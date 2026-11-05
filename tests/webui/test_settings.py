"""Settings page: every runtime setting changes and survives a reload, the webhook Test button sends a
notification (to the typed URL or, with the mask, the stored one), paths and About are shown."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from syncmusic import __version__
from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.fixture
def receiver():
    got: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            got.append(json.loads(body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/hook", got
    srv.shutdown()


def _field(page, name: str):
    return page.locator(f"#content #set-{name}")


def _hook_only(page, events: list[str]) -> None:
    for box in page.locator('[data-col="hook"]').all():
        box.set_checked(box.get_attribute("data-ev") in events)


def _save(page, ok: bool = True) -> None:
    page.locator("[data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')" if ok else ".toast.error")


@pytest.mark.parametrize("width", [1280, 390])
def test_change_every_setting_and_reload(web_server, chrome, receiver, tmp_path, width):
    url, _ = web_server
    hook, got = receiver
    page = open_page(chrome, url + "/#/settings", width)
    page.wait_for_selector("#content[data-route='/settings']")
    assert page.locator("[data-version=pygpod]").inner_text() == "0.2.3"
    assert page.locator("[data-version=host]").inner_text()
    assert page.locator("[data-about-logs]").count() == 1
    for path in ("data", "cache", "mnt"):
        assert page.locator("#content td.mono", has_text=path).count() >= 1
    sections = ["sync", "libs", "storage", "notif", "system", "about"]
    assert (
        page.locator("[data-set-section]").evaluate_all("els => els.map(e => e.dataset.setSection)")
        == sections
    )
    shot(page, f"settings-{width}")
    # the side index at 1280, the jump menu on the phone
    if width == 1280:
        assert page.locator(".set-jump").is_hidden()
        page.locator('.set-index [data-sec="about"]').click()
        page.wait_for_function(
            "document.querySelector('.set-index [data-sec=about]').hasAttribute('aria-current')"
        )
        # the section in the middle of the screen is marked, not the one above it
        page.wait_for_selector(
            ".set-layout:not([data-pinned])"
        )  # the jump's smooth scroll and its pin are over
        page.evaluate("document.querySelector('#set-notif').scrollIntoView({block: 'center'})")
        page.wait_for_function(
            "document.querySelector('.set-index [data-sec=notif]').hasAttribute('aria-current')"
        )
    else:
        assert page.locator(".set-index").is_hidden()
        page.locator(".set-jump select").select_option("notif")
    page.wait_for_function(
        "(() => { const r = document.querySelector('#set-notif').getBoundingClientRect(); return r.top < innerHeight; })()"
    )

    # the Test button uses the URL in the field, before it is saved
    _field(page, "webhook_url").fill(hook)
    page.get_by_role("button", name="Send a test notification").click()
    page.wait_for_selector(".toast:has-text('Test notification sent')")
    assert got[-1]["event"] == "test"

    _field(page, "cache_max_gb").fill("7.5")
    _field(page, "concurrency").fill("2")
    _field(page, "max_device_syncs").fill("1")
    _field(page, "write_limit_mb_s").fill("12,5")
    _hook_only(page, ["sync.finished", "sync.failed"])
    page.locator('[data-ev="sync.finished"][data-col="app"]').uncheck()
    _field(page, "quiet").check()
    _field(page, "quiet_start").fill("22:30")
    _field(page, "default_update_sync").uncheck()
    assert _field(page, "default_update_every_min").input_value() == "60"
    _field(page, "default_update_every_min").fill("")
    page.locator("input[name=setRefresh][value=hourly]").check(force=True)
    _field(page, "convert_nice").select_option("19")
    _field(page, "notification_days").select_option("")  # never
    for sel in ("setWhen][value=ask", "setVerify][value=sample", "setEject][value=full",
                "setCacheUse][value=without"):  # fmt: skip
        # the label, not the hidden radio: a force click at phone width can land on the next row
        page.locator(f"input[name={sel}]").evaluate("e => e.closest('label').click()")
    _field(page, "default_no_cache_files").fill("6")
    _save(page)

    page.reload()
    page.wait_for_selector("#content[data-route='/settings']")
    assert _field(page, "cache_max_gb").input_value() == "7.5"
    assert _field(page, "concurrency").input_value() == "2"
    assert _field(page, "max_device_syncs").input_value() == "1"
    assert _field(page, "write_limit_mb_s").input_value() == "12.5"
    assert _field(page, "webhook_url").input_value() == "********"  # a secret: masked
    assert not _field(page, "default_update_sync").is_checked()
    assert page.locator("input[name=setWhen]:checked").get_attribute("value") == "ask"
    assert page.locator("input[name=setVerify]:checked").get_attribute("value") == "sample"
    stored = httpx.get(f"{url}/api/v1/settings").json()
    assert stored["webhook_events"] == ["sync.finished", "sync.failed"]
    assert stored["default_update_sync"] is False and stored["default_verify"] == "sample"
    assert stored["default_eject_after"] == "full"
    assert stored["default_update_every_min"] is None  # empty: only on connect and on changes
    assert stored["default_no_cache"] is True and stored["default_no_cache_files"] == 6
    assert page.locator("input[name=setCacheUse]:checked").get_attribute("value") == "without"
    assert stored["snapshot_refresh"] == "hourly" and stored["convert_nice"] == 19
    assert stored["notification_days"] is None
    assert "sync.finished" not in stored["inbox_events"] and "sync.failed" in stored["inbox_events"]
    assert stored["quiet_hours"] == {"start": "22:30", "end": "07:30"}
    assert not page.locator('[data-ev="sync.finished"][data-col="app"]').is_checked()
    assert page.locator('[data-ev="sync.failed"][data-col="hook"]').is_checked()
    assert _field(page, "quiet_end").is_enabled()
    # a row that never shows in the app has no In app box
    assert page.locator('[data-event="sync.started"] [data-col="app"]').count() == 0

    # with the mask in the field the stored URL is tested
    page.get_by_role("button", name="Send a test notification").click()
    page.wait_for_selector(".toast:has-text('Test notification sent')")
    assert len(got) == 2

    # a bad value is refused by the service and nothing changes
    _field(page, "max_device_syncs").fill("0")
    _save(page, ok=False)
    assert httpx.get(f"{url}/api/v1/settings").json()["max_device_syncs"] == 1
    # not a number: refused before it is sent
    _field(page, "max_device_syncs").fill("two")
    _save(page, ok=False)
    assert page.locator(".toast.error", has_text="Devices syncing at once").count() >= 1

    _field(page, "max_device_syncs").fill("1")

    # cleared: back to no limit
    _field(page, "max_device_syncs").fill("")
    _field(page, "write_limit_mb_s").fill("")
    _field(page, "quiet").uncheck()
    assert _field(page, "quiet_start").is_disabled()
    _save(page)
    stored = httpx.get(f"{url}/api/v1/settings").json()
    assert stored["max_device_syncs"] is None and stored["write_limit_mb_s"] is None
    assert stored["quiet_hours"] is None
    assert no_horizontal_scroll(page)
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
    # Register names the inherited cache choice
    page.goto(url + "/#/devices/register")
    page.wait_for_selector("[data-reg-cache]")
    assert "without the processing cache" in page.locator("[data-reg-cache]").inner_text()
    shot(page, f"register-cache-{width}")


def test_cache_and_space(web_server, chrome, tmp_path):
    """the cache bar, Clear cache after a confirmation, the retention fields and the disk warning."""
    url, app = web_server
    src = tmp_path / "f.bin"
    src.write_bytes(b"x" * 2_000_000)
    run_on(app, app.cache.put("k1", {"f.bin": src}), 10)
    page = open_page(chrome, url + "/#/settings", 1280)
    page.wait_for_selector("[data-cache]")
    assert page.locator("[data-cache-line]").inner_text().startswith("2.0 MB of 20 GB · disk ")
    # above the limit only by what a running sync holds
    app.cache.max_bytes, app.cache._pinned["k1"] = 1_000_000, 1
    page.reload()
    page.wait_for_selector("[data-cache]")
    line = page.locator("[data-cache-line]").inner_text()
    assert line.startswith("2.0 MB of 0 GB (in use by a sync) · disk ")
    app.cache.max_bytes = 20 * 1000**3
    del app.cache._pinned["k1"]
    page.reload()
    page.wait_for_selector("[data-cache]")
    page.get_by_role("button", name="Clear cache").click()
    assert "Frees 2.0 MB" in page.locator(".modal").inner_text()
    page.locator(".modal").get_by_role("button", name="Clear cache").click()
    page.wait_for_selector(".toast:has-text('Cache cleared · 2.0 MB freed.')")
    assert page.locator("[data-cache-line]").inner_text().startswith("0 B of 20 GB")
    assert page.get_by_role("button", name="Clear cache").is_disabled()
    _field(page, "history_days").fill("30")
    _field(page, "log_days").fill("7")
    _field(page, "disk_warn_gb").fill("")
    _save(page)
    stored = httpx.get(f"{url}/api/v1/settings").json()
    assert (stored["history_days"], stored["log_days"], stored["disk_warn_gb"]) == (30, 7, None)
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_system_cache(web_server, chrome):
    """the system cache (thumbnails, previews) shows its size; Clear empties it after a confirmation."""
    url, app = web_server
    shared = app.config.cache_dir / "thumbs" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "a.jpg").write_bytes(b"x" * 3000)
    page = open_page(chrome, url + "/#/settings", 390)
    page.wait_for_selector("[data-sys-cache]")
    assert page.locator("[data-sys-used]").inner_text() == "3.0 KB"
    page.locator("[data-sys-cache]").get_by_role("button", name="Clear").click()
    assert "Frees 3.0 KB" in page.locator(".modal").inner_text()
    page.locator(".modal").get_by_role("button", name="Clear", exact=True).click()
    page.wait_for_selector(".toast:has-text('Cache cleared · 3.0 KB freed.')")
    assert page.locator("[data-sys-used]").inner_text() == "0 B"
    assert page.locator("[data-sys-cache] button").is_disabled()
    assert not shared.exists()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_conversion_ffmpeg(web_server, chrome, tmp_path):
    """the ffmpeg in use with its version and encoders; Check on a wrong path says why and saving it is
    refused."""
    url, _app = web_server
    page = open_page(chrome, url + "/#/settings", 390)
    page.wait_for_selector('[data-ffmpeg-state="ok"], [data-ffmpeg-state="missing"]')
    found = page.locator("[data-ffmpeg]").inner_text()
    assert found.startswith("Found ffmpeg ") and "aac" in found
    # on the phone the icon stays on the first line of its text; the three on-connect choices share a row
    icon_top = page.locator("[data-ffmpeg] .set-ff-head .icon").bounding_box()["y"]
    text_top = page.locator("[data-ffmpeg] .set-ff-head > span").bounding_box()["y"]
    assert abs(icon_top - text_top) <= 4, (icon_top, text_top)
    tops = {
        round(b.bounding_box()["y"]) for b in page.locator('input[name="setWhen"] + span').all()
    }
    assert len(tops) == 1, tops
    _field(page, "ffmpeg_path").fill(str(tmp_path / "ffmpeg"))
    page.get_by_role("button", name="Check").click()
    page.wait_for_selector('[data-ffmpeg-state="error"]')
    assert "does not run" in page.locator("[data-ffmpeg]").inner_text()
    _save(page, ok=False)
    assert httpx.get(f"{url}/api/v1/settings").json()["ffmpeg_path"] is None
    assert no_horizontal_scroll(page)
    page.locator("#set-conv").screenshot(path=str(tmp_path / "conv.png"))


@pytest.mark.parametrize("width", [1280, 390])
def test_maintenance(web_server, chrome, tmp_path, width):
    """log level saved, the diagnostics rows, factory reset after the typed phrase."""
    url, _app = web_server
    music = tmp_path / "music"
    make_music(music / "Скрябін" / "Зозуля.mp3", title="Зозуля", artist="Скрябін", album="Зірка")
    lib = {"id": "music", "type": "folder", "name": "Музика", "settings": {"root": str(music)}}
    assert httpx.post(f"{url}/api/v1/libraries", json=lib, timeout=60).status_code == 201
    page = open_page(chrome, url + "/#/settings", width)
    page.wait_for_selector("[data-diag-id=throttling]", timeout=30000)
    ids = page.locator("[data-diag-id]").evaluate_all("els => els.map(e => e.dataset.diagId)")
    assert ids[:2] == ["versions", "ffmpeg"] and ids[-4:] == [
        "libraries",
        "usb",
        "temperature",
        "throttling",
    ]
    assert "System check" in page.locator("#set-maint").inner_text()
    assert "Музика OK" in page.locator("[data-diag-id=libraries]").inner_text()
    page.locator("#set-maint").scroll_into_view_if_needed()
    shot(page, f"settings-maint-{width}")
    assert no_horizontal_scroll(page)
    _field(page, "log_level").select_option("DEBUG")
    _save(page)
    assert httpx.get(f"{url}/api/v1/settings").json()["log_level"] == "DEBUG"
    if width == 390:
        return
    page.get_by_role("button", name="Factory reset…").click()
    page.wait_for_selector("#typedName")
    page.fill("#typedName", "reset syncmusic")
    page.locator(".modal").get_by_role("button", name="Factory reset").click()
    page.wait_for_function("location.hash === '#/' || location.hash === ''")
    page.wait_for_selector('#content[data-route="/"]')
    assert httpx.get(f"{url}/api/v1/libraries").json() == []
    assert httpx.get(f"{url}/api/v1/settings").json()["log_level"] is None
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_backup_and_restore(web_server, chrome, tmp_path, width):
    """Download backup (passwords only when ticked), Restore… shows what it replaces first, Cancel
    keeps everything, Restore brings the backup's state back."""
    url, _app = web_server
    music = tmp_path / "music"
    make_music(music / "Скрябін" / "Зозуля.mp3", title="Зозуля", artist="Скрябін", album="Зірка")
    lib = {"id": "music", "type": "folder", "name": "Музика", "settings": {"root": str(music)}}
    assert httpx.post(f"{url}/api/v1/libraries", json=lib, timeout=60).status_code == 201
    page = open_page(chrome, url + "/#/settings", width)
    link = page.locator("[data-backup-dl]")
    link.wait_for()
    assert link.get_attribute("href") == "/api/v1/settings/backup"
    page.locator("#set-backup_secrets").check()
    assert link.get_attribute("href") == "/api/v1/settings/backup?secrets=true"
    page.locator("#set-backup_secrets").uncheck()
    with page.expect_download() as dl:
        link.click()
    path = tmp_path / dl.value.suggested_filename
    dl.value.save_as(path)
    assert path.name.startswith("syncmusic-backup-") and path.suffix == ".json"

    httpx.delete(f"{url}/api/v1/libraries/music", timeout=60)
    assert httpx.get(f"{url}/api/v1/libraries").json() == []
    tpl = {"id": "mine", "name": "Мій шаблон - Луперкаль"}
    assert httpx.post(f"{url}/api/v1/templates", json=tpl, timeout=60).status_code < 300
    page.locator("[data-change=restoreFile]").set_input_files(str(path))
    preview = page.locator("[data-restore-preview]")
    preview.wait_for()
    text = preview.inner_text()
    assert (
        "Replaces everything by the backup's 1 library, 0 devices," in text
        and "Passwords are not in the backup" in text
    )
    # what is here now and not in the file is named
    removes = preview.locator("[data-restore-removes]").inner_text()
    assert (
        removes
        == "Removed because they are not in the backup: 1 template (Мій шаблон - Луперкаль)."
    )
    shot(page, f"settings-restore-{width}")
    assert no_horizontal_scroll(page)
    page.get_by_role("button", name="Cancel").click()
    assert page.locator("[data-restore-preview]").count() == 0
    page.locator("[data-change=restoreFile]").set_input_files(str(path))
    page.get_by_role("button", name="Restore", exact=True).click()
    page.wait_for_function(
        "document.readyState === 'complete' && !document.querySelector('[data-restore-preview]')"
    )
    page.wait_for_selector('#content[data-route="/settings"]')
    assert [x["name"] for x in httpx.get(f"{url}/api/v1/libraries").json()] == ["Музика"]
    assert "mine" not in [x["id"] for x in httpx.get(f"{url}/api/v1/templates").json()]
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_db_backups(web_server, chrome, tmp_path, width):
    """interval, keep and folder are saved; Back up now lists a backup with Download and
    Restore…; Restore (after the confirm) brings the database back."""
    url, _app = web_server
    page = open_page(chrome, url + "/#/settings", width)
    _field(page, "db_backup_every").select_option("weekly")
    _field(page, "db_backup_keep").fill("3")
    folder = tmp_path / "dbb"
    _field(page, "db_backup_dir").fill(str(folder))
    _save(page)
    s = httpx.get(f"{url}/api/v1/settings").json()
    assert (s["db_backup_every"], s["db_backup_keep"], s["db_backup_dir"]) == (
        "weekly",
        3,
        str(folder),
    )
    page.get_by_role("button", name="Back up now").click()
    page.wait_for_selector(".toast:has-text('Database backed up')")
    row = page.locator("[data-db-backup]").first
    assert "Manual" in row.inner_text()
    name = row.get_attribute("data-db-backup")
    assert (folder / name).is_file()
    with page.expect_download() as dl:
        row.get_by_role("link", name="Download").click()
    assert dl.value.suggested_filename == name
    shot(page, f"settings-dbbackups-{width}")
    assert no_horizontal_scroll(page)
    (tmp_path / "m").mkdir()
    lib = {"id": "x", "type": "folder", "name": "Нова", "settings": {"root": str(tmp_path / "m")}}
    assert httpx.post(f"{url}/api/v1/libraries", json=lib, timeout=60).status_code == 201
    row.get_by_role("button", name="Restore…").click()
    # the UI reloads once the restore POST is done; the old page is already "complete", so wait for the reload
    with page.expect_navigation(timeout=60_000):
        page.locator(".modal").get_by_role("button", name="Restore", exact=True).click()
    page.wait_for_selector('#content[data-route="/settings"]')
    assert httpx.get(f"{url}/api/v1/libraries").json() == []
    reasons = {b["reason"] for b in httpx.get(f"{url}/api/v1/db-backups").json()}
    assert reasons == {"manual", "before-restore"}
    # the Download/Restore column lines up across rows
    page.wait_for_selector("[data-db-backup] >> nth=1", state="attached")
    lefts = page.evaluate(
        "[...document.querySelectorAll('[data-db-backup] .toolbar')].map((e) => Math.round(e.getBoundingClientRect().left))"
    )
    assert len(lefts) == 2 and len(set(lefts)) == 1, lefts
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_import_export(web_server, chrome, tmp_path, width):
    """Export device settings downloads a file; Import… shows the library mapping and what it adds,
    replaces or skips; Import applies it."""
    url, _app = web_server
    music = tmp_path / "music"
    make_music(music / "Скрябін" / "Зозуля.mp3", title="Зозуля", artist="Скрябін", album="Зірка")
    lib = {"id": "music", "type": "folder", "name": "Музика", "settings": {"root": str(music)}}
    assert httpx.post(f"{url}/api/v1/libraries", json=lib, timeout=60).status_code == 201
    tpl = {"name": "Тільки Скрябін", "library_ids": ["music"], "rules": [], "excludes": []}
    created = httpx.post(f"{url}/api/v1/templates", json=tpl, timeout=60)
    assert created.status_code in (200, 201), created.text
    page = open_page(chrome, url + "/#/settings", width)
    link = page.locator('[data-export="templates"]')
    link.wait_for()
    with page.expect_download() as dl:
        link.click()
    path = tmp_path / dl.value.suggested_filename
    dl.value.save_as(path)
    assert path.name.startswith("syncmusic-templates-")
    data = json.loads(path.read_text())
    data["devices"] = [{"name": "Walkman", "type": "flashdrive", "rule_set": {}}]
    path.write_text(json.dumps(data, ensure_ascii=False))
    page.locator("[data-change=importFile]").set_input_files(str(path))
    preview = page.locator("[data-import-preview]")
    preview.wait_for()
    assert "Музика (folder) → Музика" in preview.inner_text()
    assert page.locator('[data-import-change="replace"]').count() == 1
    assert "register it first" in page.locator('[data-import-change="skip"]').inner_text()
    shot(page, f"settings-import-{width}")
    assert no_horizontal_scroll(page)
    page.get_by_role("button", name="Import", exact=True).click()
    page.wait_for_selector(".toast:has-text('Imported: 1 change.')")
    assert page.locator("[data-import-preview]").count() == 0
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_updates(web_server, chrome, monkeypatch, width):
    """the installed version; with checks on, Check now shows the newest release of the channel, its
    notes and links; the channel setting switches to the pre-releases."""
    from syncmusic.infra import updates
    from tests.unit.test_updates import FakeGitHub

    url, app = web_server
    page = open_page(chrome, url + "/#/settings", width)
    page.wait_for_selector("[data-update-installed]")
    assert page.locator("[data-update-installed]").inner_text() == f"Installed: {__version__}"
    assert page.get_by_role("button", name="Check now").count() == 0  # checks off in the config
    assert page.locator('[data-update-state="off"]').count() == 1
    monkeypatch.setattr(app.config, "update_repo", "example/SyncMusic")
    monkeypatch.setattr(updates, "running_version", lambda: "0.2.0")
    app.update_checker.transport = FakeGitHub()
    page.reload()
    page.get_by_role("button", name="Check now").click()
    page.wait_for_selector('[data-update-state="available"]')
    assert "0.2.1" in page.locator('[data-update-state="available"]').inner_text()
    assert page.locator("[data-update-notes] li").all_inner_texts() == [
        "Covers on the Classic",
        "Rockbox playlists",
    ]
    assert page.get_by_role("link", name="Release notes").count() == 1
    page.locator("#set-update_channel").select_option("beta")
    _save(page)
    page.reload()
    page.get_by_role("button", name="Check now").click()
    page.wait_for_selector('[data-update-state="available"]:has-text("0.2.2-4")')
    page.locator("#set-updates").scroll_into_view_if_needed()
    shot(page, f"settings-updates-{width}")
    assert no_horizontal_scroll(page)
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
