# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line: `syncmusic run` starts the app; every other command talks to it over REST."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from pydantic import ValidationError

from syncmusic.infra.config import BootstrapConfig, env_value, is_root, load_config
from syncmusic.infra.log import setup_logging

cli = typer.Typer(
    no_args_is_help=False, invoke_without_command=True, add_completion=False, help="SyncMusic"
)

DEFAULT_URL = env_value("URL") or "http://127.0.0.1:8765"
UrlOpt = Annotated[str, typer.Option("--url", help="URL of the running instance")]


def _auth() -> dict[str, str]:
    """`SYNCMUSIC_TOKEN`: an API token (Settings → System → Access) when the instance requires a login."""
    token = env_value("TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


# the start-up options shared by `syncmusic [OPTIONS]` and `syncmusic run` (CLI > env > TOML > defaults)
ModeOpt = Annotated[str | None, typer.Option(help="web | desktop | daemon")]
ConfigOpt = Annotated[Path | None, typer.Option(help="bootstrap TOML config")]
HostOpt = Annotated[str | None, typer.Option(help="address to listen on")]
PortOpt = Annotated[int | None, typer.Option(help="port of the web UI and API")]
DataOpt = Annotated[Path | None, typer.Option(help="database and settings")]
CacheOpt = Annotated[Path | None, typer.Option(help="processing cache")]
LogDirOpt = Annotated[Path | None, typer.Option(help="log files (default: <data dir>/logs)")]
MountOpt = Annotated[Path | None, typer.Option(help="where USB devices are mounted")]
PATH_KEYS = ("host", "port", "data_dir", "cache_dir", "log_dir", "mount_root")


def _load(config: Path | None, overrides: dict[str, Any]) -> BootstrapConfig:
    """The bootstrap config, or exit 1 with what is wrong."""
    try:
        return load_config(config, overrides=overrides)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']} (got {e.get('input')!r})"
            for e in exc.errors()
        )
        raise typer.Exit(_fail(f"invalid config: {problems}")) from None
    except (OSError, ValueError) as exc:  # unreadable or malformed TOML
        raise typer.Exit(_fail(f"invalid config: {exc}")) from None


def _run_args(overrides: dict[str, Any]) -> list[str]:
    """The given start-up options as `syncmusic run` arguments (for the daemon child and the systemd unit)."""
    args: list[str] = []
    for key, value in overrides.items():
        if value is not None:
            args += [
                f"--{key.replace('_', '-')}",
                str(Path(value).absolute()) if isinstance(value, Path) else str(value),
            ]
    return args


def _start(cfg: BootstrapConfig) -> None:
    setup_logging(cfg.log_level, cfg.log_format, cfg.logs)
    from syncmusic.infra import self_update
    from syncmusic.runner import run_app

    app = asyncio.run(run_app(cfg))
    if app.restart == "exit":  # the service: its unit starts the new binary
        raise SystemExit(self_update.RESTART_EXIT_CODE)
    binary = self_update.running_binary()
    if app.restart == "exec" and binary is not None:  # started by hand: the new binary takes over
        os.execv(binary, [str(binary), *sys.argv[1:]])


@cli.callback()
def main_options(
    ctx: typer.Context,
    daemon: Annotated[
        bool, typer.Option("--daemon", "-d", help="run in the background (pid + log file)")
    ] = False,
    install: Annotated[
        bool, typer.Option("--install", help="install the systemd service (root)")
    ] = False,
    uninstall: Annotated[
        bool, typer.Option("--uninstall", help="remove the service; data stays unless --purge")
    ] = False,
    purge: Annotated[
        bool,
        typer.Option("--purge", help="with --uninstall: also delete data, cache, logs, config"),
    ] = False,
    user: Annotated[
        list[str] | None,
        typer.Option(
            "--user", help="with --install: let this user use devices via the root helper"
        ),
    ] = None,
    mode: ModeOpt = None,
    config: ConfigOpt = None,
    host: HostOpt = None,
    port: PortOpt = None,
    data_dir: DataOpt = None,
    cache_dir: CacheOpt = None,
    log_dir: LogDirOpt = None,
    mount_root: MountOpt = None,
) -> None:
    """SyncMusic. Without a command (and with options) it starts the app like `run`."""
    overrides: dict[str, Any] = {
        "host": host,
        "port": port,
        "data_dir": data_dir,
        "cache_dir": cache_dir,
        "log_dir": log_dir,
        "mount_root": mount_root,
    }
    ctx.obj = {"config": config, "overrides": overrides}
    if (user or purge) and not (install or uninstall):
        raise typer.Exit(_fail("--user needs --install, --purge needs --uninstall"))
    if install or uninstall:
        if install and uninstall:
            raise typer.Exit(_fail("--install or --uninstall, not both"))
        if not is_root():
            raise typer.Exit(
                _fail(f"--{'install' if install else 'uninstall'} needs root: run it with sudo")
            )
        from syncmusic.infra import install as inst

        cfg = _load(config, overrides)
        try:
            if install:
                run_args = _run_args({"mode": mode, **overrides})
                inst.install(cfg, config, run_args, user or [], typer.echo)
            else:
                inst.uninstall(cfg, config, purge, typer.echo)
        except (inst.InstallError, OSError) as exc:
            raise typer.Exit(_fail(str(exc))) from None
        raise typer.Exit(0)
    if ctx.invoked_subcommand is not None:
        if daemon:
            raise typer.Exit(_fail("-d/--daemon starts the app: use it without a command"))
        return
    given = mode is not None or config is not None or any(v is not None for v in overrides.values())
    if not (daemon or given):
        typer.echo(ctx.get_help())
        raise typer.Exit(0)
    cfg = _load(config, {"mode": mode, **overrides})
    if daemon:
        args = ["--config", str(config.absolute())] if config else []
        raise typer.Exit(_daemonize(cfg, [*args, *_run_args({"mode": mode, **overrides})]))
    _start(cfg)


