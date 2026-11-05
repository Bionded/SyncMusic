"""The web UI on the real allowlisted devices, clicked in a browser (Playwright, Chromium) against one live server:
everything a user does with one device, once per flash drive and per iPod Classic of tests/hardware.toml (each test
needs its one device only), and the library checks that need no device.

Flash drive: formatted from Device settings with every file system the host supports, a song sent from the library
browser, Player contents removal, an orphan removed. iPod Classic: its data partition formatted (partition table
unchanged), 6 songs from the local Navidrome (test media, read-only), pygpod re-reads the database, an update.
Each device is ejected from the UI (powered off), its USB port enabled again and the device detected again; it is
left unmounted and fsck-clean. After "Send selected to device" and after a Player contents removal the Rules tab's
Test rules and Save answer 200 and keep the rule set.
"""

from __future__ import annotations

import faulthandler
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from syncmusic.core.rules.templates import own_template_id
from syncmusic.infra.blockdev.identify import scan_block_devices
from syncmusic.infra.config import load_config
from tests.conftest import HARDWARE_TOML
from tests.hardware.test_classic_acceptance import (
    _check_db,
    _data_listing,
    _partition_table,
)
from tests.integration.test_navidrome_local import ENV
from tests.media import make_music
from tests.webui.conftest import chrome, open_page, run_on, settle, web_server  # noqa: F401 - fixtures

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]

SIX = {
    "rules": [
        {
            "id": "six",
            "name": "Six songs",
            "criteria": {"is": {"kind": "music"}},
            "limit": 6,
            "sort": "title",
        }
    ]
}
ALL = {"rules": [{"id": "all", "name": "All", "criteria": {"all": []}}]}


@pytest.fixture
def config(tmp_path):
    """Caches and media on /var/tmp: /tmp is a small tmpfs."""
    work = Path(tempfile.mkdtemp(prefix="sd-hw-ui-", dir="/var/tmp"))
    yield load_config(
        env={},
        overrides={
            "data_dir": tmp_path / "data",
            "cache_dir": work / "cache",
            "mount_root": tmp_path / "mnt",
            "device_allowlist": HARDWARE_TOML,
            "mode": "daemon",
            "allow_directory_devices": True,
            "detect_devices": False,
        },
    )
    shutil.rmtree(work, ignore_errors=True)


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=120, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _linked(url, name, rules, library_id):
    """A device has templates only: the rules as a template of the library, linked."""
    body = {"name": f"{name} rules", "library_ids": [library_id], **rules}
    return {"template_ids": [_api(url, "POST", "/templates", json=body)["id"]]}


def _disks() -> dict:
    return {d.serial: d for d in scan_block_devices() if d.tran == "usb"}


def _usb_port(disk_path: str, serial: str) -> Path:
    """The hub port (…/usbN-portM) of the disk's USB device, checked against the allowlisted serial."""
    p = (Path("/sys/class/block") / Path(disk_path).name).resolve()
    while p != Path("/") and _sysfs_serial(p) != serial:
        p = p.parent
    assert p != Path("/"), f"no USB device with the allowlisted serial above {disk_path}"
    return (p / "port").resolve()


def _sysfs_serial(p: Path) -> str | None:
    """A sysfs node's `serial`; None where there is none or it cannot be read (some hubs and controllers: ENXIO)."""
    try:
        return (p / "serial").read_text().strip()
    except OSError:
        return None


def _set_port(port: Path, disable: int) -> None:
    subprocess.run(
        ["sudo", "-n", "tee", str(port / "disable")],
        input=f"{disable}\n",
        text=True,
        capture_output=True,
        check=True,
    )


