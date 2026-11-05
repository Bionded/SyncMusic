"""`syncmusic --install` / `--uninstall` of the web binary as root on a Debian with systemd (a privileged container,
tests/integration/systemd/Dockerfile): the service starts and answers, a second `--install` of a newer build
upgrades it, `--uninstall` leaves no unit, sudoers rule, binary or runtime but keeps the data, `--purge` removes the
data and the config too."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from syncmusic import __version__
from tests.unit.test_self_update_e2e import ARCH, builds  # noqa: F401

pytestmark = [
    pytest.mark.slow,
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker"),
]

IMAGE = "syncmusic-systemd:it"
CONTEXT = Path(__file__).parent / "systemd"
PORT = 8090


def _docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def box(builds) -> Iterator[str]:  # noqa: F811
    """A booted systemd container with both builds under /b/1 and /b/2 (read-only)."""
    r = _docker("build", "-q", "-t", IMAGE, str(CONTEXT), timeout=900)
    assert r.returncode == 0, r.stderr
    name = f"syncmusic-systemd-{uuid.uuid4().hex[:8]}"
    r = _docker(
        "run", "-d", "--rm", "--name", name, "--privileged", "--cgroupns=private",
        "--memory", "1g", "--tmpfs", "/run", "--tmpfs", "/run/lock",
        "-v", f"{builds[1]}:/b/1:ro", "-v", f"{builds[2]}:/b/2:ro",
        IMAGE,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    try:
        deadline = time.monotonic() + 60
        while _sh(name, "systemctl is-system-running").stdout.strip() not in (
            "running",
            "degraded",
        ):
            assert time.monotonic() < deadline, "systemd did not boot"
            time.sleep(1)
        yield name
    finally:
        _docker("rm", "-f", name)


def _sh(box: str, command: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return _docker("exec", box, "sh", "-c", command, timeout=timeout)


def _ok(box: str, command: str) -> str:
    r = _sh(box, command)
    assert r.returncode == 0, f"{command}: {r.stdout}{r.stderr}"
    return r.stdout


def _running(box: str) -> dict[str, object]:
    """The service's update info, once it answers."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        r = _sh(box, f"curl -sf http://127.0.0.1:{PORT}/api/v1/updates")
        if r.returncode == 0:
            return dict(json.loads(r.stdout))
        time.sleep(1)
    raise AssertionError(_sh(box, "journalctl -u syncmusic --no-pager | tail -30").stdout)


def _exists(box: str, path: str) -> bool:
    return _sh(box, f"test -e {path}").returncode == 0


def test_install_upgrade_uninstall_purge(box):
    _ok(box, f"/b/1 --install --port {PORT} --user tester")
    _ok(box, "systemctl enable --now syncmusic")
    u = _running(box)
    assert u["running"] == f"{__version__}-1" and u["install_type"] == "service"
    assert _ok(box, "systemctl is-active syncmusic").strip() == "active"
    rule = _ok(box, "cat /etc/sudoers.d/syncmusic-tester")
    assert f"/opt/syncmusic/runtime/{__version__}-1-web-{ARCH}-" in rule
    _ok(box, "visudo -cqf /etc/sudoers.d/syncmusic-tester")

    # a newer build installed over it: the same unit, config and data, the new binary
    _ok(box, f"/b/2 --install --port {PORT} --user tester")
    _ok(box, "systemctl restart syncmusic")
    assert _running(box)["running"] == f"{__version__}-2"
    rule = _ok(box, "cat /etc/sudoers.d/syncmusic-tester")
    assert f"/opt/syncmusic/runtime/{__version__}-2-web-{ARCH}-" in rule
    assert _ok(box, "cmp /b/2 /usr/local/bin/syncmusic && echo same").strip() == "same"

    _ok(box, "/usr/local/bin/syncmusic --uninstall")
    for gone in (
        "/etc/systemd/system/syncmusic.service",
        "/etc/sudoers.d/syncmusic-tester",
        "/usr/local/bin/syncmusic",
        "/opt/syncmusic",
    ):
        assert not _exists(box, gone), gone
    assert _sh(box, "systemctl is-active syncmusic").stdout.strip() != "active"
    assert _exists(box, "/etc/syncmusic/config.toml")
    assert _ok(box, "ls /var/lib/syncmusic").strip()  # the database stays

    _ok(box, f"/b/2 --install --port {PORT}")
    _ok(box, "/usr/local/bin/syncmusic --uninstall --purge")
    for gone in (
        "/etc/systemd/system/syncmusic.service",
        "/usr/local/bin/syncmusic",
        "/opt/syncmusic",
        "/etc/syncmusic",
        "/var/lib/syncmusic",
    ):
        assert not _exists(box, gone), gone