def _pid_file(cfg: BootstrapConfig) -> Path:
    return cfg.data_dir / "syncmusic.pid"


def _running_pid(cfg: BootstrapConfig) -> int | None:
    """The pid of the background instance of this data dir, if it still runs."""
    try:
        pid = int(_pid_file(cfg).read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if b"syncmusic" in cmdline else None


def _prog() -> str:
    """How the user calls this program: `syncmusic` when that is on PATH, else the binary they ran
    (`./syncmusic-web-0.2.0-linux-aarch64` before `--install`, clean-VM test of)."""
    binary = os.environ.get("SYNCMUSIC_BINARY")
    if not binary:
        return "syncmusic"
    path = Path(binary).resolve()
    on_path = shutil.which("syncmusic")
    if on_path and Path(on_path).resolve() == path:
        return "syncmusic"
    return f"./{path.name}" if path.parent == Path.cwd().resolve() else str(path)


def _daemonize(cfg: BootstrapConfig, run_args: list[str]) -> int:
    """`syncmusic -d`: start `syncmusic run` in a new session, its output in <log dir>/daemon.log."""
    pid = _running_pid(cfg)
    if pid:
        return _fail(f"already running (pid {pid}, {_pid_file(cfg)})")
    cfg.logs.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.logs / "daemon.log"
    with open(log_path, "ab") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "syncmusic", "run", *run_args],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _pid_file(cfg).write_text(f"{proc.pid}\n")
    time.sleep(1.0)
    if proc.poll() is not None:
        _pid_file(cfg).unlink(missing_ok=True)
        return _fail(f"it stopped at once (exit {proc.returncode}); see {log_path}")
    typer.echo(f"SyncMusic started in the background (pid {proc.pid}); log: {log_path}")
    typer.echo(
        f"Stop it with: {_prog()} stop"
        + (f" {shlex.join(_pid_args(run_args))}" if _pid_args(run_args) else "")
    )
    return 0


def _pid_args(run_args: list[str]) -> list[str]:
    """The options `stop`/`status` need to find the pid file again (config and data dir)."""
    out: list[str] = []
    for i, a in enumerate(run_args):
        if a in ("--config", "--data-dir"):
            out += [a, run_args[i + 1]]
    return out


@cli.command()
def run(
    ctx: typer.Context,
    mode: ModeOpt = None,
    config: ConfigOpt = None,
    host: HostOpt = None,
    port: PortOpt = None,
    data_dir: DataOpt = None,
    cache_dir: CacheOpt = None,
    log_dir: LogDirOpt = None,
    mount_root: MountOpt = None,
) -> None:
    """Start SyncMusic in the foreground (the only command that starts the app; `-d` runs it in the background)."""
    top = ctx.obj or {"config": None, "overrides": {}}
    mine = {
        "host": host,
        "port": port,
        "data_dir": data_dir,
        "cache_dir": cache_dir,
        "log_dir": log_dir,
        "mount_root": mount_root,
    }
    overrides = {
        k: mine.get(k) if mine.get(k) is not None else top["overrides"].get(k) for k in PATH_KEYS
    }
    _start(_load(config or top["config"], {"mode": mode, **overrides}))


def _instance(
    ctx: typer.Context,
    config: Path | None,
    data_dir: Path | None,
    host: str | None = None,
    port: int | None = None,
) -> BootstrapConfig:
    top = ctx.obj or {"config": None, "overrides": {}}
    mine = {"data_dir": data_dir, "host": host, "port": port}
    return _load(
        config or top["config"],
        {k: v if v is not None else top["overrides"].get(k) for k, v in mine.items()},
    )


def _local_url(cfg: BootstrapConfig) -> str:
    """The URL of the instance this config starts (a wildcard listen address is reached via loopback)."""
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "", "::") else cfg.host
    return f"http://{f'[{host}]' if ':' in host else host}:{cfg.port}"


@cli.command()
def stop(
    ctx: typer.Context,
    config: ConfigOpt = None,
    data_dir: DataOpt = None,
    timeout: Annotated[int, typer.Option(help="seconds to wait before giving up")] = 300,
) -> None:
    """Stop the background instance started with -d (its pid file is in the data dir)."""
    cfg = _instance(ctx, config, data_dir)
    pid = _running_pid(cfg)
    if pid is None:
        _pid_file(cfg).unlink(missing_ok=True)
        raise typer.Exit(_fail(f"not running (no live pid in {_pid_file(cfg)})"))
    os.kill(pid, signal.SIGTERM)  # graceful: a running sync stops after the current file
    deadline = time.monotonic() + timeout
    while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if Path(f"/proc/{pid}").exists():
        raise typer.Exit(_fail(f"pid {pid} still runs after {timeout} s"))
    _pid_file(cfg).unlink(missing_ok=True)
    typer.echo(f"SyncMusic stopped (pid {pid})")


