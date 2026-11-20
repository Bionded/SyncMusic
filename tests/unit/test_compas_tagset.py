"""The Compás player's database built on the server for a Wi-Fi client: the generated set over every file the
client holds (outbox + files it already took), built once per card contents."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from syncmusic.core.models import DeviceConfig, Manifest, ManifestEntry, ManifestHeader
from syncmusic.devices.client import COMPAS_COMMIT, COMPAS_GEN_DIR, ClientDevice
from syncmusic.devices.HibyR1 import compas_sdtool as sd
from syncmusic.devices.HibyR1 import compas_tagset
from tests.contracts.harness import work_item

# info answers format 8, or generation 1 + the song count on a card with a database; scan writes generation 1
# (one file per audio file found outside the ignored folders) and logs the scanned paths
FAKE = r"""
case "$1" in
info)
  if [ -f "$2/.compas/tagcache.gen" ]; then
    n=$(cat "$2/.compas/database_idx.tcd.g1")
    [ -n "$BROKEN" ] && n=99
    echo "{\"generation\": 1, \"songs\": $n}"
  else
    echo '{"sdtool_version": "1", "build": "test", "database_format": 8}'
  fi ;;
scan)
  mkdir -p "$2/.compas"
  find "$2" -name '*.mp3' | sed "s#^$2/##" | sort > "$2/.found"
  for d in Podcasts Audiobooks; do
    [ -f "$2/$d/database.ignore" ] && sed -i "\#^$d/#d" "$2/.found"
  done
  cat "$2/.found" >> "$LOG"
  n=$(wc -l < "$2/.found")
  echo "$n" > "$2/.compas/database_idx.tcd.g1"
  echo x > "$2/.compas/database_0.tcd.g1"
  echo 1 > "$2/.compas/tagcache.gen"
  echo "{\"event\": \"done\", \"songs\": $n}" ;;
