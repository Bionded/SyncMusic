"""Devices: list, registration wizard, every device tab wired to the real API, and destructive actions
that cannot fire from one click (format needs the typed name; a folder device has no Format at all)."""

from __future__ import annotations

import httpx
import pytest

from syncmusic.core.detection import ConnectedDisk
from syncmusic.core.models import DeviceConfig, DeviceIdentity, RuleSet
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=30, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.fixture
def server(web_server, tmp_path):
    url, app = web_server
    music = tmp_path / "music"
    music.mkdir()
    lib = {
        "id": "music",
        "type": "folder",
        "name": "Music folder",
        "settings": {"root": str(music)},
    }
    _api(url, "POST", "/libraries", json=lib)
    return url, app


def test_register_a_folder_and_use_every_tab(server, chrome, tmp_path):
    url, app = server
    target = tmp_path / "stick"
    target.mkdir()
    page = open_page(chrome, url + "/#/devices/register")
    page.wait_for_selector("#regPath:visible")  # no drive connected: the folder option is selected
    page.fill("#regPath", str(target))
    page.fill("#regName", "Travel stick")
    page.locator('.chip:has(input[name="tpl"][value="default.music.all"])').click()
    page.select_option("#regProcTemplate", "builtin-flashdrive")  # nothing preselected
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.startsWith('#/devices/travel-stick/overview')")
    page.goto(url + "/#/devices/travel-stick/settings")  # the danger zone is in Device settings
    page.wait_for_selector(".dangerzone")
    text = page.locator(".dangerzone").inner_text()
    assert "cannot be formatted" in text and page.locator('[data-action="format"]').count() == 0
    cfg = _api(url, "GET", "/devices/travel-stick")["config"]
    assert cfg["mount_path"] == str(target) and cfg["rule_set"]["template_ids"] == [
        "default.music.all"
    ]
    assert cfg["library_ids"] == []  # the template decides the library
    assert _api(url, "GET", "/devices/travel-stick")["libraries"] == ["music"]
    assert page.locator('.tabs a:has-text("Libraries")').count() == 0
    shot(page, "device-overview")

    # Templates: the linked template with what it matches (the live preview)
    page.locator('.tabs a:has-text("Templates")').click()
    page.wait_for_function(
        "() => [...document.querySelectorAll('[data-tpl-sum]')].some((e) => !e.textContent.includes('Counting'))"
    )

    # Processors: move the second step up, save → the order is stored
    page.locator('.tabs a:has-text("Processors")').click()
    page.wait_for_selector("[data-proc]")
    before = [p["type"] for p in _api(url, "GET", "/devices/travel-stick")["config"]["processors"]]
    assert len(before) >= 2
    page.locator('[data-proc="1"] [data-action="procMove"][data-d="-1"]').click()
    page.wait_for_function("document.querySelector('[data-proc=\"0\"] .name') !== null")
    page.locator('[data-action="procSave"]').click()
    page.wait_for_selector(".toast:has-text('Saved')")
    after = [p["type"] for p in _api(url, "GET", "/devices/travel-stick")["config"]["processors"]]
    assert after == [before[1], before[0], *before[2:]]
    shot(page, "device-processors")

    # Sync settings: option cards and the delete threshold, saved by the Save bar
    page.locator('.tabs a:has-text("Settings")').click()
    page.wait_for_selector("[data-sync-form]")
    page.fill("#dsThreshold", "35")
    page.locator('input[name="dsWhen"][value="manual"]').check()
    page.locator("#dsDeleted").uncheck()
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    cfg = _api(url, "GET", "/devices/travel-stick")["config"]
    assert cfg["delete_threshold_pct"] == 35 and cfg["on_connect"] == "manual"
    assert cfg["remove_deleted"] is False

    # Device settings: the type's settings; a folder library takes nothing back, so reverse sync is
    # explained instead of offered; a folder device has no Format
    page.locator('.tabs a:has-text("Settings")').click()
    page.wait_for_selector("#reverseCard")
    assert page.locator("#reverseCard [data-no-reverse]").count() == 1
    assert page.locator("#formatZone").inner_text().count("cannot be formatted") == 1
    assert page.locator('#formatZone [data-action="format"]').count() == 0

    page.locator('.tabs a:has-text("History")').click()
    page.wait_for_selector("text=No syncs yet.")
    page.set_viewport_size({"width": 375, "height": 800})
    for tab in ("overview", "processors", "sync", "settings", "rules"):
        page.goto(f"{url}/#/devices/travel-stick/{tab}")
        page.wait_for_selector("[role=tabpanel] .card, [role=tabpanel] .grid")
        assert no_horizontal_scroll(page), tab
    assert page.errors == []