class Api:
    """The running instance's REST API; any error ends the command with the API's message and exit code 1."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def call(self, method: str, path: str, **kw: Any) -> Any:
        as_text = kw.pop("_text", False)
        try:
            r = httpx.request(method, f"{self.url}/api/v1{path}", timeout=60, headers=_auth(), **kw)
        except httpx.HTTPError as exc:
            raise typer.Exit(_fail(f"cannot reach {self.url}: {exc}")) from exc
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = f"{body.get('error', 'error')}: {body.get('message', r.text)}"
            except ValueError:
                msg = f"HTTP {r.status_code}: {r.text[:200]}"
            raise typer.Exit(_fail(msg))
        if as_text:
            return r.text
        return r.json() if r.content else None

    def text(self, path: str, **params: Any) -> str:
        return str(
            self.call(
                "GET", path, params={k: v for k, v in params.items() if v is not None}, _text=True
            )
        )

    def get(self, path: str, **params: Any) -> Any:
        return self.call("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: Any = None) -> Any:
        return self.call("POST", path, json=body)

    def patch(self, path: str, body: Any) -> Any:
        return self.call("PATCH", path, json=body)

    def follow(self, job: dict[str, Any], quiet: bool = False) -> dict[str, Any]:
        """Poll a job until it ends, printing its progress messages; exit 1 unless it succeeded."""
        last = ""
        while True:
            job = self.get(f"/jobs/{job['id']}")
            if job["message"] and job["message"] != last and not quiet:
                typer.echo(f"  [{job['progress'] * 100:3.0f}%] {job['message']}", err=True)
                last = job["message"]
            if job["status"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.3)
        if job["status"] != "succeeded":
            raise typer.Exit(_fail(f"job {job['id']} {job['status']}: {job.get('error') or ''}"))
        return job


def _get(url: str, path: str) -> Any:
    return Api(url).get(path)


def _fail(msg: str) -> int:
    typer.echo(msg, err=True)
    return 1


def _read(path: Path) -> str:
    """A rule file's text; an unreadable file is one error line, not a traceback."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise typer.Exit(_fail(f"cannot read {path}: not UTF-8 text")) from None
    except OSError as exc:
        raise typer.Exit(_fail(f"cannot read {path}: {exc.strerror or exc}")) from None


def _join(head: str, detail: str | None) -> str:
    return f"{head}: {detail}" if detail else head


def _out(data: Any, as_json: bool, text: Callable[[Any], str]) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False) if as_json else text(data))


def _settings(pairs: list[str]) -> dict[str, Any]:
    """key=value pairs; a value is parsed as JSON when it can be (numbers, true/false, lists), else a string."""
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise typer.Exit(_fail(f"expected key=value, got {pair!r}"))
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


JsonOpt = Annotated[bool, typer.Option("--json", help="machine-readable output")]


@cli.command()
def status(
    ctx: typer.Context,
    url: Annotated[
        str | None,
        typer.Option(
            "--url", help="URL of the running instance (default: from --host/--port/config)"
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
    config: ConfigOpt = None,
    data_dir: DataOpt = None,
    host: HostOpt = None,
    port: PortOpt = None,
) -> None:
    """Show health of the running instance and its devices (and the background instance's pid, if -d started it)."""
    cfg = _instance(ctx, config, data_dir, host, port)
    url = url or env_value("URL") or _local_url(cfg)
    pid = _running_pid(cfg)
    if pid and not as_json:
        typer.echo(f"background instance: pid {pid}")
    data = {**_get(url, "/health"), **_get(url, "/version"), "devices": _get(url, "/devices")}
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return
    typer.echo(f"SyncMusic {data['version']}: {data['status']} (mode {data['mode']})")
    devices = data["devices"]
    for d in devices["registered"]:
        state = "connected" if d["connected"] else "not connected"
        busy = f", syncing (job {d['busy']})" if d["busy"] else ""
        typer.echo(f"  {d['name']} [{d['type']}, id {d['id']}]: {state}{busy}")
    for u in devices["unknown"]:
        best = u["probes"][0]["type_name"] if u["probes"] else "unknown type"
        typer.echo(
            f"  unknown disk {u['disk']} ({u.get('vendor') or ''} {u.get('model') or ''}): {best}"
        )
    if not devices["registered"] and not devices["unknown"]:
        typer.echo("  no devices")


@cli.command()
def update(
    ctx: typer.Context,
    check: Annotated[
        bool, typer.Option("--check", help="only look for a newer version, install nothing")
    ] = False,
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="stable or beta (saved as the update channel)"),
    ] = None,
    now: Annotated[
        bool,
        typer.Option(
            "--now", help="do not wait for running syncs (they continue after the restart)"
        ),
    ] = False,
    url: Annotated[
        str | None,
        typer.Option(
            "--url", help="URL of the running instance (default: from --host/--port/config)"
        ),
    ] = None,
    config: ConfigOpt = None,
    data_dir: DataOpt = None,
    host: HostOpt = None,
    port: PortOpt = None,
) -> None:
    """Check the GitHub releases for a newer SyncMusic and install it into the running instance (single-file
    binaries and the service of --install; Docker and source installs only show what is new)."""
    cfg = _instance(ctx, config, data_dir, host, port)
    api = Api(url or env_value("URL") or _local_url(cfg))
    if channel is not None:
        if channel not in ("stable", "beta"):
            raise typer.Exit(_fail("--channel is stable or beta"))
        api.patch("/settings", {"update_channel": channel})
    u = api.post("/updates/check")
    if u.get("error"):
        raise typer.Exit(_fail(f"update check failed: {u['error']}"))
    if not u.get("enabled"):
        raise typer.Exit(_fail("update checks are off (update_repo = none in the configuration)"))
    if not u.get("available"):
        typer.echo(f"SyncMusic {u['running']} is up to date ({u['channel']} channel).")
        return
    typer.echo(
        f"SyncMusic {u['latest']} is available (running {u['running']}, {u['channel']} channel):"
    )
    for note in u.get("notes") or []:
        typer.echo(f"  - {note}")
    if u.get("url"):
        typer.echo(f"  {u['url']}")
    if check:
        return
    if not u.get("installable"):
        hint = (
            "pull the new image"
            if u.get("install_type") == "docker"
            else "update it the way you installed it"
        )
        raise typer.Exit(_fail(f"this install does not update itself: {hint}"))
    r = api.post("/updates/install", {"when": "now" if now else "after_syncs"})
    if r.get("waiting"):
        typer.echo(
            "Syncs are running: SyncMusic updates once they have finished (--now does not wait)."
        )
    else:
        typer.echo(f"Installing {r['installing']}; SyncMusic restarts.")


