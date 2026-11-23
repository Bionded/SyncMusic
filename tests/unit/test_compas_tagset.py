"""The Compás player's database built on the server for a Wi-Fi client: the generated set over every file the
client holds (outbox + files it already took), built once per card contents."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import struct
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


def test_the_installed_set_uploaded_again_is_not_rebuilt(tmp_path, tool):
    files = _files(tmp_path / "src", "Music/A/1.mp3", "Music/A/2.mp3")
    cache = tmp_path / "gen"
    sid, out, names = compas_tagset.build_cached(files, cache, tool)
    card = tmp_path / "card" / ".compas"
    card.mkdir(parents=True)
    for n in names:
        shutil.copyfile(out / n, card / n)
    (card / "tagcache.gen").write_text("1")
    scans = (tmp_path / "scans.log").read_text()
    assert compas_tagset.build_cached(files, cache, tool, card) == (sid, out, names)
    assert (tmp_path / "scans.log").read_text() == scans
    # the player wrote into its database (plays), or the files changed: built again
    more = files | _files(tmp_path / "src", "Music/B/3.mp3")
    assert compas_tagset._delivered_unchanged(more, cache, card, None, None) is None
    (card / "database_0.tcd.g1").write_text("played")
    assert compas_tagset._delivered_unchanged(files, cache, card, None, None) is None


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


def test_a_set_over_part_of_the_card_is_not_offered(tmp_path, tool, caplog):
    """The server holds copies of only the last sync's files (the rest came over USB): no set, or the player's
    complete library would be replaced by one listing only those files."""
    cfg = DeviceConfig(id="c3", type="client", name="P", settings={"player_database": "compas"})
    dev = ClientDevice(cfg, tmp_path / "box", None)
    m = Manifest(header=ManifestHeader(device_id=dev.id))
    for i in range(40):
        rel = f"Music/A/{i:02}.mp3"
        if i < 3:
            _files(dev.mount, rel)
        m.items[f"k{i}"] = ManifestEntry.model_validate(
            {
                "ref": {"library_id": "l", "item_id": f"i{i}"},
                "device_ref": rel,
                "path": rel,
                "size": 1,
            }
        )
    assert dev.generated_files(m) is None
    assert "knows only 3 of the 40 songs" in caplog.text


REAL_TOOL = os.environ.get("COMPAS_SDTOOL") or shutil.which("compas-sdtool")


@pytest.mark.skipif(not REAL_TOOL, reason="set COMPAS_SDTOOL to a built compas-sdtool")
def test_real_tool_builds_incrementally_over_the_cards_database(tmp_path):
    """Card indexed over USB with 2 songs; the server holds none of them, only the one new song: the set built over
    the card's database lists all 3 and reads only the new file."""
    tool = Path(REAL_TOOL)
    samples = sorted(Path("tests/integration/abs-samples").rglob("*.mp3"))
    assert len(samples) >= 3
    card = tmp_path / "card"
    rels = [f"Music/Тест/{i:02}.mp3" for i in range(3)]
    for rel, s in zip(rels[:2], samples, strict=False):
        (card / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, card / rel)
        compas_tagset.even_mtime(card / rel)
    (card / ".compas").mkdir()
    asyncio.run(sd.scan(tool, card))
    new = tmp_path / "src" / "new.mp3"
    new.parent.mkdir()
    shutil.copy2(samples[2], new)
    rows = {e.path: e for e in compas_tagset.cf.read_tagcache(card) or []}
    stubs = {rel: (rows[rel].size, rows[rel].mtime) for rel in rels[:2]}
    _, out, names = compas_tagset.build_cached(
        {rels[2]: new}, tmp_path / "gen", tool, card / ".compas", stubs
    )
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["songs"] == 3 and manifest["generation"] == 2 and manifest["read"] == 1
    assert "tagcache.refs.g2" in names and "database_idx.tcd.g2" in names
    # a stand-in whose size does not match its row would be read as a song without tags: refused
    bad = {**stubs, rels[0]: (stubs[rels[0]][0] + 1, stubs[rels[0]][1])}
    with pytest.raises(compas_tagset.TagsetError, match="read 2 files"):
        compas_tagset.build_cached({rels[2]: new}, tmp_path / "gen2", tool, card / ".compas", bad)


