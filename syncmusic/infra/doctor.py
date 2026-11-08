# SPDX-License-Identifier: GPL-3.0-or-later
"""System check: what a new install needs, each with a one-line fix.

`local_checks()` runs without the app (the `syncmusic doctor` command) and inside it (Settings → Maintenance →
System check, which adds the library connections, USB devices and the Pi's temperature; facade.diagnostics).
Every check is a `diagnostics.check` dict plus `fix` (empty when there is nothing to do); state `ok` / `warning` /
`error` / `unknown`. The probes are parameters so the tests can fake them."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from syncmusic import __version__
from syncmusic.infra import diagnostics, ffmpeg, storage
from syncmusic.infra.blockdev import mount
from syncmusic.infra.config import BootstrapConfig

UDEV_CONTROL = Path("/run/udev/control")
DOCKERENV = Path("/.dockerenv")
GIB = 1024**3


def item(id_: str, label: str, detail: str, state: str, fix: str = "") -> dict[str, Any]:
    return {**diagnostics.check(id_, label, detail, state), "fix": fix if state != "ok" else ""}


def check_ffmpeg(probe: Callable[[], dict[str, Any]] = ffmpeg.check) -> dict[str, Any]:
    ff = probe()
    if ff["error"]:
        return item(
            "ffmpeg",
            "ffmpeg",
            ff["error"],
            "error",
            "sudo apt install ffmpeg (or set Settings → Conversion)",
        )
    missing = ff["missing"]
    probe_exe = Path(ff["path"]).with_name("ffprobe") if os.sep in ff["path"] else None
    has_probe = probe_exe.is_file() if probe_exe else shutil.which("ffprobe") is not None
    detail = f"{ff['version']} · " + (
        f"missing {', '.join(missing)}" if missing else "all needed encoders"
    )
    if not has_probe:
        return item(
            "ffmpeg",
            "ffmpeg",
            f"{detail} · no ffprobe next to it",
            "error",
            "sudo apt install ffmpeg",
        )
    if missing:
        return item(
            "ffmpeg",
            "ffmpeg",
            detail,
            "warning",
            "install a full ffmpeg build (Debian's ffmpeg package)",
        )
    return item("ffmpeg", "ffmpeg", detail, "ok")


def check_space(
    data_dir: Path,
    cache_dir: Path,
    warn_gb: float | None,
    usage: Callable[[str], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    parts, state = [], "ok"
    for label, path in (("data", data_dir), ("cache", cache_dir)):
        existing = next((p for p in (path, *path.parents) if p.exists()), Path("/"))
        free = usage(str(existing)).free / GIB
        parts.append(f"{label} {free:.1f} GB free")
        if warn_gb is not None and free < warn_gb:
            state = "warning"
    fix = (
        f"free space below {warn_gb:g} GB: delete files, or move --cache-dir to a bigger disk"
        if warn_gb
        else ""
    )
    return item("space", "Free disk space", " · ".join(parts), state, fix)


def check_usage(cfg: BootstrapConfig) -> dict[str, Any]:
    """How much SyncMusic itself takes on disk: its data (database, backups, logs) and its cache."""
    data, _ = storage.tree_size([cfg.data_dir])
    cache, _ = storage.tree_size([cfg.cache_dir])
    detail = f"data {data / GIB:.2f} GB · cache {cache / GIB:.2f} GB (details: Settings → Storage & resources)"
    return item("usage", "Space used by SyncMusic", detail, "ok")


def check_dirs(cfg: BootstrapConfig) -> dict[str, Any]:
    """The data, cache and log dirs must be writable (or creatable) by this user."""
    bad = []
    for label, path in (("data", cfg.data_dir), ("cache", cfg.cache_dir), ("logs", cfg.logs)):
        existing = next((p for p in (path, *path.parents) if p.exists()), None)
        if existing is None or not os.access(existing, os.W_OK | os.X_OK):
            bad.append(f"{label} {path}")
    if bad:
        return item(
            "dirs",
            "Folders",
            "not writable: " + ", ".join(bad),
            "error",
            f"sudo chown -R {_user()} <folder>, or choose other folders with --data-dir / --cache-dir / --log-dir",
        )
    return item("dirs", "Folders", f"data {cfg.data_dir} · cache {cfg.cache_dir}", "ok")


def check_privileges(
    euid: Callable[[], int] = os.geteuid,
    route: Callable[[], list[str]] = mount.privilege_route,
    sudo_ok: Callable[..., bool] = mount._sudo_ok,
) -> dict[str, Any]:
    """Mounting and formatting USB devices needs root, passwordless sudo or the root helper."""
    if euid() == 0:
        return item("privileges", "Device access", "running as root", "ok")
    r = route()
    if len(r) == 3:
        return item("privileges", "Device access", "through the root helper syncmusic-priv", "ok")
    if sudo_ok("true"):
        return item("privileges", "Device access", "through passwordless sudo", "ok")
    return item(
        "privileges",
        "Device access",
        "not root, no passwordless sudo, no root helper: devices are listed but cannot be mounted",
        "error",
        f"sudo syncmusic --install --user {_user()} (or run SyncMusic as a service)",
    )


def check_udev(cfg: BootstrapConfig, control: Path = UDEV_CONTROL) -> dict[str, Any]:
    if not cfg.detect_devices:
        return item("udev", "Hot plug", "device detection is off (detect_devices = false)", "ok")
    if control.exists():
        return item("udev", "Hot plug", "udev is running", "ok")
    return item(
        "udev",
        "Hot plug",
        "udev is not reachable: plugged-in devices are only seen at start-up",
        "warning",
        "sudo apt install udev (in Docker: the usb variant mounts /run/udev)",
    )


def check_port(cfg: BootstrapConfig, running_url: str | None) -> dict[str, Any]:
    """Only for the command line: is the configured port free, or used by SyncMusic itself? `running_url` is the
    URL that answered (it may be another port than the configured one)."""
    if running_url:
        return item("port", "Port", f"SyncMusic answers at {running_url}", "ok")
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((cfg.host or "0.0.0.0", cfg.port))
        except OSError as e:
            return item(
                "port",
                "Port",
                f"{cfg.host}:{cfg.port} is in use by another program ({e.strerror})",
                "error",
                "choose another port with --port",
            )
    return item("port", "Port", f"{cfg.host}:{cfg.port} is free (SyncMusic is not running)", "ok")


def check_clock(run: Callable[[list[str]], str | None] | None = None) -> dict[str, Any]:
    """The system clock should be synchronised: play times and iPod clocks come from it."""
    out = (run or _run)(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if out is None:
        return item("clock", "Clock", "timedatectl not available", "unknown")
    if out.strip() == "yes":
        return item("clock", "Clock", "synchronised (NTP)", "ok")
    return item("clock", "Clock", "not synchronised", "warning", "sudo timedatectl set-ntp true")


def check_docker(
    cfg: BootstrapConfig, dockerenv: Path = DOCKERENV, dev: Path = Path("/dev")
) -> dict[str, Any] | None:
    """In a container: the usb variant needs the host's /dev (privileged); None outside Docker."""
    if not dockerenv.exists():
        return None
    if not cfg.detect_devices:
        return item("docker", "Docker", "network variant (no USB devices)", "ok")
    disks = any(dev.glob("sd*"))
    if disks:
        return item("docker", "Docker", "usb variant with the host's /dev", "ok")
    return item(
        "docker",
        "Docker",
        "device detection is on, but the container sees no disks",
        "warning",
        "use the usb profile of packaging/docker/compose.yaml (privileged, /dev mounted)",
    )


def versions() -> dict[str, Any]:
    def v(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "missing"

    detail = (
        f"SyncMusic {__version__} · Python {platform.python_version()} · pygpod {v('pygpod')} · "
        f"mutagen {v('mutagen')} · {platform.system()} {platform.machine()}"
    )
    return item("versions", "Versions", detail, "ok")


def local_checks(cfg: BootstrapConfig, warn_gb: float | None = 5.0) -> list[dict[str, Any]]:
    checks = [
        versions(),
        check_ffmpeg(),
        check_space(cfg.data_dir, cfg.cache_dir, warn_gb),
        check_usage(cfg),
        check_dirs(cfg),
        check_privileges(),
        check_udev(cfg),
        check_clock(),
    ]
    docker = check_docker(cfg)
    return checks + ([docker] if docker else [])


def _run(cmd: list[str]) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _user() -> str:
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USER", "<user>")