async def _disk_device(app, calls):
    await app.store.put_device(
        DeviceConfig(
            id="zorvex",
            type="flashdrive",
            name="Zorvex",
            identity=DeviceIdentity(usb_serial="S1"),
            library_ids=["music"],
            rule_set=RuleSet(),
        )
    )
    await app.sync.load()
    disk = BlockDeviceInfo(name="sdz", path="/dev/sdz", serial="S1", tran="usb")
    app.sync.detector.connected["sdz"] = ConnectedDisk(
        disk=disk, device_id="zorvex", data_partition="/dev/sdz1"
    )

    async def fake_format(device_id, confirm, fs=None):
        calls.append((device_id, confirm) if fs is None else (device_id, confirm, fs))
        return await app.sync.config(device_id)

    app.sync.format = fake_format


def test_format_needs_the_typed_name(server, chrome):
    url, app = server
    calls: list = []
    run_on(app, _disk_device(app, calls))
    page = open_page(chrome, url + "/#/devices/zorvex/settings")
    page.wait_for_selector('[data-action="format"]')
    page.locator('[data-action="format"]').click()
    page.wait_for_selector("#typedName")
    page.locator(".modal").get_by_role("button", name="Format").click()  # empty name
    assert page.locator("#typedName").is_visible() and calls == []
    page.fill("#typedName", "zorvex")
    page.locator(".modal").get_by_role("button", name="Format").click()
    assert page.locator("#typedName").is_visible() and calls == []
    page.keyboard.press("Escape")
    assert calls == []
    page.locator('[data-action="format"]').click()
    page.fill("#typedName", "Zorvex")
    page.locator(".modal").get_by_role("button", name="Format").click()
    page.wait_for_selector(".toast:has-text('was formatted')")
    assert calls == [("zorvex", "Zorvex", "vfat")]  # the first file system is preselected
    # the fake disk cannot be mounted: GET /space answers 403 (guard), shown in the storage card
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_format_from_device_settings_with_a_file_system_choice(server, chrome, monkeypatch):
    from syncmusic import facade as facade_mod

    monkeypatch.setattr(
        facade_mod,
        "filesystem_support",
        lambda: {"vfat": None, "exfat": None, "ext4": None, "ntfs": "mkfs.ntfs is not installed"},
    )
    url, app = server
    calls: list = []
    run_on(app, _disk_device(app, calls))
    page = open_page(chrome, url + "/#/devices/zorvex/settings")
    page.wait_for_selector('#formatZone [data-action="format"]')
    assert "NTFS - mkfs.ntfs is not installed" in page.locator("#formatZone").inner_text()
    page.locator('#formatZone [data-action="format"]').click()
    page.wait_for_selector("#typedName")
    radios = page.locator('.modal input[name="formatFs"]')
    assert radios.count() == 4
    assert page.locator('.modal input[value="ntfs"]').is_disabled()
    assert "mkfs.ntfs is not installed" in page.locator(".modal").inner_text()
    page.locator('.modal input[value="exfat"]').check()
    page.fill("#typedName", "Zorvex")
    page.locator(".modal").get_by_role("button", name="Format").click()
    page.wait_for_selector(".toast:has-text('was formatted')")
    assert calls == [("zorvex", "Zorvex", "exfat")]
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_register_a_detected_disk_from_the_list(server, chrome):
    url, app = server

    async def plug():
        disk = BlockDeviceInfo(
            name="sdx",
            path="/dev/sdx",
            vendor="Zorvex",
            model="FlashStick",
            tran="usb",
            serial="S9",
            size=64 * 10**9,
        )
        await app.sync.detector.disk_added(disk)

    run_on(app, plug())
    page = open_page(chrome, url + "/#/devices")
    row = page.locator('[data-unknown="/dev/sdx"]')
    row.wait_for()
    assert "Zorvex FlashStick" in row.inner_text() and "64.0 GB" in row.inner_text()
    shot(page, "devices-unknown-1280")
    # the design: Ignore hides it (also from the dashboard), Undo in the toast brings it back
    row.locator('[data-action="ignoreDisk"]').click()
    page.wait_for_selector('[data-unknown="/dev/sdx"]', state="detached")
    assert page.locator("[data-show-ignored]").inner_text() == "Show ignored (1)"
    page.locator("[data-toast-undo]").click()
    row.wait_for()
    assert httpx.get(url + "/api/v1/devices").json()["ignored"] == []
    page.locator('[data-register="/dev/sdx"]').click()
    page.wait_for_function("location.hash.startsWith('#/devices/register')")
    page.wait_for_selector('input[name="target"][value="/dev/sdx"]:checked')
    assert page.locator("#regPath").is_hidden()
    page.fill("#regName", "Stick")
    page.select_option("#regProcTemplate", "builtin-flashdrive")
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.startsWith('#/devices/stick/overview')")
    cfg = _api(url, "GET", "/devices/stick")["config"]
    assert cfg["mount_path"] is None and cfg["identity"]["usb_serial"] == "S9"
    page.goto(url + "/#/devices/stick/settings")
    page.wait_for_selector('[data-action="format"]')  # a disk device can be formatted
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_ipod_clock_time_zone_and_reverse_sync(server, chrome, tmp_path):
    """the iPod's clock switch and time zone (with suggestions) save; with a Navidrome library attached the
    reverse sync settings are offered."""
    url, _ = server
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "nd",
            "type": "navidrome",
            "name": "Navidrome",
            "settings": {"url": "http://127.0.0.1:1", "username": "u", "password": "p"},
        },
    )
    pod = tmp_path / "pod"
    pod.mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={
            "name": "Pod",  # id "pod" = slug of the name (register refuses an id field)
            "type": "ipod",
            "mount_path": str(pod),
            "library_ids": ["nd"],  # links its "All" default
        },
    )
    page = open_page(chrome, url + "/#/devices/pod/settings")
    page.wait_for_selector('input[id$="d-timezone"]')
    options = page.locator('datalist[id$="d-timezone-l"] option')
    assert options.count() > 300
    page.locator('input[id$="d-time_sync"]').check()
    page.fill('input[id$="d-timezone"]', "Asia/Tokyo")
    assert page.locator('#reverseCard input[id$="r-plays"]').is_visible()
    page.locator('#reverseCard input[id$="r-ratings"]').uncheck()
    # the iPod card and the Save bar
    assert page.locator('#typeCard[data-type-card="ipod"] input[id$="d-timezone"]').count() == 1
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    cfg = _api(url, "GET", "/devices/pod")["config"]
    assert cfg["settings"]["timezone"] == "Asia/Tokyo" and cfg["settings"]["time_sync"] is True
    assert cfg["reverse_sync"]["ratings"] is False and cfg["reverse_sync"]["plays"] is True

    page.wait_for_selector(".savebar[hidden]", state="attached")
    page.locator(".toast").last.wait_for(state="detached")
    page.fill('input[id$="d-timezone"]', "Mars/Olympus")
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast.error:has-text('unknown time zone')")
    assert _api(url, "GET", "/devices/pod")["config"]["settings"]["timezone"] == "Asia/Tokyo"
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_rename_from_device_settings(server, chrome, tmp_path):
    """Rename in Device settings writes the iPod's name (a virtual iPod folder here: no disk label), is not
    a sync, and the new name is shown after a reload."""
    from pygpod import Database

    from tests.contracts.harness import ipod_tree

    url, app = server
    mount = ipod_tree(tmp_path / "ipod", name="Old iPod")
    body = {"name": "Old iPod", "mount_path": str(mount), "type": "ipod", "library_ids": ["music"]}
    dev = _api(url, "POST", "/devices/register", json=body)["id"]
    page = open_page(chrome, url + f"/#/devices/{dev}/settings")
    # the design: the Name card's "Also rename it on the device", saved by the Save bar
    page.wait_for_selector("#nameCard #dsRename:enabled")
    assert "the iPod's name" in page.locator("#nameCard").inner_text()
    page.fill("#dsName", "Плеєр на кухні")
    page.locator("#dsRename").check()
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".modal")
    assert "Rename Old iPod to Плеєр на кухні?" in page.locator(".modal").inner_text()
    assert "disk label is not changed" in page.locator(".modal").inner_text()
    page.locator(".modal").get_by_role("button", name="Rename").click()
    page.wait_for_selector(".toast:has-text('Renamed to Плеєр на кухні')")
    assert Database(str(mount), apply_play_counts=False).master_playlist.name == "Плеєр на кухні"
    assert _api(url, "GET", f"/devices/{dev}")["config"]["name"] == "Плеєр на кухні"
    assert _api(url, "GET", "/jobs") == []  # not a sync
    page.reload()
    page.wait_for_selector("#content[data-route] #nameCard")
    assert page.locator("#dsName").input_value() == "Плеєр на кухні"
    assert "Плеєр на кухні" in page.locator("#content h1").inner_text()
    # without the box only the name in SyncMusic changes
    page.fill("#dsName", "Тільки тут")
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Renamed to Тільки тут')")
    assert _api(url, "GET", f"/devices/{dev}")["config"]["name"] == "Тільки тут"
    assert Database(str(mount), apply_play_counts=False).master_playlist.name == "Плеєр на кухні"
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_rockbox_play_log_toggle_in_device_settings(server, chrome, tmp_path):
    """Device settings show the player's play log state from config.cfg and switch it on/off at once."""
    from tests.contracts.harness import rockbox_tree

    url, app = server
    mount = rockbox_tree(tmp_path / "rb")
    cfg_file = mount / ".rockbox" / "config.cfg"
    cfg_file.write_text("volume: -23\n")
    body = {"name": "Mini", "mount_path": str(mount), "type": "rockbox", "library_ids": ["music"]}
    dev = _api(url, "POST", "/devices/register", json=body)["id"]
    page = open_page(chrome, url + f"/#/devices/{dev}/settings")
    page.wait_for_selector("#playLogCard")
    card = page.locator("#playLogCard")
    assert "On the player now: not set (off)." in card.inner_text()
    assert "SyncMusic keeps it on." in card.inner_text()
    assert page.locator("#content").get_by_label("Turn on the Rockbox play log").count() == 0
    card.get_by_role("button", name="Switch off").click()
    page.wait_for_selector(".toast:has-text('Play log switched off on the player')")
    page.wait_for_selector("#playLogCard[data-state='off']")
    assert cfg_file.read_text() == "volume: -23\nplay log: off\n"
    page.reload()
    page.wait_for_selector("#playLogCard[data-state='off'][data-setting='off']")
    assert "keeps it off" in page.locator("#playLogCard").inner_text()
    page.locator("#playLogCard").get_by_role("button", name="Switch on").click()
    page.wait_for_selector("#playLogCard[data-state='on'][data-setting='on']")
    assert cfg_file.read_text() == "volume: -23\nplay log: on\n"
    assert _api(url, "GET", f"/devices/{dev}")["config"]["settings"]["enable_play_log"] is True
    assert _api(url, "GET", "/jobs") == []  # not a sync
    # saving the other settings keeps the toggle's value
    page.locator("#typeCard input[id$='d-playlist_dir']").fill("Playlists2")
    page.locator(".savebar [data-savebar=save]").click()
    page.wait_for_selector(".toast:has-text('Settings saved')")
    assert _api(url, "GET", f"/devices/{dev}")["config"]["settings"]["enable_play_log"] is True
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