def _wait(cond, timeout=60.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def _fsck(part: str) -> None:
    r = subprocess.run(["sudo", "-n", "fsck.fat", "-n", "-v", part], capture_output=True, text=True)
    assert r.returncode == 0, f"fsck {part}: rc {r.returncode}\n{r.stdout}{r.stderr}"


def _fstype(part: str) -> str:
    out = subprocess.run(["lsblk", "-no", "FSTYPE", part], capture_output=True, text=True)
    return out.stdout.strip()


def _wait_jobs(url, device_id, since: set[str], timeout=900.0) -> list[dict]:
    """The device's jobs that are not in `since`, once all of them are finished."""

    def mine():
        return [
            j
            for j in _api(url, "GET", "/jobs")
            if j.get("device_id") == device_id and j["id"] not in since
        ]

    _wait(
        lambda: mine() and all(j["status"] not in ("queued", "running") for j in mine()),
        timeout,
        "jobs",
    )
    return mine()


def _job_ids(url) -> set[str]:
    return {j["id"] for j in _api(url, "GET", "/jobs")}


def _goto(page, url, route: str) -> None:
    """Open a screen and wait until it is rendered (a same-view navigation keeps the old DOM for a moment)."""
    page.goto(f"{url}/#{route}")
    page.wait_for_function(
        f"document.getElementById('content').dataset.route === {route!r}", timeout=120_000
    )


def _confirm(page, button: str) -> None:
    try:
        page.locator(".modal").get_by_role("button", name=button).click(timeout=60_000)
    except Exception as e:
        toasts = page.locator(".toast").all_inner_texts()
        page.screenshot(path="/var/tmp/sd-hw-ui-fail.png", full_page=True)
        raise AssertionError(f"no {button!r} dialog; toasts {toasts}; errors {page.errors}") from e
    page.wait_for_selector(".modal", state="detached")


def _register(page, url, disk, name: str, library: str) -> str:
    _goto(page, url, "/devices")
    page.locator(
        f'[data-register="{disk.path}"]'
    ).click()  # the v2 list: unknown drives with Register
    page.wait_for_selector(f'input[name="target"][value="{disk.path}"]:checked')
    page.fill("#regName", name)
    # the library's first default template decides what goes on it (no library step); chips
    page.locator(f'.chip:has(input[name="tpl"][value^="default.{library}."])').first.click()
    # nothing preselected: the device type's built-in processor template (also checks procMode)
    page.select_option(
        "#regProcTemplate", f"builtin-{page.locator('[name=regType]:checked').input_value()}"
    )
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("/#\\/devices\\/[^/]+\\/overview/.test(location.hash)")
    return page.evaluate("location.hash.split('/')[2]")


def _add_to_device(page, device_id: str, name: str) -> None:
    """The browser's "Add to device…" onto the device itself (its linked "<name> rules" template matches the name
    too, so the target is picked by id)."""
    page.get_by_role("button", name="Add to device…").click()
    modal = page.locator(".modal")
    modal.locator(f'.target[data-target="device:{device_id}"]').click()
    modal.get_by_role("button", name="Add rule").click()
    page.locator(f".toast:has-text('Rule added to {name}'):has-text('1 file')").last.wait_for()


def _format(page, url, device_id: str, name: str, fs: str | None) -> None:
    _goto(page, url, f"/devices/{device_id}/settings")
    page.locator('#formatZone [data-action="format"]').click()
    page.wait_for_selector("#typedName")
    if fs is not None:
        page.locator(f'.modal input[name="formatFs"][value="{fs}"]').check()
    page.fill("#typedName", name)
    # wait for this format's answer (an earlier "was formatted" toast may still be on screen)
    with page.expect_response(lambda r: r.url.endswith("/format"), timeout=300_000) as resp:
        page.locator(".modal").get_by_role("button", name="Format").click()
    assert resp.value.ok, resp.value.text()


def _apply_from_plan(page, url, device_id: str) -> None:
    _open_plan(page, url, device_id)
    _apply(page, device_id)


def _open_plan(page, url, device_id: str) -> None:
    page.goto(f"{url}/#/devices/{device_id}/plan")  # redirects to /plan/<id>
    page.wait_for_function(
        f"/#\\/devices\\/{device_id}\\/plan\\/.+/.test(location.hash)", timeout=300_000
    )
    try:
        page.wait_for_selector('[data-action="apply"]:enabled', timeout=300_000)
    except Exception:
        _dump_stall(page)
        raise


def _apply(page, device_id: str) -> None:
    first = page.locator('input[name="firstSync"][value="keep"]')
    if first.count():
        first.check()
    page.locator('[data-action="apply"]').click()
    settle(page, quiet_ms=200)  # a confirmation dialog, if the plan asks for one
    if page.locator(".modal").count():
        page.locator(".modal .modalfoot button").last.click()
    # the plan apply can take long under -n 2 load
    try:
        page.wait_for_function(
            f"/#\\/devices\\/{device_id}\\/sync\\//.test(location.hash)", timeout=300_000
        )
    except Exception:
        _dump_stall(page)
        raise


def _dump_stall(page) -> None:
    """What blocked an Apply: every thread's stack (the app runs in this process), the page's state."""
    print(f"stall at {page.url}; modal={page.locator('.modal').count()}", file=sys.stderr)
    print(page.locator("#content").inner_text()[:2000], file=sys.stderr)
    for toast in page.locator(".toast").all_inner_texts():  # a refused POST (409) shows here
        print(f"toast: {toast}", file=sys.stderr)
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)


