"""Emulated devices for tests: loop images that look like a USB devices.

`attach(kind, ...)` creates a sparse image under /var/tmp/syncmusic-emulated, writes the `<image>.device.json`
sidecar with the USB identity of `kind` from tests/emulated.toml (scan_block_devices reads it for loop disks), and
attaches it with `losetup --partscan`. On exit everything mounted from it is unmounted, the loop device detached
and the image deleted. The builders lay out what the real device has:

- `flash(fs)`: one partition with vfat/exfat/ext4/ntfs (a Zorvex-like flash drive);
- `ipod_classic()`: DOS table, one FAT32 data partition, iPod_Control + empty database (pygpod);
- `ipod_mini()`: DOS table, type-0 firmware partition (random bytes) + FAT32 data partition, iPod_Control;
- `rockbox()`: the mini layout with /.rockbox (rockbox-info.txt, a codec, config.cfg) on the data partition.

Only these loop devices are ever written: the guard (`emulated_guard()`) accepts a loop device only when its
serial is in tests/emulated.toml, and no real disk carries those serials. Needs passwordless sudo (`needs_sudo`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from syncmusic.app import Application
from syncmusic.core.models import LibraryConfig
from syncmusic.infra.blockdev import format as fmt
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.infra.blockdev.guard import Guard
from syncmusic.infra.blockdev.identify import BlockDeviceInfo, scan_block_devices
from syncmusic.infra.config import BootstrapConfig, load_config

EMULATED_TOML = Path(__file__).parent / "emulated.toml"
IMAGE_ROOT = Path("/var/tmp/syncmusic-emulated")
IDENTITIES: dict[str, dict[str, str]] = {
    name: entry for name, entry in tomllib.loads(EMULATED_TOML.read_text()).items()
}
FW_SECTORS = 16384  # 8 MiB firmware partition at sector 2048 (the mini's is 39 MB)
ROCKBOX_INFO = (
    "Target: ipodmini2g\nTarget id: 21\nVersion: 4.0\n"
    "Features: albumart:dircache:rtc:swcodec:tagcache:tc_ramcache\n"
)
EROS_INFO = (
    "Target: aigoerosq\nTarget id: 107\nVersion: 4.0\n"
    "Features: albumart:dircache:lcd_bitmap:swcodec:tagcache:tc_ramcache\n"
)


def _sudo_ok() -> bool:
    try:
        return (
            subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10).returncode == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_sudo = pytest.mark.skipif(not _sudo_ok(), reason="needs passwordless sudo for loop devices")


def sh(*cmd: str, input: str | None = None) -> str:
    return subprocess.run(
        ["sudo", "-n", *cmd], input=input, capture_output=True, text=True, check=True
    ).stdout.strip()


def serial_of(kind: str) -> str:
    """The USB serial of an emulated `kind` on this xdist worker: tests on several workers at once never show two
    disks with one serial (code that finds a device by its serial would take the other worker's). gw0 and a
    single process have the kind's own serial; worker N the N-th of its `usb_serial_aliases` in emulated.toml."""
    ident = IDENTITIES[kind]
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw")
    index = int(worker) if worker.isdigit() else 0
    aliases = ident.get("usb_serial_aliases", [])
    return ident["usb_serial"] if index == 0 or not aliases else aliases[(index - 1) % len(aliases)]


def emulated_guard() -> Guard:
    return Guard(EMULATED_TOML, allow_loop=True)


@dataclass
class EmulatedDisk:
    kind: str  # entry in tests/emulated.toml
    dev: str  # /dev/loopN ("" while unplugged)
    image: Path

    @property
    def serial(self) -> str:
        return serial_of(self.kind)

    def part(self, n: int) -> str:
        return f"{self.dev}p{n}"


def _unmount_all(dev: str) -> None:
    out = subprocess.run(
        ["lsblk", "-rn", "-o", "MOUNTPOINTS", dev], capture_output=True, text=True
    ).stdout
    subprocess.run(["sync"], check=False)
    for target in out.split():
        subprocess.run(["sudo", "-n", "umount", target], check=False, capture_output=True)


@contextmanager
def attach(
    kind: str,
    size_mb: int = 128,
    *,
    layout: str | None = None,
    sector_size: int | None = None,
    image_dir: Path | None = None,
) -> Iterator[EmulatedDisk]:
    """A loop device with the identity of `kind`; `layout` is an sfdisk script for the image. `image_dir` puts the
    sparse image elsewhere (e.g. on a small tmpfs: when that is full, the device fails writes with EIO)."""
    ident = IDENTITIES[kind]
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    detach_stale()
    workdir = Path(tempfile.mkdtemp(prefix=f"{kind}-", dir=image_dir or IMAGE_ROOT))
    image = workdir / "disk.img"
    with open(image, "wb") as f:
        f.truncate(size_mb * 1024**2)
    if layout is not None:
        sfdisk = [
            "sfdisk",
            "-q",
            *(["--sector-size", str(sector_size)] if sector_size else []),
            str(image),
        ]
        subprocess.run(sfdisk, input=layout, text=True, check=True, capture_output=True)
    sidecar = {
        "serial": serial_of(kind),
        "usb_vendor_id": ident.get("usb_vendor_id"),
        "usb_product_id": ident.get("usb_product_id"),
        "model": f"Emulated {kind}",
    }
    Path(f"{image}.device.json").write_text(json.dumps(sidecar))
    losetup = ["losetup", "--find", "--show", "--partscan"]
    if sector_size:
        losetup += ["--sector-size", str(sector_size)]
    disk = EmulatedDisk(kind, sh(*losetup, str(image)), image)
    try:
        sh("udevadm", "settle", "--timeout=10")
        yield disk
    finally:
        if disk.dev:
            unplug(disk)
        shutil.rmtree(workdir, ignore_errors=True)


STALE_S = 3 * 3600
STALE_DIR_S = 12 * 3600


def detach_stale() -> None:
    """Detach loop devices left by a killed run: their image under IMAGE_ROOT is gone or untouched for
    STALE_S. Newer images may belong to a parallel worker and stay attached."""
    out = subprocess.run(["losetup", "-J"], capture_output=True, text=True).stdout
    for d in json.loads(out or "{}").get("loopdevices", []):
        back = (d.get("back-file") or "").removesuffix(" (deleted)")
        if not back.startswith(str(IMAGE_ROOT)):
            continue
        try:
            fresh = time.time() - Path(back).stat().st_mtime < STALE_S
        except OSError:
            fresh = False
        if not fresh:
            _unmount_all(d["name"])
            subprocess.run(
                ["sudo", "-n", "losetup", "-d", d["name"]], check=False, capture_output=True
            )
    remove_stale_dirs()


def remove_stale_dirs() -> None:
    """Delete image, cache and small-store dirs a killed run left under IMAGE_ROOT (they pile up to many GB):
    untouched for STALE_DIR_S, not a mount point, no attached loop device on an image in them."""
    attached = subprocess.run(["losetup", "-J"], capture_output=True, text=True).stdout
    for entry in IMAGE_ROOT.glob("*"):
        try:
            if not entry.is_dir() or os.path.ismount(entry) or str(entry) + "/" in attached:
                continue
            newest = max([entry.stat().st_mtime, *(c.stat().st_mtime for c in entry.iterdir())])
        except OSError:
            continue
        if time.time() - newest > STALE_DIR_S:
            subprocess.run(
                ["sudo", "-n", "rm", "-rf", str(entry)], check=False, capture_output=True
            )


def unplug(disk: EmulatedDisk) -> None:
    """Pull the emulated device out: unmount what is left and detach the loop device (the image stays)."""
    _unmount_all(disk.dev)
    subprocess.run(["sudo", "-n", "losetup", "-d", disk.dev], check=False, capture_output=True)
    sh("udevadm", "settle", "--timeout=10")
    disk.dev = ""


def replug(disk: EmulatedDisk) -> None:
    """Plug the same image in again (it may get another loop device)."""
    disk.dev = sh("losetup", "--find", "--show", "--partscan", str(disk.image))
    sh("udevadm", "settle", "--timeout=10")


@contextmanager
def plain_loop(image: Path, size_mb: int = 16) -> Iterator[str]:
    """A loop device WITHOUT an identity sidecar (the guard must refuse it)."""
    with open(image, "wb") as f:
        f.truncate(size_mb * 1024**2)
    dev = sh("losetup", "--find", "--show", str(image))
    try:
        yield dev
    finally:
        subprocess.run(["sudo", "-n", "losetup", "-d", dev], check=False, capture_output=True)


@contextmanager
def small_backing_store(size_mb: int) -> Iterator[Path]:
    """A tmpfs of `size_mb` for images: an emulated device on it gets real I/O errors once it is full
    (dm-flakey is not in this kernel). `grow(path, mb)` makes room again."""
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="small-store-", dir=IMAGE_ROOT))
    sh(
        "mount",
        "-t",
        "tmpfs",
        "-o",
        f"size={size_mb}m,uid={os.getuid()},gid={os.getgid()},mode=0700",
        "sd-emulated",
        str(path),
    )
    try:
        yield path
    finally:
        subprocess.run(["sudo", "-n", "umount", str(path)], check=False, capture_output=True)
        shutil.rmtree(path, ignore_errors=True)