@cli.command()
def doctor(
    ctx: typer.Context,
    url: Annotated[
        str | None,
        typer.Option(
            "--url", help="URL of the running instance (default: from --host/--port/config)"
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
    config: ConfigOpt = None,
    data_dir: DataOpt = None,
    host: HostOpt = None,
    port: PortOpt = None,
) -> None:
    """Check this install (ffmpeg, space, folders, device access, udev, clock, port; with a running instance also
    the libraries and USB devices) and say how to fix each problem. Exit code 1 when a check fails."""
    from syncmusic.infra import doctor as doc

    cfg = _instance(ctx, config, data_dir, host, port)
    url = url or env_value("URL") or _local_url(cfg)
    try:
        running: list[dict[str, Any]] | None = httpx.get(
            f"{url}/api/v1/diagnostics", timeout=60, headers=_auth()
        ).json()
    except (httpx.HTTPError, ValueError):
        running = None
    checks = running if isinstance(running, list) else doc.local_checks(cfg)
    checks = [*checks, doc.check_port(cfg, url if running is not None else None)]
    if as_json:
        typer.echo(json.dumps(checks, indent=2))
    else:
        marks = {"ok": "OK  ", "warning": "WARN", "error": "FAIL", "unknown": "--  "}
        for c in checks:
            typer.echo(f"{marks.get(c['state'], c['state'])}  {c['label']}: {c['detail']}")
            if c.get("fix"):
                typer.echo(f"      fix: {c['fix']}")
    if any(c["state"] == "error" for c in checks):
        raise typer.Exit(1)


@cli.command()
def diagnostics(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="zip file (default: in the current folder)"),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option(
            "--url", help="URL of the running instance (default: from --host/--port/config)"
        ),
    ] = None,
    config: ConfigOpt = None,
    data_dir: DataOpt = None,
    host: HostOpt = None,
    port: PortOpt = None,
) -> None:
    """Download the diagnostics bundle of the running instance (versions, settings, libraries, devices, recent
    reports and logs; secrets masked) for a bug report."""
    cfg = _instance(ctx, config, data_dir, host, port)
    url = url or env_value("URL") or _local_url(cfg)
    try:
        r = httpx.get(f"{url}/api/v1/diagnostics/bundle", timeout=300, headers=_auth())
        r.raise_for_status()
    except httpx.HTTPError as e:
        typer.echo(f"cannot get the bundle from {url}: {e}", err=True)
        raise typer.Exit(1) from e
    name = re.search(r'filename="([^"]+)"', r.headers.get("content-disposition", ""))
    path = output or Path(name.group(1) if name else "syncmusic-diagnostics.zip")
    path.write_bytes(r.content)
    typer.echo(f"wrote {path} ({len(r.content) // 1024} KiB)")


@cli.command("set-password")
def set_password(
    ctx: typer.Context,
    password: Annotated[
        str,
        typer.Option(
            prompt=True, confirmation_prompt=True, hide_input=True, help="the new password"
        ),
    ],
    config: ConfigOpt = None,
    data_dir: DataOpt = None,
) -> None:
    """Set the web UI / API password and turn the login on (Settings → System → Access). Works on
    the database directly, also to recover a lost password; run it as the user that runs SyncMusic. Ends every
    session; API tokens stay valid."""
    from syncmusic.infra.auth import set_password_offline

    cfg = _instance(ctx, config, data_dir)
    db = cfg.data_dir / "syncmusic.sqlite3"
    if not db.exists():
        raise typer.Exit(_fail(f"no database at {db}: start SyncMusic once or pass --data-dir"))
    try:
        set_password_offline(db, password)
    except (ValueError, OSError) as exc:
        raise typer.Exit(_fail(str(exc))) from None
    typer.echo("password set; the login is on")


# --- libraries -----------------------------------------------------------------------------------------

