"""install.sh (curl … | sudo bash): release choice, refusals before anything is touched."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "install.sh"
DATA = Path(__file__).parent / "data" / "github_releases.json"


def _functions(snippet: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    """Run `snippet` with the script's functions loaded (its last line, `main "$@"`, left out)."""
    body = "\n".join(SCRIPT.read_text().splitlines()[:-1])
    return subprocess.run(
        ["bash", "-c", f"{body}\n{snippet}"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _releases() -> str:
    data = json.loads(DATA.read_text())
    for r in data:  # every published release here has SHA256SUMS next to its binaries
        if r["assets"]:
            r["assets"].append(
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": f"https://example.org/{r['tag_name']}/sums",
                }
            )
    return json.dumps(data)


@pytest.mark.parametrize(
    "args, picked",
    [
        ("stable '' aarch64", "v0.2.1 https://example.org/web-a https://example.org/v0.2.1/sums"),
        ("stable '' x86_64", "v0.2.1 https://example.org/web-x https://example.org/v0.2.1/sums"),
        ("beta '' aarch64", "v0.2.2-4 https://example.org/web-a https://example.org/v0.2.2-4/sums"),
        (
            "stable 0.2.2-4 aarch64",
            "v0.2.2-4 https://example.org/web-a https://example.org/v0.2.2-4/sums",
        ),
        (
            "beta '' x86_64",
            "v0.2.1 https://example.org/web-x https://example.org/v0.2.1/sums",
        ),  # no beta for x86
        ("stable 0.2.0 aarch64", ""),  # no binary in that release
    ],
)
def test_pick_release(args, picked):
    r = _functions(f"pick_release {args}", _releases())
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == picked


def test_arch_names():
    r = _functions("for a in x86_64 amd64 aarch64 arm64 armv7l; do arch_name $a || echo no; done")
    assert r.stdout.split() == ["x86_64", "x86_64", "aarch64", "aarch64", "no"]


@pytest.mark.parametrize(
    "args, message",
    [
        (["--nope"], "unknown option: --nope"),
        (["--port"], "--port needs a value"),
        ([], "run it as root"),
    ],
)
def test_refusals(args, message):
    r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, check=False)
    assert r.returncode != 0 and message in r.stderr


def test_help_and_safe_for_curl_pipe():
    r = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
    assert r.returncode == 0 and "curl -fsSL" in r.stdout and "--uninstall" in r.stdout
    lines = SCRIPT.read_text().rstrip().splitlines()
    assert lines[-1] == 'main "$@"' and "set -euo pipefail" in lines[:20]


# --dir: a folder install from a fake release (file:// URLs), its run.sh and the upgrade/uninstall paths

FAKE_BINARY = """#!/usr/bin/env bash
# stands in for the web binary: records how it was started, answers /api/v1/health
env | grep '^SYNCMUSIC_' | sort > "$SYNCMUSIC_DATA_DIR/started-env"
echo "$PWD $*" > "$SYNCMUSIC_DATA_DIR/started-args"
exec python3 -c '
import http.server, os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"{\\"status\\": \\"ok\\"}")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(os.environ["SYNCMUSIC_PORT"])), H).serve_forever()
'
# VERSION
"""


def _fake_release(tmp_path: Path, version: str) -> dict[str, str]:
    import hashlib
    import platform

    arch = {"x86_64": "x86_64", "amd64": "x86_64"}.get(platform.machine(), "aarch64")
    rel = tmp_path / f"release-{version}"
    rel.mkdir()
    name = f"syncmusic-web-{version}-linux-{arch}"
    binary = rel / name
    binary.write_text(FAKE_BINARY.replace("VERSION", version))
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    (rel / "SHA256SUMS").write_text(f"{digest}  {name}\n")
    api = [
        {
            "tag_name": f"v{version}",
            "draft": False,
            "prerelease": False,
            "assets": [
                {"name": name, "browser_download_url": f"file://{binary}"},
                {"name": "SHA256SUMS", "browser_download_url": f"file://{rel / 'SHA256SUMS'}"},
            ],
        }
    ]
    (rel / "api.json").write_text(json.dumps(api))
    return {"SYNCMUSIC_RELEASES_URL": f"file://{rel / 'api.json'}"}


