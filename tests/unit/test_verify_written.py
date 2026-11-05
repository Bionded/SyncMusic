"""after a sync the device is flushed, re-mounted and read back before the report says it holds music.

A Classic showed no music after a sync: the iTunesDB, ArtworkDB and manifest had directory entries
but their FAT chains never reached the disk (fsck: "File size is 752048 bytes, cluster chain length is 0").
"""
# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.infra.blockdev import mount as mnt
from tests.unit.test_api_sync import env, music  # noqa: F401
from tests.unit.test_unmountable import _disk_device


def _fake_mounts(monkeypatch, tmp_path, on_unmount=None):
    target = tmp_path / "mnt" / "zorvex"
    calls: list[str] = []

    async def mount(guard, dev_path, device_id, mount_root, **kw):
        target.mkdir(parents=True, exist_ok=True)
        calls.append("mount")
        return mnt.Mounted(target, dev_path, "vfat", owned=True)

    async def unmount(m, **kw):
        calls.append("unmount")
        if on_unmount:
            on_unmount(target)

    monkeypatch.setattr(mnt, "mount", mount)
    monkeypatch.setattr(mnt, "unmount", unmount)
    monkeypatch.setattr(mnt, "is_live", lambda m: True)
    return target, calls


async def _apply(app, c):
    plan = (await c.post("/api/v1/devices/zorvex/plan")).json()
    assert plan["counts"]["add"] == 3, plan
    r = await c.post(
        f"/api/v1/plans/{plan['id']}/apply", json={"confirm": True, "first_sync": "keep"}
    )
    assert r.status_code == 202, r.text
    await app.jobs.wait(r.json()["id"])
    (report,) = await app.sync.reports("zorvex")
    return report


async def test_sync_remounts_and_verifies_every_item(env, monkeypatch, tmp_path):
    app, c = env
    _, calls = _fake_mounts(monkeypatch, tmp_path)
    await _disk_device(app, on_connect="manual")
    report = await _apply(app, c)
    assert report.status == "succeeded", report.message
    assert report.counts["verified"] == 3
    assert calls == ["mount", "unmount", "mount"], calls  # read back from the disk, not the cache


async def test_items_lost_on_flush_fail_the_sync(env, monkeypatch, tmp_path):
    """What the Classic showed: files were written, but after the flush the device does not hold them."""
    app, c = env

    def lose_one(target):
        next(target.rglob("*.mp3")).unlink()

    _fake_mounts(monkeypatch, tmp_path, on_unmount=lose_one)
    await _disk_device(app, on_connect="manual")
    report = await _apply(app, c)
    assert report.status == "failed"
    assert report.message.startswith("verification failed"), report.message
    assert "1 of 3 items are not on it" in report.message
    assert report.counts["verified"] == 2


async def test_unreadable_manifest_fails_the_sync(env, monkeypatch, tmp_path):
    app, c = env

    def lose_manifest(target):
        (target / ".syncmusic" / "manifest.jsonl").unlink()

    _fake_mounts(monkeypatch, tmp_path, on_unmount=lose_manifest)
    await _disk_device(app, on_connect="manual")
    report = await _apply(app, c)
    assert report.status == "failed"
    assert "manifest is unreadable" in report.message


async def test_verify_sample_checks_a_stable_tenth(env, monkeypatch, tmp_path):
    """`verify: sample` reads back a stable 10 % (at least one item) after the flush and re-mount."""
    from syncmusic.core.models import ItemRef, ManifestEntry
    from syncmusic.core.sync import verify_sample

    def lose_all(target):
        for f in target.rglob("*.mp3"):
            f.unlink()

    app, c = env
    _, calls = _fake_mounts(monkeypatch, tmp_path, on_unmount=lose_all)
    await _disk_device(app, on_connect="manual")
    r = await c.patch("/api/v1/devices/zorvex", json={"sync": {"verify": "sample"}})
    assert r.status_code == 200, r.text
    report = await _apply(app, c)
    assert report.status == "failed" and "1 of 1 items are not on it" in report.message, (
        report.message
    )
    assert calls == ["mount", "unmount", "mount"]

    entries = {
        f"l/{i}": ManifestEntry(
            ref=ItemRef(library_id="l", item_id=str(i)), device_ref="x", path="x", size=1
        )
        for i in range(25)
    }
    sample = verify_sample(entries)
    assert len(sample) == 3 and sample == verify_sample(dict(reversed(entries.items())))
    assert verify_sample({}) == {}


async def test_verify_off_skips_the_read_back(env, monkeypatch, tmp_path):
    def lose_all(target):
        for f in target.rglob("*.mp3"):
            f.unlink()

    app, c = env
    _, calls = _fake_mounts(monkeypatch, tmp_path, on_unmount=lose_all)
    await _disk_device(app, on_connect="manual")
    r = await c.patch("/api/v1/devices/zorvex", json={"sync": {"verify": "off"}})
    assert r.status_code == 200, r.text
    report = await _apply(app, c)
    assert report.status == "succeeded" and "verified" not in report.counts
    assert calls == ["mount"]  # no flush / re-mount