@pytest.mark.skipif(
    not REAL_TOOL or not shutil.which("ffmpeg"), reason="needs compas-sdtool and ffmpeg"
)
@pytest.mark.parametrize("ext", ["flac", "mp3"])
def test_real_tool_reads_the_same_song_from_a_synthetic_stand_in(tmp_path, ext):
    """A stand-in made from the manifest's metadata alone gives the same library row as the real file: same
    strings, numbers and (within a second) length."""
    import subprocess

    from syncmusic.devices.HibyR1 import compas_feedback as cf

    meta = {
        "title": "Пісня",
        "artist": "Гурт",
        "album_artist": "Гурт",
        "album": "Альбом",
        "track_no": "3",
        "disc_no": "1",
        "year": "2011",
        "genres": "Rock",
    }
    real = tmp_path / "real" / "Music" / f"a.{ext}"
    real.parent.mkdir(parents=True)
    tags = {"title": "title", "artist": "artist", "album_artist": "album_artist", "album": "album"}
    args = [f"{v}={meta[k]}" for k, v in tags.items()]
    args += ["track=3", "disc=1", "date=2011", "genre=Rock"]
    cmd = ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=7.5:r=44100"]
    cmd += ["-ac", "2", *[x for a in args for x in ("-metadata", a)], str(real)]
    subprocess.run(cmd, check=True)
    os.utime(real, (1_700_000_000, 1_700_000_000))
    synth = tmp_path / "synth" / "Music" / f"a.{ext}"
    assert compas_tagset.synthetic(synth, meta, 7.5, real.stat().st_size, 1_700_000_000)
    tool = Path(REAL_TOOL)
    rows = []
    for card in (tmp_path / "real", tmp_path / "synth"):
        (card / ".compas").mkdir()
        asyncio.run(sd.scan(tool, card))
        db = card / ".compas"
        master = (db / "database_idx.tcd.g1").read_bytes()
        rows.append(struct.unpack_from("<24i", master, cf.MASTER_HEADER))
        tables = {p.name: p.read_bytes() for p in db.glob("database_[0-9]*.tcd.g1")}
        rows.append(tables)
    real_row, real_tables, synth_row, synth_tables = rows
    assert real_tables == synth_tables  # every string tag the same
    length = 14
    assert abs(real_row[length] - synth_row[length]) < 1000
    differ = {i for i, (a, b) in enumerate(zip(real_row, synth_row, strict=True)) if a != b}
    assert differ <= {length, 13, 19}  # length, bitrate (from it) and the scan's commit id


def _entries(m: Manifest, n: int, ext: str = "mp3") -> list[str]:
    rels = []
    for i in range(n):
        rel = f"Music/A/{i:02}.{ext}"
        m.items[f"k{i}"] = ManifestEntry.model_validate(
            {
                "ref": {"library_id": "l", "item_id": f"i{i}"},
                "device_ref": rel,
                "path": rel,
                "size": 500_000,
                "duration_s": 30.0,
                "meta": {"title": f"Song {i}", "artist": "Гурт", "album": "A"},
            }
        )
        rels.append(rel)
    return rels


def test_from_scratch_the_set_is_made_from_the_manifest_alone(tmp_path, tool, caplog):
    """No audio on the server, no card database: asked to rebuild, the server makes every song from what the
    manifest knows; formats it cannot make still count as missing."""
    caplog.set_level("INFO")
    cfg = DeviceConfig(id="c4", type="client", name="P", settings={"player_database": "compas"})
    dev = ClientDevice(cfg, tmp_path / "box", None)
    m = Manifest(header=ManifestHeader(device_id=dev.id))
    _entries(m, 20)
    assert dev.generated_files(m) is not None  # nothing better known: made from scratch
    dev.request_rebuild()  # 'Rebuild player database': once, at the next build
    gen = dev.generated_files(m)
    assert gen is not None and not dev.rebuild_requested
    assert (gen.files[1].source).read_text().strip() == "20"
    assert "from scratch (asked for)" in caplog.text
    _entries(m, 30, "opus")  # 30 more the server cannot make: too many missing
    m2 = Manifest(header=ManifestHeader(device_id=dev.id))
    _entries(m2, 30, "opus")
    assert dev.generated_files(m2, from_scratch=True) is None


@pytest.mark.skipif(
    not REAL_TOOL or not shutil.which("ffmpeg"), reason="needs compas-sdtool and ffmpeg"
)
def test_real_tool_rebuilds_from_scratch_when_the_card_database_does_not_match(
    tmp_path, caplog, monkeypatch
):
    """The indexer reads stand-ins of the card's rows anyway (here: rows read back with another size, as when the
    player's clock frame differs): they would become empty songs, so the server builds from scratch instead."""
    import dataclasses
    import subprocess

    from syncmusic.devices.HibyR1 import compas_feedback as cf

    caplog.set_level("INFO")
    sd.configure(REAL_TOOL)
    cfg = DeviceConfig(id="c5", type="client", name="P", settings={"player_database": "compas"})
    dev = ClientDevice(cfg, tmp_path / "box", None)
    m = Manifest(header=ManifestHeader(device_id=dev.id))
    rels = _entries(m, 3)
    card = tmp_path / "card"
    for rel in rels:
        (card / rel).parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=2", str(card / rel)]
        subprocess.run(cmd, check=True)
    (card / ".compas").mkdir()
    asyncio.run(sd.scan(tool := Path(REAL_TOOL), card))
    assert tool
    db = card / ".compas"
    dev.store_card_database(
        {p.name: p.read_bytes() for p in db.iterdir() if compas_tagset.TCD_FILE.match(p.name)}
    )
    real_read = cf.read_tagcache
    monkeypatch.setattr(
        cf,
        "read_tagcache",
        lambda mount: [dataclasses.replace(e, size=e.size + 1) for e in real_read(mount) or []],
    )
    try:
        gen = dev.generated_files(m)
    finally:
        sd.configure(None)
    assert gen is not None and "does not match its files" in caplog.text
    manifest = json.loads((gen.files[0].source.parent / "manifest.json").read_text())
    assert manifest["songs"] == 3 and manifest["generation"] == 1
