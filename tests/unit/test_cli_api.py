"""CLI over REST: every command against a real in-process server (uvicorn in a thread)."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn
from typer.testing import CliRunner

from syncmusic.api.server import create_api
from syncmusic.app import Application
from syncmusic.cli.main import cli
from syncmusic.facade import AppFacade
from tests.unit.test_api_sync import music  # noqa: F401


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def server(config, music):
    app = Application(config)
    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(
            create_api(AppFacade(app)), host="127.0.0.1", port=port, log_config=None, lifespan="on"
        )
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(500):
        if srv.started:
            break
        time.sleep(0.02)
    url = f"http://127.0.0.1:{port}"
    yield url, music
    srv.should_exit = True
    thread.join(30)


def run(url: str, *args: str, input: str | None = None):
    return CliRunner().invoke(cli, [*args, "--url", url], input=input)


def ok(url: str, *args: str, input: str | None = None) -> str:
    r = run(url, *args, input=input)
    assert r.exit_code == 0, (args, r.output)
    return r.output


def err(url: str, *args: str) -> str:
    r = run(url, *args)
    assert r.exit_code == 1 and "Traceback" not in r.output, (args, r.output)
    return r.output


def _library(url: str, music) -> None:
    ok(
        url,
        "libraries",
        "add",
        "music",
        "--type",
        "folder",
        "-s",
        f"root={music}",
        "--name",
        "Музика",
    )


def test_libraries(server):
    url, music = server
    assert "no libraries" in ok(url, "libraries", "list")
    assert "added music" in ok(
        url, "libraries", "add", "music", "--type", "folder", "-s", f"root={music}"
    )
    assert "music: music [folder]" in ok(url, "libraries", "list")
    listed = json.loads(ok(url, "libraries", "list", "--json"))
    assert listed[0]["settings"] == {"root": str(music)}
    out = ok(url, "libraries", "check", "music")
    assert out.startswith("ok") and not out.rstrip("\n").endswith(":")
    assert "music: 3 tracks, 0 playlists" in ok(url, "libraries", "refresh", "music")
    assert json.loads(ok(url, "libraries", "refresh", "music", "--json")) == {
        "tracks": 3,
        "playlists": 0,
    }
    out = err(url, "libraries", "add", "x", "--type", "nope")
    assert "invalid: unknown library type 'nope'" in out
    assert "not_found" in err(url, "libraries", "refresh", "nope")
    assert "expected key=value" in err(
        url, "libraries", "add", "y", "--type", "folder", "-s", "root"
    )


def test_unreachable_server_is_one_line():
    out = err(f"http://127.0.0.1:{_free_port()}", "jobs")
    assert out.startswith("cannot reach") and len(out.strip().splitlines()) == 1


def test_devices_sync_jobs_logs(server, tmp_path):
    url, music = server
    _library(url, music)
    flash = tmp_path / "flash"
    flash.mkdir()
    out = ok(
        url,
        "devices",
        "register",
        "Flash",
        "--mount-path",
        str(flash),
        "--type",
        "flashdrive",
        "-l",
        "music",
    )
    assert "registered flash (flashdrive)" in out
    assert "flash: Flash [flashdrive] connected" in ok(url, "devices", "list")
    assert json.loads(ok(url, "devices", "list", "--json"))["registered"][0]["id"] == "flash"

    # -l links the library's "All …" default
    assert json.loads(ok(url, "devices", "list", "--json"))["registered"][0]["libraries"] == [
        "music"
    ]
    assert not list(flash.glob("**/*.mp3"))

    assert "3 items" in ok(url, "rules", "test", "flash")  # the linked default
    tpl = tmp_path / "all.yaml"
    tpl.write_text("rules:\n  - id: all\n    criteria: {all: []}\n")
    assert "3 items" in ok(url, "rules", "test", "flash", "--file", str(tpl))

    # a template linked to the device, then a sync with --yes (first sync -> keep)
    import httpx

    httpx.post(
        f"{url}/api/v1/templates",
        json={"id": "all", "name": "All", "rules": [{"id": "all", "criteria": {"all": []}}]},
    )
    assert "all linked to flash" in ok(url, "templates", "apply", "all", "flash")
    assert "already linked" in ok(url, "templates", "apply", "all", "flash")
    out = ok(url, "sync", "flash", "--plan-only")
    assert "add 3" in out and "needs confirmation: first sync" in out
    report = json.loads(ok(url, "sync", "flash", "--yes", "--json"))
    assert report["status"] == "succeeded" and report["counts"]["add"] == 3
    assert len(list(flash.glob("**/*.mp3"))) == 3
    out = ok(url, "sync", "flash", "--yes")
    assert "succeeded" in out and "keep 3" in out
    assert not out.splitlines()[0].endswith(":")
    jobs = json.loads(ok(url, "jobs", "--json"))
    assert {j["kind"] for j in jobs} == {"sync"}
    assert "succeeded" in ok(url, "jobs")
    sync_job = next(j for j in jobs if j["kind"] == "sync")
    ok(url, "logs", "--job", sync_job["id"])  # the job's own log
    ok(url, "logs", "-n", "5")  # the app log (empty in tests: stdout only)
    assert "not_found" in err(url, "logs", "--job", "nope")
    assert "not_found" in err(url, "sync", "nope")
    assert "confirm" in err(url, "devices", "format", "flash", "--confirm", "wrong name")
    assert "flash renamed to Stick 2" in ok(url, "devices", "rename", "flash", "Stick 2")
    assert "Stick 2" in ok(url, "devices", "list")


def test_sync_asks_before_applying(server, tmp_path):
    url, music = server
    _library(url, music)
    flash = tmp_path / "flash"
    flash.mkdir()
    ok(
        url,
        "devices",
        "register",
        "Flash",
        "--mount-path",
        str(flash),
        "--type",
        "flashdrive",
        "-l",
        "music",
    )
    import httpx

    httpx.patch(
        f"{url}/api/v1/devices/flash",
        json={"rule_set": {"rules": [{"id": "all", "criteria": {"all": []}}]}},
    )
    r = run(url, "sync", "flash", input="keep\nn\n")
    assert r.exit_code == 1 and "cancelled" in r.output and not list(flash.glob("**/*.mp3"))
    out = ok(url, "sync", "flash", input="adopt\ny\n")
    assert "succeeded" in out and len(list(flash.glob("**/*.mp3"))) == 3


def test_rules_validate_and_templates(server, tmp_path):
    url, _ = server
    good = tmp_path / "good.nsp"
    good.write_text('{"all": [{"is": {"loved": true}}]}')
    assert ok(url, "rules", "validate", str(good)).startswith("ok")
    bad = tmp_path / "bad.json"
    bad.write_text('{"all": [{"nonsense": {"x": 1}}]}')
    r = run(url, "rules", "validate", str(bad))
    assert r.exit_code == 1 and "INVALID" in r.output
    assert json.loads(run(url, "rules", "validate", str(bad), "--json").output)["ok"] is False

    assert ok(url, "templates", "list").strip() == "no templates"  # no library, no defaults
    import httpx

    httpx.post(
        f"{url}/api/v1/templates",
        json={
            "id": "rock",
            "name": "Рок",
            "rules": [{"id": "r", "criteria": {"is": {"genre": "Rock"}}}],
        },
    )
    assert "rock: Рок (1 rule)" in ok(url, "templates", "list")
    out = ok(url, "templates", "show", "rock")
    assert "rule r:" in out and "used by: -" in out
    assert json.loads(ok(url, "templates", "show", "rock", "--json"))["used_by"] == []
    assert "not_found" in err(url, "templates", "show", "nope")
    assert "not_found" in err(url, "templates", "apply", "nope", "dev")


@pytest.mark.parametrize("content", [None, b"\xff\xfe{"])
def test_unreadable_rule_file_is_one_line(server, tmp_path, content):
    url, _ = server
    path = tmp_path / "rules.json"
    if content is not None:
        path.write_bytes(content)
    r = run(url, "rules", "validate", str(path))
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit), r.exception
    assert str(path) in r.output and len(r.output.strip().splitlines()) == 1


def test_unreadable_rule_file_for_rules_test(server, tmp_path):
    url, _ = server
    path = tmp_path / "nope.json"
    r = run(url, "rules", "test", "flash", "--file", str(path))
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit), r.exception
    assert str(path) in r.output and len(r.output.strip().splitlines()) == 1


def test_update_command(server, monkeypatch):
    url, _ = server
    r = run(url, "update", "--check")
    assert r.exit_code == 1 and "update checks are off" in r.output
    r = run(url, "update", "--channel", "nightly")
    assert r.exit_code == 1 and "stable or beta" in r.output


def test_devices_allowlist(server, tmp_path):
    url, _ = server
    out = tmp_path / "hardware.toml"
    r = run(url, "devices", "allowlist", "--all", "-o", str(out))
    assert r.exit_code == 0, r.output
    assert out.read_text().startswith("# Hardware allowlist") and "format every device" in r.output
    r = run(url, "devices", "allowlist", "--device", "nope")
    assert r.exit_code == 1 and "unknown device 'nope'" in r.output
    r = run(url, "devices", "allowlist", input="x\n")
    assert r.exit_code == 1 and "no registered devices" in r.output
