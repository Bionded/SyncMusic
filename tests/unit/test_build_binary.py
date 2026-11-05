"""Single-file Linux binaries packaging/build-desktop and build-web (single file)."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import platform
import shutil
import struct
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest

from syncmusic import __version__

REPO = Path(__file__).parents[2]
PACKAGING = REPO / "packaging"
SCRIPT = PACKAGING / "build-desktop"
E_MACHINE = {"x86_64": 62, "aarch64": 183}
BASH = shutil.which("bash") or "/bin/bash"


def _run(*args: str, env: dict[str, str] | None = None, timeout: int = 60, script: Path = SCRIPT):
    return subprocess.run(
        [BASH, str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def test_help():
    r = _run("--help")
    assert r.returncode == 0
    assert (
        "--target" in r.stdout
        and "Examples:" in r.stdout
        and "packaging/build-web --target all" in r.stdout
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--target", "riscv64"), "unsupported target: riscv64"),
        (("--target", "linux-x86_64,linux-armv7"), "unsupported target: linux-armv7"),
        (("--target",), "--target needs a value"),
        (("--target", "x86_64", "--bogus"), "unknown argument: --bogus"),
        (("--mode", "tui"), "unsupported mode: tui"),
    ],
)
def test_bad_arguments(args, message):
    r = _run(*args)
    assert r.returncode == 1 and message in r.stderr


def test_missing_tools(tmp_path):
    env = {**os.environ, "UV": str(tmp_path / "no-uv")}
    r = _run("--target", "x86_64", "--out", str(tmp_path / "out"), env=env)
    assert r.returncode == 1 and "uv not found" in r.stderr
    # a PATH with everything but curl
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("dirname", "sed", "tar", "gzip", "sha256sum", "python3", "uv"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    env = {**os.environ, "PATH": str(bin_dir)}
    env.pop("UV", None)
    r = _run("--target", "x86_64", "--out", str(tmp_path / "out"), env=env)
    assert r.returncode == 1 and "curl not found" in r.stderr
    assert not (tmp_path / "out").exists()


def _machine(data: bytes) -> int:
    assert data[:4] == b"\x7fELF"
    return int(struct.unpack("<H", data[18:20])[0])


def _online() -> bool:
    try:
        return httpx.head("https://pypi.org/simple/", timeout=10).status_code < 500
    except httpx.HTTPError:
        return False


def _payload(binary: Path) -> tarfile.TarFile:
    data = binary.read_bytes()
    marker = b"\n__PAYLOAD__\n"
    return tarfile.open(fileobj=io.BytesIO(data[data.index(marker) + len(marker) :]))


@pytest.mark.slow
@pytest.mark.skipif(
    not shutil.which("uv") and not Path.home().joinpath(".local/bin/uv").exists(), reason="uv"
)
def test_build_both_targets(tmp_path):
    if not _online():
        pytest.skip("needs network (PyPI, GitHub, python-build-standalone)")
    out = tmp_path / "out"
    r = _run("--target", "linux-x86_64,linux-aarch64", "--out", str(out), timeout=1800)
    assert r.returncode == 0, r.stderr + r.stdout
    version = __version__
    host = platform.machine()
    for arch, machine in E_MACHINE.items():
        name = f"syncmusic-desktop-{version}-linux-{arch}"
        art = out / name
        assert f"artifact: {art}" in r.stdout
        assert art.stat().st_mode & 0o111 and art.read_bytes().startswith(b"#!/bin/sh")
        digest = hashlib.sha256(art.read_bytes()).hexdigest()
        assert (out / f"{name}.sha256").read_text() == f"{digest}  {name}\n"
        assert f"sha256:   {digest}" in r.stdout
        assert f"ELF files, all {arch}" in r.stdout
        if arch == host:
            assert f"smoke run ok ({name} --help)" in r.stdout
        with _payload(art) as tar:
            names = [n.removeprefix("./") for n in tar.getnames()]

            def read(member: str, tar: tarfile.TarFile = tar) -> bytes:
                f = tar.extractfile(tar.getmember(f"./{member}"))
                assert f is not None
                return f.read()

            # the binaries are for the labelled architecture, not the host's
            py = next(n for n in names if n.startswith("python/bin/python3."))
            assert _machine(read(py)) == machine
            pydantic_so = next(
                n for n in names if "/pydantic_core/_pydantic_core" in n and n.endswith(".so")
            )
            assert _machine(read(pydantic_so)) == machine
            build = next(n for n in names if n.endswith("/site-packages/syncmusic/_build.py"))
            assert b'DEFAULT_MODE = "desktop"' in read(build)
            site = build[: -len("syncmusic/_build.py")]
            record = read(f"{site}syncmusic-{version}.dist-info/RECORD").decode()
            digest = base64.urlsafe_b64encode(hashlib.sha256(read(build)).digest())
            assert f"syncmusic/_build.py,sha256={digest.rstrip(b'=').decode()}," in record
            assert "uv_cache.json" not in record and "direct_url.json" not in record
            assert not [
                n
                for n in names
                if n.endswith(".pyc") or "/.git/" in n or "/site-packages/tests/" in n
            ]
    assert not (out / "work").exists()

    # same inputs -> byte-identical binary (downloads are reused and re-verified)
    first = (out / f"syncmusic-desktop-{version}-linux-aarch64.sha256").read_text()
    r = _run("--target", "arm64", "--out", str(out), timeout=1800)
    assert r.returncode == 0, r.stderr
    assert (out / f"syncmusic-desktop-{version}-linux-aarch64.sha256").read_text() == first


@pytest.mark.slow
@pytest.mark.skipif(platform.machine() not in E_MACHINE, reason="host target not supported")
def test_web_binary_for_the_host_unpacks_once_and_serves(tmp_path):
    if not _online():
        pytest.skip("needs network (PyPI, GitHub, python-build-standalone)")
    out = tmp_path / "out"
    r = _run(
        "--out", str(out), timeout=1800, script=PACKAGING / "build-web"
    )  # no --target: the host
    assert r.returncode == 0, r.stderr + r.stdout
    art = out / f"syncmusic-web-{__version__}-linux-{platform.machine()}"
    assert r.stdout.count("artifact: ") == 1 and art.exists()
    env = {**os.environ, "SYNCMUSIC_RUNTIME_DIR": str(tmp_path / "rt")}
    procs = [  # two first runs at once share one unpacked runtime
        subprocess.Popen([str(art), "--help"], env=env, stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    for p in procs:
        stdout, _ = p.communicate(timeout=300)
        assert p.returncode == 0 and "Usage: syncmusic" in stdout
    (runtime,) = (tmp_path / "rt").iterdir()
    assert (
        runtime.name.startswith(f"{__version__}-web-{platform.machine()}-")
        and (runtime / ".complete").exists()
    )
    shown = subprocess.run(
        [BASH, "-c", 'cd / && "$0" --help', str(art)], env=env, capture_output=True, text=True
    )
    assert shown.returncode == 0 and "Usage: syncmusic" in shown.stdout