@pytest.mark.parametrize("width", [1280, 390])
def test_devices_list_v2(server, chrome, tmp_path, width):
    """List: a registered device's row - badge, used space, last sync, one action (Sync) -
    'Add FTP device' opens the register screen with the server choice made; the Gentle chip."""
    url, app = server
    (tmp_path / "stick").mkdir()
    r = httpx.post(
        url + "/api/v1/devices/register",
        json={
            "name": "Флешка в машині (SanDisk Ultra Fit)",
            "mount_path": str(tmp_path / "stick"),
            "type": "flashdrive",
        },
    )
    r.raise_for_status()
    did = r.json()["id"]
    sync = r.json()["sync"]
    httpx.patch(
        url + f"/api/v1/devices/{did}", json={"sync": {**sync, "gentle_write": True}}
    ).raise_for_status()
    page = open_page(chrome, url + "/#/devices", width=width)
    row = page.locator(f'[data-device-row="{did}"]')
    row.locator("[data-dev-line]:has-text('free')").wait_for()
    assert row.locator("[data-gentle]").count() == 1
    assert (
        row.locator("[data-dev-action]").inner_text() == "Templates"
    )  # no templates yet: choose them first
    assert "Never" in row.locator(".dev-last").inner_text()
    shot(page, f"devices-list-{width}")
    assert no_horizontal_scroll(page)
    page.locator('a[href="#/devices/register?target=network"]').first.click()
    page.wait_for_selector('input[name="target"][value="network:"]:checked')
    assert page.locator("#regNetwork").is_visible()
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_hiby_is_not_beta_android_is(server, chrome, tmp_path, width):
    """HiBy SonixOS left beta - no badge in the list or on the Overview; the beta badge
    stays on Android MTP in the register screen's network choice when this host offers it."""
    url, _ = server
    for name, typ in (("R1", "hiby"), ("Stick", "flashdrive")):
        (tmp_path / name).mkdir()
        _api(
            url,
            "POST",
            "/devices/register",
            json={"name": name, "mount_path": str(tmp_path / name), "type": typ},
        )
    page = open_page(chrome, url + "/#/devices", width=width)
    page.locator('[data-device-row="r1"]').wait_for()
    assert page.locator("[data-beta]").count() == 0
    page.goto(url + "/#/devices/r1")
    page.locator("[data-model-line]").wait_for()
    assert page.locator("[data-model-line] [data-beta]").count() == 0
    shot(page, f"hiby-no-beta-{width}")
    assert no_horizontal_scroll(page)
    page.goto(url + "/#/devices/register?target=network")
    page.locator("label.reg-type").first.wait_for()
    android = page.locator('label.reg-type:has(input[value="android"])')
    if android.count():
        assert android.locator("[data-beta]").count() == 1
        assert "beta" in (android.locator("[data-beta]").get_attribute("title") or "")
    assert page.locator("label.reg-type [data-beta]").count() == android.count()
    assert page.errors == []


