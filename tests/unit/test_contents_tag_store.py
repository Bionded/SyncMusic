"""Player contents of a file device after a restart reads no file again when the card is unchanged, and an
update sync with nothing to do leaves the manifest file alone."""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import Manifest, ManifestHeader
from syncmusic.devices import _filedevice
from syncmusic.devices.flashdrive import FlashDrive
from tests.media import make_music
from tests.unit.test_applier import _cfg


def _card(tmp_path, n: int = 30):
    mount = tmp_path / "card"
    first = make_music(mount / "Music" / "a" / "0.mp3", title="Пісня 0")
    for i in range(1, n):
        shutil.copy(first, mount / "Music" / "a" / f"{i}.mp3")
    return mount


def _device(mount, cache):
    return FlashDrive(_cfg(), mount, SimpleNamespace(cache_dir=cache))  # type: ignore[arg-type]


async def test_restart_with_an_unchanged_card_reads_no_tags(tmp_path, monkeypatch):
    mount, cache = _card(tmp_path), tmp_path / "cache"
    calls: list[str] = []
    real = _filedevice.read_tags

    def spy(f):
        calls.append(str(f))
        return real(f)

    monkeypatch.setattr(_filedevice, "read_tags", spy)
    first = await _device(mount, cache).read_contents()
    assert len(calls) == 30 and (cache / "device-tags" / "dev.json").is_file()

    _filedevice._TAG_CACHE.clear()  # a restart: nothing in memory
    calls.clear()
    again = await _device(mount, cache).read_contents()
    assert calls == [] and again == first

    (mount / "Music" / "a" / "3.mp3").unlink()
    st = os.stat(mount / "Music" / "a" / "4.mp3")
    os.utime(mount / "Music" / "a" / "4.mp3", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    _filedevice._TAG_CACHE.clear()
    third = await _device(mount, cache).read_contents()
    assert calls == [str(mount / "Music" / "a" / "4.mp3")] and len(third.items) == 29
    stored = _filedevice.load_tag_store(cache / "device-tags" / "dev.json")
    assert "Music/a/3.mp3" not in stored and len(stored) == 29


async def test_without_an_app_context_nothing_is_stored(tmp_path):
    mount = _card(tmp_path, n=2)
    contents = await FlashDrive(_cfg(), mount, None).read_contents()
    assert len(contents.items) == 2 and not (tmp_path / "cache").exists()


def test_broken_store_is_empty(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text("{nope")
    assert _filedevice.load_tag_store(bad) == {}


def test_manifest_with_only_a_new_time_is_not_rewritten(tmp_path):
    m = Manifest(header=ManifestHeader(device_id="dev"))
    manifest_io.write(tmp_path, m)
    path = tmp_path / manifest_io.MANIFEST_PATH
    before = path.stat().st_mtime_ns, path.read_bytes()
    os.utime(path, ns=(1, 1))
    m2 = manifest_io.read(tmp_path)
    assert m2 is not None
    m2.header.updated_at = m2.header.updated_at.replace(year=2030)
    manifest_io.write(tmp_path, m2)
    assert path.stat().st_mtime_ns == 1 and path.read_bytes() == before[1]
    m2.header.app_version = "9.9"
    manifest_io.write(tmp_path, m2)
    assert path.stat().st_mtime_ns != 1 and manifest_io.read(tmp_path).header.app_version == "9.9"  # type: ignore[union-attr]