@contextmanager
def _smart_playlist() -> Iterator[str]:
    """A smart playlist on the local Navidrome for the run (Navidrome bumps its `changed` on every read)."""
    base = ENV["NAVIDROME_URL"].rstrip("/")
    with httpx.Client(base_url=base, timeout=30) as http:
        for _ in range(8):  # Navidrome rate-limits logins
            r = http.post(
                "/auth/login",
                json={"username": ENV["NAVIDROME_USER"], "password": ENV["NAVIDROME_PASSWORD"]},
            )
            if r.status_code != 429:
                break
            time.sleep(5)
        r.raise_for_status()
        auth = {"x-nd-authorization": f"Bearer {r.json()['token']}"}
        body = {
            "name": "sd-test smart",
            "rules": {"all": [{"gt": {"playcount": -1}}], "limit": 5},
        }
        r = http.post("/api/playlist", json=body, headers=auth)
        r.raise_for_status()
        pid = r.json()["id"]
        try:
            yield pid
        finally:
            http.delete(f"/api/playlist/{pid}", headers=auth)


def _rules_round_trip(page, url, device_id: str) -> None:
    """Open the device's own template (its picks and removals) in the template editor, Test and Save
        without touching anything: both 200, rules unchanged (the builder rewrote hand-picked id lists into a title match
    )."""
    tid = own_template_id(device_id)
    before = _api(url, "GET", f"/templates/{quote(tid, safe='')}")
    _goto(page, url, f"/templates/{quote(tid, safe='')}")
    with page.expect_response(
        lambda r: "/rules/test" in r.url and r.request.method == "POST"
    ) as test:
        page.locator('#content [data-action="test"]').click()
    assert test.value.status == 200, test.value.text()
    with page.expect_response(
        lambda r: r.request.method == "PATCH" and "/templates/" in r.url
    ) as save:
        page.locator('#content [data-action="save"]').click()
    assert save.value.status == 200, save.value.text()
    after = _api(url, "GET", f"/templates/{quote(tid, safe='')}")
    assert (after["rules"], after["excludes"]) == (before["rules"], before["excludes"])


@pytest.fixture
def setup(web_server, tmp_path):  # noqa: F811
    """A live server with a folder library (synthetic songs in /var/tmp) and the local Navidrome."""
    url, app = web_server
    music = Path(tempfile.mkdtemp(prefix="sd-hw-ui-music-", dir="/var/tmp"))
    for artist, title in (("Скрябін", "Група крові"), ("Скрябін", "Зозуля"), ("Тартак", "Орбіт")):
        make_music(
            music / artist / f"{title}.mp3", title=title, artist=artist, album=f"{artist} album"
        )
    make_music(music / "Gone" / "Зниклий.mp3", title="Зниклий", artist="Gone", album="Gone")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "folder", "type": "folder", "name": "Folder", "settings": {"root": str(music)}},
    )
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "nd",
            "type": "navidrome",
            "name": "Navidrome",
            "settings": {
                "url": ENV["NAVIDROME_URL"],
                "username": ENV["NAVIDROME_USER"],
                "password": ENV["NAVIDROME_PASSWORD"],
            },
        },
    )
    try:
        yield url, app, music
    finally:
        shutil.rmtree(music, ignore_errors=True)


def _eject_and_back(page, url, app, disk, device_id: str, name: str) -> object:
    """Eject the device from the UI (powered off), enable its USB port again and see it detected again; returns
    the disk as the scan sees it after."""
    port = _usb_port(disk.path, disk.serial)
    try:
        _goto(page, url, f"/devices/{device_id}/overview")
        page.locator('[data-action="eject"]').click()
        _confirm(page, "Eject")
        page.wait_for_selector(f".toast:has-text('{name} can be unplugged now')", timeout=120_000)
        _wait(lambda: disk.serial not in _disks(), 60, "the disk gone after eject")
        run_on(
            app, app.sync.on_disk_removed(disk.name)
        )  # what the udev monitor reports (off in tests)
        assert not _connected(url, device_id)
    finally:
        # a powered-off disk only comes back when its port is disabled and enabled again (also after a failure)
        _set_port(port, 1)
        _set_port(port, 0)
    _wait(lambda: (d := _disks().get(disk.serial)) is not None and d.children, 120, "the disk back")
    back = _disks()[disk.serial]
    run_on(app, app._disk_added(back))
    assert _connected(url, device_id)
    return back


