"""The Docker image: built locally by packaging/docker/build.sh, run like the compose `network`
variant (unprivileged, no udev) on the integration stack's network; healthy, writes its default config, and reaches
the compose Navidrome as a library."""

from __future__ import annotations

import json
import platform
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.integration import services

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO = Path(__file__).parents[2]
IMAGE = "syncmusic:it"
NETWORK = f"{services.project()}_default"  # tests/integration/compose.yaml's project network


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _arch() -> str:
    return {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(what: str, ready, timeout: float) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if ready():
            return
        time.sleep(1)
    pytest.fail(f"timed out waiting for {what}")


def _health(url: str) -> bool:
    try:
        return httpx.get(f"{url}/api/v1/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def test_network_image_serves_and_reaches_navidrome(request):
    if _docker("buildx", "version").returncode != 0:
        pytest.skip("docker buildx not installed (packaging/docker/build.sh builds with it)")
    services = request.getfixturevalue(
        "services"
    )  # the compose stack only once the image can be built
    build = subprocess.run(
        ["bash", str(REPO / "packaging/docker/build.sh"), "--tag", IMAGE],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert build.returncode == 0, build.stderr[-3000:]
    assert "nothing pushed" in build.stdout
    name = f"syncmusic-it-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    run = _docker(
        "run", "-d", "--name", name, "--network", NETWORK, "--memory", "1g",
        "-e", "SYNCMUSIC_DETECT_DEVICES=false", "-p", f"127.0.0.1:{port}:8080",
        "--health-interval", "2s", IMAGE,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    try:
        url = f"http://127.0.0.1:{port}"
        _wait("the web server", lambda: _health(url), 120)

        def healthy() -> bool:
            state = json.loads(_docker("inspect", name).stdout)[0]["State"]
            return state.get("Health", {}).get("Status") == "healthy"

        _wait("the image's HEALTHCHECK", healthy, 60)
        config = _docker("exec", name, "cat", "/config/config.toml")
        assert config.returncode == 0 and "# port = " in config.stdout  # written on first start
        user = _docker("exec", name, "sh", "-c", "command -v ffmpeg && id -u")
        assert user.returncode == 0 and "ffmpeg" in user.stdout

        r = httpx.post(
            f"{url}/api/v1/libraries",
            json={
                "id": "nd",
                "type": "subsonic",
                "name": "Navidrome",
                "settings": {
                    "url": "http://navidrome:4533",
                    "username": services["NAVIDROME_USER"],
                    "password": services["NAVIDROME_PASSWORD"],
                },
            },
            timeout=30,
        )
        assert r.status_code < 300, r.text
        check = httpx.post(f"{url}/api/v1/libraries/nd/check", timeout=60)
        assert check.status_code == 200, check.text
        assert check.json()["ok"] is True, check.json()
    finally:
        logs = _docker("logs", "--tail", "50", name).stderr
        _docker("rm", "-f", name)
        _docker("rmi", IMAGE, f"{IMAGE}-{_arch()}")  # the layers stay cached for the next build
    assert "Traceback" not in logs, logs