def grow(store: Path, size_mb: int) -> None:
    sh("mount", "-o", f"remount,size={size_mb}m", str(store))


def repair_vfat(part: str) -> None:
    """`fsck.vfat -a`: exit 0 = clean, 1 = errors corrected (files cut off by a failing storage)."""
    r = subprocess.run(["sudo", "-n", "fsck.vfat", "-a", part], capture_output=True, text=True)
    assert r.returncode in (0, 1), r.stdout + r.stderr


# --- builders: put the real device's layout on an attached disk -------------------------------------------------


async def flash(disk: EmulatedDisk, fs: str = "vfat", label: str = "FLASH") -> str:
    """One partition with `fs`; returns the partition path."""
    return await fmt.format_disk(emulated_guard(), disk.dev, fs, label)


MINI_LAYOUT = (
    f"label: dos\nstart=2048, size={FW_SECTORS}, type=0\nstart={2048 + FW_SECTORS}, type=b\n"
)
CLASSIC_LAYOUT = "label: dos\nstart=2048, type=b\n"


def write_firmware(disk: EmulatedDisk) -> None:
    """Recognisable bytes in the mini's firmware partition (tests compare them after a format)."""
    with open(disk.image, "r+b") as f:
        f.seek(2048 * 512)
        f.write(os.urandom(FW_SECTORS * 512))