libraries = typer.Typer(no_args_is_help=True, help="Libraries")
cli.add_typer(libraries, name="libraries")


@libraries.command("list")
def libraries_list(url: UrlOpt = DEFAULT_URL, as_json: JsonOpt = False) -> None:
    """Registered libraries."""
    _out(
        Api(url).get("/libraries"),
        as_json,
        lambda ls: "\n".join(f"{x['id']}: {x['name']} [{x['type']}]" for x in ls) or "no libraries",
    )


@libraries.command("add")
def libraries_add(
    library_id: Annotated[str, typer.Argument(help="id, e.g. music")],
    type_: Annotated[str, typer.Option("--type", help="folder | subsonic | audiobookshelf")],
    name: Annotated[str | None, typer.Option(help="display name (default: the id)")] = None,
    setting: Annotated[list[str] | None, typer.Option("--setting", "-s", help="key=value")] = None,
    url: UrlOpt = DEFAULT_URL,
    as_json: JsonOpt = False,
) -> None:
    """Add (or replace) a library."""
    body = {"id": library_id, "type": type_, "name": name or library_id}
    body["settings"] = _settings(setting or [])
    _out(Api(url).post("/libraries", body), as_json, lambda x: f"added {x['id']}")


@libraries.command("check")
def libraries_check(library_id: str, url: UrlOpt = DEFAULT_URL, as_json: JsonOpt = False) -> None:
    """Check that a library is reachable."""
    health = Api(url).post(f"/libraries/{library_id}/check")
    _out(health, as_json, lambda h: _join("ok" if h["ok"] else "NOT OK", h.get("message")))
    if not health["ok"]:
        raise typer.Exit(1)


@libraries.command("refresh")
def libraries_refresh(library_id: str, url: UrlOpt = DEFAULT_URL, as_json: JsonOpt = False) -> None:
    """Reload a library's snapshot (waits for the job)."""
    api = Api(url)
    job = api.follow(api.post(f"/libraries/{library_id}/refresh"), quiet=as_json)
    res = job["result"]
    _out(res, as_json, lambda r: f"{library_id}: {r['tracks']} tracks, {r['playlists']} playlists")


# --- devices -------------------------------------------------------------------------------------------

devices = typer.Typer(no_args_is_help=True, help="Devices")
cli.add_typer(devices, name="devices")


def _devices_text(d: dict[str, Any]) -> str:
    lines = [
        f"{x['id']}: {x['name']} [{x['type']}] {'connected' if x['connected'] else 'not connected'}"
        for x in d["registered"]
    ]
    lines += [
        f"unknown disk {u['disk']}: {u.get('vendor') or ''} {u.get('model') or ''}"
        for u in d["unknown"]
    ]
    return "\n".join(lines) or "no devices"


@devices.command("list")
def devices_list(url: UrlOpt = DEFAULT_URL, as_json: JsonOpt = False) -> None:
    """Registered devices and connected unknown disks."""
    _out(Api(url).get("/devices"), as_json, _devices_text)


@devices.command("allowlist")
def devices_allowlist(
    device: Annotated[
        list[str] | None, typer.Option("--device", help="a registered device id (repeatable)")
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="every registered device")] = False,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="write it here (default: print it)")
    ] = None,
    url: UrlOpt = DEFAULT_URL,
) -> None:
    """The hardware test suite's allowlist (tests/hardware.toml) for registered devices: the tests may FORMAT
    every listed device."""
    api = Api(url)
    if not device and not all_:
        regs = api.get("/devices")["registered"]
        if not regs:
            raise typer.Exit(_fail("no registered devices"))
        for d in regs:
            typer.echo(f"  {d['id']}: {d['name']} [{d['type']}]")
        picked = typer.prompt("device ids for the allowlist (comma-separated)")
        device = [x.strip() for x in picked.split(",") if x.strip()]
    text = api.text("/devices/allowlist.toml", device=None if all_ else device)
    if output is None:
        typer.echo(text, nl=False)
        return
    output.write_text(text)
    typer.echo(f"written to {output} - the hardware tests may format every device in it")


@devices.command("register")
def devices_register(
    name: str,
    disk: Annotated[str | None, typer.Option(help="/dev path of a connected unknown disk")] = None,
    mount_path: Annotated[
        str | None, typer.Option(help="or: a directory used as the device")
    ] = None,
    type_: Annotated[
        str | None, typer.Option("--type", help="device type (default: detected)")
    ] = None,
    library: Annotated[
        list[str] | None,
        typer.Option(
            "--library",
            "-l",
            help="library id: link its first default template (its 'All …'; templates decide)",
        ),
    ] = None,
    template: Annotated[
        list[str] | None, typer.Option("--template", "-t", help="template id")
    ] = None,
    on_connect: Annotated[str | None, typer.Option(help="auto | ask | manual")] = None,
    url: UrlOpt = DEFAULT_URL,
    as_json: JsonOpt = False,
) -> None:
    """Register a connected disk (or a directory) as a device."""
    body: dict[str, Any] = {"name": name}
    api = Api(url)
    template = list(template or [])
    for lid in library or []:  # devices have templates only: the library's "All …" default
        first = next((x for x in api.get("/templates") if x.get("default_of") == lid), None)
        if first is None:
            raise typer.BadParameter(f"library {lid!r} has no default template", param_hint="-l")
        template.append(first["id"])
    for key, value in (
        ("disk", disk),
        ("mount_path", mount_path),
        ("type", type_),
        ("on_connect", on_connect),
    ):
        if value is not None:
            body[key] = value
    if template:
        body["rule_set"] = {"template_ids": template}
    _out(
        api.post("/devices/register", body),
        as_json,
        lambda c: f"registered {c['id']} ({c['type']})",
    )