esac
"""


@pytest.fixture
def tool(tmp_path, monkeypatch):
    t = tmp_path / "compas-sdtool"
    t.write_text("#!/bin/sh\n" + FAKE)
    t.chmod(0o755)
    monkeypatch.setenv("LOG", str(tmp_path / "scans.log"))
    monkeypatch.setattr(sd, "_setting", str(t))
    return t


def _files(root: Path, *rels: str) -> dict[str, Path]:
    out = {}
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"audio " + rel.encode())
        os.utime(p, (1_700_000_001.5, 1_700_000_001.5))
        out[rel] = p
    return out


def test_a_set_is_built_once_per_card_contents(tmp_path, tool):
    files = _files(tmp_path / "src", "Music/A/1.mp3", "Music/A/2.mp3", "Podcasts/S/e.mp3")
    cache = tmp_path / "gen"
    sid, out, names = compas_tagset.build_cached(files, cache, tool)
    assert names == ["database_0.tcd.g1", "database_idx.tcd.g1"]
    # the podcast folder is kept out of the music library, as on the card
    assert (out / "database_idx.tcd.g1").read_text().strip() == "2"
    assert all(p.stat().st_mtime == 1_700_000_000 for p in files.values())  # even seconds
    scans = (tmp_path / "scans.log").read_text()
    again = compas_tagset.build_cached(files, cache, tool)
    assert again == (sid, out, names) and (tmp_path / "scans.log").read_text() == scans
    more = files | _files(tmp_path / "src", "Music/B/3.mp3")
    sid2, out2, _ = compas_tagset.build_cached(more, cache, tool)
    assert sid2 != sid and not out.exists() and out2.is_dir()


def test_a_set_that_does_not_load_is_not_offered(tmp_path, tool, monkeypatch):
    monkeypatch.setenv("BROKEN", "1")
    files = _files(tmp_path / "src", "Music/A/1.mp3")
    with pytest.raises(compas_tagset.TagsetError):
        compas_tagset.build_cached(files, tmp_path / "gen", tool)
    assert not any((tmp_path / "gen").iterdir())


async def test_client_with_the_compas_database_gets_a_set_over_every_file_it_holds(tmp_path, tool):
    cfg = DeviceConfig(
        id="c1", type="client", name="Player", settings={"player_database": "compas"}
    )
    dev = ClientDevice(cfg, tmp_path / "box", None)
    m = Manifest(header=ManifestHeader(device_id=dev.id))
    s = await dev.begin()
    refs = []
    for title in ("One", "Two"):
        item = work_item(tmp_path, title, size=1000)
        ref = await dev.put_track(s, item)
        refs.append(ref)
        m.items[item.track.ref.key()] = ManifestEntry(
            ref=item.track.ref, device_ref=ref.device_ref, path=ref.path, size=ref.size
        )
    await dev.commit(s, m)
    dev.delivered(refs[0].path)  # the client took one: it leaves the outbox but still counts
    assert not (dev.mount / refs[0].path).exists()
    gen = await asyncio.to_thread(dev.generated_files, m)  # as the session calls it
    assert gen is not None and gen.commit == COMPAS_COMMIT
    assert [f.path for f in gen.files] == [
        f"{COMPAS_GEN_DIR}/database_0.tcd.g1",
        f"{COMPAS_GEN_DIR}/database_idx.tcd.g1",
    ]
    assert (gen.files[1].source).read_text().strip() == "2"
    dev.deleted(refs[0].path)
    del m.items[next(iter(m.items))]
    gen2 = await asyncio.to_thread(dev.generated_files, m)
    assert gen2 is not None and gen2.set_id != gen.set_id


def test_plain_client_or_missing_tool_builds_nothing(tmp_path, monkeypatch):
    m = Manifest(header=ManifestHeader(device_id="c1"))
    plain = ClientDevice(DeviceConfig(id="c1", type="client", name="P"), tmp_path / "a", None)
    assert plain.generated_files(m) is None
    monkeypatch.setattr(sd, "_setting", str(tmp_path / "missing"))
    cfg = DeviceConfig(id="c2", type="client", name="P", settings={"player_database": "compas"})
    dev = ClientDevice(cfg, tmp_path / "b", None)
    p = dev.mount / "Music/x.mp3"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    m.items["k"] = ManifestEntry.model_validate(
        {
            "ref": {"library_id": "l", "item_id": "i"},
            "device_ref": "Music/x.mp3",
            "path": "Music/x.mp3",
            "size": 1,
        }
    )
    assert dev.generated_files(m) is None


@pytest.mark.skipif(
    not os.environ.get("SYNCMUSIC_TEST_SDTOOL"), reason="SYNCMUSIC_TEST_SDTOOL not set"
)
def test_real_tool_builds_a_loadable_set(tmp_path):
    samples = sorted(Path("tests/integration/abs-samples").rglob("*.mp3"))[:2]
    files = {}
    for i, s in enumerate(samples):
        dest = tmp_path / "src" / f"{i}.mp3"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, dest)
        files[f"Music/Тест/{i:02}.mp3"] = dest
    tool = Path(os.environ["SYNCMUSIC_TEST_SDTOOL"])
    _, out, names = compas_tagset.build_cached(files, tmp_path / "gen", tool)
    assert json.loads((out / "manifest.json").read_text())["songs"] == len(files)
    assert any(n.startswith("database_idx.tcd.g1") for n in names)


def _id3_file(path: Path, tag: int, audio: int) -> bytes:
    """An ID3v2 tag of `tag` bytes, then `audio` bytes of 'audio', then an ID3v1 tag."""
    n = tag - 10
    size = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    data = b"ID3\x04\x00\x00" + size + b"t" * n + os.urandom(audio) + b"TAG" + b"v" * 125
    path.write_bytes(data)
    return data


def test_tag_copy_keeps_tags_and_end_sparse(tmp_path):
    src = tmp_path / "a.mp3"
    data = _id3_file(src, 300_000, 4_000_000)
    os.utime(src, (1_700_000_000, 1_700_000_000))
    dest = tmp_path / "shadow" / "a.mp3"
    compas_tagset.tag_copy(src, dest)
    got = dest.read_bytes()
    head = 300_000 + compas_tagset.TAG_COPY_AUDIO
    tail = compas_tagset.TAG_COPY_TAIL
    assert len(got) == len(data) and dest.stat().st_mtime == 1_700_000_000
    assert got[:head] == data[:head] and got[-tail:] == data[-tail:]
    assert got[head:-tail] == bytes(len(data) - head - tail)
    assert dest.stat().st_blocks * 512 < 1_000_000  # a hole, not a copy


def test_tag_copy_of_mp4_keeps_every_box_but_the_media(tmp_path):
    def box(kind: bytes, body: bytes) -> bytes:
        return (8 + len(body)).to_bytes(4, "big") + kind + body

    moov = box(b"moov", b"m" * 50_000)
    data = box(b"ftyp", b"M4A " * 4) + box(b"mdat", os.urandom(3_000_000)) + moov
    src = tmp_path / "a.m4a"
    src.write_bytes(data)
    dest = tmp_path / "b.m4a"
    compas_tagset.tag_copy(src, dest)
    assert dest.read_bytes().endswith(moov) and dest.stat().st_size == len(data)


def test_shrink_turns_whole_kept_files_into_tag_copies(tmp_path):
    root = tmp_path / "dev.shadow"
    (root / "A").mkdir(parents=True)
    data = _id3_file(root / "A" / "a.mp3", 1000, 3_000_000)
    (root / "A" / "small.mp3").write_bytes(b"x" * 100)
    freed = compas_tagset.shrink_tag_copies(root)
    assert freed > 2_000_000
    assert (root / "A" / "a.mp3").read_bytes()[:1000] == data[:1000]
    assert (root / "A" / "small.mp3").read_bytes() == b"x" * 100
    assert compas_tagset.shrink_tag_copies(root) == 0  # already shrunk


async def test_a_delivered_file_is_kept_as_a_sparse_tag_copy(tmp_path):
    cfg = DeviceConfig(
        id="c2", type="client", name="Player", settings={"player_database": "compas"}
    )
    dev = ClientDevice(cfg, tmp_path / "box", None)
    s = await dev.begin()
    item = work_item(tmp_path, "Big", size=1000)
    ref = await dev.put_track(s, item)
    await dev.commit(s, Manifest(header=ManifestHeader(device_id=dev.id)))
    src = dev.mount / ref.path
    data = _id3_file(src, 2_000, 3_000_000)  # replace the outbox file with a big tagged one
    os.utime(src, (1_700_000_000, 1_700_000_000))
    dev.delivered(ref.path)
    kept = dev._shadow / ref.path
    st = kept.stat()
    assert st.st_size == len(data) and int(st.st_mtime) == 1_700_000_000
    assert kept.read_bytes()[:2_000] == data[:2_000]
    assert st.st_blocks * 512 < len(data) // 2  # not a whole copy (no hard link either)
    assert st.st_nlink == 1
