"""End to end: a web binary updates itself from a release list served over local HTTP, and an update that does not
start is rolled back by the launcher (packaging/launcher.in, infra/self_update.py)."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import httpx
import pytest

from syncmusic import __version__

REPO = Path(__file__).parents[2]
BASH = shutil.which("bash") or "/bin/bash"
ARCH = platform.machine()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.e2e,
    pytest.mark.skipif(ARCH not in ("x86_64", "aarch64"), reason="host target not supported"),
]


def _online() -> bool:
    try:
        httpx.head("https://pypi.org/simple/", timeout=5)
    except httpx.HTTPError:
        return False
    return True


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def builds(tmp_path_factory) -> dict[int, Path]:
    """Web binaries of build 1 and build 2 of this version (prebuilt ones: SYNCMUSIC_TEST_BUILDS=<dir> with
    b1/ and b2/ from `SYNCMUSIC_BUILD_NUMBER=N packaging/build-web --out <dir>/bN`)."""
    root = (
        Path(os.environ["SYNCMUSIC_TEST_BUILDS"])
        if os.environ.get("SYNCMUSIC_TEST_BUILDS")
        else None
    )
    if root is None:
        if not _online():
            pytest.skip("needs network (PyPI, GitHub, python-build-standalone)")
        root = tmp_path_factory.mktemp("builds")
        for n in (1, 2):
            r = subprocess.run(
                [BASH, str(REPO / "packaging" / "build-web"), "--out", str(root / f"b{n}")],
                env={**os.environ, "SYNCMUSIC_BUILD_NUMBER": str(n)},
                capture_output=True,
                text=True,
                timeout=1800,
            )
            assert r.returncode == 0, r.stderr + r.stdout
    return {n: root / f"b{n}" / f"syncmusic-web-{__version__}-{n}-linux-{ARCH}" for n in (1, 2)}


class Release:
    """A releases list in GitHub's shape plus its assets, served from a folder."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir()
        handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
        handler.log_message = lambda *a: None  # type: ignore[method-assign]
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, tag: str, binary: Path) -> None:
        name = f"syncmusic-web-{tag}-linux-{ARCH}"
        shutil.copy2(binary, self.folder / name)
        digest = hashlib.sha256((self.folder / name).read_bytes()).hexdigest()
        (self.folder / "SHA256SUMS").write_text(f"{digest}  {name}\n")
        assets = [
            {"name": n, "browser_download_url": f"{self.url}/{n}", "size": 1}
            for n in (name, "SHA256SUMS")
        ]
        doc = [{"tag_name": f"v{tag}", "prerelease": False, "body": "- test", "assets": assets}]
        (self.folder / "releases").write_text(json.dumps(doc))


class Instance:
    """The binary at `binary` run by hand (install type standalone) on a free port."""

    def __init__(self, binary: Path, home: Path, releases: str) -> None:
        self.binary = binary
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/api/v1"
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("SYNCMUSIC_")},
            "SYNCMUSIC_RUNTIME_DIR": str(home / "rt"),
            "SYNCMUSIC_DATA_DIR": str(home / "data"),
            "SYNCMUSIC_CACHE_DIR": str(home / "cache"),
            "SYNCMUSIC_LOG_DIR": str(home / "log"),
            "SYNCMUSIC_HOST": "127.0.0.1",
            "SYNCMUSIC_PORT": str(self.port),
            "SYNCMUSIC_DETECT_DEVICES": "false",
            "SYNCMUSIC_UPDATE_REPO": releases,
            # never the config of a SyncMusic installed on this host
            "SYNCMUSIC_CONFIG": os.devnull,
        }
        self.log_path = home / "out.log"
        self.log = self.log_path.open("ab")
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> subprocess.Popen[bytes]:
        self.proc = subprocess.Popen(
            [str(self.binary), "run"], env=self.env, stdout=self.log, stderr=subprocess.STDOUT
        )
        return self.proc

    def running(self, timeout: float = 300) -> str:
        """The version that answers, once it does."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            assert self.proc is not None and self.proc.poll() is None, (
                "the instance exited: " + self.log_path.read_text(errors="replace")[-2000:]
            )
            try:
                return str(httpx.get(f"{self.url}/updates", timeout=5).json()["running"])
            except (httpx.HTTPError, ValueError, KeyError):
                time.sleep(1)
        raise AssertionError("the instance did not answer")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()


@pytest.fixture
def instance(builds, tmp_path) -> Iterator[tuple[Instance, Release]]:
    release = Release(tmp_path / "release")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy2(builds[1], bin_dir / "syncmusic")
    inst = Instance(bin_dir / "syncmusic", tmp_path, f"{release.url}/releases")
    yield inst, release
    inst.stop()
    release.server.shutdown()


def _install(inst: Instance, latest: str) -> None:
    u = httpx.post(f"{inst.url}/updates/check", timeout=30).json()
    assert u["error"] is None and u["available"] and u["latest"] == latest, u
    assert u["install_type"] == "standalone" and u["installable"], u
    r = httpx.post(f"{inst.url}/updates/install", json={"when": "now"}, timeout=120)
    assert r.status_code == 200, r.text


def test_update_to_a_newer_build_then_roll_back_one_that_does_not_start(builds, instance):
    inst, release = instance
    paths = {
        s: inst.binary.with_name("syncmusic" + s) for s in (".previous", ".trial", ".rolled-back")
    }
    inst.start()
    assert inst.running() == f"{__version__}-1"

    # build 2 is published: downloaded, checked, swapped in, and the process execs itself into it
    release.publish(f"{__version__}-2", builds[2])
    first = inst.proc
    _install(inst, f"{__version__}-2")
    time.sleep(2)
    assert inst.running() == f"{__version__}-2"
    assert inst.proc is first  # the same process (exec), not a new one
    assert paths[".previous"].read_bytes() == builds[1].read_bytes()
    assert paths[".trial"].exists()  # until it ran healthy for a minute
    assert inst.binary.read_bytes() == builds[2].read_bytes()

    # build 3 unpacks fine (`--help`) but exits at once when run
    broken = inst.binary.parent.parent / "broken"
    text = builds[2].read_bytes()
    start = b"[ $# -gt 0 ] || set -- run\n"
    assert start in text
    broken.write_bytes(text.replace(start, b'[ "${1:-}" = --help ] || exit 3\n', 1))
    release.publish(f"{__version__}-3", broken)
    _install(inst, f"{__version__}-3")
    assert inst.proc.wait(120) == 3
    assert inst.binary.read_bytes() == broken.read_bytes()
    # run by hand, so restarted by hand: the launcher counts the failed starts, the 4th one goes back
    for _ in range(2):
        assert inst.start().wait(120) == 3
    inst.start()
    assert inst.running() == f"{__version__}-2"
    assert inst.binary.read_bytes() == builds[2].read_bytes()
    assert not paths[".previous"].exists()
    # the app reported the rollback and cleared the trial
    deadline = time.monotonic() + 30
    while paths[".rolled-back"].exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert not paths[".rolled-back"].exists() and not paths[".trial"].exists()
    assert not (inst.binary.parent.parent / "data" / "update.json").exists()