@devices.command("eject")
def devices_eject(device_id: str, url: UrlOpt = DEFAULT_URL) -> None:
    """Unmount and power off a device."""
    Api(url).post(f"/devices/{device_id}/eject")
    typer.echo(f"{device_id} ejected")


@devices.command("format")
def devices_format(
    device_id: str,
    confirm: Annotated[str, typer.Option(help="the device's name, to confirm")],
    fs: Annotated[str | None, typer.Option(help="file system (default: the device's)")] = None,
    url: UrlOpt = DEFAULT_URL,
    as_json: JsonOpt = False,
) -> None:
    """Format a device's data partition (needs --confirm <device name>)."""
    body = {"confirm": confirm, **({"fs": fs} if fs else {})}
    _out(
        Api(url).post(f"/devices/{device_id}/format", body),
        as_json,
        lambda c: f"{c['id']} formatted",
    )


@devices.command("rename")
def devices_rename(
    device_id: str,
    name: str,
    url: UrlOpt = DEFAULT_URL,
    as_json: JsonOpt = False,
) -> None:
    """Rename a device on the device itself (iPod name, file system label); not a sync."""
    _out(
        Api(url).post(f"/devices/{device_id}/rename", {"name": name}),
        as_json,
        lambda r: (
            f"{device_id} renamed to {r['name']}"
            + (f" (label {r['label']})" if r.get("label") else "")
        ),
    )


# --- sync ----------------------------------------------------------------------------------------------


def _plan_text(p: dict[str, Any]) -> str:
    c = p["counts"]
    space = p.get("space") or {}
    lines = [
        f"plan {p['id']}: add {c['add']}, replace {c['replace']}, update {c['update_meta']}, "
        f"remove {c['remove']}, keep {c['keep']}",
        f"space: {'fits' if space.get('fits', True) else 'DOES NOT FIT'}",
    ]
    lines += [f"warning: {w}" for w in p.get("warnings", [])]
    lines += [f"needs confirmation: {r}" for r in p.get("confirmation_reasons", [])]
    return "\n".join(lines)


def _report_text(r: dict[str, Any]) -> str:
    c = r.get("counts") or {}
    rev = r.get("reverse") or {}
    lines = [_join(r["status"], ", ".join(f"{k} {v}" for k, v in c.items() if v))]
    if rev:
        lines.append(
            f"reverse: {rev.get('plays_submitted', 0)} plays, {rev.get('ratings_pushed', 0)} ratings, "
            f"{rev.get('positions_pushed', 0)} positions, {rev.get('pending', 0)} pending"
        )
        lines += [f"warning: {w}" for w in rev.get("warnings", [])]
        lines += [f"error: {e}" for e in rev.get("errors", [])]
    lines += [f"failed: {f['ref']}: {f['error']}" for f in r.get("failures", [])]
    if r.get("message"):
        lines.append(r["message"])
    return "\n".join(lines)


@cli.command()
def sync(
    device_id: str,
    plan_only: Annotated[
        bool, typer.Option("--plan-only", help="show the plan, change nothing")
    ] = False,
    force: Annotated[bool, typer.Option(help="trim the plan to what fits")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="confirm without asking")] = False,
    first_sync: Annotated[str | None, typer.Option(help="keep | adopt | wipe (first sync)")] = None,
    url: UrlOpt = DEFAULT_URL,
    as_json: JsonOpt = False,
) -> None:
    """Plan, confirm and apply a sync, then follow the job."""
    api = Api(url)
    plan = api.post(f"/devices/{device_id}/plan", {"force": force})
    if plan_only:
        _out(plan, as_json, _plan_text)
        return
    if not as_json:
        typer.echo(_plan_text(plan))
    if plan["first_sync"] and first_sync is None:
        first_sync = (
            "keep"
            if yes
            else typer.prompt(
                "first sync: keep unknown files, adopt matches or wipe the device?", default="keep"
            )
        )
    if plan["needs_confirmation"] and not yes and not typer.confirm("apply this plan?"):
        raise typer.Exit(_fail("cancelled"))
    body: dict[str, Any] = {"confirm": True}
    if first_sync:
        body["first_sync"] = first_sync
    job = api.follow(api.post(f"/plans/{plan['id']}/apply", body), quiet=as_json)
    _out(job["result"], as_json, _report_text)


# --- configuration file ---------------------------------------------------------------------------------

config_app = typer.Typer(no_args_is_help=True, help="The bootstrap configuration file")
cli.add_typer(config_app, name="config")


@config_app.command("init")
def config_init(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="write to this file (default: stdout)")
    ] = None,
    force: Annotated[bool, typer.Option(help="overwrite an existing file")] = False,
) -> None:
    """Write a commented default configuration: every key with its default and what it does."""
    from syncmusic.infra.config import default_config_text

    text = default_config_text()
    if output is None:
        typer.echo(text, nl=False)
        return
    if output.exists() and not force:
        raise typer.Exit(_fail(f"{output} exists; use --force to overwrite it"))
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise typer.Exit(_fail(f"cannot write {output}: {exc.strerror or exc}")) from None
    typer.echo(f"wrote {output}", err=True)


