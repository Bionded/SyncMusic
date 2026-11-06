"""scripts/release.sh: checks, dry run, and a real run with the builds stubbed, in a throwaway
clone of the repo (never in the repo itself: it tags)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
ID = {"NAME": "t", "EMAIL": "t@t"}
# a CI runner has no git identity: every commit here (and in release.sh) uses this one
GIT_ENV = os.environ | {f"GIT_{w}_{k}": v for w in ("AUTHOR", "COMMITTER") for k, v in ID.items()}
pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout


@pytest.fixture
def repo(tmp_path):
    """A clone of HEAD with the working copy's release script, on main, committed."""
    dst = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--no-tags", f"file://{ROOT}", str(dst)], check=True
    )
    _git(dst, "checkout", "-q", "-B", "main")
    for script in ("release.sh", "changelog-section.sh", "beta-notes.sh"):
        shutil.copy(ROOT / "scripts" / script, dst / "scripts" / script)
    for name in ("build-web", "build-desktop"):  # builds stubbed: one fake binary per target
        stub = dst / "packaging" / name
        stub.write_text(
            '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = --out ] && out=$2; shift; done\n'
            f'for a in x86_64 aarch64; do echo bin > "$out/syncmusic-{name[6:]}-v-linux-$a"; '
            f'echo x > "$out/syncmusic-{name[6:]}-v-linux-$a.sha256"; done\n'
        )
        stub.chmod(0o755)
    _git(dst, "add", "-A")
    _git(dst, "commit", "-qm", "stub builds")
    return dst


