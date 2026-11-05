"""Acceptance: the allowlisted iPod mini 2G with Rockbox (CF mod) formatted with a name and
synced through the real stack from the local Navidrome (test media, read-only), incremental sync, then the
feedback readers on the device's real tagcache/logs and on synthetic logs written to it.

Only the mini (matched by USB serial from tests/hardware.toml) is touched: its FAT32 data partition is reformatted(the device may be wiped). The partition table and the firmware partition are checked unchanged by
sha256, `/.rockbox` file by file. The synthetic logs are removed again (they would be counted as real plays by a
later reverse sync) and a playback.log the player had before is written back byte for byte. The synced music stays on the mini for a playback check; it is unmounted, not
ejected.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import subprocess
from pathlib import Path

import pytest

from syncmusic.app import Application
from syncmusic.base.device import DeviceSession
from syncmusic.core.models import ActionKind, LibraryConfig
from syncmusic.core.sync import RegisterRequest
from syncmusic.devices.Rockbox import rockbox_feedback as rf
from syncmusic.devices.Rockbox.rockbox import FORMAT_DROP
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.identify import find_disk_of, scan_block_devices
from tests.factories import own_rules
from tests.integration.services import media_listing
from tests.integration.test_navidrome_local import ENV

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no local Navidrome in .env.test"),
]

NAME = "Домашній mini"
PLAYLIST = "Усе підряд"
ALL_MUSIC = {
    "rules": [
        {
            "id": "music",
            "name": "All music",
            "criteria": {"is": {"kind": "music"}},
            "as_playlist": PLAYLIST,
        }
    ]
}
LESS_MUSIC = {
    "rules": [
        {
            "id": "music",
            "name": "Some music",
            "criteria": {"is": {"kind": "music"}},
            "limit": 5,
            "sort": "title",
            "as_playlist": PLAYLIST,
        }
    ]
}


def _data_listing() -> list[str]:
    return media_listing()


def _sha_of(dev_path: str, count: int | None = None) -> str:
    """sha256 of a block device (or its first `count` 512-byte sectors), read with sudo dd."""
    cmd = ["sudo", "-n", "dd", f"if={dev_path}", "bs=512", "status=none"]
    if count is not None:
        cmd.append(f"count={count}")
    return hashlib.sha256(subprocess.run(cmd, capture_output=True, check=True).stdout).hexdigest()


def _kept(files: dict[str, str]) -> dict[str, str]:
    """`/.rockbox` files a format restores: all but the database and the play logs (Rockbox.FORMAT_DROP)."""
    return {
        k: v
        for k, v in files.items()
        if not any(fnmatch.fnmatch(k.rsplit("/", 1)[-1], p) for p in FORMAT_DROP)
    }


def _rockbox_files(root: Path) -> dict[str, str]:
    rb = root / ".rockbox"
    return {
        p.relative_to(rb).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(rb.rglob("*"))
        if p.is_file()
    }


def _assert_rockbox(root: Path, expected: dict[str, str], what: str) -> None:
    """/.rockbox equals `expected`; on a mismatch name the files that were added, removed or changed (and show
    config.cfg), so an intermittent failure can be diagnosed from the run's output alone."""
    got = _rockbox_files(root)
    if got == expected:
        return
    changed = sorted(k for k in got.keys() & expected.keys() if got[k] != expected[k])
    cfg = root / ".rockbox" / "config.cfg"
    config = cfg.read_bytes().decode("utf-8", errors="replace") if cfg.is_file() else None
    raise AssertionError(
        f"{what}: added {sorted(got.keys() - expected.keys())}, removed {sorted(expected.keys() - got.keys())}, "
        f"changed {changed}; config.cfg now {config!r}"
    )


def _snapshot(root: Path) -> dict[str, object]:
    """/.rockbox hashes plus the real feedback sources (bytes) of the device before the test."""
    rb = root / ".rockbox"
    logs = {
        p.name: p.read_bytes() for p in [*rb.glob("playback*.log"), *root.glob(".scrobbler*.log")]
    }
    tc = {}
    if (rb / rf.MASTER_FILE).is_file():
        tc = {n: (rb / n).read_bytes() for n in (rf.MASTER_FILE, "database_4.tcd")}
    cfg = rb / "config.cfg"
    # bytes, not read_text(): universal newlines would turn a CRLF config.cfg (Rockbox writes CRLF) into LF
    config = cfg.read_bytes().decode("utf-8", errors="surrogateescape") if cfg.is_file() else None
    return {"files": _rockbox_files(root), "logs": logs, "tagcache": tc, "config": config}