# --- rules and templates -------------------------------------------------------------------------------

rules = typer.Typer(no_args_is_help=True, help="Rules")
cli.add_typer(rules, name="rules")


@rules.command("validate")
def rules_validate(
    file: Annotated[Path, typer.Argument(help="rule / rule set as JSON, YAML or .nsp")],
    url: UrlOpt = DEFAULT_URL,
    as_json: JsonOpt = False,
) -> None:
    """Validate a rule file."""
    res = Api(url).post("/rules/validate", {"text": _read(file)})
    _out(
        res,
        as_json,
        lambda r: "\n".join(
            [
                "ok" if r["ok"] else "INVALID",
                *r["errors"],
                *(f"warning: {w}" for w in r["warnings"]),
            ]
        ),
    )
    if not res["ok"]:
        raise typer.Exit(1)


@rules.command("test")
def rules_test(
    device_id: str,
    file: Annotated[
        Path | None, typer.Option(help="rule set to try instead of the device's")
    ] = None,
    url: UrlOpt = DEFAULT_URL,
    as_json: JsonOpt = False,
) -> None:
    """What a device's rules (or a rule file) select from its libraries."""
    api = Api(url)
    body: dict[str, Any] = {}
    if file is not None:
        parsed = api.post("/rules/validate", {"text": _read(file)})
        if not parsed["ok"]:
            raise typer.Exit(_fail("invalid rules: " + "; ".join(parsed["errors"])))
        body["rule_set"] = parsed["rule_set"]
    res = api.post(f"/devices/{device_id}/rules/test", body)

    def text(r: dict[str, Any]) -> str:
        lines = [f"{r['items']} items, {r['bytes'] / 1e6:.1f} MB"]
        lines += [f"  rule {x['rule_id']}: {x['items']} items" for x in r["per_rule"]]
        lines += [f"  {x['artist'] or ''} - {x['title']}" for x in r["sample"][:20]]
        lines += [f"warning: {w}" for w in r["warnings"]]
        return "\n".join(lines)

    _out(res, as_json, text)


templates = typer.Typer(no_args_is_help=True, help="Rule templates")
cli.add_typer(templates, name="templates")


@templates.command("list")
def templates_list(url: UrlOpt = DEFAULT_URL, as_json: JsonOpt = False) -> None:
    """Rule templates."""
    _out(
        Api(url).get("/templates"),
        as_json,
        lambda ts: (
            "\n".join(
                f"{t['id']}: {t['name']} ({len(t['rules'])} rule{'' if len(t['rules']) == 1 else 's'})"
                for t in ts
            )
            or "no templates"
        ),
    )


@templates.command("show")
def templates_show(template_id: str, url: UrlOpt = DEFAULT_URL, as_json: JsonOpt = False) -> None:
    """A template with the devices that use it."""
    api = Api(url)
    tpl = api.get(f"/templates/{template_id}")
    tpl["used_by"] = api.get(f"/templates/{template_id}/usage")

    def text(t: dict[str, Any]) -> str:
        lines = [f"{t['id']}: {t['name']}"]
        lines += [
            f"  rule {r['id']}: {r.get('name') or ''} {json.dumps(r['criteria'], ensure_ascii=False)}"
            for r in t["rules"]
        ]
        lines += [
            f"  exclude {r['id']}: {json.dumps(r['criteria'], ensure_ascii=False)}"
            for r in t["excludes"]
        ]
        lines.append("used by: " + (", ".join(d["name"] for d in t["used_by"]) or "-"))
        return "\n".join(lines)

    _out(tpl, as_json, text)


@templates.command("apply")
def templates_apply(template_id: str, device_id: str, url: UrlOpt = DEFAULT_URL) -> None:
    """Link a template to a device (its rules then apply at the next sync)."""
    api = Api(url)
    api.get(f"/templates/{template_id}")
    cfg = api.get(f"/devices/{device_id}")["config"]
    rule_set = cfg["rule_set"]
    if template_id in rule_set["template_ids"]:
        typer.echo(f"{template_id} is already linked to {device_id}")
        return
    rule_set["template_ids"].append(template_id)
    api.patch(f"/devices/{device_id}", {"rule_set": rule_set})
    typer.echo(f"{template_id} linked to {device_id}")


# --- jobs and logs -------------------------------------------------------------------------------------


@cli.command()
def jobs(url: UrlOpt = DEFAULT_URL, as_json: JsonOpt = False) -> None:
    """Recent jobs."""
    _out(
        Api(url).get("/jobs"),
        as_json,
        lambda js: (
            "\n".join(
                f"{j['id']} {j['kind']:8} {j['status']:9} {j['progress'] * 100:3.0f}% {j['name']}"
                for j in js
            )
            or "no jobs"
        ),
    )


@cli.command()
def logs(
    job: Annotated[
        str | None, typer.Option(help="the log of one job instead of the app log")
    ] = None,
    lines: Annotated[int, typer.Option("--lines", "-n")] = 50,
    url: UrlOpt = DEFAULT_URL,
) -> None:
    """Tail of the application log, or a job's log."""
    api = Api(url)
    out = api.get(f"/jobs/{job}")["log"][-lines:] if job else api.get("/logs", lines=lines)
    typer.echo("\n".join(out))


