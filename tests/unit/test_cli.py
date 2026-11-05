from typer.testing import CliRunner

from syncmusic.cli.main import cli


def test_run_with_invalid_mode_prints_one_line_error():
    result = CliRunner().invoke(cli, ["run", "--mode", "bogus", "--port", "18766"])
    assert result.exit_code == 1
    assert "invalid config: mode" in result.output and "bogus" in result.output
    assert "Traceback" not in result.output


def test_run_with_broken_toml(tmp_path):
    bad = tmp_path / "c.toml"
    bad.write_text("mode = = web")
    result = CliRunner().invoke(cli, ["run", "--config", str(bad)])
    assert (
        result.exit_code == 1
        and "invalid config" in result.output
        and "Traceback" not in result.output
    )


def test_python_m_usage_line_names_syncmusic():
    # the desktop bundle launches `python -m syncmusic`
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "syncmusic", "devices", "register", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "Usage: syncmusic devices register" in r.stdout
    assert "python -m" not in r.stdout


def test_config_init_writes_a_commented_default_config(tmp_path):
    """`syncmusic config init`: every bootstrap key with its default and an explanation;
    uncommented, the file loads to the defaults; an existing file needs --force."""
    import tomllib

    from syncmusic.infra.config import EXPLAIN, BootstrapConfig, load_config

    assert set(EXPLAIN) == set(BootstrapConfig.model_fields)
    out = CliRunner().invoke(cli, ["config", "init"])
    assert out.exit_code == 0 and tomllib.loads(out.stdout) == {}  # all commented out
    for key in BootstrapConfig.model_fields:
        assert f"# {key} = " in out.stdout
    path = tmp_path / "sm.toml"
    assert CliRunner().invoke(cli, ["config", "init", "--output", str(path)]).exit_code == 0
    again = CliRunner().invoke(cli, ["config", "init", "-o", str(path)])
    assert again.exit_code == 1 and "--force" in again.stderr
    assert CliRunner().invoke(cli, ["config", "init", "-o", str(path), "--force"]).exit_code == 0
    active = "\n".join(
        line.removeprefix("# ").split("  (no default")[0]
        for line in path.read_text().splitlines()
        if line.split(" = ")[0].removeprefix("# ") in BootstrapConfig.model_fields
        and "device_allowlist" not in line
    )
    cfg = load_config(None, env={}, overrides=tomllib.loads(active))
    assert cfg == load_config(None, env={})


def _commands(cmd, path=()):
    yield path, cmd
    if hasattr(cmd, "commands"):  # a group (typer vendors its own click)
        for name, sub in cmd.commands.items():
            yield from _commands(sub, (*path, name))


def test_every_command_help_has_examples_that_parse(tmp_path):
    """every command and group's --help has an Examples section, and every example is a valid
    command line for it (options and arguments parse)."""
    import shlex

    import typer

    from syncmusic.cli.main import EXAMPLES

    cfg = tmp_path / "config.toml"
    cfg.touch()
    root = typer.main.get_command(cli)
    seen = set()
    for path, _cmd in _commands(root):
        key = " ".join(path)
        seen.add(key)
        out = CliRunner().invoke(cli, [*path, "--help"])
        assert out.exit_code == 0 and "Examples:" in out.stdout, key
        assert EXAMPLES[key], key
        for example in EXAMPLES[key]:
            # an installed SyncMusic's config is root-only; the examples parse against a readable stand-in
            args = [
                str(cfg) if a == "/etc/syncmusic/config.toml" else a
                for a in shlex.split(example)[1:]
            ]
            target, rest = root, args
            while hasattr(target, "commands") and rest and rest[0] in target.commands:
                target, rest = target.commands[rest[0]], rest[1:]
            target.make_context(" ".join(path) or "syncmusic", list(rest), resilient_parsing=False)
    assert seen == set(EXAMPLES)


def test_every_script_help_has_examples():
    """every script in scripts/ and packaging/ answers --help with an Examples section and does
    nothing else."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    scripts = sorted(
        [*root.glob("scripts/*.sh"), *root.glob("scripts/*.py"), *root.glob("packaging/*.sh")]
        + [
            root / "packaging/build-web",
            root / "packaging/build-desktop",
            root / "packaging/docker/build.sh",
        ]
    )
    assert scripts
    for script in scripts:
        cmd = (
            ["bash", str(script)] if script.suffix in (".sh", "") else [sys.executable, str(script)]
        )
        out = subprocess.run([*cmd, "--help"], cwd=root, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and "Examples" in out.stdout, (script.name, out.stderr[-500:])
