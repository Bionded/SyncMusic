# SPDX-License-Identifier: GPL-3.0-or-later
"""`syncmusic --install` / `--uninstall`: a systemd service for this install, run as root.

Works for the single-file binary (`SYNCMUSIC_BINARY` is set by its launcher) and for a venv install:
- the binary is copied to `<root>/usr/local/bin/syncmusic` and unpacked once into the root-owned
  `<root>/opt/syncmusic/runtime/` (the launcher prefers it), so the root helper a `--user` may run is not writable
  by that user; a venv install runs from where it is;
- the unit `<root>/etc/systemd/system/syncmusic.service` starts `syncmusic run` with the given path options;
- the config file (default `/etc/syncmusic/config.toml`) is written with the commented defaults if missing, the
  options given to `--install` uncommented in it (so `syncmusic status` & co. find the service's port and paths);
- the data, cache and log dirs are created;
- `--user NAME` adds a sudoers drop-in that allows NAME exactly the root helper `syncmusic-priv`.

`root` is `/` in production; tests pass a temp dir (then systemctl is not called). Uninstall keeps data, cache, logs
and the config unless `purge`; it only deletes paths with "syncmusic" in their name.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from syncmusic.infra.config import DEFAULT_CONFIG_PATH, BootstrapConfig, default_config_text

UNIT_NAME = "syncmusic"
BIN = Path("/usr/local/bin/syncmusic")
RUNTIME = Path("/opt/syncmusic/runtime")  # matches packaging/launcher.in
UNIT_DIR = Path("/etc/systemd/system")
SUDOERS_DIR = Path("/etc/sudoers.d")
HELPER = "syncmusic-priv"

Echo = Callable[[str], None]


class InstallError(Exception):
    pass


def _under(root: Path, path: Path) -> Path:
    return root / path.relative_to("/") if path.is_absolute() else root / path


def _systemctl(root: Path, *args: str) -> None:
    if root == Path("/"):
        subprocess.run(["systemctl", *args], check=False)


def _unit_text(exec_start: list[str], env: dict[str, str]) -> str:
    lines = [
        "[Unit]",
        "Description=SyncMusic",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        f"ExecStart={shlex.join(exec_start)}",
        "Restart=on-failure",
        "User=root",
        *(f"Environment={k}={v}" for k, v in env.items()),
        "# graceful stop: the running sync stops after the current file and writes manifest + device DB first",
        "TimeoutStopSec=300",
        "# hardening that leaves mounting, mkfs and the device files alone",
        "PrivateTmp=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "UMask=0077",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(mode)
    tmp.replace(path)


def _binary_id(binary: Path) -> str:
    """The runtime id in the launcher header (`ID=<version>-<mode>-<arch>-<sha12>`, packaging/launcher.in)."""
    with open(binary, "rb") as f:
        for _ in range(30):
            m = re.fullmatch(rb"ID=([\w.+-]+)\n", f.readline())
            if m:
                return m.group(1).decode()
    raise InstallError(f"{binary} is not a SyncMusic binary (no ID= line)")


def _binary_id_or_none(binary: Path) -> str | None:
    try:
        return _binary_id(binary)
    except (OSError, InstallError):
        return None


def prune_runtimes(runtime: Path, keep: set[str]) -> int:
    """Remove unpacked runtimes not named in `keep` (the new and the replaced version stay)."""
    if not runtime.is_dir():
        return 0
    removed = 0
    for d in runtime.iterdir():
        if d.is_dir() and d.name not in keep and not d.name.startswith("."):
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def _program(root: Path, echo: Echo) -> tuple[list[str], Path]:
    """The command that starts SyncMusic after the install and the root helper it uses."""
    binary = os.environ.get("SYNCMUSIC_BINARY")
    if not binary:  # a venv install: run from here
        return [sys.executable, "-m", "syncmusic"], Path(sys.executable).parent / HELPER
    target = _under(root, BIN)
    target.parent.mkdir(parents=True, exist_ok=True)
    previous_id = _binary_id_or_none(target)
    if Path(binary).resolve() != target.resolve():
        shutil.copyfile(binary, target.with_name(target.name + ".tmp"))
        target.with_name(target.name + ".tmp").chmod(0o755)
        target.with_name(target.name + ".tmp").replace(target)
        echo(f"Binary copied to {target}")
    # unpack into the root-owned runtime dir now (the launcher prefers it over ~/.cache)
    runtime = _under(root, RUNTIME)
    r = subprocess.run(
        [str(target), "--help"],
        env={**os.environ, "SYNCMUSIC_RUNTIME_DIR": str(runtime)},
        capture_output=True,
        text=True,
        check=False,
    )
    runtime_id = _binary_id(target)
    if r.returncode != 0 or not (runtime / runtime_id / ".complete").exists():
        raise InstallError(f"could not unpack {target} into {runtime}: {r.stderr.strip()}")
    removed = prune_runtimes(runtime, {runtime_id, previous_id} - {None})
    if removed:
        echo(f"Removed {removed} old runtime(s) from {runtime}")
    return [str(BIN)], RUNTIME / runtime_id / "python" / "bin" / HELPER


def _unit_env(cfg: BootstrapConfig, program: list[str]) -> dict[str, str]:
    env = {"SYNCMUSIC_LOG_LEVEL": cfg.log_level, "MALLOC_ARENA_MAX": "2"}
    if program == [
        str(BIN)
    ]:  # the single-file binary as a service: the update check offers its download
        env["SYNCMUSIC_INSTALL_TYPE"] = "service"
    return env


def install(
    cfg: BootstrapConfig,
    config_path: Path | None,
    run_args: list[str],
    users: list[str],
    echo: Echo,
    root: Path = Path("/"),
) -> None:
    """Install (or update) the service. `run_args`: the path options for `syncmusic run` (already validated)."""
    for u in users:
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", u):
            raise InstallError(f"unusual user name: {u}")
        if root == Path("/"):
            try:
                import pwd

                pwd.getpwnam(u)
            except KeyError:
                raise InstallError(f"no such user: {u}") from None
    program, helper = _program(root, echo)
    config = config_path or DEFAULT_CONFIG_PATH
    conf = _under(root, config.absolute())
    if not conf.exists():
        given = {
            name: getattr(cfg, name)
            for name in BootstrapConfig.model_fields
            if f"--{name.replace('_', '-')}" in run_args
        }
        _write(conf, default_config_text(given), 0o600)
        echo(
            f"Config written to {config} (commented defaults{', ' + ', '.join(given) if given else ''})"
        )
    for d in (cfg.data_dir, cfg.cache_dir, cfg.logs):
        _under(root, d).mkdir(mode=0o700, parents=True, exist_ok=True)
    exec_start = [*program, "run", "--config", str(config.absolute()), *run_args]
    unit = _under(root, UNIT_DIR / f"{UNIT_NAME}.service")
    _write(
        unit,
        _unit_text(exec_start, _unit_env(cfg, program)),
        0o644,
    )
    echo(f"Unit written to {UNIT_DIR / f'{UNIT_NAME}.service'}")
    for u in users:
        rule = _under(root, SUDOERS_DIR / f"syncmusic-{u}")
        text = (
            f"# SyncMusic root helper for {u} (syncmusic --install --user)\n"
            f"{u} ALL=(root) NOPASSWD: {helper}\n"
        )
        _write(rule, text, 0o440)
        checked = root == Path("/") and shutil.which("visudo")
        if checked and subprocess.run(["visudo", "-cqf", str(rule)], check=False).returncode:
            rule.unlink()
            raise InstallError(f"visudo rejected the rule for {u}")
        echo(f"User {u} may now use devices through {helper} ({SUDOERS_DIR / f'syncmusic-{u}'})")
    _systemctl(root, "daemon-reload")
    echo(f"Installed. Start with: systemctl enable --now {UNIT_NAME}")


def _remove(path: Path, echo: Echo) -> None:
    if "syncmusic" not in str(path):  # never a shared dir like /var/lib or /etc
        echo(f"kept {path} (not a SyncMusic path)")
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()
    else:
        return
    echo(f"removed {path}")


def uninstall(
    cfg: BootstrapConfig,
    config_path: Path | None,
    purge: bool,
    echo: Echo,
    root: Path = Path("/"),
) -> None:
    """Stop and remove the service, the installed binary, its runtime and the sudoers drop-ins; data stays unless
    `purge` (then data, cache, logs and the config go too)."""
    _systemctl(root, "disable", "--now", UNIT_NAME)
    paths = [UNIT_DIR / f"{UNIT_NAME}.service", BIN, RUNTIME.parent]
    sudoers = _under(root, SUDOERS_DIR)
    if sudoers.is_dir():
        paths += [SUDOERS_DIR / p.name for p in sudoers.glob("syncmusic-*")]
    if purge:
        config = (config_path or DEFAULT_CONFIG_PATH).absolute()
        paths += [cfg.data_dir, cfg.cache_dir, cfg.logs, config]
        if config.parent.name == "syncmusic":
            paths.append(config.parent)
    for p in paths:
        _remove(_under(root, p), echo)
    _systemctl(root, "daemon-reload")
    if not purge:
        echo(f"Data kept in {cfg.data_dir} (and the config); --purge removes them too")
