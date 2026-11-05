# SPDX-License-Identifier: GPL-3.0-or-later
"""Bootstrap configuration: TOML file + environment variables (env overrides file)."""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from syncmusic import _build

Mode = Literal["web", "desktop", "daemon"]

DEFAULT_CONFIG_PATH = Path("/etc/syncmusic/config.toml")
ENV_PREFIX = "SYNCMUSIC_"

log = logging.getLogger(__name__)


def env_value(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """`SYNCMUSIC_<name>`; empty counts as unset."""
    env = os.environ if env is None else env
    return env.get(ENV_PREFIX + name) or None


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _default_dirs() -> dict[str, Path]:
    """Default data and cache dir."""
    if is_root():
        data, cache = Path("/var/lib"), Path("/var/cache")
    else:
        data = Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share"))
        cache = Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache"))
    return {"data_dir": data / "syncmusic", "cache_dir": cache / "syncmusic"}


class BootstrapConfig(BaseModel):
    mode: Mode = _build.DEFAULT_MODE  # type: ignore[assignment]
    host: str = "0.0.0.0"
    port: int = 8765
    # Host names the web UI may be reached by besides IP addresses, localhost and this machine's name (DNS
    # rebinding guard); ["*"] turns the check off.
    allowed_hosts: list[str] = []
    data_dir: Path
    cache_dir: Path
    mount_root: Path = Path("/run/syncmusic")
    log_dir: Path | None = None  # None: <data_dir>/logs
    log_level: str = "INFO"
    log_format: Literal["kv", "json"] = "kv"
    # Path to a hardware allowlist (tests/hardware.toml). When set, the blockdev guard refuses
    # every disk that is not listed in it.
    device_allowlist: Path | None = None
    # GitHub repository (owner/name) whose releases the update check reads, or the http(s) URL of a releases
    # list in GitHub's shape (a mirror); "none" or empty: no checks
    update_repo: str = "Bionded/SyncMusic"
    # Allow loop devices (virtual device images in tests). Never enabled in production.
    allow_loop_devices: bool = False
    # Accept device directories (`mount_path`) that are not on a removable disk. Tests only.
    allow_directory_devices: bool = False
    # Follow udev for USB devices (startup scan + hot plug). Off in unit tests.
    detect_devices: bool = True

    @property
    def logs(self) -> Path:
        """The log directory: `log_dir`, else `<data_dir>/logs`."""
        return self.log_dir or self.data_dir / "logs"


_ENV_KEYS: dict[str, str] = {
    "mode": "SYNCMUSIC_MODE",
    "host": "SYNCMUSIC_HOST",
    "port": "SYNCMUSIC_PORT",
    "allowed_hosts": "SYNCMUSIC_ALLOWED_HOSTS",
    "data_dir": "SYNCMUSIC_DATA_DIR",
    "cache_dir": "SYNCMUSIC_CACHE_DIR",
    "mount_root": "SYNCMUSIC_MOUNT_ROOT",
    "log_dir": "SYNCMUSIC_LOG_DIR",
    "log_level": "SYNCMUSIC_LOG_LEVEL",
    "log_format": "SYNCMUSIC_LOG_FORMAT",
    "device_allowlist": "SYNCMUSIC_DEVICE_ALLOWLIST",
    "allow_loop_devices": "SYNCMUSIC_ALLOW_LOOP_DEVICES",
    "detect_devices": "SYNCMUSIC_DETECT_DEVICES",
    "update_repo": "SYNCMUSIC_UPDATE_REPO",
    "allow_directory_devices": "SYNCMUSIC_ALLOW_DIRECTORY_DEVICES",
}


def load_config(
    path: Path | None = None,
    env: dict[str, str] | None = None,
    overrides: dict[str, object] | None = None,
) -> BootstrapConfig:
    """Load bootstrap config. Precedence: overrides (CLI) > env > file > defaults."""
    env = dict(os.environ) if env is None else env
    values: dict[str, object] = {}
    env_cfg = env_value("CONFIG", env)
    cfg_path = path or (Path(env_cfg) if env_cfg else None)
    if cfg_path is None:
        cfg_path = DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.is_file() else None
    if cfg_path is not None:
        with open(cfg_path, "rb") as f:
            values.update(tomllib.load(f))
    for key, env_key in _ENV_KEYS.items():
        val = env_value(env_key.removeprefix(ENV_PREFIX), env)
        if val:
            values[key] = (
                [v.strip() for v in val.split(",") if v.strip()] if key == "allowed_hosts" else val
            )
    for key, val in (overrides or {}).items():
        if val is not None:
            values[key] = val
    for key, default in _default_dirs().items():
        values.setdefault(key, default)
    values.setdefault("log_dir", Path(str(values["data_dir"])) / "logs")
    return BootstrapConfig.model_validate(values)


# one line per bootstrap key for `syncmusic config init`; a test checks every field has one
EXPLAIN: dict[str, str] = {
    "mode": "web (server + browser UI), desktop (own window) or daemon (no UI)",
    "host": "address the web server listens on (0.0.0.0 = every interface)",
    "port": "port of the web UI and the API",
    "allowed_hosts": 'extra host names for the web UI (IP addresses, localhost and this machine\'s name always work; "*" = any)',
    "data_dir": "database, settings and logs (default: /var/lib/syncmusic as root, else ~/.local/share/syncmusic)",
    "cache_dir": "processing cache: converted files and downloads (default: /var/cache/syncmusic or ~/.cache/syncmusic)",
    "mount_root": "where USB devices are mounted",
    "log_dir": "log files (default: <data_dir>/logs)",
    "log_level": "DEBUG, INFO or WARNING",
    "log_format": "kv (readable key=value lines) or json",
    "device_allowlist": "a TOML allowlist of devices; when set, only listed disks are touched (unset: every USB disk)",
    "allow_loop_devices": "accept loop devices (emulated test devices only)",
    "allow_directory_devices": "accept device folders that are not on a removable disk (tests only)",
    "detect_devices": "follow udev for USB devices (start-up scan + hot plug)",
    "update_repo": 'GitHub repository (owner/name) checked daily for new releases, or the URL of a releases list in GitHub\'s shape (a mirror); "none": no checks',
}


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def default_config_text(values: dict[str, Any] | None = None) -> str:
    """A commented default configuration: every key with its default (commented out) and what it does. Keys in
    `values` are written uncommented with that value (`--install --port 8080`, so the CLI finds the service)."""
    values = values or {}
    defaults = _default_dirs()
    lines = [
        "# SyncMusic configuration (bootstrap keys; everything else is set in the web UI under Settings).",
        "# Precedence: command-line options > SYNCMUSIC_<KEY> environment variables > this file > defaults.",
        "# Every key below shows its default; uncomment a line to change it.",
    ]
    for name, field in BootstrapConfig.model_fields.items():
        value = defaults.get(name, field.default)
        lines.append("")
        lines.append(f"# {EXPLAIN[name]}")
        if name in values:
            lines.append(f"{name} = {_toml_value(values[name])}")
            continue
        if name == "log_dir":
            shown = _toml_value(defaults["data_dir"] / "logs")
        elif value is None:
            shown = '"/etc/syncmusic/hardware.toml"  (no default: unset)'
        else:
            shown = _toml_value(value)
        lines.append(f"# {name} = {shown}")
    return "\n".join(lines) + "\n"
