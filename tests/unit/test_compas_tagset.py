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