def _install(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    import os

    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
    )


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_dir_install_run_upgrade_uninstall(tmp_path):
    import os
    import time
    import urllib.request

    home = tmp_path / "home"
    home.mkdir()
    target = home / "syncmusic"
    port = _free_port()
    env = {**_fake_release(tmp_path, "0.2.1"), "HOME": str(home)}
    r = _install(env, "--dir", str(target), "--port", str(port))
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in target.iterdir()) == [
        "README.txt",
        "cache",
        "config.toml",
        "data",
        "logs",
        "mnt",
        "run.sh",
        "runtime",
        "syncmusic",
        "syncmusic.env",
    ]
    assert sorted(p.name for p in home.iterdir()) == ["syncmusic"]  # nothing else in $HOME
    assert "--uninstall --dir" in (target / "README.txt").read_text()

    proc = subprocess.Popen(
        [str(target / "run.sh")],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(home)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health") as resp:
                    assert resp.status == 200
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("run.sh did not start the binary")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    started = (target / "data" / "started-env").read_text()
    for key, sub in [
        ("CONFIG", "config.toml"),
        ("DATA_DIR", "data"),
        ("CACHE_DIR", "cache"),
        ("LOG_DIR", "logs"),
        ("MOUNT_ROOT", "mnt"),
        ("RUNTIME_DIR", "runtime"),
    ]:
        assert f"SYNCMUSIC_{key}={target / sub}\n" in started
    assert (
        f"SYNCMUSIC_PORT={port}\n" in started and "SYNCMUSIC_INSTALL_TYPE=standalone\n" in started
    )
    assert (target / "data" / "started-args").read_text() == f"{target} run --mode web\n"

    # a rerun upgrades: new binary, the old one kept, config/data/port untouched
    (target / "config.toml").write_text("# mine\n")
    env2 = {**_fake_release(tmp_path, "0.2.2"), "HOME": str(home)}
    r = _install(env2, "--dir", str(target))
    assert r.returncode == 0, r.stderr
    assert "# 0.2.2" in (target / "syncmusic").read_text()
    assert "# 0.2.1" in (target / "syncmusic.previous").read_text()
    assert (target / "config.toml").read_text() == "# mine\n"
    assert (target / "data" / "started-env").exists()
    assert f"SYNCMUSIC_PORT={port}\n" in (target / "syncmusic.env").read_text()

    r = _install(env2, "--uninstall", "--dir", str(target))
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in target.iterdir()) == [
        "cache",
        "config.toml",
        "data",
        "logs",
        "mnt",
    ]
    (target / "syncmusic").write_text("x")
    (target / "syncmusic").chmod(0o755)
    r = _install(env2, "--uninstall", "--purge", "--dir", str(target))
    assert r.returncode == 0, r.stderr
    assert not target.exists()


def test_dir_checksum_mismatch_installs_nothing(tmp_path):
    env = _fake_release(tmp_path, "0.2.1")
    for sums in tmp_path.glob("release-*/SHA256SUMS"):
        sums.write_text("0" * 64 + "  " + sums.read_text().split()[1] + "\n")
    r = _install(env, "--dir", str(tmp_path / "t"))
    assert r.returncode != 0 and "checksum mismatch" in r.stderr
    assert not (tmp_path / "t" / "syncmusic").exists()


@pytest.mark.parametrize(
    "args, message",
    [
        (["--dir", "X", "--user", "me"], "service install, not --dir"),
        (["--dir", "X", "--install-deps"], "service install, not --dir"),
        (["--dir", "X", "--purge"], "--purge only with --uninstall"),
        (["--dir", "X", "--uninstall"], "not installed in X"),
    ],
)
def test_dir_refusals(tmp_path, args, message):
    r = subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, check=False, cwd=tmp_path
    )
    assert r.returncode != 0 and message in r.stderr
    assert not (tmp_path / "X").exists()