async def _populate(part: str, mount_root: Path, fill: object) -> None:
    m = await mnt.mount(emulated_guard(), part, "build", mount_root)
    try:
        fill(m.path)  # type: ignore[operator]
    finally:
        await mnt.unmount(m)


async def ipod(disk: EmulatedDisk, mount_root: Path, name: str = "Emulated iPod") -> str:
    """A stock iPod (classic: one data partition; mini: firmware + data); returns the data partition."""
    from pygpod import init_ipod

    mini = disk.kind == "emulated_ipod_mini"
    data = disk.part(2 if mini else 1)
    if mini:
        write_firmware(disk)
    sh("mkfs.vfat", "-F", "32", "-n", "IPOD", data)
    sh("udevadm", "settle", "--timeout=10")
    model = "xA204" if mini else "xC297"
    sysinfo = f"ModelNumStr: {model}\nFirewireGuid: 0x{disk.serial}\n".encode()
    await _populate(
        data, mount_root, lambda p: init_ipod(str(p), name=name, sysinfo_files={"SysInfo": sysinfo})
    )
    return data


def _rockbox_tree(root: Path, info: str = ROCKBOX_INFO) -> None:
    rb = root / ".rockbox"
    (rb / "codecs").mkdir(parents=True)
    (rb / "rockbox-info.txt").write_text(info)
    (rb / "codecs" / "mpa.codec").write_bytes(b"codec")
    # Rockbox writes config.cfg with CRLF line endings
    lines = [
        "# .cfg file created by rockbox 4.0 - http://www.rockbox.org",
        "volume: -25",
        "play log: off",
    ]
    (rb / "config.cfg").write_bytes(("\r\n".join(lines) + "\r\n").encode())


