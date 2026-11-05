"""Selection logic of scripts/test-cleanup.sh: it never picks a foreign device, path or process."""

import json
from pathlib import Path

from tests import cleanup
from tests.cleanup import Proc, is_pytest, select_loops, select_paths, select_procs


def test_only_loops_on_emulated_images():
    data = json.dumps(
        {
            "loopdevices": [
                {"name": "/dev/loop0", "back-file": "/var/tmp/syncmusic-emulated/a/disk.img"},
                {"name": "/dev/loop1", "back-file": "/var/tmp/syncmusic-emulated/b.img (deleted)"},
                {"name": "/dev/loop2", "back-file": "/var/lib/snapd/snaps/core.snap"},
                {"name": "/dev/loop3", "back-file": "/var/tmp/syncmusic-emulated-other/x.img"},
                {"name": "/dev/nvme0n1", "back-file": "/var/tmp/syncmusic-emulated/c.img"},
                {"name": "/dev/loop4", "back-file": None},
            ]
        }
    )
    assert select_loops(data, Path("/var/tmp/syncmusic-emulated")) == ["/dev/loop0", "/dev/loop1"]
    assert select_loops("") == []


def test_only_own_temp_paths(tmp_path, monkeypatch):
    own = tmp_path / "pytest-of-someone"
    own.mkdir()
    foreign = tmp_path / "music"
    foreign.mkdir()
    monkeypatch.setattr(cleanup, "PYTEST_ROOT", own)
    assert select_paths([own, foreign, tmp_path / "missing", Path("/var/tmp"), Path("/")]) == [own]


def test_only_test_processes():
    root = Path("/tmp/pytest-of-someone")
    procs = [
        Proc(
            10,
            5,
            1000,
            ["python", "-m", "syncmusic", "run", "--config", f"{root}/pytest-3/t0/config.toml"],
        ),
        Proc(
            11,
            1,
            1000,
            ["/home/u/.cache/ms-playwright/chromium-1/chrome-linux/chrome", "--headless"],
        ),
        Proc(
            12, 99, 1000, ["/home/u/.cache/ms-playwright/chromium-1/chrome-linux/chrome"]
        ),  # parent alive
        Proc(13, 1, 1000, ["Xvfb", "-displayfd", "5", "-screen", "0", "1280x800x24"]),
        Proc(14, 1, 1000, ["Xvfb", ":0"]),  # a real X server
        Proc(15, 5, 2000, ["python", f"{root}/x"]),  # another user
        Proc(16, 5, 1000, ["syncmusic", "run", "--config", "/etc/syncmusic/config.toml"]),
        Proc(17, 5, 1000, ["python", "-m", "pytest", f"--basetemp={root}/x"]),  # a run itself
        Proc(18, 5, 1000, ["bash"]),
    ]
    assert select_procs(procs, 1000, root) == [10, 11, 13]


def test_is_pytest():
    assert is_pytest([".venv/bin/python", ".venv/bin/pytest", "-q"])
    assert is_pytest(["uv", "run", "pytest"])
    assert is_pytest(["python", "-m", "pytest"])
    assert not is_pytest(["claude", "-p", "a prompt that mentions pytest"])
    assert not is_pytest(["bash", "-c", "uv run pytest -q"])
