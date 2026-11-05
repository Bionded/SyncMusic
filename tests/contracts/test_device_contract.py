"""DeviceContract, run against a virtual device per implementation."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from syncmusic.base.device import Device
from syncmusic.core.models import Manifest, ManifestEntry, ManifestHeader


def _manifest(dev: Device, refs) -> Manifest:
    items = {
        item.track.ref.key(): ManifestEntry(
            ref=item.track.ref, device_ref=r.device_ref, path=r.path, size=r.size
        )
        for item, r in refs
    }
    return Manifest(header=ManifestHeader(device_id=dev.id), items=items)


async def _sync(dev: Device, tmp_path, titles):
    s = await dev.begin()
    refs = []
    for title, artist in titles:
        item = dev._harness.make_item(tmp_path, title, artist)  # type: ignore[attr-defined]
        refs.append((item, await dev.put_track(s, item)))
    manifest = _manifest(dev, refs)
    await dev.commit(s, manifest)
    return manifest, refs


@pytest.fixture
def device(device_case, tmp_path):
    name, h, expected = device_case
    dev = h.make(tmp_path)
    if expected is not None:
        assert isinstance(dev, expected)
    assert dev.type_name == name.split("-")[0]  # "android-libmtp": a second backend of one type
    dev._harness = h  # type: ignore[attr-defined]
    return dev


def test_probe_recognises_own_fixture_only(device, tmp_path):
    cls = type(device)
    if cls.network:  # registered with its settings, never detected
        assert cls.probe(None, device.mount) is None
        return
    res = cls.probe(None, device.mount)
    assert res is not None and res.type_name == cls.type_name
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cls.probe(None, empty) is None


async def test_put_commit_roundtrip(device, tmp_path):
    assert await device.read_manifest() is None
    manifest, refs = await _sync(device, tmp_path, [("One", "A"), ("Two", "B")])
    read = await device.read_manifest()
    assert read is not None and read.items.keys() == manifest.items.keys()
    assert read.header.device_id == device.id
    on_device = {i.device_ref for i in (await device.read_contents()).items}
    assert {r.device_ref for _, r in refs} <= on_device


async def test_manifest_written_last(device, tmp_path):
    first, _ = await _sync(device, tmp_path, [("One", "A")])
    s = await device.begin()
    item = device._harness.make_item(tmp_path, "Two", "B")
    await device.put_track(s, item)
    # nothing committed yet: the manifest still describes the previous state
    still = await device.read_manifest()
    assert still is not None and still.items.keys() == first.items.keys()
    await device.abort(s)


async def test_commit_writes_manifest_last(device, tmp_path):
    """An interrupted commit must never leave a manifest describing files that are not there yet."""
    rel = device._harness.manifest_path
    if rel is None:
        pytest.skip(f"{device.type_name}: manifest is not a file on the mount")
    await _sync(device, tmp_path, [("One", "A"), ("Two", "B")])
    manifest_mtime = (device.mount / rel).stat().st_mtime_ns
    newer = [
        str(p.relative_to(device.mount))
        for p in device.mount.rglob("*")
        if p.is_file() and p.stat().st_mtime_ns > manifest_mtime
    ]
    assert not newer, f"written after the manifest: {newer}"


async def test_remove(device, tmp_path):
    manifest, refs = await _sync(device, tmp_path, [("One", "A"), ("Two", "B")])
    s = await device.begin()
    gone_item, gone_ref = refs[0]
    await device.remove_track(s, gone_ref)
    manifest.items.pop(gone_item.track.ref.key())
    await device.commit(s, manifest)
    on_device = {i.device_ref for i in (await device.read_contents()).items}
    assert gone_ref.device_ref not in on_device and refs[1][1].device_ref in on_device
    assert (await device.read_manifest()).items.keys() == manifest.items.keys()


async def test_free_space_decreases(device, tmp_path):
    cap = await device.capacity()
    # directory-backed devices share their filesystem (/tmp) with other tests and processes, which may free space
    # at the same moment (parallel workers): a write must show up in one of a few tries
    for attempt in range(3):
        before = await device.free_space()
        assert 0 < before <= cap
        await _sync(device, tmp_path, [(f"One {attempt}", "A")])
        if await device.free_space() < before:
            return
    pytest.fail("free space did not decrease after three writes")


async def test_path_rules_respected(device, tmp_path):
    rules = device.effective_capabilities().path_rules
    nasty = 'Bad: <name> "x" / y? * | trailing. ' + "Ж" * 300
    _, refs = await _sync(device, tmp_path, [(nasty, 'AC/DC: "live"?')])
    path = refs[0][1].path
    assert len(path) <= rules.max_path_len
    for part in PurePosixPath(path).parts:
        assert len(part) <= rules.max_name_len
        assert not set(part) & set(rules.forbidden_chars.replace("/", ""))
        assert part == part.rstrip(rules.strip_trailing) or not rules.strip_trailing


async def test_feedback_roundtrip(device, tmp_path):
    h = device._harness
    if h.inject_feedback is None:
        assert (await device.read_feedback(None)).is_empty()
        pytest.skip(f"{device.type_name}: no device feedback")
    manifest, _ = await _sync(device, tmp_path, [("One", "A"), ("Two", "B")])
    await h.inject_feedback(device, manifest)
    fb = await device.read_feedback(await device.read_manifest())
    assert not fb.is_empty()
    keys = {p.ref.key() for p in fb.plays} | set(fb.ratings) | set(fb.positions)
    assert keys <= manifest.items.keys()


async def test_rename_keeps_contents(device, tmp_path):
    """Rename writes only the device's own name (if it keeps one in its files); contents, manifest and
    recognition stay. The file system label a Rename asks for fits the file system."""
    manifest, _ = await _sync(device, tmp_path, [("One", "A")])
    before = {i.device_ref for i in (await device.read_contents()).items}
    await device.rename("Нова назва")
    read = await device.read_manifest()
    assert read is not None and read.items.keys() == manifest.items.keys()
    assert {i.device_ref for i in (await device.read_contents()).items} == before
    assert type(device).network or type(device).probe(None, device.mount) is not None
    for fs, limit in (("vfat", 11), ("exfat", 15), ("ext4", 16), ("ntfs", 32)):
        label = device.label_for("Нова назва для тесту", fs, "OLD")
        assert label is None or 0 < len(label.encode()) <= limit, (fs, label)
    assert device.label_for("x", "hfsplus", "OLD") is None


async def test_format(device, tmp_path):
    if not device._harness.can_format:
        pytest.skip(f"{device.type_name}: format not testable on its virtual device")
    await _sync(device, tmp_path, [("One", "A")])
    await device.format("Test")
    assert await device.read_manifest() is None
    assert not (await device.read_contents()).items
    assert type(device).probe(None, device.mount) is not None  # still recognised after format