def _connected(url, device_id: str) -> bool:
    return next(d for d in _api(url, "GET", "/devices")["registered"] if d["id"] == device_id)[
        "connected"
    ]


def _jobs_settled(url, idle_s: float = 5.0, timeout: float = 300.0) -> None:
    """No job queued or running for `idle_s`: a library refresh's automatic update may start only after the refresh
    job ends, and an Apply clicked while it holds the device starts no sync."""
    idle_since = [time.monotonic()]

    def settled() -> bool:
        if any(j["status"] in ("queued", "running") for j in _api(url, "GET", "/jobs")):
            idle_since[0] = time.monotonic()
        return time.monotonic() - idle_since[0] > idle_s

    _wait(settled, timeout, "jobs settle")


def _unmount_and_fsck(page, app, device_id: str, disk) -> None:
    # fsck needs it unmounted: leave the UI first, or a sidebar /space request still in flight (a statfs on a big
    # FAT takes seconds) mounts the device again behind the test's back and fsck sees the dirty bit
    page.goto("about:blank")
    time.sleep(15)
    run_on(app, app.sync._unmount(device_id), 60)
    part = disk.children[0].path
    assert not subprocess.run(["findmnt", "-S", part], capture_output=True).stdout, part
    _fsck(part)


def test_ui_library_checks(setup, chrome):  # noqa: F811
    """No device: Test connection checks the edited values, not the saved ones (nothing is stored); with a smart
    playlist on the server the library snapshot is loaded once, then served from the cache."""
    url, _, _ = setup
    page = open_page(chrome, url)
    _goto(page, url, "/libraries/nd")
    page.fill('input[id$="url"]', "http://127.0.0.1:1")
    page.get_by_role("button", name="Test connection").click()
    page.wait_for_function("!document.querySelector('#libCheck').innerText.includes('…')")
    settle(page)  # no late answer turns it into OK
    assert page.locator("#libCheck .status.ok").count() == 0, page.inner_text("#libCheck")
    saved = next(x for x in _api(url, "GET", "/libraries") if x["id"] == "nd")
    assert saved["settings"]["url"] == ENV["NAVIDROME_URL"]
    with _smart_playlist():
        browse = "/libraries/nd/items?query=&page=0&page_size=50&kind=music"
        _api(url, "GET", browse)
        started = time.monotonic()
        _api(url, "GET", browse)
        assert time.monotonic() - started < 1.0, "the second Browse request reloaded the library"
        _goto(page, url, "/libraries/nd/browse")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []


def test_ui_flash_drive(setup, chrome, flash_disk):  # noqa: F811
    """A flash drive: registered from the detected list, formatted from Device settings with every file system the
    host supports (ending on FAT32), one song sent from the library browser, synced, a song removed through Player
    contents, the sent song's source goes → orphan → Remove orphans, ejected from the UI and detected again."""
    url, app, music = setup
    before = _data_listing()
    run_on(app, app._disk_added(flash_disk))
    page = open_page(chrome, url)
    stick = _register(page, url, flash_disk, "UI Stick", "folder")
    _api(
        url,
        "PATCH",
        f"/devices/{stick}",
        json={"rule_set": _linked(url, "UI Stick", ALL, "folder"), "on_connect": "manual"},
    )
    supported = [
        f["fs"]
        for f in next(t for t in _api(url, "GET", "/device-types") if t["type"] == "flashdrive")[
            "format_filesystems"
        ]
        if f["available"]
    ]
    assert "vfat" in supported
    for fs in [*[x for x in supported if x != "vfat"], "vfat"]:
        _format(page, url, stick, "UI Stick", fs)
        part = run_on(app, _data_partition(app, stick))
        assert _fstype(part) == fs, fs

    # one song from the library browser goes to the stick as a manual item
    _goto(page, url, "/libraries/folder/browse")
    page.fill("#q", "Зниклий")
    page.locator('form[data-action="search"] button').click()
    page.wait_for_selector("text=1 item")
    page.locator('input[name="sel"]').first.check()
    _add_to_device(page, stick, "UI Stick")  # the v2 browser adds a rule
    _rules_round_trip(page, url, stick)  # the Manual selection survives Test rules and Save
    _apply_from_plan(page, url, stick)
    page.wait_for_selector("[data-report]", timeout=900_000)
    assert "Verified on the device after flushing" in page.locator("#content").inner_text()

    # Player contents: remove Зозуля
    _goto(page, url, f"/devices/{stick}/contents")
    page.locator('input[name="pcItem"][aria-label="Зозуля"]').check()
    page.locator('[data-action="pcRemove"]').click()
    _confirm(page, "Remove 1")
    page.wait_for_selector("[data-report]", timeout=300_000)
    names = {i["title"] for i in _api(url, "GET", f"/devices/{stick}/contents")}
    assert "Зозуля" not in names and {"Група крові", "Орбіт", "Зниклий"} <= names
    _rules_round_trip(page, url, stick)  # and so does the exclusion "Removed from the device"

    # the manual song's source goes: kept as an orphan (with `remove_deleted` off), then Remove orphans
    _api(url, "PATCH", f"/devices/{stick}", json={"remove_deleted": False})
    (music / "Gone" / "Зниклий.mp3").unlink()
    _api(url, "POST", "/libraries/folder/refresh")
    _jobs_settled(url)
    _apply_from_plan(page, url, stick)
    page.wait_for_selector("[data-report]", timeout=300_000)
    _goto(page, url, f"/devices/{stick}/settings")
    page.locator("#orphansCard").get_by_role("button", name="Remove orphans…").click()
    _confirm(page, "Remove 1")
    page.wait_for_selector("[data-report]", timeout=300_000)
    names = {i["title"] for i in _api(url, "GET", f"/devices/{stick}/contents")}
    assert "Зниклий" not in names and {"Група крові", "Орбіт"} <= names

    back = _eject_and_back(page, url, app, flash_disk, stick, "UI Stick")
    _unmount_and_fsck(page, app, stick, back)
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
    assert _data_listing() == before, "the test media changed"


def test_ui_ipod_classic(setup, chrome, classic_disk):  # noqa: F811
    """An iPod Classic: registered from the detected list, its data partition formatted (partition table unchanged),
    6 songs from the local Navidrome (test media, read-only), pygpod re-reads the database, an update with its
    reverse step, ejected from the UI and detected again, its contents read again from the database."""
    url, app, _ = setup
    before = _data_listing()
    table = _partition_table(classic_disk.path)
    run_on(app, app._disk_added(classic_disk))
    page = open_page(chrome, url)
    pod = _register(page, url, classic_disk, "UI Classic", "nd")
    _api(
        url,
        "PATCH",
        f"/devices/{pod}",
        json={"rule_set": _linked(url, "UI Classic", SIX, "nd"), "on_connect": "manual"},
    )
    _format(page, url, pod, "UI Classic", None)  # the data partition only
    assert _partition_table(classic_disk.path) == table
    _apply_from_plan(page, url, pod)
    page.wait_for_selector("[data-report]", timeout=900_000)
    assert "Verified on the device after flushing" in page.locator("#content").inner_text()
    dev, manifest, _ = run_on(app, _check_db(app, pod), 120)
    assert len(manifest.items) == 6

    # an update (the standalone reverse sync went, its reverse step stays)
    jobs0 = _job_ids(url)
    _goto(page, url, f"/devices/{pod}/overview")
    page.locator('[data-action="updateNow"]').click()
    assert [j["status"] for j in _wait_jobs(url, pod, jobs0)] == ["succeeded"]

    back = _eject_and_back(page, url, app, classic_disk, pod, "UI Classic")
    _goto(page, url, f"/devices/{pod}/contents")
    # the database is read again - after the clock on connect and its 30 s settle
    page.wait_for_selector('input[name="pcItem"]', timeout=120_000)
    assert page.locator('input[name="pcItem"]').count() == 6
    _unmount_and_fsck(page, app, pod, back)
    assert _partition_table(back.path) == table
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
    assert _data_listing() == before, "the test media changed"


async def _data_partition(app, device_id: str) -> str:
    cd = app.sync.detector.by_device(device_id)
    assert cd is not None and cd.data_partition
    return cd.data_partition
