"""Regressions of the sync API and the sync service on directory flash drives: plans, runs, cancels, resumes,
settings and their events."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

from pathlib import Path

import pytest

from syncmusic.devices._filedevice import FileDeviceMixin
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.contracts.harness import ipod_tree
from tests.unit.test_api_sync import ALL, _apply, _register, env, music  # noqa: F401
from tests.unit.test_unmountable import _disk_device

BAD_RULES = {"rules": [{"id": "x", "name": "x", "criteria": {"bogusop": {"artist": "a"}}}]}


async def _first_sync(app, c, name):
    plan = (await c.post(f"/api/v1/devices/{name}/plan")).json()
    await _apply(app, c, plan["id"], first_sync="keep")


async def test_file_deleted_on_device_is_copied_again(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    await _first_sync(app, c, "flash")
    victim = next((tmp_path / "flash").rglob("Орбіт без цукру.mp3"))
    victim.unlink()
    plan = (await c.post("/api/v1/devices/flash/plan")).json()
    assert plan["counts"]["add"] == 1 and plan["counts"]["keep"] == 2


async def test_lrc_edit_in_library_reaches_device(env, tmp_path, music):
    app, c = env
    lrc = music / "Тартак" / "Орбіт.lrc"
    lrc.write_text("[00:01.00]first\n", encoding="utf-8")
    await _register(c, tmp_path / "flash", name="Flash")
    await _first_sync(app, c, "flash")
    on_device = next((tmp_path / "flash").rglob("Орбіт без цукру.lrc"))
    assert "first" in on_device.read_text(encoding="utf-8")
    lrc.write_text("[00:01.00]second\n", encoding="utf-8")
    plan = (await c.post("/api/v1/devices/flash/plan")).json()
    assert plan["counts"]["replace"] == 1
    await _apply(app, c, plan["id"])
    assert "second" in on_device.read_text(encoding="utf-8")


async def test_invalid_device_config_rejected(env, tmp_path):
    app, c = env
    (tmp_path / "bad").mkdir()
    r = await c.post(
        "/api/v1/devices/register",
        json={
            "name": "Bad",
            "mount_path": str(tmp_path / "bad"),
            "type": "flashdrive",
            "library_ids": ["music"],
            "rule_set": BAD_RULES,
        },
    )
    assert r.status_code == 400
    await _register(c, tmp_path / "flash", name="Flash")
    for body in (
        {"rule_set": BAD_RULES},
        {"library_ids": ["nope"]},
        {"type": "no-such-type"},
        {"bogus_field": 1},
    ):
        r = await c.patch("/api/v1/devices/flash", json=body)
        assert r.status_code == 400, (body, r.status_code)
    assert (await c.post("/api/v1/devices/flash/plan")).status_code == 200


async def test_second_device_on_same_directory_refused(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    await _first_sync(app, c, "flash")
    r = await c.post(
        "/api/v1/devices/register",
        json={
            "name": "Other",
            "mount_path": str(tmp_path / "flash"),
            "type": "flashdrive",
            "library_ids": ["music"],
        },
    )
    assert r.status_code == 409


async def test_invalid_config_leaves_stored_config_unchanged(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    before = (await c.get("/api/v1/devices/flash")).json()["config"]
    for body in ({"rule_set": BAD_RULES}, {"processors": [{"type": "nope"}]}, {"id": "other"}):
        r = await c.patch("/api/v1/devices/flash", json=body)
        assert r.status_code == 400 and r.json()["error"] == "invalid", body
    assert (await c.get("/api/v1/devices/flash")).json()["config"] == before


async def test_stored_bad_rule_plans_400_not_500(env, tmp_path):
    """A config stored before validation existed must not crash the plan endpoint."""
    from syncmusic.core.models import RuleSet

    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    cfg = await app.store.device("flash")
    cfg.rule_set = RuleSet.model_validate(BAD_RULES)
    await app.store.put_device(cfg)
    await app.sync.load()
    r = await c.post("/api/v1/devices/flash/plan")
    assert r.status_code == 400 and "bogusop" in r.json()["message"]


async def test_nested_directory_of_another_device_refused(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    (tmp_path / "flash" / "sub").mkdir()
    for path in (tmp_path / "flash" / "sub", tmp_path):
        r = await c.post(
            "/api/v1/devices/register",
            json={
                "name": "Other",
                "mount_path": str(path),
                "type": "flashdrive",
                "library_ids": ["music"],
            },
        )
        assert r.status_code == 409, path


async def test_manifest_of_another_registration_needs_confirmation(env, tmp_path):
    """the same slug id re-registered (other app DB / deleted + registered again) must not
    silently treat the old registration's manifest as its own."""
    from syncmusic.core import manifest as manifest_io

    app, c = env
    first = await _register(c, tmp_path / "flash", name="Flash")
    await _first_sync(app, c, "flash")
    old_uid = manifest_io.read(tmp_path / "flash").header.device_uid
    assert old_uid == first["identity"]["manifest_device_id"] and old_uid
    assert (await c.delete("/api/v1/devices/flash")).status_code == 204
    # "keep files": a new identity (the default would continue with the device's manifest)
    second = await _register(c, tmp_path / "flash", name="Flash", continuity="keep")
    assert second["id"] == "flash" and second["identity"]["manifest_device_id"] != old_uid
    plan = (await c.post("/api/v1/devices/flash/plan")).json()
    assert plan["needs_confirmation"] and not plan["first_sync"]
    assert "another registration" in plan["confirmation_reasons"][0]
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={})
    assert r.status_code == 409 and r.json()["error"] == "confirmation_required"
    await _apply(app, c, plan["id"], confirm=True)
    header = manifest_io.read(tmp_path / "flash").header
    assert header.device_uid == second["identity"]["manifest_device_id"]
    plan2 = (await c.post("/api/v1/devices/flash/plan")).json()
    assert not plan2["needs_confirmation"]