def _after_sync(snap: dict[str, object]) -> dict[str, str]:
    """/.rockbox expected after a sync that followed the format: what the format kept (`_kept`), config.cfg with
    `play log: on` (default on)."""
    files = _kept(snap["files"])  # type: ignore[arg-type]
    config = snap["config"]
    assert isinstance(config, str), "no /.rockbox/config.cfg on the mini"
    new = rf.with_play_log_on(config)
    if new is not None:
        files["config.cfg"] = hashlib.sha256(
            new.encode("utf-8", errors="surrogateescape")
        ).hexdigest()
    return files


@pytest.fixture
async def app(config, mini_disk):
    mini = mini_disk
    a = Application(config)
    await a.start()
    await a.store.put_library(
        LibraryConfig(
            id="nd",
            type="navidrome",
            name="Navidrome",
            settings={
                "url": ENV["NAVIDROME_URL"],
                "username": ENV["NAVIDROME_USER"],
                "password": ENV["NAVIDROME_PASSWORD"],
            },
        )
    )
    await a._disk_added(mini)
    yield a, mini
    await a.stop()


async def _apply(a, plan_id, **kw):
    job = await a.sync.apply(plan_id, **kw)
    job = await a.jobs.wait(job.id)
    assert job.status == "succeeded", job.error
    rep = job.result
    assert rep["status"] == "succeeded" and not rep["failures"], [
        f["error"] for f in rep["failures"]
    ] or rep
    return rep


async def _check_files(a, device_id):
    """Every manifest entry is an MP3 (lossless converted) of its size; the playlist lists existing files."""
    dev = await a.sync.open_device(device_id)
    m = await dev.read_manifest()
    for e in m.items.values():
        assert (dev.mount / e.path).stat().st_size == e.size
        assert e.path.startswith("Music/") and e.path.endswith(".mp3"), e.path
        for side in e.sidecars:
            assert (dev.mount / side).is_file(), side
    pl = dev.mount / "Playlists" / f"{PLAYLIST}.m3u8"
    lines = [x for x in pl.read_text(encoding="utf-8").splitlines() if x and not x.startswith("#")]
    assert len(lines) == len(m.items)
    assert all((pl.parent / x).resolve().is_file() for x in lines)
    assert not list(dev.mount.rglob(".sdtmp-*"))
    return dev, m


async def test_rockbox_format_sync_incremental_feedback(app):
    # the real mini's CF card hangs on many small flushed writes: gentle write mode
    await rockbox_scenario(*app, gentle=True)


