"""the UI click-through (tests/hardware/test_ui_click_through.py) on emulated devices - a
flash drive and an iPod Classic as loop images (tests/emulated.py), clicked in a browser against one live server.

Same flow as the hardware version, with a folder library (synthetic songs in /var/tmp) for both devices instead of
the Navidrome parts (those stay in the hardware test and the Navidrome integration tests): register both from the
detected list, format the stick with every file system the host supports (ending on FAT32) and the Classic's data
partition (partition table unchanged), send a song from the library browser, Rules round trip, both sync
at the same time, Player contents removal, orphan → Remove orphans, Eject both from the UI; then the loop devices
are unplugged and plugged in again (instead of re-enabling USB ports) and both are detected again with their songs.
Real disks are not touched (their lsblk view is compared before and after).
"""

from __future__ import annotations

import asyncio
import faulthandler
import io
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from tests.emulated import (
    CLASSIC_LAYOUT,
    IMAGE_ROOT,
    attach,
    block_device,
    emulated_config,
    flash,
    ipod,
    needs_sudo,
    replug,
    unplug,
)
from tests.hardware.test_classic_acceptance import _check_db, _partition_table
from tests.hardware.test_ui_click_through import (
    ALL,
    _add_to_device,
    _api,
    _apply,
    _apply_from_plan,
    _confirm,
    _data_partition,
    _format,
    _fsck,
    _fstype,
    _goto,
    _job_ids,
    _linked,
    _open_plan,
    _register,
    _rules_round_trip,
    _wait,
    _wait_jobs,
)
from tests.integration.test_emulated_devices import _real_disks
from tests.media import make_music
from tests.webui.conftest import chrome, open_page, run_on, web_server  # noqa: F401 - fixtures

pytestmark = [pytest.mark.integration, pytest.mark.emulated, pytest.mark.slow, needs_sudo]

SONGS = (
    ("Скрябін", "Група крові"),
    ("Скрябін", "Зозуля"),
    ("Тартак", "Орбіт"),
    ("Тартак", "Виходу немає"),
)


async def _task_stacks() -> str:
    """Every asyncio task of the app's loop with the frame it waits in (a job that never ends)."""
    out = io.StringIO()
    for task in asyncio.all_tasks():
        print(f"--- {task.get_name()}: {task.get_coro()!r}", file=out)
        task.print_stack(file=out)
    return out.getvalue()


@pytest.fixture
def config(tmp_path):
    """The web server's app on emulated devices: loop devices allowed, tests/emulated.toml as the allowlist."""
    cache = Path(tempfile.mkdtemp(prefix="ui-cache-", dir=IMAGE_ROOT))
    yield emulated_config(tmp_path, cache).model_copy(update={"allow_directory_devices": True})
    shutil.rmtree(cache, ignore_errors=True)


@pytest.fixture
def disks(tmp_path):
    # 256 MB: three 2-minute stereo FLACs (~60 MB) plus the plan's reserve for the device database and the
    # safety margin must fit, or Apply stays disabled
    with (
        attach("emulated_flash", 256) as stick,
        attach("emulated_ipod_classic", 256, layout=CLASSIC_LAYOUT) as pod,
    ):
        with ThreadPoolExecutor(
            1
        ) as ex:  # Playwright's sync API keeps a loop running in this thread
            ex.submit(asyncio.run, flash(stick, "vfat", "UISTICK")).result()
            ex.submit(asyncio.run, ipod(pod, tmp_path / "build", "UI Classic")).result()
        yield stick, pod