async def test_legacy_manifest_not_owned_by_a_new_registration(env, tmp_path):
    """a manifest without uid (no build writes one since) with the same device id is not
    owned by a registration: another registration's."""
    from syncmusic.core import manifest as manifest_io

    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    await _first_sync(app, c, "flash")
    m = manifest_io.read(tmp_path / "flash")
    m.header.device_uid = None  # as an older SyncMusic wrote it
    manifest_io.write(tmp_path / "flash", m)
    assert (await c.delete("/api/v1/devices/flash")).status_code == 204
    again = await _register(c, tmp_path / "flash", name="Flash")
    assert again["id"] == "flash" == m.header.device_id
    plan = (await c.post("/api/v1/devices/flash/plan")).json()
    assert plan["needs_confirmation"] and "another registration" in plan["confirmation_reasons"][0]


async def test_mount_path_on_library_root_refused(env, tmp_path, music):
    app, c = env
    for path in (music, tmp_path):  # the library root itself and a directory containing it
        r = await c.post(
            "/api/v1/devices/register",
            json={
                "name": f"Bad {path.name}",
                "mount_path": str(path),
                "type": "flashdrive",
                "library_ids": ["music"],
            },
        )
        assert r.status_code in (400, 409), (path, r.status_code)


async def test_patch_mount_path_validated_like_register(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "one", name="One")
    await _register(c, tmp_path / "two", name="Two")
    before = (await c.get("/api/v1/devices/two")).json()
    for path in ("/", str(Path.home()), str(tmp_path / "one")):  # config only: never planned
        r = await c.patch("/api/v1/devices/two", json={"mount_path": path})
        assert r.status_code in (400, 409), (path, r.status_code)
    assert (await c.get("/api/v1/devices/two")).json() == before


async def test_delete_threshold_out_of_range_rejected(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    for value in (-5, 101):
        r = await c.patch("/api/v1/devices/flash", json={"delete_threshold_pct": value})
        assert r.status_code == 400, (value, r.status_code)
    for value in (0, 100):
        r = await c.patch("/api/v1/devices/flash", json={"delete_threshold_pct": value})
        assert r.status_code == 200 and r.json()["delete_threshold_pct"] == value
    for changes in (
        {"sync": {"safety_margin_pct": -1}},
        {"sync": {"backup_keep": -1}},
    ):
        assert (await c.patch("/api/v1/devices/flash", json=changes)).status_code == 400, changes


async def test_file_vanishing_during_first_sync_walk_is_skipped(env, tmp_path, monkeypatch):
    app, c = env
    dev = tmp_path / "flash"
    (dev / "Other").mkdir(parents=True)
    (dev / "Other" / "keep.mp3").write_bytes(b"x" * 10)
    (dev / "Other" / "gone.mp3").write_bytes(b"x" * 10)
    await _register(c, dev, name="Flash")
    real_stat = Path.stat

    def stat(self, *a, **kw):
        if self.name == "gone.mp3":
            raise FileNotFoundError(str(self))
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", stat)
    r = await c.post("/api/v1/devices/flash/plan")
    assert r.status_code == 200, r.text
    contents = await (await app.sync.open_device("flash")).read_contents()
    assert [i.path for i in contents.items] == ["Other/keep.mp3"]


async def test_plan_outdated_after_config_change(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan")).json()
    r = await c.patch("/api/v1/devices/flash", json={"on_connect": "manual", "processors": []})
    assert r.status_code == 200
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert r.status_code in (404, 409), r.status_code


async def test_wipe_of_plain_directory_refused(env, tmp_path):
    app, c = env
    dev = tmp_path / "flash"
    (dev / "owner").mkdir(parents=True)
    (dev / "owner" / "precious.txt").write_text("keep me")
    await _register(c, dev, name="Flash")
    plan = (await c.post("/api/v1/devices/flash/plan")).json()
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "wipe"})
    assert r.status_code == 400 and "wipe" in r.json()["message"]
    assert (dev / "owner" / "precious.txt").read_text() == "keep me"


