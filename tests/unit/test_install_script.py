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
