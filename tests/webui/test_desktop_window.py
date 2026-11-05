"""Desktop mode with the web UI: a Chromium-family browser in app mode is the window; closing it stops
the application, stopping the application closes it."""

from __future__ import annotations

import asyncio
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from syncmusic.webui import window
from tests.webui.xvfb import _close_like_a_user, _health, _wait, display  # noqa: F401 - fixture


def _fake_browser(tmp_path: Path, body: str) -> str:
    script = tmp_path / "browser"
    script.write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/args\n{body}\n')
    script.chmod(0o755)
    return str(script)


def test_find_browser_prefers_the_env_override(monkeypatch):
    assert window.find_browser({"SYNCMUSIC_BROWSER": "/opt/x/chrome"}) == "/opt/x/chrome"
    monkeypatch.setattr(
        window.shutil, "which", lambda n: "/usr/bin/chromium" if n == "chromium" else None
    )
    assert window.find_browser({}) == "/usr/bin/chromium"
    monkeypatch.setattr(window.shutil, "which", lambda n: None)
    assert window.find_browser({}) is None


async def test_closing_the_window_sets_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCMUSIC_BROWSER", _fake_browser(tmp_path, "exit 0"))
    stop = asyncio.Event()
    code = await window.run_window("http://127.0.0.1:1/", stop, tmp_path / "profile")
    assert code == 0 and stop.is_set()
    args = (tmp_path / "args").read_text()
    assert "--app=http://127.0.0.1:1/" in args and f"--user-data-dir={tmp_path / 'profile'}" in args


async def test_stopping_the_app_closes_the_window(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCMUSIC_BROWSER", _fake_browser(tmp_path, "exec sleep 60"))
    stop = asyncio.Event()
    task = asyncio.ensure_future(window.run_window("http://x/", stop, tmp_path / "p"))
    await asyncio.sleep(0.3)
    stop.set()
    assert await asyncio.wait_for(task, 15) is None  # terminated by us, not closed by the user


async def test_without_a_browser_the_default_browser_opens(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNCMUSIC_BROWSER", raising=False)
    monkeypatch.setattr(window, "find_browser", lambda env=None: None)
    opened = []
    monkeypatch.setattr(window.webbrowser, "open", lambda url: opened.append(url))
    stop = asyncio.Event()
    stop.set()
    assert await window.run_window("http://x/", stop, tmp_path / "p") is None
    assert opened == ["http://x/"]


def _playwright_chromium() -> str | None:
    found = glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    return found[0] if found else None


@pytest.mark.ui
@pytest.mark.slow
@pytest.mark.skipif(
    not (shutil.which("Xvfb") and shutil.which("xdotool") and _playwright_chromium()),
    reason="needs Xvfb, xdotool and Playwright's Chromium",
)
def test_desktop_window_shows_the_ui_and_close_stops_the_app(display, tmp_path):  # noqa: F811
    disp = display
    port = 19000 + os.getpid() % 1000
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'data_dir = "{tmp_path / "data"}"\ncache_dir = "{tmp_path / "cache"}"\nui = "web"\n'
        f'mount_root = "{tmp_path / "mnt"}"\ndetect_devices = false\nhost = "127.0.0.1"\nport = {port}\n'
    )
    log = tmp_path / "out.log"
    env = {**os.environ, "DISPLAY": disp, "SYNCMUSIC_BROWSER": _playwright_chromium()}
    env.pop("XAUTHORITY", None)
    with log.open("w") as out:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "syncmusic",
                "run",
                "--mode",
                "desktop",
                "--config",
                str(cfg),
            ],
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait(lambda: _health(url), 60, "the API")

        def ui_window() -> str:  # the window title is the page title once the UI has loaded
            r = subprocess.run(
                ["xdotool", "search", "--name", "^SyncMusic$"],
                env=env,
                capture_output=True,
                text=True,
            )
            return r.stdout.split()[0] if r.stdout.split() else ""

        win = _wait(ui_window, 60, "the UI window")
        assert proc.poll() is None
        _close_like_a_user(disp, int(win))
        assert proc.wait(60) == 0, log.read_text()[-3000:]
    finally:
        if proc.poll() is None:
            proc.kill()
    text = log.read_text()
    assert "desktop window closed" in text and "window closed: stopping" in text
    assert "SyncMusic stopped" in text
    assert not _health(url)