def _release(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["scripts/release.sh", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=GIT_ENV,
    )


def test_dry_run_changes_nothing(repo):
    head = _git(repo, "rev-parse", "HEAD")
    r = _release(repo, "0.2.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "+ git tag -a v0.2.0" in r.stdout and "packaging/build-web" not in r.stdout
    assert "The first public release." in r.stdout  # the notes from the CHANGELOG entry
    assert _git(repo, "rev-parse", "HEAD") == head and not _git(repo, "tag")
    r = _release(repo, "0.2.0", "--dry-run", "--build")  # the optional local build
    assert r.returncode == 0, r.stderr
    assert "+ packaging/build-web --target all" in r.stdout and "docker" not in r.stdout
    assert _git(repo, "rev-parse", "HEAD") == head and not _git(repo, "tag")
    assert not (repo / "dist").exists()


def _no_unreleased(repo: Path) -> None:
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [0.2.0]\n\nThe first public release.\n")
    _git(repo, "commit", "-qam", "changelog")


@pytest.mark.parametrize(
    ("setup", "args", "error"),
    [
        (lambda r: (r / "x").write_text("x"), ["0.2.0"], "not clean"),
        (_no_unreleased, ["9.9.9"], 'no "## [9.9.9]" section'),
        (lambda r: _git(r, "tag", "v0.2.0"), ["0.2.0"], "exists already"),
        (lambda r: _git(r, "checkout", "-qb", "other"), ["0.2.0"], "not on main"),
        (None, ["latest"], "not a version"),
        (None, ["0.2.0", "--docker"], "need --build"),
    ],
)
def test_checks(repo, setup, args, error):
    if setup:
        setup(repo)
    r = _release(repo, *args, "--dry-run")
    assert r.returncode != 0 and error in r.stderr, r.stderr


def test_release_tags_without_building(repo):
    r = _release(repo, "0.2.0")
    assert r.returncode == 0, r.stderr
    assert _git(repo, "tag").split() == ["v0.2.0"] and not (repo / "dist").exists()
    assert "git push origin main v0.2.0" in r.stdout
    assert 'version = "0.2.1"' in (repo / "pyproject.toml").read_text()


def test_release_builds_sums_notes_and_tags(repo):
    r = _release(repo, "0.2.0", "--build")
    assert r.returncode == 0, r.stderr
    out = repo / "dist" / "release-0.2.0"
    sums = (out / "SHA256SUMS").read_text().splitlines()
    assert len(sums) == 4 and not list(out.glob("*.sha256"))
    assert (
        (out / "RELEASE-NOTES.md")
        .read_text()
        .startswith("# SyncMusic 0.2.0\n\nThe first public release.")
    )
    assert _git(repo, "tag").split() == ["v0.2.0"]
    assert "Nothing was pushed" in r.stdout
    # main moved on: its betas are 0.2.1-N, after the tagged 0.2.0
    assert 'version = "0.2.1"' in (repo / "pyproject.toml").read_text()
    assert '__version__ = "0.2.1"' in (repo / "syncmusic" / "__init__.py").read_text()
    assert _git(repo, "rev-parse", "HEAD~1") == _git(repo, "rev-parse", "v0.2.0^{commit}")
    assert not _git(repo, "status", "--porcelain")


def _changelog(repo: Path, unreleased: str) -> None:
    """Replace CHANGELOG.md with one that has an [Unreleased] section and a released 0.2.0, committed."""
    (repo / "CHANGELOG.md").write_text(
        f"# Changelog\n\nIntro.\n\n## [Unreleased]\n{unreleased}\n## [0.2.0]\n\nThe first public release.\n"
    )
    _git(repo, "commit", "-qam", "changelog")


def test_release_turns_unreleased_into_the_version(repo):
    _changelog(repo, "\n### Fixed\n- A fix.\n")
    r = _release(repo, "0.3.0", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "[Unreleased] section" in r.stdout and "    - A fix." in r.stdout
    assert "## [0.3.0] - " in r.stdout and not _git(repo, "tag")
    r = _release(repo, "0.3.0")
    assert r.returncode == 0, r.stderr
    text = (repo / "CHANGELOG.md").read_text()
    head, rest = text.split("## [Unreleased]\n\n## [0.3.0] - ", 1)
    assert head == "# Changelog\n\nIntro.\n\n" and rest.split("\n", 1)[1].startswith(
        "\n### Fixed\n- A fix."
    )
    assert "## [0.2.0]" in rest
    tagged = _git(
        repo, "show", "v0.3.0:CHANGELOG.md"
    )  # the tag has the version section, no Unreleased
    assert "## [Unreleased]" not in tagged and "## [0.3.0] - " in tagged
    assert 'version = "0.3.0"' in _git(repo, "show", "v0.3.0:pyproject.toml")
    assert 'version = "0.3.1"' in (repo / "pyproject.toml").read_text()
    assert not _git(repo, "status", "--porcelain")


@pytest.mark.parametrize("unreleased", ["", "\n### Added\n\n### Fixed\n"])
def test_release_refuses_an_empty_unreleased_section(repo, unreleased):
    _changelog(repo, unreleased)
    r = _release(repo, "0.3.0", "--dry-run")
    assert r.returncode != 0 and "[Unreleased]" in r.stderr and "is empty" in r.stderr


def test_existing_version_section_keeps_unreleased(repo):
    _changelog(repo, "\n")
    r = _release(repo, "0.2.0")
    assert r.returncode == 0, r.stderr
    assert (repo / "CHANGELOG.md").read_text().count("## [Unreleased]") == 1


def _beta_notes(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["scripts/beta-notes.sh", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def test_beta_notes_with_and_without_unreleased_entries(repo):
    _changelog(repo, "\n### Fixed\n- A fix.\n")
    notes = _beta_notes(repo, "7")
    assert notes.startswith(
        "Test build 7 of main - may have bugs.\n\n## Changes since the start\n\n### Fixed"
    )
    assert "\n## Commits\n\n- changelog\n" in notes and "## [0.2.0]" not in notes
    _git(repo, "tag", "v0.2.0")
    _changelog(repo, "\n### Added\n")
    notes = _beta_notes(repo, "8", "v0.2.0")
    assert (
        notes == "Test build 8 of main - may have bugs.\n\nCommits since v0.2.0:\n\n- changelog\n"
    )