async def test_plain_directory_refused_without_opt_in(env, tmp_path):
    app, c = env
    app.config.allow_directory_devices = False
    (tmp_path / "flash").mkdir()
    r = await c.post(
        "/api/v1/devices/register",
        json={"name": "F", "mount_path": str(tmp_path / "flash"), "type": "flashdrive"},
    )
    assert r.status_code == 400 and "removable" in r.json()["message"]


async def test_stored_bad_mount_path_not_planned(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    cfg = await app.sync.config("flash")
    cfg.mount_path = str(tmp_path)  # contains the library root: stored's checks
    await app.store.put_device(cfg)
    await app.sync.load()
    r = await c.post("/api/v1/devices/flash/plan")
    assert r.status_code == 400 and "overlaps" in r.json()["message"]


def test_protected_reason_and_mount_source(tmp_path):
    from syncmusic.core.mount_path import mount_source, protected_reason

    home = Path.home().resolve()
    for bad in (Path("/"), home, home.parent, Path("/etc/x"), Path("/usr"), Path("/home")):
        assert protected_reason(bad, []) is not None, bad
    lib = tmp_path / "music"
    assert "library" in protected_reason(tmp_path, [("library 'm'", lib)])
    assert "library" in protected_reason(lib / "sub", [("library 'm'", lib)])
    assert protected_reason(tmp_path / "flash", [("library 'm'", lib)]) is None
    assert mount_source(Path("/")) is not None  # the root filesystem is a /dev mount


async def test_plans_dropped_on_rule_and_path_change_and_eject(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    for changes in (
        {"rule_set": {"template_ids": ["default.music.all"]}},
        {"mount_path": str(tmp_path / "other")},
    ):
        (tmp_path / "other").mkdir(exist_ok=True)
        plan = (await c.post("/api/v1/devices/flash/plan")).json()
        assert (await c.patch("/api/v1/devices/flash", json=changes)).status_code == 200
        r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
        assert r.status_code == 404, changes
    await c.patch("/api/v1/devices/flash", json={"rule_set": {"template_ids": ["flash-rules"]}})
    plan = (await c.post("/api/v1/devices/flash/plan")).json()
    assert (await c.post("/api/v1/devices/flash/eject")).status_code in (200, 204)
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert r.status_code == 404
    plan = (await c.post("/api/v1/devices/flash/plan")).json()  # plan again: applies
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert r.status_code == 202


async def test_request_validation_errors_are_400_error_message(env, tmp_path):
    app, c = env
    (tmp_path / "flash").mkdir()
    bad_kind = {
        "name": "F",
        "mount_path": str(tmp_path / "flash"),
        "type": "flashdrive",
        "rule_set": {"rules": [{"id": "r", "name": "R", "kind": ["podcast_episode"]}]},
    }
    r = await c.post("/api/v1/devices/register", json=bad_kind)
    assert r.status_code == 400 and r.json()["error"] == "invalid", r.text
    assert "rule_set.rules.0.kind" in r.json()["message"]
    r = await c.post(
        "/api/v1/devices/register",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid"
    await _register(c, tmp_path / "f2", name="F2")
    plan = (await c.post("/api/v1/devices/f2/plan")).json()
    r = await c.post(f"/api/v1/plans/{plan['id']}/apply", json={"confirm": "maybe"})
    assert r.status_code == 400 and "confirm" in r.json()["message"]


@pytest.mark.parametrize("marker", ["iPod_Control/iTunes", ".rockbox"])
async def test_mount_path_on_ipod_data_partition_refused(env, tmp_path, marker):
    app, c = env
    dev = tmp_path / "ipod"
    (dev / marker).mkdir(parents=True)
    (dev / marker / "precious").write_text("device database")
    r = await c.post(
        "/api/v1/devices/register",
        json={
            "name": "Ipod",
            "mount_path": str(dev),
            "type": "flashdrive",
            "library_ids": ["music"],
        },
    )
    assert r.status_code in (400, 409), r.text
    assert (dev / marker / "precious").read_text() == "device database"
    assert "iPod" in r.json()["message"]
    # nested below the marker's parent, and via PATCH of a working device
    sub = dev / "Music"
    sub.mkdir()
    r = await c.post(
        "/api/v1/devices/register",
        json={
            "name": "Sub",
            "mount_path": str(sub),
            "type": "flashdrive",
            "library_ids": ["music"],
        },
    )
    assert r.status_code in (400, 409), r.text
    await _register(c, tmp_path / "flash", name="Flash")
    r = await c.patch("/api/v1/devices/flash", json={"mount_path": str(dev)})
    assert r.status_code in (400, 409), r.text
    assert (await app.sync.config("flash")).mount_path == str(tmp_path / "flash")


def test_device_tree_reason(tmp_path, monkeypatch):
    from syncmusic.core import mount_path as mp
    from syncmusic.infra.blockdev import mount as mnt

    assert mp.device_tree_reason(tmp_path) is None
    (tmp_path / "x" / ".rockbox").mkdir(parents=True)
    assert ".rockbox" in mp.device_tree_reason(tmp_path / "x" / "Music")
    real = mnt.mount_table()
    monkeypatch.setattr(
        mnt,
        "mount_table",
        lambda: [*real, mnt.MountEntry("0:0", str(tmp_path / "y" / "m"), "none")],
    )
    assert "mount point" in mp.device_tree_reason(tmp_path / "y")


async def test_wipe_keeps_ipod_and_rockbox_trees(tmp_path):
    from syncmusic.core.models import DeviceConfig
    from syncmusic.devices.flashdrive import FlashDrive

    for marker in ("iPod_Control", ".rockbox", "junk"):
        (tmp_path / marker).mkdir()
        (tmp_path / marker / "f").write_text("x")
    dev = FlashDrive(DeviceConfig(id="f", type="flashdrive", name="F"), tmp_path, None)
    await dev.wipe()
    assert sorted(p.name for p in tmp_path.iterdir()) == [".rockbox", "iPod_Control"]


async def test_deleted_device_disk_is_unknown_again(env):
    app, c = env
    cd = await _disk_device(app, on_connect="manual")
    cd.disk = BlockDeviceInfo(name="sdz", path="/dev/sdz", serial="S1", tran="usb", type="disk")
    assert (await c.delete("/api/v1/devices/zorvex")).status_code == 204
    assert cd.device_id is None
    assert app.sync.detector.by_device("zorvex") is None
    devices = (await c.get("/api/v1/devices")).json()
    assert [u["disk"] for u in devices["unknown"]] == ["/dev/sdz"]
    r = await c.post(
        "/api/v1/devices/register",
        json={"disk": "/dev/sdz", "name": "Again", "type": "flashdrive", "library_ids": ["music"]},
    )
    assert r.status_code == 201, r.text
    assert app.sync.detector.by_device(r.json()["id"]) is not None


async def test_service_validation_errors_use_the_short_message_format(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    r = await c.patch("/api/v1/devices/flash", json={"delete_threshold_pct": -1})
    assert r.status_code == 400
    message = r.json()["message"]
    assert "delete_threshold_pct" in message
    assert "\n" not in message and "errors.pydantic.dev" not in message, message


async def test_rematch_only_touches_changed_disks(env):
    app, c = env
    cd = await _disk_device(app, on_connect="manual")
    q = app.ctx.bus.subscribe()
    await app.sync.detector.rematch()
    assert cd.device_id == "zorvex" and q.empty()


async def test_unknown_routes_and_methods_answer_error_message(env):
    app, c = env
    r = await c.get("/api/v1/nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found", r.text
    r = await c.put("/api/v1/libraries/music")  # DELETE exists since
    assert r.status_code == 405 and r.json()["error"] == "method_not_allowed", r.text
    assert "message" in r.json() and "allow" in r.headers


async def test_library_root_overlapping_a_device_refused(env, tmp_path):
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    for i, root in enumerate((tmp_path / "flash" / "lib2", tmp_path, app.ctx.config.data_dir)):
        root.mkdir(parents=True, exist_ok=True)
        r = await c.post(
            "/api/v1/libraries",
            json={"id": f"l{i}", "type": "folder", "settings": {"root": str(root)}},
        )
        assert r.status_code in (400, 409), r.text
        assert await app.store.library(f"l{i}") is None
    assert r.status_code == 400
    assert (await c.post("/api/v1/devices/flash/plan")).status_code == 200


async def test_mount_removed_behind_our_back_is_not_reused(env, monkeypatch, tmp_path):
    """The daemon's cached mount is gone (umount by hand, another registration's eject): the
    mountpoint is a plain directory on the system disk now and must not be planned/synced into."""
    app, c = env
    target = tmp_path / "mnt" / "zorvex"
    calls: list[str] = []

    async def mount(guard, dev_path, device_id, mount_root, **kw):
        calls.append(dev_path)
        target.mkdir(parents=True, exist_ok=True)
        return mnt.Mounted(target, dev_path, "vfat", owned=True)

    monkeypatch.setattr(mnt, "mount", mount)
    await _disk_device(app, on_connect="manual")
    await c.post("/api/v1/devices/zorvex/plan")
    assert calls == ["/dev/sdz1"]
    # nothing is mounted at `target` (it is a plain tmp directory): the next plan must re-mount or refuse
    r = await c.post("/api/v1/devices/zorvex/plan")
    assert len(calls) == 2 or r.status_code != 200, r.text


async def test_concurrent_put_of_a_key_keeps_the_entry_readable(tmp_path):
    """Two syncs prepare the same item through the same chain: a second put of the key must never
    make the entry the first sync is copying to its device disappear (seen as ENOENT on a real run)."""
    import threading

    from syncmusic.infra.cache import ProcessingCache, make_key
    from syncmusic.infra.db import Database

    db = Database(tmp_path / "sd.db")
    await db.open()
    try:
        c = ProcessingCache(db, tmp_path / "cache")
        key = make_key("lib", "item", "1:1", [("retag", "x", 1)])
        src = tmp_path / "m.flac"
        src.write_bytes(b"x" * 100)
        first = await c.put(key, {"media.flac": src})
        stop, missing = threading.Event(), []

        def reader() -> None:
            while not stop.is_set():
                try:
                    with open(first.file("media.flac"), "rb") as f:
                        f.read()
                except FileNotFoundError:
                    missing.append(1)

        t = threading.Thread(target=reader)
        t.start()
        try:
            async with c.pinned(key):
                for _ in range(300):
                    await c.put(key, {"media.flac": src})
        finally:
            stop.set()
            t.join()
        assert not missing, f"entry vanished {len(missing)} times during re-puts"
    finally:
        await db.close()


async def test_eject_of_a_device_sharing_the_mount_leaves_it_to_the_other(
    env, monkeypatch, tmp_path
):
    """Two registrations of one disk share a mountpoint: ejecting the disconnected one must not
    unmount the file system the connected one uses; ownership moves over."""
    app, _ = env
    unmounted: list[mnt.Mounted] = []

    async def unmount(m, **kw):
        unmounted.append(m)

    monkeypatch.setattr(mnt, "unmount", unmount)
    await _disk_device(app, on_connect="manual")
    path = tmp_path / "mnt" / "zorvex"
    app.sync._mounts["old"] = mnt.Mounted(path, "/dev/sdz1", "vfat", owned=True)
    app.sync._mounts["zorvex"] = mnt.Mounted(path, "/dev/sdz1", "vfat", owned=False)
    await app.sync._unmount("old")
    assert unmounted == []
    assert app.sync._mounts == {"zorvex": mnt.Mounted(path, "/dev/sdz1", "vfat", owned=True)}
    await app.sync._unmount("zorvex")  # the last user unmounts it
    assert [m.path for m in unmounted] == [path]


async def test_patch_that_disconnects_a_device_drops_its_mount(env, monkeypatch, tmp_path):
    app, c = env
    unmounted: list[mnt.Mounted] = []

    async def unmount(m, **kw):
        unmounted.append(m)

    monkeypatch.setattr(mnt, "unmount", unmount)
    await _disk_device(app, on_connect="manual")
    app.sync._mounts["zorvex"] = mnt.Mounted(tmp_path, "/dev/sdz1", "vfat", owned=True)
    r = await c.patch("/api/v1/devices/zorvex", json={"identity": {"usb_serial": "OTHER"}})
    assert r.status_code == 200, r.text
    assert app.sync.detector.by_device("zorvex") is None
    assert "zorvex" not in app.sync._mounts and [m.path for m in unmounted] == [tmp_path]


def test_is_live_checks_the_visible_mount_and_its_device(monkeypatch, tmp_path):
    import os

    dev = tmp_path / "sdz1"
    dev.touch()
    rdev = os.makedev(8, 33)
    real_stat = os.stat

    def stat(p, *a, **k):
        return _Stat(rdev) if str(p) == str(dev) else real_stat(p, *a, **k)

    monkeypatch.setattr(mnt.os, "stat", stat)
    m = mnt.Mounted(tmp_path / "k", str(dev), "vfat", owned=True)
    table: list[mnt.MountEntry] = []
    monkeypatch.setattr(mnt, "mount_table", lambda: table)
    assert not mnt.is_live(m)  # nothing mounted there
    table.append(mnt.MountEntry("8:33", str(m.path), str(dev)))
    assert mnt.is_live(m)
    table.append(mnt.MountEntry("8:49", str(m.path), "/dev/sdx1"))  # something else on top
    assert not mnt.is_live(m)


class _Stat:
    def __init__(self, rdev: int) -> None:
        self.st_rdev = rdev


async def test_library_that_returns_no_items_removes_nothing(env, music, tmp_path):
    """an attached library suddenly empty (unmounted share) must not silently empty the device - since
    then its items are orphans: kept, with a warning naming the library. One deleted item is removed."""
    app, c = env
    await _register(c, tmp_path / "flash")
    plan = (await c.post("/api/v1/devices/my-flash/plan")).json()
    await _apply(app, c, plan["id"], first_sync="keep")
    (music / "Тартак").rename(tmp_path / "away")  # deleted from the library: removed
    plan = (await c.post("/api/v1/devices/my-flash/plan")).json()
    assert plan["counts"]["remove"] == 1 and plan["orphans"] == 0 and not plan["needs_confirmation"]
    assert not any("returned no items" in w for w in plan["warnings"])
    (music / "Скрябін").rename(tmp_path / "away2")  # the root stays, empty
    plan = (await c.post("/api/v1/devices/my-flash/plan")).json()
    assert plan["counts"]["remove"] == 0 and plan["orphans"] == 3
    assert any("'music' returned no items" in w for w in plan["warnings"])
    await _apply(app, c, plan["id"])
    assert len(list((tmp_path / "flash").rglob("*.mp3"))) == 3


async def test_malformed_json_body_message(env):
    """no bare JSON offset presented as a field name."""
    _, c = env
    r = await c.post(
        "/api/v1/libraries", content=b"{bad", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid"
    msg = r.json()["message"]
    assert "malformed JSON body at position 1" in msg and ": 1:" not in msg, msg


async def test_missing_body_message(env):
    """no body at all names what is missing."""
    _, c = env
    for path in ("/api/v1/libraries", "/api/v1/devices/register"):
        for headers in ({}, {"content-type": "application/json"}):
            r = await c.post(path, headers=headers)
            assert r.status_code == 400 and r.json()["error"] == "invalid", (path, r.text)
            assert "a JSON body is required" in r.json()["message"], (path, r.text)


async def test_space_after_eject_answers_409_without_mounting(env, monkeypatch, tmp_path):
    """after Eject the disk stays known until udev reports it gone; GET /space in that window (the UI
    re-renders on device.ejected) must answer 409 without a mount attempt (the guard refused it: 403)."""
    app, c = env
    target = tmp_path / "mnt" / "zorvex"
    calls: list[str] = []

    async def mount(guard, dev_path, device_id, mount_root, **kw):
        calls.append(dev_path)
        target.mkdir(parents=True, exist_ok=True)
        return mnt.Mounted(target, dev_path, "vfat", owned=True)

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(mnt, "mount", mount)
    monkeypatch.setattr(mnt, "unmount", nothing)
    monkeypatch.setattr(mnt, "eject", nothing)
    cd = await _disk_device(app, on_connect="manual")
    assert (await c.post("/api/v1/devices/zorvex/eject")).status_code == 204
    r = await c.get("/api/v1/devices/zorvex/space")
    assert r.status_code == 409 and r.json()["error"] == "not_connected", r.text
    assert calls == []
    # the device list says so: the UI does not ask an ejected device for /space
    dev = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == "zorvex"
    )
    assert dev["ejected"] is True and dev["ejecting"] is False
    # unplugged and connected again: usable as before
    await app.sync.on_disk_removed("sdz")
    app.sync.detector.connected["sdz"] = cd
    monkeypatch.setattr(mnt, "is_live", lambda m: True)
    r = await c.get("/api/v1/devices/zorvex/space")
    assert r.status_code == 200, r.text
    assert calls == ["/dev/sdz1"]
    dev = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == "zorvex"
    )
    assert dev["ejected"] is False


async def test_mount_lost_during_apply_stops_the_sync(env, monkeypatch, tmp_path):
    """The device's mount vanishes after the first put (lazy umount, udisks, ...): the mountpoint is
    a plain directory on the system disk from then on. The apply must stop writing and fail, not
    fill the bare directory and report success (real run: 172 MB + manifest on the NVMe)."""
    app, c = env
    target = tmp_path / "mnt" / "zorvex"
    live = {"v": True}

    async def mount(guard, dev_path, device_id, mount_root, **kw):
        target.mkdir(parents=True, exist_ok=True)
        return mnt.Mounted(target, dev_path, "vfat", owned=True)

    monkeypatch.setattr(mnt, "mount", mount)
    monkeypatch.setattr(mnt, "is_live", lambda m: live["v"])
    real_put = FileDeviceMixin.put_track

    async def put_track(self, s, item):
        ref = await real_put(self, s, item)
        live["v"] = False  # the mount is gone after the first file
        return ref

    monkeypatch.setattr(FileDeviceMixin, "put_track", put_track)
    await _disk_device(app, on_connect="manual")
    plan = (await c.post("/api/v1/devices/zorvex/plan")).json()
    assert plan["counts"]["add"] == 3, plan
    r = await c.post(
        f"/api/v1/plans/{plan['id']}/apply", json={"confirm": True, "first_sync": "keep"}
    )
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    # the first file was in flight (never in a committed manifest): abort removed it again
    written = [p for p in target.rglob("*.mp3")]
    assert job.status == "failed" and len(written) <= 1, (job.status, written)
    assert "unmounted during the sync" in (job.error or ""), job.error
    assert not (target / ".syncmusic" / "manifest.jsonl").exists()


async def test_mount_made_by_format_is_live_and_unmounted_on_close(env, monkeypatch):
    """format() records its mount with the partition, not the disk path (/dev/sdz): with the disk path the
    mount liveness check would call it gone, the next open would re-mount it as 'reused' (owned=False) and
    close/stop would never unmount it - the flash drive would stay mounted after the run."""
    from syncmusic.devices.flashdrive import FlashDrive

    app, c = env
    mounted: dict[str, str] = {}  # mountpoint -> source partition, like the kernel's table
    calls: list[str] = []
    unmounted: list[mnt.Mounted] = []

    async def fmt(self, name, fs=None):
        self.mount.mkdir(parents=True, exist_ok=True)
        mounted[str(self.mount)] = "/dev/sdz1"  # format mounts the new partition

    async def mount(guard, dev_path, device_id, mount_root, **kw):
        calls.append(dev_path)
        target = mount_root / device_id
        reused = mounted.get(str(target)) == dev_path
        mounted[str(target)] = dev_path
        return mnt.Mounted(target, dev_path, "vfat", owned=not reused)

    async def unmount(m, **kw):
        unmounted.append(m)

    monkeypatch.setattr(FlashDrive, "format", fmt)
    monkeypatch.setattr(mnt, "mount", mount)
    monkeypatch.setattr(mnt, "unmount", unmount)
    monkeypatch.setattr(mnt, "is_live", lambda m: mounted.get(str(m.path)) == m.dev_path)
    await _disk_device(app, on_connect="manual")
    await app.sync.format("zorvex", confirm="Zorvex", fs="vfat")
    r = await c.post("/api/v1/devices/zorvex/plan")
    assert r.status_code == 200, r.text
    assert calls == [], "the mount made by format was not recognised as live"
    await app.sync.close()
    assert [m.owned for m in unmounted] == [True]


async def test_ipod_registered_by_directory_syncs(env, tmp_path):
    """an iPod tree registered by mount_path with type ipod is accepted and synced."""
    from pygpod import Database

    app, c = env
    mount = ipod_tree(tmp_path / "ipod")
    cfg = await _register(c, mount, name="Virtual iPod", type="ipod")
    assert cfg["type"] == "ipod"
    plan = (await c.post(f"/api/v1/devices/{cfg['id']}/plan")).json()
    assert plan["counts"]["add"] == 3, plan
    await _apply(app, c, plan["id"], confirm=True, first_sync="keep")
    titles = sorted(t.title for t in Database(str(mount), apply_play_counts=False).tracks)
    assert titles == ["Група крові", "Зачини за мною двері", "Орбіт без цукру"]
    assert (await c.post(f"/api/v1/devices/{cfg['id']}/plan")).json()["counts"]["add"] == 0


async def test_ipod_type_still_refused_inside_or_without_type(env, tmp_path):
    app, c = env
    mount = ipod_tree(tmp_path / "ipod")
    inner = mount / "iPod_Control" / "Music"
    for body, needle in (
        ({"mount_path": str(inner), "type": "ipod"}, f"inside the iPod/Rockbox tree at {mount}"),
        ({"mount_path": str(mount), "type": "flashdrive"}, "register it as an iPod"),
        ({"mount_path": str(mount)}, "this directory with that type"),
    ):
        r = await c.post(
            "/api/v1/devices/register",
            json={"name": "X", "library_ids": ["music"], **body},
        )
        assert r.status_code == 400, r.text
        assert needle in r.json()["message"], r.json()
    assert (await c.get("/api/v1/devices")).json()["registered"] == []


def test_device_tree_reason_owns_tree(tmp_path, monkeypatch):
    from syncmusic.core import mount_path as mp
    from syncmusic.infra.blockdev import mount as mnt

    (tmp_path / "d" / "iPod_Control").mkdir(parents=True)
    assert mp.device_tree_reason(tmp_path / "d", owns_tree=True) is None
    assert "iPod_Control" in mp.device_tree_reason(tmp_path / "d")
    assert "inside" in mp.device_tree_reason(tmp_path / "d" / "iPod_Control", owns_tree=True)
    real = mnt.mount_table()
    monkeypatch.setattr(
        mnt,
        "mount_table",
        lambda: [*real, mnt.MountEntry("0:0", str(tmp_path / "d" / "m"), "none")],
    )
    assert "mount point" in mp.device_tree_reason(tmp_path / "d", owns_tree=True)


async def test_rockbox_registered_by_unmounted_disk_keeps_identity(env):
    """an unmounted Rockbox iPod registered by disk with type rockbox stores its serial and the
    data partition's UUID (the Rockbox probe needs no mount for Apple disks)."""
    from syncmusic.core.detection import ConnectedDisk, probe_disk
    from syncmusic.infra.blockdev.identify import BlockDeviceInfo

    app, c = env
    data = BlockDeviceInfo(
        name="sdy2", path="/dev/sdy2", type="part", fstype="vfat", uuid="C5D6-E7F8", size=10**11
    )
    fw = BlockDeviceInfo(name="sdy1", path="/dev/sdy1", type="part", size=4 * 10**7)
    disk = BlockDeviceInfo(
        name="sdy",
        path="/dev/sdy",
        tran="usb",
        type="disk",
        serial="MINI1",
        usb_vendor_id="05ac",
        children=[fw, data],
    )
    probes = probe_disk(disk)
    assert [p.type_name for p in probes][:2] == ["ipod", "rockbox"]
    app.sync.detector.connected["sdy"] = ConnectedDisk(disk=disk, probes=probes)
    r = await c.post(
        "/api/v1/devices/register",
        json={"disk": "/dev/sdy", "name": "Mini", "type": "rockbox", "library_ids": ["music"]},
    )
    assert r.status_code == 201, r.text
    ident = r.json()["identity"]
    assert ident["usb_serial"] == "MINI1" and ident["fs_uuid"] == "C5D6-E7F8"
    assert app.sync.detector.connected["sdy"].data_partition == "/dev/sdy2"


async def test_saving_rules_does_not_rematch_disks_but_an_identity_change_does(
    env, tmp_path, monkeypatch
):
    """a rule / settings change of a device never re-reads connected disks (their manifests);
    a change of what matching uses (identity, type, folder) does."""
    app, c = env
    await _register(c, tmp_path / "flash", name="Flash")
    calls = []

    async def spy() -> None:
        calls.append(1)

    monkeypatch.setattr(app.sync.detector, "rematch", spy)
    r = await c.patch("/api/v1/devices/flash", json={"rule_set": {"rules": [], "mode": "additive"}})
    assert r.status_code == 200, r.text
    r = await c.patch("/api/v1/devices/flash", json={"delete_threshold_pct": 30})
    assert r.status_code == 200, r.text
    assert calls == []
    assert (await app.sync.config("flash")).delete_threshold_pct == 30
    r = await c.patch("/api/v1/devices/flash", json={"identity": {"fs_uuid": "1234-ABCD"}})
    assert r.status_code == 200, r.text
    assert calls == [1]
