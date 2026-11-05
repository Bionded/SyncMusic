"""Installing an update of a single-file binary: download + checksum, unpack test, swap with `.previous`, restart,
and the launcher's rollback after a new version that never starts."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import hashlib
import io
import platform
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest

from syncmusic.infra import self_update as su
from tests.unit.test_api_sync import env, music  # noqa: F401

LAUNCHER = Path(__file__).parents[2] / "packaging" / "launcher.in"


def _binary(path: Path, runtime_id: str, python: str) -> Path:
    """A real launcher (packaging/launcher.in) whose payload's `python3` is the shell script `python`."""
    head = (
        LAUNCHER.read_text()
        .replace("@ID@", runtime_id)
        .replace("@ARCH@", platform.machine())
        .replace("@MODE@", "web")
        .replace("@VERSION@", runtime_id)
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = f"#!/bin/sh\n{python}\n".encode()
        info = tarfile.TarInfo("python/bin/python3")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    path.write_bytes(head.encode() + buf.getvalue())
    path.chmod(0o755)
    return path


def _sums(*files: Path) -> str:
    return "".join(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n" for f in files)


class Releases(httpx.AsyncBaseTransport):
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    async def handle_async_request(self, request):
        name = request.url.path.rsplit("/", 1)[-1]
        if name not in self.files:
            return httpx.Response(404)
        return httpx.Response(200, content=self.files[name])


def test_expected_sha256():
    sums = "a" * 64 + "  syncmusic-web-1-linux-x\n" + "b" * 64 + " *other\n"
    assert su.expected_sha256(sums, "syncmusic-web-1-linux-x") == "a" * 64
    assert su.expected_sha256(sums, "other") == "b" * 64
    with pytest.raises(su.Refused, match="no line"):
        su.expected_sha256(sums, "missing")


async def test_download_checks_the_checksum(tmp_path):
    good = tmp_path / "syncmusic-web-2-linux-x"
    good.write_bytes(b"new binary")
    files = {good.name: good.read_bytes(), "SHA256SUMS": _sums(good).encode()}
    asset = {"name": good.name, "url": f"https://example.org/{good.name}"}
    dest = tmp_path / "dl"
    await su.download(asset, "https://example.org/SHA256SUMS", dest, Releases(files))
    assert dest.read_bytes() == b"new binary"
    files[good.name] = b"tampered"
    with pytest.raises(su.Refused, match="checksum mismatch"):
        await su.download(asset, "https://example.org/SHA256SUMS", dest, Releases(files))
    assert not dest.exists()
    with pytest.raises(su.Refused, match="download failed"):
        await su.download(
            {"name": good.name, "url": "https://example.org/gone"},
            "https://example.org/x",
            dest,
            Releases(files),
        )
    with pytest.raises(su.Refused, match="no download"):
        await su.download({}, "", dest)


def test_unpack_swap_outcome_and_confirm(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCMUSIC_RUNTIME_DIR", str(tmp_path / "rt"))
    binary = _binary(tmp_path / "syncmusic", "1-web-x-old", "echo old")
    new = _binary(tmp_path / ".syncmusic.download", "2-web-x-new", 'echo "new $*"')
    assert su.unpack(new, service=False) == "2-web-x-new"
    assert (tmp_path / "rt" / "2-web-x-new" / ".complete").exists()
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\necho kaputt >&2\nexit 3\n")
    broken.chmod(0o755)
    with pytest.raises(su.Refused, match="does not start: kaputt"):
        su.unpack(broken, service=False)
    paths = su.Paths(binary)
    old = binary.read_bytes()
    su.swap(paths, new)
    assert paths.previous.read_bytes() == old and su.runtime_id(binary) == "2-web-x-new"
    assert paths.trial.read_text() == "0\n" and not new.exists()
    marker = tmp_path / "data" / "update.json"
    su.write_marker(marker, "0.2.0", "0.2.1")
    assert su.outcome(marker, paths, "0.2.1") == ("updated", {"from": "0.2.0", "to": "0.2.1"})
    assert marker.exists()  # still on trial until confirmed
    su.confirm(marker, paths)
    assert not marker.exists() and not paths.trial.exists()
    assert su.outcome(marker, paths, "0.2.1") is None
    su.write_marker(marker, "0.2.0", "0.2.1")
    paths.rolled_back.touch()
    assert su.outcome(marker, paths, "0.2.0") == ("rolled_back", {"from": "0.2.0", "to": "0.2.1"})
    assert not marker.exists() and not paths.rolled_back.exists()


def test_launcher_rolls_back_a_version_that_never_starts(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "SYNCMUSIC_RUNTIME_DIR": str(tmp_path / "rt"),
        "HOME": str(tmp_path),
    }
    binary = _binary(tmp_path / "syncmusic", "2-web-x-new", "echo crash >&2; exit 1")
    _binary(tmp_path / "syncmusic.previous", "1-web-x-old", 'echo "old $*"')
    paths = su.Paths(binary)
    paths.trial.write_text("0\n")
    for start in (1, 2, 3):  # systemd restarts it: each start counts
        r = subprocess.run(
            [str(binary), "run"], env=env, capture_output=True, text=True, check=False
        )
        assert r.returncode == 1 and paths.trial.read_text().strip() == str(start)
    r = subprocess.run([str(binary), "run"], env=env, capture_output=True, text=True, check=False)
    assert r.returncode == 0 and r.stdout.strip() == "old -I -m syncmusic run"
    assert "going back to the previous version" in r.stderr
    assert paths.rolled_back.exists() and not paths.trial.exists() and not paths.previous.exists()
    assert su.runtime_id(binary) == "1-web-x-old"
    r = subprocess.run([str(binary)], env=env, capture_output=True, text=True, check=False)
    assert r.stdout.strip() == "old -I -m syncmusic run"  # no trial: a plain start


def test_retarget_sudoers_and_prune_runtimes(tmp_path):
    rules = tmp_path / "sudoers.d"
    rules.mkdir()
    (rules / "syncmusic-alice").write_text(
        "# SyncMusic root helper\nalice ALL=(root) NOPASSWD: /opt/syncmusic/runtime/1-web-x-old/python/bin/syncmusic-priv\n"
    )
    (rules / "other").write_text(
        "bob ALL=(root) NOPASSWD: /opt/syncmusic/runtime/1-web-x-old/python/bin/syncmusic-priv\n"
    )
    helper = su.service_helper("2-web-x-new")
    assert su.retarget_sudoers(helper, rules) == [rules / "syncmusic-alice"]
    assert (rules / "syncmusic-alice").read_text().endswith(f"NOPASSWD: {helper}\n")
    assert "1-web-x-old" in (rules / "other").read_text()  # not ours: untouched
    rt = tmp_path / "runtime"
    for d in ("1-old", "2-prev", "3-cur", ".unpack.x"):
        (rt / d).mkdir(parents=True)
    su.prune_runtimes({"2-prev", "3-cur"}, rt)
    assert sorted(p.name for p in rt.iterdir()) == [".unpack.x", "2-prev", "3-cur"]


async def test_api_install_and_restart(env, monkeypatch, tmp_path):
    app, c = env
    r = await c.post("/api/v1/updates/install")
    assert r.status_code == 409 and "check for updates first" in r.json()["message"]
    monkeypatch.setenv("SYNCMUSIC_RUNTIME_DIR", str(tmp_path / "rt"))
    binary = _binary(tmp_path / "syncmusic", "1-web-x-old", "echo old")
    new = _binary(tmp_path / "syncmusic-web-0.2.1-linux-x", "2-web-x-new", "echo new")
    monkeypatch.setenv("SYNCMUSIC_BINARY", str(binary))
    app.update_transport = Releases({new.name: new.read_bytes(), "SHA256SUMS": _sums(new).encode()})
    u = app.updates
    u.available, u.latest, u.running, u.install_type = True, "0.2.1", "0.2.0", "docker"
    u.asset = {"name": new.name, "url": f"https://example.org/{new.name}"}
    u.sums = "https://example.org/SHA256SUMS"
    r = await c.post("/api/v1/updates/install")
    assert r.status_code == 409 and "does not update itself" in r.json()["message"]
    u.install_type = "standalone"
    restarted = []
    app.on_restart = lambda: restarted.append(True)
    blocker_done = app.jobs.submit("test", "hold", lambda ctx: _sleep(0.2))
    r = (await c.post("/api/v1/updates/install", json={"when": "after_syncs"})).json()
    assert r == {"waiting": True, "version": "0.2.1"} and app.update_pending and not restarted
    await app.jobs.wait(blocker_done.id)
    r = (await c.post("/api/v1/updates/install", json={"when": "now"})).json()
    assert r == {"installing": "0.2.1"} and restarted == [True] and app.restart == "exec"
    assert su.runtime_id(binary) == "2-web-x-new" and su.Paths(binary).previous.exists()
    assert (app.config.data_dir / "update.json").exists()
    assert (await c.post("/api/v1/updates/install", json={"when": "later"})).status_code == 400


async def _sleep(s: float) -> None:
    import asyncio

    await asyncio.sleep(s)


async def test_auto_update_policies(env, monkeypatch, tmp_path):
    from syncmusic.infra import updates
    from tests.unit.test_updates import FakeGitHub

    app, c = env
    monkeypatch.setattr(app.config, "update_repo", "example/SyncMusic")
    monkeypatch.setattr(updates, "running_version", lambda: "0.2.0")
    monkeypatch.setenv("SYNCMUSIC_INSTALL_TYPE", "standalone")
    monkeypatch.setenv("SYNCMUSIC_BINARY", str(tmp_path / "syncmusic"))
    app.update_checker.transport = FakeGitHub()
    installs = []

    async def fake_install():
        installs.append(app.updates.latest)
        return app.updates.latest

    monkeypatch.setattr(app, "install_update", fake_install)
    await app.check_updates()  # default off: notify only
    assert installs == [] and not app.update_pending
    await c.patch("/api/v1/settings", json={"auto_update": "after_syncs"})
    app._update_announced = None
    hold = app.jobs.submit("test", "hold", lambda ctx: _sleep(0.3))
    await app.check_updates()  # a job runs: waits, automatic syncs held back
    assert installs == [] and app.update_pending and app.sync.hold_automatic
    assert (await c.get("/api/v1/updates")).json()["pending"] is True
    await app.jobs.wait(hold.id)
    await app._install_pending()  # what the scheduler does once no job runs
    assert installs == ["0.2.1"] and not app.update_pending and not app.sync.hold_automatic
    await c.patch("/api/v1/settings", json={"auto_update": "now"})
    app._update_announced = None
    hold = app.jobs.submit("test", "hold", lambda ctx: _sleep(0.3))
    await app.check_updates()  # right away, even with a job running
    assert installs == ["0.2.1", "0.2.1"]
    await app.jobs.wait(hold.id)
    monkeypatch.setenv("SYNCMUSIC_INSTALL_TYPE", "docker")
    await c.patch("/api/v1/settings", json={"auto_update": "now"})
    app._update_announced = None
    await app.check_updates()  # Docker: never installs itself
    assert len(installs) == 2
