"""`syncmusic --install/--uninstall` into a temp root and `-d` / `stop` / `status`."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from syncmusic.cli.main import cli
from syncmusic.infra import install as inst
from syncmusic.infra.config import load_config


def _cfg(tmp_path: Path):
    return load_config(
        None,
        env={},
        overrides={"data_dir": "/srv/syncmusic", "cache_dir": "/var/cache/syncmusic"},
    )


def test_install_from_a_venv_and_uninstall_keeps_data(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNCMUSIC_BINARY", raising=False)
    root = tmp_path / "root"
    said: list[str] = []
    cfg = _cfg(tmp_path)
    given = cfg.model_copy(update={"port": 8080})  # the CLI loads the config with the options given
    inst.install(given, None, ["--port", "8080"], ["alice"], said.append, root=root)
    unit = (root / "etc/systemd/system/syncmusic.service").read_text()
    assert (
        f"ExecStart={sys.executable} -m syncmusic run --config /etc/syncmusic/config.toml --port 8080"
        in unit
    )
    assert "User=root" in unit and "WantedBy=multi-user.target" in unit
    assert (
        "SYNCMUSIC_INSTALL_TYPE" not in unit
    )  # a venv install updates itself, not from the releases
    conf = (root / "etc/syncmusic/config.toml").read_text()
    # the commented defaults, the option given uncommented: `syncmusic status` finds the service
    assert "\nport = 8080\n" in conf and "# host = " in conf and "# port = " not in conf
    assert load_config(root / "etc/syncmusic/config.toml", env={}).port == 8080
    for d in ("srv/syncmusic", "srv/syncmusic/logs", "var/cache/syncmusic"):
        assert (root / d).is_dir(), d
    rule = root / "etc/sudoers.d/syncmusic-alice"
    helper = Path(sys.executable).parent / "syncmusic-priv"
    assert rule.read_text().endswith(f"alice ALL=(root) NOPASSWD: {helper}\n")
    assert rule.stat().st_mode & 0o777 == 0o440
    # a second install keeps an edited config
    (root / "etc/syncmusic/config.toml").write_text("port = 9000\n")
    inst.install(cfg, None, [], [], said.append, root=root)
    assert (root / "etc/syncmusic/config.toml").read_text() == "port = 9000\n"
    assert "--port" not in (root / "etc/systemd/system/syncmusic.service").read_text()

    (root / "srv/syncmusic/syncmusic.db").write_text("x")
    inst.uninstall(cfg, None, False, said.append, root=root)
    assert not (root / "etc/systemd/system/syncmusic.service").exists() and not rule.exists()
    assert (root / "srv/syncmusic/syncmusic.db").exists()
    assert (root / "etc/syncmusic/config.toml").exists()
    inst.uninstall(cfg, None, True, said.append, root=root)
    assert not (root / "srv/syncmusic").exists() and not (root / "var/cache/syncmusic").exists()
    assert not (root / "etc/syncmusic").exists()
    assert (root / "srv").is_dir() and (root / "etc").is_dir()  # shared parents stay


def test_purge_never_deletes_a_path_without_syncmusic_in_it(tmp_path):
    root = tmp_path / "root"
    cfg = load_config(None, env={}, overrides={"data_dir": "/data", "cache_dir": "/cache"})
    (root / "data").mkdir(parents=True)
    said: list[str] = []
    inst.uninstall(cfg, Path("/etc/sm.toml"), True, said.append, root=root)
    assert (root / "data").is_dir()
    assert any("kept" in s and s.endswith("/data (not a SyncMusic path)") for s in said)


def test_install_from_the_binary_copies_and_unpacks_it(tmp_path, monkeypatch):
    fake = tmp_path / "syncmusic-web-0.1.0-linux-x"
    fake.write_text(
        "#!/bin/sh\nset -eu\nID=0.1.0-web-x-abc123\n"
        'mkdir -p "$SYNCMUSIC_RUNTIME_DIR/$ID" && touch "$SYNCMUSIC_RUNTIME_DIR/$ID/.complete"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("SYNCMUSIC_BINARY", str(fake))
    root = tmp_path / "root"
    inst.install(_cfg(tmp_path), None, [], ["bob"], lambda _: None, root=root)
    assert (root / "usr/local/bin/syncmusic").read_bytes() == fake.read_bytes()
    assert (root / "opt/syncmusic/runtime/0.1.0-web-x-abc123/.complete").exists()
    unit = (root / "etc/systemd/system/syncmusic.service").read_text()
    assert "ExecStart=/usr/local/bin/syncmusic run --config /etc/syncmusic/config.toml\n" in unit
    assert (
        "Environment=SYNCMUSIC_INSTALL_TYPE=service\n" in unit
    )  # the update check offers its download
    helper = "/opt/syncmusic/runtime/0.1.0-web-x-abc123/python/bin/syncmusic-priv"
    assert (root / "etc/sudoers.d/syncmusic-bob").read_text().endswith(f"NOPASSWD: {helper}\n")
    inst.uninstall(_cfg(tmp_path), None, False, lambda _: None, root=root)
    assert not (root / "usr/local/bin/syncmusic").exists() and not (root / "opt/syncmusic").exists()


def test_reinstall_keeps_only_the_new_and_the_replaced_runtime(tmp_path, monkeypatch):
    root = tmp_path / "root"
    rt = root / "opt/syncmusic/runtime"
    for rid in ("0.0.8-web-x-aaa", "0.0.9-web-x-bbb", "0.1.0-web-x-ccc"):
        (rt / rid).mkdir(parents=True)
    (rt / ".tmp-unpack").mkdir()
    old = root / "usr/local/bin/syncmusic"
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/sh\nID=0.1.0-web-x-ccc\n")
    fake = tmp_path / "syncmusic-web-0.1.1-linux-x"
    fake.write_text(
        "#!/bin/sh\nset -eu\nID=0.1.1-web-x-ddd\n"
        'mkdir -p "$SYNCMUSIC_RUNTIME_DIR/$ID" && touch "$SYNCMUSIC_RUNTIME_DIR/$ID/.complete"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("SYNCMUSIC_BINARY", str(fake))
    said: list[str] = []
    inst.install(_cfg(tmp_path), None, [], ["bob"], said.append, root=root)
    assert sorted(p.name for p in rt.iterdir()) == [
        ".tmp-unpack",
        "0.1.0-web-x-ccc",
        "0.1.1-web-x-ddd",
    ]
    assert any("Removed 2 old runtime(s)" in s for s in said)


def test_install_rejects_odd_user_names(tmp_path):
    with pytest.raises(inst.InstallError, match="unusual user name"):
        inst.install(_cfg(tmp_path), None, [], ["root;rm"], lambda _: None, root=tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="tests the non-root refusal")
@pytest.mark.parametrize("flag", ["--install", "--uninstall"])
def test_install_needs_root(flag):
    out = CliRunner().invoke(cli, [flag])
    assert out.exit_code == 1 and "needs root: run it with sudo" in out.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--user", "x"], "--user needs --install"),
        (["--purge"], "--purge needs --uninstall"),
        (["-d", "jobs"], "use it without a command"),
    ],
)
def test_option_misuse(args, message):
    out = CliRunner().invoke(cli, args)
    assert out.exit_code == 1 and message in out.stderr


def test_no_arguments_prints_help():
    out = CliRunner().invoke(cli, [])
    assert out.exit_code == 0 and "--daemon" in out.stdout and "Examples:" in out.stdout


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_daemon_start_status_stop(tmp_path):
    conf = tmp_path / "sm.toml"
    conf.write_text("detect_devices = false\n")
    port = _free_port()
    base = [sys.executable, "-m", "syncmusic"]
    paths = ["--config", str(conf), "--data-dir", str(tmp_path / "data")]
    env = {k: v for k, v in os.environ.items() if not k.startswith("SYNCMUSIC_")}

    def sm(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*base, *args], env=env, capture_output=True, text=True, timeout=timeout
        )

    started = sm(
        "-d",
        *paths,
        "--cache-dir",
        str(tmp_path / "cache"),
        "--log-dir",
        str(tmp_path / "logs"),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    )
    assert started.returncode == 0, started.stderr
    pid = int((tmp_path / "data/syncmusic.pid").read_text())
    assert f"(pid {pid})" in started.stdout and str(tmp_path / "logs/daemon.log") in started.stdout
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                if httpx.get(f"{url}/api/v1/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail((tmp_path / "logs/daemon.log").read_text()[-2000:])
        again = sm("-d", *paths)
        assert again.returncode == 1 and f"already running (pid {pid}" in again.stderr
        status = sm("status", *paths, "--url", url)
        assert status.returncode == 0 and f"background instance: pid {pid}" in status.stdout
        derived = sm("status", *paths, "--port", str(port))  # URL from --port, no --url
        assert derived.returncode == 0 and "SyncMusic " in derived.stdout, derived.stderr
        conf.write_text(f"detect_devices = false\nport = {port}\n")  # ... or from the config file
        from_conf = sm("status", *paths)
        assert from_conf.returncode == 0 and f"pid {pid}" in from_conf.stdout, from_conf.stderr
        assert (tmp_path / "logs/syncmusic.log").exists()  # --log-dir reached the app
        stopped = sm("stop", *paths)
        assert stopped.returncode == 0 and f"stopped (pid {pid})" in stopped.stdout
        assert not (tmp_path / "data/syncmusic.pid").exists()
        assert not Path(f"/proc/{pid}").exists()
        gone = sm("stop", *paths)
        assert gone.returncode == 1 and "not running" in gone.stderr
    finally:
        if Path(f"/proc/{pid}").exists():
            os.kill(pid, 9)


def test_stop_hint_names_the_binary_the_user_ran(tmp_path, monkeypatch):
    """Clean-VM test: before --install there is no `syncmusic` on PATH; the hint names the binary."""
    from syncmusic.cli import main as cli

    binary = tmp_path / "syncmusic-web-0.2.0-linux-aarch64"
    binary.write_text("")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYNCMUSIC_BINARY", str(binary))
    assert cli._prog() == "./syncmusic-web-0.2.0-linux-aarch64"
    monkeypatch.chdir("/")
    assert cli._prog() == str(binary)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "syncmusic").symlink_to(binary)
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert cli._prog() == "syncmusic"
    monkeypatch.delenv("SYNCMUSIC_BINARY")
    assert cli._prog() == "syncmusic"