async def rockbox(disk: EmulatedDisk, mount_root: Path) -> str:
    """A Rockbox player; returns the data partition. The mini (attach with MINI_LAYOUT): firmware partition + FAT32
    data with /.rockbox. `emulated_rockbox_eros` (attach with CLASSIC_LAYOUT): a non-Apple player with one FAT32
    partition."""
    if disk.kind == "emulated_rockbox_eros":
        data, label, info = disk.part(1), "EROS Q", EROS_INFO
    else:
        write_firmware(disk)
        data, label, info = disk.part(2), "IPOD", ROCKBOX_INFO
    sh("mkfs.vfat", "-F", "32", "-n", label, data)
    sh("udevadm", "settle", "--timeout=10")
    await _populate(data, mount_root, lambda root: _rockbox_tree(root, info))
    return data


# --- the application on emulated devices ------------------------------------------------------------------------


def emulated_config(tmp_path: Path, cache_dir: Path) -> BootstrapConfig:
    """Like the `config` fixture, with loop devices allowed and tests/emulated.toml as the allowlist; the cache (work
    files of whole albums and podcasts) on `cache_dir`, not on the small /tmp tmpfs."""
    # never the config of a SyncMusic installed on this host
    return load_config(
        env={"SYNCMUSIC_CONFIG": os.devnull},
        overrides={
            "data_dir": tmp_path / "data",
            "cache_dir": cache_dir,
            "mount_root": tmp_path / "mnt",
            "device_allowlist": EMULATED_TOML,
            "allow_loop_devices": True,
            "mode": "daemon",
            "detect_devices": False,
        },
    )


def navidrome_config(env: dict[str, str]) -> LibraryConfig:
    """The compose stack's Navidrome (tests/integration/services.py) as library `nd`."""
    settings = {
        "url": env["NAVIDROME_URL"],
        "username": env["NAVIDROME_USER"],
        "password": env["NAVIDROME_PASSWORD"],
        # one Navidrome serves every xdist worker: a download can wait longer than the default 30 s
        "timeout_s": 180.0,
    }
    return LibraryConfig(id="nd", type="navidrome", name="Navidrome", settings=settings)


def abs_config(env: dict[str, str]) -> LibraryConfig:
    """The compose stack's Audiobookshelf as library `abs`."""
    settings = {"url": env["ABS_URL"], "api_token": env["ABS_TOKEN"]}
    return LibraryConfig(id="abs", type="audiobookshelf", name="Audiobookshelf", settings=settings)


def block_device(disk: EmulatedDisk) -> BlockDeviceInfo:
    """The emulated disk as the application's scan sees it (with its USB identity)."""
    return next(d for d in scan_block_devices() if d.path == disk.dev)


@asynccontextmanager
async def running_app(
    tmp_path: Path, disks: list[EmulatedDisk], libraries: list[LibraryConfig]
) -> AsyncIterator[tuple[Application, list[BlockDeviceInfo]]]:
    """An Application on emulated devices with `libraries`, told that `disks` were plugged in. It starts without
    the udev watcher (it never sees the real disks); device detection is switched on afterwards, so unknown disks
    get their read-only probe mount like in a running daemon."""
    cache = Path(tempfile.mkdtemp(prefix="cache-", dir=IMAGE_ROOT))
    app = Application(emulated_config(tmp_path, cache))
    await app.start()
    app.config.detect_devices = True
    try:
        for lib in libraries:
            await app.store.put_library(lib)
        blks = [block_device(d) for d in disks]
        for blk in blks:
            await app._disk_added(blk)
        yield app, blks
    finally:
        await app.stop()
        shutil.rmtree(cache, ignore_errors=True)
