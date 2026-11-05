"""README.md is the user guide: its commands, options, paths and config keys exist, its rule example
parses, and nothing of the old autonomous-build bundle or the sibling pygpod checkout is left in it."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import typer.main
from typer.testing import CliRunner

from syncmusic.cli.main import cli
from syncmusic.core.rules.parser import parse_criteria, parse_sort
from syncmusic.infra.config import _ENV_KEYS, BootstrapConfig
from tests.unit.test_api_sync import music  # noqa: F401
from tests.unit.test_cli_api import server  # noqa: F401

REPO = Path(__file__).parents[2]
README = (REPO / "README.md").read_text(encoding="utf-8")
CODE_BLOCKS = re.findall(r"```(\w*)\n(.*?)```", README, re.S)


def _cli_lines() -> list[str]:
    """Every `syncmusic ...` command line in the README's code blocks and inline code."""
    lines: list[str] = []
    for _, block in CODE_BLOCKS:
        for line in block.replace("\\\n", " ").splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith(("syncmusic ", "uv run syncmusic ")):
                lines.append(line.removeprefix("uv run "))
    lines += [c for c in re.findall(r"`(syncmusic [^`]+)`", README) if "<command>" not in c]
    return lines


def _resolve(words: list[str]) -> tuple[Any, list[str]]:
    cmd: Any = typer.main.get_command(
        cli
    )  # typer's own click types: duck-typed like test_docs_reference
    rest = words[1:]
    while hasattr(cmd, "commands") and rest and rest[0] in cmd.commands:
        cmd, rest = cmd.commands[rest[0]], rest[1:]
    return cmd, rest


def test_every_cli_command_and_option_exists():
    lines = _cli_lines()
    assert len(lines) >= 15
    for line in lines:
        words = shlex.split(
            line.replace("<", "'<").replace(">", ">'").replace("[", "'[").replace("]", "]'")
        )
        cmd, rest = _resolve(words)
        assert not hasattr(cmd, "commands") or rest == ["--help"] or not rest, line
        opts = {o for p in cmd.params for o in (*p.opts, *p.secondary_opts)}
        used = {
            w.split("=", 1)[0] for w in rest if w.startswith("-") and not w.startswith("--url=")
        }
        assert used <= opts | {"--help"}, f"{line}: unknown options {used - opts}"


def test_run_modes_match_the_cli():
    run, _ = _resolve(["syncmusic", "run"])
    opts = {o for p in run.params for o in p.opts}
    assert {"--mode", "--config", "--host", "--port"} <= opts
    for mode in ("web", "desktop", "daemon"):
        assert f"| `{mode}`" in README


def test_paths_exist():
    paths = set(re.findall(r"`((?:packaging|scripts|docs|tests)/[\w./-]+)`", README))
    paths |= set(re.findall(r"\]\(((?:docs)/[\w.-]+)(?:#[\w-]+)?\)", README))
    paths |= set(re.findall(r"\b((?:packaging|scripts)/[\w.-]+\.(?:sh|py))", README))
    assert {
        "packaging/install-systemd.sh",
        "packaging/build-desktop",
    } <= paths
    missing = sorted(p for p in paths if not (REPO / p.rstrip("/")).exists())
    assert missing == []


def test_config_table_matches_bootstrap_config():
    rows = re.findall(
        r"^\| `(\w+)`(?: / `(\w+)`)? \| `(SYNCMUSIC_\w+)`(?: / `(SYNCMUSIC_\w+)`)? \|",
        README,
        re.M,
    )
    assert len(rows) >= 6
    for key1, key2, env1, env2 in rows:
        for key, env in ((key1, env1), (key2, env2)):
            if key:
                assert key in BootstrapConfig.model_fields, key
                assert _ENV_KEYS[key] == env, key


def test_rule_example_parses():
    (example,) = [b for lang, b in CODE_BLOCKS if lang == "json"]
    rule = json.loads(example)
    result = parse_criteria({"all": rule["all"]})
    assert result.warnings == []
    _, _, warnings = parse_sort(rule["sort"], rule["order"])
    assert warnings == []


@pytest.mark.parametrize(
    ("script", "flags"),
    [
        (
            "packaging/build-desktop",
            ["--target", "--out", "--python"],
        ),
    ],
)
def test_script_options_exist(script, flags):
    out = subprocess.run(
        ["bash", str(REPO / script), "--help"], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0
    assert "set -euo" not in out.stdout and "$(" not in out.stdout  # the comment header only
    for flag in flags:
        assert flag in out.stdout and flag in README, flag


def test_install_options_exist():
    text = (REPO / "packaging/install-systemd.sh").read_text()
    assert "--prefix" in text and "--unit-dir" in text and "--pygpod" not in text


def test_nothing_obsolete():
    for stale in (
        "../pygpod",
        "--pygpod",
        "develop-sync",
        "PROMPT.md",
        "documentation bundle",
    ):
        assert stale not in README, stale


TEST_ONLY_KEYS = {"allow_loop_devices", "allow_directory_devices"}


def test_every_user_facing_config_key_has_a_row():
    # device_allowlist and log_format were missing
    for key in set(BootstrapConfig.model_fields) - TEST_ONLY_KEYS:
        assert re.search(rf"^\| `{key}`(?: / `\w+`)? \| `", README, re.M) or re.search(
            rf"^\| `\w+` / `{key}` \|", README, re.M
        ), key
    for key in TEST_ONLY_KEYS:
        assert f"`{key}`" in README and "test suite only" in README
    (allowlist,) = [b for lang, b in CODE_BLOCKS if lang == "toml" and "usb_serial" in b]
    doc = tomllib.loads(allowlist)
    assert all({"usb_serial"} <= e.keys() or {"fs_uuid"} <= e.keys() for e in doc.values())


def test_url_example_runs_after_the_command(server):  # noqa: F811
    # --url is an option of each command, not of the group
    (example,) = re.findall(r"`(syncmusic status --url \S+)`", README)
    url, _ = server
    words = shlex.split(example)[1:]
    words[words.index("--url") + 1] = url
    result = CliRunner().invoke(cli, words)
    assert result.exit_code == 0, result.output
    misplaced = CliRunner().invoke(cli, ["--url", url, "status"])
    assert misplaced.exit_code != 0 and "No such option: --url" in misplaced.output
