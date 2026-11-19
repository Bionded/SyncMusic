# SPDX-License-Identifier: GPL-3.0-or-later
"""The downloads table at the top of every release page links to that release's own assets."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "release-downloads.sh"
ASSETS = [
    "syncmusic-web-1.2.3-4-linux-x86_64",
    "syncmusic-web-1.2.3-4-linux-aarch64",
    "syncmusic-desktop-1.2.3-4-linux-x86_64",
    "syncmusic-desktop-1.2.3-4-linux-aarch64",
    "syncmusic-compas-1.2.3-4-hard.zip",
    "syncmusic-compas-1.2.3-4-soft.zip",
    "compas-sdtool-linux-x86_64",
    "compas-sdtool-linux-arm64",
    "SHA256SUMS",
]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True)


def test_table_links_every_asset_of_the_tag() -> None:
    out = _run("Owner/Repo", "v1.2.3-4").stdout
    assert out.startswith("## Downloads\n")
    links = re.findall(r"\]\((https://[^)]*/releases/download/[^)]*)\)", out)
    assert {link.rsplit("/", 1)[1] for link in links} == set(ASSETS)
    assert all("/releases/download/v1.2.3-4/" in link for link in links)
    assert "ghcr.io/owner/repo:v1.2.3-4" in out
    assert "--version 1.2.3-4" in out
    assert out.index("hard.zip") < out.index("soft.zip")


def test_check_passes_with_all_assets_and_names_a_missing_one(tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    notes.write_text(_run("Owner/Repo", "v1.2.3-4").stdout + "\nchanges\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ASSETS:
        (dist / name).touch()
    assert _run("--check", str(notes), str(dist)).returncode == 0
    (dist / "syncmusic-compas-1.2.3-4-soft.zip").unlink()
    res = _run("--check", str(notes), str(dist))
    assert res.returncode == 1
    assert "MISSING syncmusic-compas-1.2.3-4-soft.zip" in res.stderr