# `--help` of every command and group ends with real examples (a test checks each has some)
EXAMPLES: dict[str, list[str]] = {
    "": [
        "syncmusic --port 8080 --data-dir /srv/syncmusic",
        "syncmusic -d --config ./sm.toml",
        "sudo syncmusic --install --port 8080 --data-dir /srv/syncmusic",
        "sudo syncmusic --uninstall --purge",
    ],
    "run": [
        "syncmusic run",
        "syncmusic run --mode web --host 127.0.0.1 --port 8080",
        "syncmusic run --config ./sm.toml",
    ],
    "set-password": [
        "syncmusic set-password --password 'a long passphrase'",
        "sudo syncmusic set-password --config /etc/syncmusic/config.toml --password 'new one'",
    ],
    "diagnostics": [
        "syncmusic diagnostics",
        "syncmusic diagnostics -o /tmp/bug.zip --url http://pi.local:8080",
    ],
    "update": [
        "syncmusic update --check",
        "syncmusic update",
        "syncmusic update --channel beta --now",
    ],
    "doctor": [
        "syncmusic doctor",
        "syncmusic doctor --json",
        "syncmusic doctor --url http://pi.local:8080",
    ],
    "status": [
        "syncmusic status",
        "syncmusic status --json",
        "syncmusic status --port 8080 --data-dir /srv/syncmusic",
        "syncmusic status --url http://pi.local:8080",
    ],
    "stop": [
        "syncmusic stop",
        "syncmusic stop --config ./sm.toml",
        "syncmusic stop --data-dir /srv/sm",
    ],
    "libraries": ["syncmusic libraries list", "syncmusic libraries refresh music"],
    "libraries list": ["syncmusic libraries list", "syncmusic libraries list --json"],
    "libraries add": [
        "syncmusic libraries add music --type folder -s root=/srv/music",
        "syncmusic libraries add nd --type subsonic --name Navidrome -s url=http://nd:4533 -s username=me -s password=pw",
    ],
    "libraries check": ["syncmusic libraries check music", "syncmusic libraries check nd --json"],
    "libraries refresh": ["syncmusic libraries refresh music"],
    "devices": ["syncmusic devices list", "syncmusic devices eject my-ipod"],
    "devices list": ["syncmusic devices list", "syncmusic devices list --json"],
    "devices allowlist": [
        "syncmusic devices allowlist --all -o tests/hardware.toml",
        "syncmusic devices allowlist --device my-ipod --device stick",
    ],
    "devices register": [
        'syncmusic devices register "My iPod" --disk /dev/sdb -l music',
        "syncmusic devices register Stick --mount-path /media/stick --type flashdrive -t music-all",
    ],
    "devices eject": ["syncmusic devices eject my-ipod"],
    "devices format": [
        'syncmusic devices format stick --confirm "Stick"',
        'syncmusic devices format stick --confirm "Stick" --fs exfat',
    ],
    "devices rename": ['syncmusic devices rename my-ipod "Kitchen iPod"'],
    "sync": [
        "syncmusic sync my-ipod",
        "syncmusic sync my-ipod --plan-only",
        "syncmusic sync stick --yes --first-sync adopt",
    ],
    "config": [
        "syncmusic config init",
        "syncmusic config init --output /etc/syncmusic/config.toml",
    ],
    "config init": [
        "syncmusic config init",
        "syncmusic config init --output /etc/syncmusic/config.toml",
        "syncmusic config init -o ./sm.toml --force",
    ],
    "rules": ["syncmusic rules validate rules.json", "syncmusic rules test my-ipod"],
    "rules validate": ["syncmusic rules validate rules.json", "syncmusic rules validate smart.nsp"],
    "rules test": ["syncmusic rules test my-ipod", "syncmusic rules test my-ipod --json"],
    "templates": ["syncmusic templates list", "syncmusic templates apply music-all my-ipod"],
    "templates list": ["syncmusic templates list", "syncmusic templates list --json"],
    "templates show": ["syncmusic templates show music-all"],
    "templates apply": ["syncmusic templates apply music-all my-ipod"],
    "jobs": ["syncmusic jobs", "syncmusic jobs --json"],
    "logs": ["syncmusic logs", "syncmusic logs -n 200", "syncmusic logs --job 3f2a…"],
}


def _epilog(path: str) -> str:
    # Click rewraps paragraphs; a \b paragraph keeps its lines (one example per line)
    return "\b\nExamples:\n" + "\n".join(f"  {e}" for e in EXAMPLES[path])


def _add_examples(app: typer.Typer, prefix: str = "") -> None:
    app.info.epilog = _epilog(prefix.strip())
    for cmd in app.registered_commands:
        name = cmd.name or (cmd.callback.__name__ if cmd.callback else "")
        cmd.epilog = _epilog(f"{prefix}{name}".strip())
    for group in app.registered_groups:
        if group.typer_instance is not None:
            _add_examples(group.typer_instance, f"{prefix}{group.name} ")


_add_examples(cli)


def main() -> None:
    # fixed name: under `python -m syncmusic` (the desktop bundle's launcher) Click would
    # otherwise print "Usage: python -m syncmusic ..."
    cli(prog_name="syncmusic")