@pytest.fixture
def setup(disks, web_server):  # noqa: F811
    url, app = web_server
    stick, pod = disks
    app.config.detect_devices = (
        True  # unknown disks get their read-only probe mount (like running_app)
    )
    music = Path(tempfile.mkdtemp(prefix="ui-music-", dir="/var/tmp"))
    for artist, title in SONGS:
        make_music(
            music / artist / f"{title}.flac",
            seconds=120,  # the Classic transcodes FLAC: long enough for the syncs to overlap
            title=title,
            artist=artist,
            album=f"{artist} album",
        )
    make_music(music / "Gone" / "Зниклий.mp3", title="Зниклий", artist="Gone", album="Gone")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "folder", "type": "folder", "name": "Folder", "settings": {"root": str(music)}},
    )
    _api(url, "PATCH", "/settings", json={"max_device_syncs": 2})
    blks = [block_device(d) for d in (stick, pod)]
    for blk in blks:
        run_on(app, app._disk_added(blk))
    try:
        yield url, app, stick, pod, blks, music
    finally:
        shutil.rmtree(music, ignore_errors=True)


def test_ui_on_emulated_stick_and_classic(setup, chrome):  # noqa: F811
    url, app, stick_disk, pod_disk, (flash_blk, classic_blk), music = setup
    real = _real_disks()
    table = _partition_table(pod_disk.dev)
    page = open_page(chrome, url)

    stick = _register(page, url, flash_blk, "UI Stick", "folder")
    pod = _register(page, url, classic_blk, "UI Classic", "folder")
    for device_id, name in ((stick, "UI Stick"), (pod, "UI Classic")):
        _api(
            url,
            "PATCH",
            f"/devices/{device_id}",
            json={"rule_set": _linked(url, name, ALL, "folder"), "on_connect": "manual"},
        )

    # stick: every file system the host can make, from Device settings; FAT32 last
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
        assert _fstype(run_on(app, _data_partition(app, stick))) == fs, fs
    # Classic: the data partition only
    _format(page, url, pod, "UI Classic", None)
    assert _partition_table(pod_disk.dev) == table

    # one song from the library browser goes to the stick (the v2 browser adds a rule)
    _goto(page, url, "/libraries/folder/browse")
    page.fill("#q", "Зниклий")
    page.locator('form[data-action="search"] button').click()
    page.wait_for_selector("text=1 item")
    page.locator('input[name="sel"]').first.check()
    _add_to_device(page, stick, "UI Stick")
    _rules_round_trip(page, url, stick)  # the pick survives Test rules and Save

    # both devices sync at the same time (two tabs); gentle write paces the Classic (a pause after every file) so
    # its sync still runs when the second tab's Apply starts the stick's
    _api(
        url,
        "PATCH",
        f"/devices/{pod}",
        json={"sync": {"gentle_write": True, "gentle_batch_files": 1, "gentle_pause_s": 3}},
    )
    jobs0 = _job_ids(url)
    other = open_page(chrome, url)
    _open_plan(page, url, stick)
    _open_plan(other, url, pod)
    _apply(other, pod)
    _apply(page, stick)
    for p in (page, other):
        p.wait_for_selector("[data-report]", timeout=600_000)
        assert "Verified on the device after flushing" in p.locator("#content").inner_text()
    jobs = [j for j in _api(url, "GET", "/jobs") if j["id"] not in jobs0 and j["kind"] == "sync"]
    assert {j["device_id"] for j in jobs} == {stick, pod}
    assert all(j["status"] == "succeeded" for j in jobs)
    t = sorted(
        (datetime.fromisoformat(j["started_at"]), datetime.fromisoformat(j["finished_at"]))
        for j in jobs
    )
    assert t[1][0] < t[0][1], f"the two syncs did not overlap: {t}"
    other.close()
    _, manifest, _ = run_on(app, _check_db(app, pod), 120)
    assert len(manifest.items) == len(SONGS) + 1

    # an update on the Classic (the standalone reverse sync went, its reverse step stays)
    jobs0 = _job_ids(url)
    _goto(page, url, f"/devices/{pod}/overview")
    page.locator('[data-action="updateNow"]').click()
    done = _wait_jobs(url, pod, jobs0)
    assert "update" in {j["kind"] for j in done} and {j["status"] for j in done} == {"succeeded"}

    # Player contents: remove Зозуля from the stick
    _goto(page, url, f"/devices/{stick}/contents")
    page.locator('input[name="pcItem"][aria-label="Зозуля"]').check()
    page.locator('[data-action="pcRemove"]').click()
    _confirm(page, "Remove 1")
    page.wait_for_selector("[data-report]", timeout=300_000)
    names = {i["title"] for i in _api(url, "GET", f"/devices/{stick}/contents")}
    assert "Зозуля" not in names and {"Група крові", "Орбіт", "Зниклий"} <= names
    _rules_round_trip(page, url, stick)  # and so does the exclusion "Removed from the device"

    # a song's source goes: kept as an orphan (with `remove_deleted` off), then Remove orphans
    _api(url, "PATCH", f"/devices/{stick}", json={"remove_deleted": False})
    (music / "Gone" / "Зниклий.mp3").unlink()
    _api(url, "POST", "/libraries/folder/refresh")
    _wait(  # the refresh runs as a job: the plan must see the song gone
        lambda: all(j["status"] not in ("queued", "running") for j in _api(url, "GET", "/jobs")),
        120,
        "library refresh",
    )
    # the refresh's automatic update of the stick may start only after the refresh job ends: an Apply clicked while
    # it holds the device starts no sync - wait until no job ran for a few seconds
    idle_since = [time.monotonic()]

    def settled() -> bool:
        if any(j["status"] in ("queued", "running") for j in _api(url, "GET", "/jobs")):
            idle_since[0] = time.monotonic()
        return time.monotonic() - idle_since[0] > 5

    try:
        _wait(settled, 300, "jobs settle after the refresh")
    except AssertionError:  # name the job that never ends
        busy = [j for j in _api(url, "GET", "/jobs") if j["status"] in ("queued", "running")]
        # where the stuck job's coroutine waits
        print(run_on(app, _task_stacks(), 30), file=sys.stderr)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        raise AssertionError(f"jobs never settled after the refresh: {busy}") from None
    jobs0 = _job_ids(url)
    _apply_from_plan(page, url, stick)
    done = _wait_jobs(url, stick, jobs0)  # an automatic update may run alongside
    assert "sync" in {j["kind"] for j in done} and {j["status"] for j in done} == {"succeeded"}
    _goto(page, url, f"/devices/{stick}/settings")
    page.locator("#orphansCard").get_by_role("button", name="Remove orphans…").click()
    _confirm(page, "Remove 1")
    page.wait_for_selector("[data-report]", timeout=300_000)
    names = {i["title"] for i in _api(url, "GET", f"/devices/{stick}/contents")}
    assert "Зниклий" not in names and {"Група крові", "Орбіт"} <= names

    # eject both from the UI, pull the loop devices out (what the udev monitor would report) and plug them in again
    for device_id, name in ((stick, "UI Stick"), (pod, "UI Classic")):
        _goto(page, url, f"/devices/{device_id}/overview")
        page.locator('[data-action="eject"]').click()
        _confirm(page, "Eject")
        page.wait_for_selector(f".toast:has-text('{name} can be unplugged now')", timeout=120_000)
    for disk, blk in ((stick_disk, flash_blk), (pod_disk, classic_blk)):
        unplug(disk)
        run_on(app, app.sync.on_disk_removed(blk.name))
    assert not any(d["connected"] for d in _api(url, "GET", "/devices")["registered"])
    for disk in (stick_disk, pod_disk):
        replug(disk)
        run_on(app, app._disk_added(block_device(disk)))
    assert all(d["connected"] for d in _api(url, "GET", "/devices")["registered"])
    _goto(page, url, f"/devices/{pod}/contents")
    page.wait_for_selector('input[name="pcItem"]', timeout=120_000)  # after the update on connect
    # the update on connect removed Зниклий, deleted from the library
    page.wait_for_function(
        f"document.querySelectorAll('input[name=pcItem]').length === {len(SONGS)}"
    )

    page.goto("about:blank")
    for device_id in (stick, pod):
        run_on(app, app.sync._unmount(device_id), 60)
    _fsck(stick_disk.part(1))
    _fsck(pod_disk.part(1))
    assert _partition_table(pod_disk.dev) == table
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
    assert _real_disks() == real
