"""System check: every doctor check with faked probes, the CLI's exit code and the API's `fix`."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

from typer.testing import CliRunner

from syncmusic.infra import doctor
from syncmusic.infra.config import load_config

Usage = namedtuple("Usage", "total used free")


def _cfg(tmp_path: Path, **kw):
    return load_config(
        env={},
        overrides={"data_dir": tmp_path / "data", "cache_dir": tmp_path / "cache", **kw},
    )


def _ff(**kw):
    return lambda: {"path": "ffmpeg", "version": "7.1", "missing": [], "error": None, **kw}


def test_ffmpeg_states(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert doctor.check_ffmpeg(_ff())["state"] == "ok"
    missing = doctor.check_ffmpeg(_ff(missing=["libmp3lame"]))
    assert missing["state"] == "warning" and "libmp3lame" in missing["detail"] and missing["fix"]
    gone = doctor.check_ffmpeg(_ff(error="ffmpeg does not run at ffmpeg: FileNotFoundError"))
    assert gone["state"] == "error" and "apt install ffmpeg" in gone["fix"]
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    no_probe = doctor.check_ffmpeg(_ff())
    assert no_probe["state"] == "error" and "ffprobe" in no_probe["detail"]


def test_space_warns_below_the_threshold(tmp_path):
    low = doctor.check_space(
        tmp_path / "d", tmp_path / "c", 5.0, usage=lambda p: Usage(0, 0, 2 * doctor.GIB)
    )
    assert low["state"] == "warning" and "2.0 GB free" in low["detail"] and low["fix"]
    ok = doctor.check_space(
        tmp_path / "d", tmp_path / "c", 5.0, usage=lambda p: Usage(0, 0, 50 * doctor.GIB)
    )
    assert ok["state"] == "ok" and ok["fix"] == ""
    assert (
        doctor.check_space(tmp_path, tmp_path, None, usage=lambda p: Usage(0, 0, 0))["state"]
        == "ok"
    )


def test_dirs_not_writable(tmp_path):
    assert doctor.check_dirs(_cfg(tmp_path))["state"] == "ok"  # missing but creatable
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        bad = doctor.check_dirs(_cfg(tmp_path, data_dir=locked / "data"))
        assert bad["state"] == "error" and "data" in bad["detail"] and "chown" in bad["fix"]
    finally:
        locked.chmod(0o700)


def test_privileges():
    assert doctor.check_privileges(euid=lambda: 0)["state"] == "ok"
    helper = doctor.check_privileges(
        euid=lambda: 1000, route=lambda: ["sudo", "-n", "/x/syncmusic-priv"]
    )
    assert helper["state"] == "ok" and "helper" in helper["detail"]
    sudo = doctor.check_privileges(
        euid=lambda: 1000, route=lambda: ["sudo", "-n"], sudo_ok=lambda *a: True
    )
    assert sudo["state"] == "ok" and "sudo" in sudo["detail"]
    none = doctor.check_privileges(
        euid=lambda: 1000, route=lambda: ["sudo", "-n"], sudo_ok=lambda *a: False
    )
    assert none["state"] == "error" and "--install --user" in none["fix"]


def test_udev_clock_docker_port(tmp_path):
    cfg = _cfg(tmp_path)
    assert doctor.check_udev(cfg, control=tmp_path / "nope")["state"] == "warning"
    (tmp_path / "control").touch()
    assert doctor.check_udev(cfg, control=tmp_path / "control")["state"] == "ok"
    assert (
        doctor.check_udev(_cfg(tmp_path, detect_devices=False), control=tmp_path / "nope")["state"]
        == "ok"
    )

    assert doctor.check_clock(run=lambda cmd: "yes\n")["state"] == "ok"
    assert doctor.check_clock(run=lambda cmd: "no\n")["fix"] == "sudo timedatectl set-ntp true"
    assert doctor.check_clock(run=lambda cmd: None)["state"] == "unknown"

    assert doctor.check_docker(cfg, dockerenv=tmp_path / "nope") is None
    dev = tmp_path / "dev"
    dev.mkdir()
    assert doctor.check_docker(cfg, dockerenv=tmp_path / "control", dev=dev)["state"] == "warning"
    (dev / "sda").touch()
    assert doctor.check_docker(cfg, dockerenv=tmp_path / "control", dev=dev)["state"] == "ok"

    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = _cfg(tmp_path, host="127.0.0.1", port=s.getsockname()[1])
        assert doctor.check_port(busy, None)["state"] == "error"
        assert doctor.check_port(busy, "http://127.0.0.1:8080")["state"] == "ok"
        # the URL that answered is named, not the configured port
        assert "127.0.0.1:8080" in doctor.check_port(busy, "http://127.0.0.1:8080")["detail"]


def test_cli_doctor_exit_code_and_json(tmp_path, monkeypatch):
    import json

    from syncmusic.cli.main import cli

    failing = [doctor.item("ffmpeg", "ffmpeg", "gone", "error", "sudo apt install ffmpeg")]
    monkeypatch.setattr(doctor, "local_checks", lambda cfg: failing)
    args = ["--data-dir", str(tmp_path), "doctor", "--url", "http://127.0.0.1:9"]
    out = CliRunner().invoke(cli, args)
    assert (
        out.exit_code == 1
        and "FAIL  ffmpeg: gone" in out.output
        and "fix: sudo apt install ffmpeg" in out.output
    )
    monkeypatch.setattr(doctor, "local_checks", lambda cfg: [doctor.item("x", "X", "fine", "ok")])
    out = CliRunner().invoke(cli, [*args, "--json"])
    assert out.exit_code == 0
    assert [c["id"] for c in json.loads(out.output)] == ["x", "port"]


def test_usage_check_reports_data_and_cache(tmp_path):
    from syncmusic.infra import doctor
    from syncmusic.infra.config import BootstrapConfig

    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x").write_bytes(b"x" * 1000)
    cfg = BootstrapConfig(data_dir=tmp_path / "d", cache_dir=tmp_path / "c")
    c = doctor.check_usage(cfg)
    assert c["state"] == "ok" and c["detail"].startswith("data 0.00 GB · cache 0.00 GB")
