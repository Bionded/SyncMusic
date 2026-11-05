"""On-device manifest and inflight list."""

from __future__ import annotations

import json
import os

import pytest

from syncmusic.core import manifest as mf
from syncmusic.core.models import ItemRef, Manifest, ManifestEntry, ManifestHeader


def _manifest(n: int = 3) -> Manifest:
    items = {}
    for i in range(n):
        ref = ItemRef(library_id="nav", item_id=f"id{i}")
        path = f"Music/Тартак/Гранатовий альбом/{i:02d} - Орбіт без цукру.mp3"
        items[ref.key()] = ManifestEntry(
            ref=ref, device_ref=path, path=path, size=1000 + i, rules=["r1"], last_rating=4
        )
    return Manifest(
        header=ManifestHeader(device_id="dev1", app_version="0.1"), items=items, playlists=["Мій"]
    )


def test_round_trip_cyrillic(tmp_path):
    m = _manifest()
    mf.write(tmp_path, m)
    text = (tmp_path / mf.MANIFEST_PATH).read_text(encoding="utf-8")
    assert "Тартак" in text  # readable UTF-8, not \u escapes in the playlist line either
    assert "Мій" in text
    assert json.loads(text.splitlines()[0])["type"] == "header"
    got = mf.read(tmp_path)
    assert got == m


def test_no_manifest_is_none(tmp_path):
    assert mf.read(tmp_path) is None


def test_atomic_write_keeps_old_file_on_failure(tmp_path, monkeypatch):
    old = _manifest(1)
    mf.write(tmp_path, old)

    def boom(fd):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        mf.write(tmp_path, _manifest(5))
    monkeypatch.undo()
    assert mf.read(tmp_path) == old
    assert not list((tmp_path / mf.STATE_DIR).glob("*.tmp"))


def test_newer_version_raises_with_choices(tmp_path):
    m = _manifest(1)
    text = mf.dumps(m).replace('"manifest_version":1', '"manifest_version":99', 1)
    (tmp_path / mf.STATE_DIR).mkdir()
    (tmp_path / mf.MANIFEST_PATH).write_text(text)
    with pytest.raises(mf.ManifestTooNew) as e:
        mf.read(tmp_path)
    assert e.value.found == 99 and set(e.value.choices) == {"wipe", "try_anyway"}
    assert mf.read(tmp_path, allow_newer=True).items.keys() == m.items.keys()


def test_newer_version_with_unknown_fields_raises_too_new_even_if_trying(tmp_path):
    text = '{"type":"header","manifest_version":2,"device_id":"d"}\n{"type":"item","ref":42}\n'
    with pytest.raises(mf.ManifestTooNew):
        mf.loads(text, allow_newer=True)


def test_truncated_last_line_raises(tmp_path):
    text = mf.dumps(_manifest(3))
    with pytest.raises(mf.ManifestError, match="not valid JSON"):
        mf.loads(text[: len(text) - 40])


@pytest.mark.parametrize("text", ["", "\n", '{"type":"item"}\n', "[1]\n", '{"type":"header"}\n'])
def test_garbage_raises(text):
    with pytest.raises(mf.ManifestError):
        mf.loads(text)


def test_invalid_utf8_raises(tmp_path):
    (tmp_path / mf.STATE_DIR).mkdir()
    (tmp_path / mf.MANIFEST_PATH).write_bytes(b"\xff\xfe garbage")
    with pytest.raises(mf.ManifestError):
        mf.read(tmp_path)


def test_unknown_line_type_skipped():
    text = mf.dumps(_manifest(2)) + '{"type":"future_thing","x":1}\n'
    assert len(mf.loads(text).items) == 2


def test_inflight_add_clear_and_clean(tmp_path):
    assert mf.read_inflight(tmp_path) == []
    (tmp_path / "Music").mkdir()
    for name in ("a.mp3", "b.mp3", "kept.mp3"):
        (tmp_path / "Music" / name).write_bytes(b"x")
    mf.add_inflight(tmp_path, ["Music/a.mp3", "Music/kept.mp3"])
    mf.add_inflight(tmp_path, ["Music/b.mp3", "../outside.mp3", "Music/missing.mp3"])
    assert mf.read_inflight(tmp_path)[:3] == ["Music/a.mp3", "Music/kept.mp3", "Music/b.mp3"]
    (tmp_path.parent / "outside.mp3").write_bytes(b"x")
    ref = ItemRef(library_id="l", item_id="k")
    m = Manifest(
        header=ManifestHeader(device_id="d"),
        items={
            ref.key(): ManifestEntry(
                ref=ref, device_ref="Music/kept.mp3", path="Music/kept.mp3", size=1
            )
        },
    )
    removed = mf.clean_inflight(tmp_path, m)
    assert removed == ["Music/a.mp3", "Music/b.mp3"]
    assert (tmp_path / "Music" / "kept.mp3").exists()
    assert (tmp_path.parent / "outside.mp3").exists()  # never outside the device root
    assert mf.read_inflight(tmp_path) == []
    (tmp_path.parent / "outside.mp3").unlink()
