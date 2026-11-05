"""FLACs through the full processor chain.

FLAC → ALAC / AAC / MP3 outputs must decode cleanly (ffprobe + full ffmpeg decode), keep tags and the cover,
export the `.lrc`, and the planned size (Chain.plan) must be within ±5 % (ALAC) / ±10 % (lossy) of the result.
Works on a copy of the generated test media's Music (read-only); prints a size accuracy table.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from syncmusic.base.capabilities import CoverSpec, DeviceCapabilities
from syncmusic.core.models import LibraryConfig, ProcessorSlot, Track
from syncmusic.core.prepare import Chain, Preparer, initial_plan_item
from syncmusic.infra import ffmpeg
from syncmusic.infra.cache import ProcessingCache
from syncmusic.infra.db import Database
from syncmusic.libraries.folder import FolderLibrary
from tests.integration.services import media_root

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,  # ~1 min of encoding on the Pi
]
CAPS = DeviceCapabilities(formats={"mp3", "aac", "alac"}, cover=CoverSpec(max_size=500))
TARGETS: dict[str, tuple[dict[str, object], float | None]] = {  # target -> (rule, size tolerance)
    "alac": ({"from": ["flac"], "to": "alac"}, 0.05),
    "aac": ({"from": ["flac"], "to": "aac"}, 0.10),  # default 256 kbit/s
    "mp3": ({"from": ["flac"], "to": "mp3"}, 0.10),  # default CBR 320 kbit/s
    # VBR: per-track size depends on the content (measured -8.5 %..+11.4 %), so only the album total is
    # checked (VBR_TOTAL_TOLERANCE); the per-track errors are printed
    "mp3-v0": ({"from": ["flac"], "to": "mp3", "quality": {"vbr": 0}}, None),
}
VBR_TOTAL_TOLERANCE = 0.10
MIN_SECONDS = 30
PARALLEL = 3  # the Pi: keep heavy work at 3 parallel encodes


def _listing(root: Path) -> list[tuple[str, int, int]]:
    return sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*"))


async def _decodes_cleanly(path: Path) -> None:
    await ffmpeg.run_ffmpeg(["-xerror", "-i", ffmpeg.fpath(path), "-f", "null", "-"])


async def test_flac_through_the_full_processor_chain(tmp_path):
    music = media_root() / "Music"
    before = _listing(music)
    root = tmp_path / "Music"
    await asyncio.to_thread(shutil.copytree, music, root)
    lib = FolderLibrary(
        LibraryConfig(id="music", type="folder", name="Music", settings={"root": str(root)}), None
    )
    snap = await lib.snapshot()
    # tracks of song length only: in a 2 s file the tags and padding are a large part of the source size the
    # prediction starts from (the test media's long FLAC album)
    flacs = sorted(
        (
            t
            for t in snap.tracks.values()
            if t.format == "flac" and (t.duration_s or 0) >= MIN_SECONDS
        ),
        key=lambda t: t.path or "",
    )
    assert flacs, f"the test media has FLACs of {MIN_SECONDS} s or more"
    db = Database(tmp_path / "sd.db")
    await db.open()
    cache = ProcessingCache(db, tmp_path / "cache")
    prep = Preparer(cache, tmp_path / "work")
    sem = asyncio.Semaphore(PARALLEL)
    rows: list[tuple[str, str, int, int, float]] = []
    failures: list[str] = []

    async def one(target: str, track: Track) -> None:
        rule, tolerance = TARGETS[target]
        chain = Chain.from_slots(
            [
                ProcessorSlot(type="transcode", settings={"rules": [rule]}),
                ProcessorSlot(type="retag"),
                ProcessorSlot(type="covers", settings={"mode": "embed"}),
                ProcessorSlot(type="lyrics"),
            ]
        )
        item = initial_plan_item(track)
        planned = chain.plan(item, CAPS)
        src = await ffmpeg.probe(root / (track.path or ""))
        async with sem, prep.prepared(lib, item, chain, CAPS) as w:
            info = await ffmpeg.probe(w.path)
            await _decodes_cleanly(w.path)
            name = f"{target}: {track.path}"
            expected_codec = {"alac": "alac", "aac": "aac", "mp3": "mp3", "mp3-v0": "mp3"}[target]
            if info.codec != expected_codec:
                failures.append(f"{name}: codec {info.codec}")
            if abs((info.duration_s or 0) - (src.duration_s or 0)) > 0.5:
                failures.append(f"{name}: duration {info.duration_s} vs {src.duration_s}")
            for tag in ("title", "artist", "album"):
                if src.tags.get(tag) and info.tags.get(tag) != src.tags.get(tag):
                    failures.append(
                        f"{name}: tag {tag} {info.tags.get(tag)!r} != {src.tags[tag]!r}"
                    )
            if src.has_cover and not info.has_cover:
                failures.append(f"{name}: cover lost")
            lrc = (root / (track.path or "")).with_suffix(".lrc")
            if lrc.exists():
                got = w.sidecars.get(".lrc")
                original = lrc.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
                if got is None or got.read_text(encoding="utf-8").rstrip("\n") != original.rstrip(
                    "\n"
                ):
                    failures.append(f"{name}: .lrc not exported verbatim")
            actual = w.path.stat().st_size
            err = (planned.file.size - actual) / actual
            rows.append((target, Path(track.path or "").name, planned.file.size, actual, err))
            if tolerance is not None and abs(err) > tolerance:
                failures.append(
                    f"{name}: size predicted {planned.file.size}, actual {actual} ({err:+.1%})"
                )

    try:
        await asyncio.gather(*(one(t, tr) for t in TARGETS for tr in flacs))
    finally:
        await db.close()

    print("\nM3 size prediction accuracy (planned vs actual):")
    print(f"{'target':6} {'predicted':>11} {'actual':>11} {'error':>7}  file")
    for target, name, pred, actual, err in sorted(rows):
        print(f"{target:6} {pred:>11} {actual:>11} {err:>+7.1%}  {name}")
    for target in TARGETS:
        errs = [abs(r[4]) for r in rows if r[0] == target]
        print(
            f"{target}: n={len(errs)} max |error| {max(errs):.1%}, mean {sum(errs) / len(errs):.1%}"
        )
    vbr = [r for r in rows if r[0] == "mp3-v0"]
    total_err = sum(r[2] for r in vbr) / sum(r[3] for r in vbr) - 1
    print(f"mp3-v0 album total error {total_err:+.1%}")
    if abs(total_err) > VBR_TOTAL_TOLERANCE:
        failures.append(f"mp3-v0: total size error {total_err:+.1%}")
    assert _listing(music) == before, "the test media must stay untouched"
    assert not failures, "\n".join(failures)