async def rockbox_scenario(a, mini, gentle: bool = False):
    """The acceptance scenario on one Rockbox player (real here, emulated in tests/integration/test_emulated_rockbox.py)."""
    before = _data_listing()
    data = next(p for p in mini.partitions() if p.fstype == "vfat")
    firmware = next(p for p in mini.partitions() if p.path != data.path)
    table, fw = _sha_of(mini.path, 1), _sha_of(firmware.path)
    snap = await mnt.probe_read(a.ctx.guard, data.path, a.ctx.config.mount_root, _snapshot)
    assert snap["files"], "no /.rockbox on the mini"

    cfg = await a.sync.register(
        RegisterRequest(
            name=NAME,
            disk=mini.path,
            type="rockbox",
            rule_set=await own_rules(a, NAME, ALL_MUSIC, ["nd"]),
            on_connect="manual",
            # "Rockbox: copy" is the default; this scenario checks the MP3 320 conversion
            processor_template_id="builtin-rockbox",
        )
    )
    assert cfg.identity.usb_serial == mini.serial and cfg.identity.fs_uuid
    assert [p.type for p in cfg.processors][0] == "transcode"
    if gentle:
        sync = cfg.sync.model_copy(update={"gentle_write": True})
        cfg = await a.sync.update_device(cfg.model_copy(update={"sync": sync}))
    cfg = await a.sync.format(cfg.id, confirm=NAME)
    assert _sha_of(mini.path, 1) == table, "partition table changed"
    assert _sha_of(firmware.path) == fw, "firmware partition changed"
    dev = await a.sync.open_device(cfg.id)
    # the database and the play logs are not restored (FORMAT_DROP: their plays would be counted again)
    assert _rockbox_files(dev.mount) == _kept(snap["files"]), "/.rockbox not restored exactly"
    caps = dev.effective_capabilities()
    assert caps.has_rtc is True and not caps.supports_podcasts

    pc = await a.sync.plan(cfg.id)
    adds = pc.plan.count(ActionKind.ADD)
    assert adds >= 25, pc.plan.warnings
    rep = await _apply(a, pc.plan.id, first_sync="keep")
    dev, m = await _check_files(a, cfg.id)
    assert len(m.items) == rep["counts"]["add"] == adds
    assert all(e.last_playcount == 0 and e.last_rating is None for e in m.items.values())

    pc = await a.sync.plan(cfg.id)  # re-run: no-op
    assert {x.kind for x in pc.plan.actions} == {ActionKind.KEEP}
    assert (await _apply(a, pc.plan.id))["bytes_written"] == 0

    cfg = await a.sync.update_device(
        cfg.model_copy(update={"rule_set": await own_rules(a, cfg.name, LESS_MUSIC, ["nd"])})
    )
    pc = await a.sync.plan(cfg.id)
    assert pc.plan.count(ActionKind.REMOVE) == adds - 5 and pc.plan.count(ActionKind.ADD) == 0
    removed = [x.entry.path for x in pc.plan.actions if x.kind is ActionKind.REMOVE]
    await _apply(a, pc.plan.id, confirm=True)
    dev, m = await _check_files(a, cfg.id)
    assert len(m.items) == 5 and not any((dev.mount / p).exists() for p in removed)

    cfg = await a.sync.update_device(
        cfg.model_copy(update={"rule_set": await own_rules(a, cfg.name, ALL_MUSIC, ["nd"])})
    )
    pc = await a.sync.plan(cfg.id)
    assert pc.plan.count(ActionKind.ADD) == adds - 5 and pc.plan.count(ActionKind.REMOVE) == 0
    await _apply(a, pc.plan.id)
    dev, m = await _check_files(a, cfg.id)
    assert len(m.items) == adds
    _assert_rockbox(dev.mount, _after_sync(snap), "/.rockbox changed beyond play log: on")
    assert rf.with_play_log_on((dev.mount / ".rockbox" / "config.cfg").read_text()) is None

    # --- feedback: the device's real sources (from before the format) ---------------------------
    now = dt.datetime.now(dt.UTC)
    if snap["tagcache"]:
        tc = snap["tagcache"]
        entries = rf.parse_tagcache(tc[rf.MASTER_FILE], tc["database_4.tcd"])
        assert entries and all(e.filename.startswith("/") for e in entries)
        fb = rf.tagcache_feedback(entries, m, caps.path_rules, now=now, device_id=cfg.id)
        assert fb.plays == [] or all(p.played_at_estimated for p in fb.plays)
    for name, raw in snap["logs"].items():
        text = raw.decode("utf-8", errors="replace")
        if name.startswith("playback"):
            rf.parse_playback_log(text)
        else:
            rf.parse_scrobbler_log(text)

    # --- feedback: synthetic logs written to the device ------------------------------------------
    entries3 = sorted(m.items.values(), key=lambda e: e.path)[:3]
    # the player clock is local wall time
    wall = dt.datetime.now().replace(microsecond=0) - dt.timedelta(hours=1)
    stamp = int((wall - dt.datetime(1970, 1, 1)).total_seconds())
    lines = ["# Started Ver. 4.0 synthetic (SyncMusic hardware test)"]
    for i, e in enumerate(entries3):
        path = e.path.upper() if i == 0 else e.path  # FAT: case-insensitive match
        lines.append(f"{stamp + 600 * i}:200000:240000:/{path}")
    lines.append(f"{stamp + 3000}:5000:240000:/{entries3[0].path}")  # a skip
    lines.append(f"{stamp + 3600}:250000:250000:/Music/Not managed.mp3")
    rb = dev.mount / ".rockbox"
    (rb / "playback.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        fb = await dev.read_feedback(m)
        assert [p.ref for p in fb.plays] == [e.ref for e in entries3]
        assert fb.unmapped == 1
        expected = wall.astimezone().astimezone(dt.UTC)
        assert fb.plays[0].played_at == expected and not fb.plays[0].played_at_estimated
        await dev.reset_feedback(DeviceSession(m), fb)
        assert not (rb / "playback.log").exists()
        rotated = list(rb.glob("playback.log.syncmusic-*"))
        assert len(rotated) == 1  # rename works on the mini's FAT32
        assert (await dev.read_feedback(m)).plays == []
    finally:
        for p in [rb / "playback.log", *rb.glob("*.syncmusic-*")]:
            p.unlink(missing_ok=True)
        real = snap["logs"].get("playback.log")  # type: ignore[attr-defined]
        if real is not None:  # the player's own log: put it back byte for byte
            (rb / "playback.log").write_bytes(real)
    _assert_rockbox(dev.mount, _after_sync(snap), "/.rockbox changed")
    assert _data_listing() == before, "the test media changed"


def _config_bytes(mount) -> bytes:
    return (mount / ".rockbox" / "config.cfg").read_bytes()


def _without_play_log(raw: bytes) -> list[bytes]:
    return [x for x in raw.splitlines() if not x.strip().lower().startswith(b"play log")]


async def test_rockbox_play_log_switch_and_rename(app):
    # runs after the acceptance test above in this module: it works on the music that test leaves on the mini
    # the real mini's CF card hangs on many small flushed writes: gentle write mode
    await play_log_rename_scenario(*app, gentle=True)


async def play_log_rename_scenario(a, mini, gentle: bool = False):
    """Play log switch and Rename on a Rockbox player (also emulated, tests/integration/test_emulated_rockbox.py)."""
    before = _data_listing()
    data = next(p for p in mini.partitions() if p.fstype == "vfat")
    firmware = next(p for p in mini.partitions() if p.path != data.path)
    table, fw = _sha_of(mini.path, 1), _sha_of(firmware.path)

    cfg = await a.sync.register(
        RegisterRequest(
            name=NAME,
            disk=mini.path,
            type="rockbox",
            rule_set=await own_rules(a, NAME, ALL_MUSIC, ["nd"]),
            on_connect="manual",
        )
    )
    if gentle:
        sync = cfg.sync.model_copy(update={"gentle_write": True})
        cfg = await a.sync.update_device(cfg.model_copy(update={"sync": sync}))
    dev = await a.sync.open_device(cfg.id)
    rockbox = {k: v for k, v in _rockbox_files(dev.mount).items() if k != "config.cfg"}
    files = sorted(p.relative_to(dev.mount).as_posix() for p in dev.mount.rglob("*") if p.is_file())
    raw = _config_bytes(dev.mount)
    jobs = len(a.jobs.jobs())

    state = await a.sync.play_log(cfg.id)
    assert state["state"] == rf.play_log_state(raw.decode("utf-8", errors="replace"))

    # --- 231: off, then on, written to the real config.cfg at once -------------------------------
    for on in (False, True):
        res = await a.sync.set_play_log(cfg.id, on)
        assert res["applied"] is True and res["setting"] is on
        assert res["state"] == ("on" if on else "off")
        dev = await a.sync.open_device(cfg.id)
        now = _config_bytes(dev.mount)
        want = b"play log: on" if on else b"play log: off"
        assert want in now.lower(), now[-200:]
        assert _without_play_log(now) == _without_play_log(raw), "other config.cfg lines changed"
        assert (b"\r\n" in now) == (b"\r\n" in raw), "line endings changed"
        assert (await a.sync.play_log(cfg.id))["state"] == ("on" if on else "off")

    # --- 230: Rename on the device (FAT label) and back ------------------------------------------
    new_name = "Домашній mini 2"
    preview = await a.sync.rename_preview(cfg.id, new_name)
    assert preview["fs"] == "vfat" and preview["label"] == "MINI 2", preview
    cfg, label = await a.sync.rename(cfg.id, new_name)
    assert cfg.name == new_name and label == "MINI 2"
    part = a.sync.detector.by_device(cfg.id).data_partition
    assert find_disk_of(part, scan_block_devices(False))[1].label == "MINI 2"
    dev = await a.sync.open_device(cfg.id)  # mounted again
    assert (
        sorted(p.relative_to(dev.mount).as_posix() for p in dev.mount.rglob("*") if p.is_file())
        == files
    )
    cfg, label = await a.sync.rename(cfg.id, NAME)
    assert cfg.name == NAME and label == "MINI"  # FAT: upper-case ASCII only
    assert find_disk_of(part, scan_block_devices(False))[1].label == "MINI"

    dev = await a.sync.open_device(cfg.id)
    after = {k: v for k, v in _rockbox_files(dev.mount).items() if k != "config.cfg"}
    assert after == rockbox, "/.rockbox changed beyond config.cfg"
    assert b"play log: on" in _config_bytes(dev.mount).lower()
    assert len(a.jobs.jobs()) == jobs, "a device action ran a job"
    assert _sha_of(mini.path, 1) == table, "partition table changed"
    assert _sha_of(firmware.path) == fw, "firmware partition changed"
    assert _data_listing() == before, "the test media changed"