def test_empty_devices_list_says_it_once(server, chrome):
    """the empty state's text does not repeat its title 'No devices yet'."""
    url, _ = server
    page = open_page(chrome, url + "/#/devices", width=390)
    empty = page.locator("[data-devs-empty]")
    empty.wait_for()
    assert empty.locator("b").inner_text() == "No devices yet"
    assert "No devices yet" not in empty.locator("p").inner_text()


@pytest.mark.parametrize("width", [1280, 390])
def test_danger_zone_per_type(server, chrome, tmp_path, width):
    """a Rockbox player has Wipe (typed name → a wipe job; /.rockbox stays), a folder
    device only Delete - which also asks for the typed name."""
    from tests.contracts.harness import rockbox_tree

    url, _app = server
    mount = rockbox_tree(tmp_path / "mini")
    (mount / "Music").mkdir()
    (mount / "Music" / "x.mp3").write_bytes(b"x")
    body = {"name": "Міні", "type": "rockbox", "mount_path": str(mount), "library_ids": ["music"]}
    dev = _api(url, "POST", "/devices/register", json=body)["id"]
    page = open_page(chrome, url + f"/#/devices/{dev}/settings", width=width)
    zone = page.locator("#formatZone")
    zone.wait_for()
    assert [x.get_attribute("data-dz") for x in zone.locator("[data-dz]").all()] == [
        "wipe",
        "delete",
    ]
    assert "Rockbox itself and its settings stay" in zone.inner_text()
    assert no_horizontal_scroll(page)
    shot(page, f"danger-rockbox-{width}")
    if width == 390:
        return
    zone.get_by_role("button", name="Wipe…").click()
    page.wait_for_selector("#typedName")
    assert "Wipe Міні?" in page.locator(".modal").inner_text()
    page.fill("#typedName", "Міні")
    page.locator(".modal").get_by_role("button", name="Wipe").click()
    # the device's job view, not the generic job page
    page.wait_for_function(f"decodeURIComponent(location.hash).startsWith('#/devices/{dev}/sync/')")
    report = page.locator('[data-report][data-outcome="succeeded"]')
    report.wait_for()
    text = page.locator("#content").inner_text()
    assert "Wipe · Міні" in text and "Міні is wiped" in text, text
    assert "1 file removed; Rockbox and its settings are kept" in text, text
    assert page.locator("[data-counts] .count-tile b").all_inner_texts() == ["−1"]
    assert page.locator(".job-line .status").inner_text() == "Wiped"
    assert "1 files removed, /.rockbox kept" in page.locator("[data-log]").inner_text()
    assert dev not in text.replace(f"#/devices/{dev}", "")  # no raw id slug
    assert page.get_by_role("link", name="Open device").count() == 1
    assert (mount / ".rockbox").is_dir() and not (mount / "Music").exists()
    shot(page, "wiped-1280")
    # the device pages name the wipe, not an old "synced"
    page.goto(url + f"/#/devices/{dev}")
    page.reload()
    page.locator('[data-last-sync="wiped"]').wait_for()
    assert (
        "1 file removed. The next sync is a first sync."
        in page.locator("[data-erased]").inner_text()
    )
    card = page.locator(f'[data-device-card="{dev}"]')
    card.and_(page.locator('[data-state="erased"]')).wait_for()
    assert "Wiped - not synced yet" in card.inner_text()
    shot(page, "wiped-overview-1280")

    (tmp_path / "stick").mkdir()
    stick = _api(
        url,
        "POST",
        "/devices/register",
        json={"name": "Stick", "type": "flashdrive", "mount_path": str(tmp_path / "stick")},
    )["id"]
    page.goto(url + f"/#/devices/{stick}/settings")
    page.wait_for_selector("#formatZone [data-dz]")
    assert [x.get_attribute("data-dz") for x in zone.locator("[data-dz]").all()] == ["delete"]
    zone.get_by_role("button", name="Delete device…").click()
    page.wait_for_selector("#typedName")
    page.fill("#typedName", "Stick")
    page.locator(".modal").get_by_role("button", name="Delete device").click()
    page.wait_for_function("location.hash === '#/devices'")
    assert all(d["id"] != stick for d in _api(url, "GET", "/devices")["registered"])
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_unknown_device_type_page_offers_delete(server, chrome, tmp_path):
    url, app = server
    target = tmp_path / "old"
    target.mkdir()
    cfg = DeviceConfig(id="old-player", type="usb_drive", name="Old player", mount_path=str(target))
    run_on(app, app.store.put_device(cfg))
    page = open_page(chrome, url + "/#/devices/old-player/overview")
    page.wait_for_selector("[data-unknown-type]")
    assert "usb_drive" in page.locator("[data-unknown-type]").inner_text()
    shot(page, "device-unknown-type")
    page.locator('[data-action="remove"]').click()
    page.fill(".modal input", "Old player")
    page.locator('.modal [data-ready="true"], .modal button.danger').last.click()
    page.wait_for_function(
        "location.hash.startsWith('#/devices') && !location.hash.includes('old-player')"
    )
    assert run_on(app, app.store.device("old-player")) is None
