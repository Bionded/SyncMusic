#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the SyncMusic test suites with a live progress display.

    scripts/test.py SUITE [SUITE ...] [options] [-- extra pytest arguments]

{suites}

While pytest runs, the display shows the elapsed time, done/total with the counts, an estimate of what is left,
what the compose stack is busy with (starting, waiting for a server's scan), every test running right now (with
its worker, its queue and for how long) and the latest failures. The full pytest output goes to a log file whose
path is printed; at the end the summary of failures is shown from it.

The compose stack after the run (--compose-after, also SD_COMPOSE_AFTER):
  clean  remove containers and volumes (default; the next run starts from scratch, a few minutes)
  stop   stop the containers, keep their state (the next run starts them again without new scans)
  keep   leave the stack running (fastest edit-test loop; `--compose-after clean` or
         `uv run python -m tests.integration.services down` when done)

Examples:
  scripts/test.py fast
  scripts/test.py lint fast
  scripts/test.py no_hardware                   (or: scripts/test.py nohw / nh)
  scripts/test.py fi                            fast, then integration
  scripts/test.py integration --compose-after keep
  scripts/test.py fast -- -k rules tests/unit/test_rules.py
  scripts/test.py nohw --changed               only what the changes since the last commit can affect
  scripts/test.py nohw --failed                only what failed last time
  scripts/test.py hardware -n 0
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAST = "not hardware and not slow and not integration"


@dataclass(frozen=True)
class Suite:
    help: str  # for --help: what it runs and what it needs
    marks: str = ""  # pytest -m expression ("" for lint / all)
    # default -n: "auto" (tests/conftest.py: all CPUs but two), "half" (half of them: a browser and a server per
    # worker - ten of them on twelve cores starve the slower screens), None (one process)
    workers: str | None = None
    paths: tuple[str, ...] = ()  # test paths (default: all of tests/)
    part: bool = False  # only a part of a group (not listed in the help)


# the one list of suites: the help, the argument check and the runs all come from it
SUITES: dict[str, Suite] = {
    "lint": Suite(
        "ruff check, ruff format --check, mypy --strict on syncmusic/core and syncmusic/base"
    ),
    "fast": Suite(
        "unit, contract and fake-server tests - needs nothing (run it before handing work back)",
        FAST,
        "auto",
    ),
    "unit": Suite("only tests/unit, as in fast", FAST, "auto", ("tests/unit",)),
    "contracts": Suite(
        "only the contract suites every library, device and processor must pass (tests/contracts)",
        FAST,
        "auto",
        ("tests/contracts",),
    ),
    "integration": Suite(
        "the compose stack (Navidrome, Audiobookshelf, FTP, SMB) and the emulated devices, without the slow"
        " ones - needs Docker, and `sudo -n` for the emulated devices (those skip without it)",
        "integration and not slow and not hardware",
        "auto",
    ),
    "emulated": Suite(
        "every emulated-device scenario (loop images), also the slow ones - needs Docker and `sudo -n`",
        "emulated and not hardware",
        "auto",
    ),
    "ui": Suite(
        "Playwright web UI tests - needs `uv run playwright install chromium` once",
        "ui and not hardware",
        "half",
    ),
    "e2e": Suite(
        "end-to-end flows (self-update, install, full UI passes)", "e2e and not hardware", "half"
    ),
    "slow": Suite("everything slow: UI, builds, soak tests", "slow and not hardware", "half"),
    # no_hardware splits slow into ui, these two and nothing twice (5 e2e tests are UI tests)
    "e2e_rest": Suite(
        "e2e tests that are not UI tests", "e2e and not ui and not hardware", "auto", part=True
    ),
    "slow_rest": Suite(
        "slow tests that are neither UI nor e2e: builds, emulated, soaks",
        "slow and not ui and not e2e and not hardware",
        "auto",
        part=True,
    ),
    "heavy": Suite(
        "live-scale soak tests, minutes each (also part of slow); one process: they time the host",
        "heavy and not hardware",
    ),
    "hardware": Suite(
        "real USB players listed in tests/hardware.toml - nothing is collected without that file",
        "hardware",
    ),
    "not_slow_hardware": Suite(
        "lint, fast, integration: everything that is neither slow nor hardware (unit is in fast)"
    ),
    "no_hardware": Suite(
        "lint, fast, integration, ui, e2e, the rest of slow: every test except hardware, each once (unit and"
        " contracts are in fast, emulated in integration and the rest of slow, heavy in the rest of slow)"
    ),
}
LISTED = [name for name, suite in SUITES.items() if not suite.part]
# suites made of other suites
GROUPS = {
    "not_slow_hardware": ["lint", "fast", "integration"],
    "no_hardware": ["lint", "fast", "integration", "ui", "e2e_rest", "slow_rest"],
}


# short names; a word made only of the one-letter ones runs those suites letter by letter ("fisu")
ALIASES = {
    "l": "lint",
    "f": "fast",
    "u": "unit",
    "c": "contracts",
    "i": "integration",
    "e": "emulated",
    "s": "slow",
    "hv": "heavy",
    "hw": "hardware",
    "nsh": "not_slow_hardware",
    "nh": "no_hardware",
    "nohw": "no_hardware",
}
LETTERS = {k: v for k, v in ALIASES.items() if len(k) == 1}
assert not set(ALIASES) & set(SUITES), "an alias must not shadow a suite name"


def resolve(word: str) -> list[str]:
    """A suite name, an alias or a word of one-letter aliases -> suite names; ValueError for anything else."""
    if word in SUITES:
        return [word]
    if word in ALIASES:
        return [ALIASES[word]]
    if word and all(ch in LETTERS for ch in word):
        return [LETTERS[ch] for ch in word]
    raise ValueError(word)


def _suites_help() -> str:
    lines = [
        "Suites (run in the order given; the run stops at the first one that fails unless --keep-going -",
        "not_slow_hardware and no_hardware always run all their parts and report each).",
        "A short name in brackets works too; a word of one-letter names runs those suites in that order:",
        f"`fisu` = {', '.join(resolve('fisu'))}.",
        "",
    ]
    short: dict[str, list[str]] = {}
    for alias, name in ALIASES.items():
        short.setdefault(name, []).append(alias)
    names = {n: f"{n} ({', '.join(short[n])})" if n in short else n for n in LISTED}
    col = max(map(len, names.values()))
    for name, suite in SUITES.items():
        if suite.part:
            continue
        text = suite.help
        if suite.marks:
            where = " ".join(suite.paths) or "tests/"
            text += f'  [pytest {"-n " + suite.workers + " " if suite.workers else ""}-m "{suite.marks}" {where}]'
        lines += textwrap.wrap(
            text,
            width=110,
            initial_indent=f"  {names[name]:<{col}} ",
            subsequent_indent=" " * (col + 3),
        )
    return "\n".join(lines)


LINT = [
    ["ruff", "check"],
    ["ruff", "format", "--check"],
    ["mypy", "syncmusic/core", "syncmusic/base"],
]
TTY = sys.stdout.isatty()
PLAIN_EVERY_S = 30.0  # without a terminal: one progress line this often


def _c(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if TTY else text


def _dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def _requirements(suite: str) -> list[str]:
    """Warnings about what the suite needs and this machine lacks (the tests then skip)."""
    warn = []
    docker = shutil.which("docker") is not None
    if suite in ("integration", "emulated", "hardware") and not docker:
        warn.append("Docker is not installed: the compose-stack tests skip")
    if (
        suite in ("integration", "emulated", "hardware")
        and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0
    ):
        warn.append("no passwordless sudo (`sudo -n`): the emulated-device tests skip")
    if suite == "hardware" and not (ROOT / "tests" / "hardware.toml").is_file():
        warn.append("no tests/hardware.toml: no hardware test is collected")
    if suite in ("ui", "slow") and not any(
        (Path.home() / ".cache" / "ms-playwright").glob("chromium*")
    ):
        warn.append("Playwright Chromium is missing: uv run playwright install chromium")
    return warn


def _stack_phase() -> str:
    from tests.integration import services

    try:
        since, text = services.STATUS.read_text().strip().split(" ", 1)
    except (OSError, ValueError):
        return ""
    return f"{text} ({_dur(time.time() - float(since))})"


def _render(suite: str, state: dict, started: float, width: int) -> list[str]:
    now = time.time()
    counts = state.get("counts", {})
    total = state.get("total")
    done = sum(counts.values())
    elapsed = now - started
    line = f"{_c('1', suite)}  {_dur(elapsed)}  "
    if total:
        line += f"{done}/{total} ({100 * done // total}%)"
        if done >= 5 and done < total:
            line += f"  ~{_dur(elapsed / done * (total - done))} left"
    else:
        line += "collecting…"
    parts = [_c("32", f"{counts.get('passed', 0)} passed")]
    for key, colour in (("failed", "31"), ("error", "31"), ("skipped", "33"), ("xfailed", "33")):
        if counts.get(key):
            parts.append(_c(colour, f"{counts[key]} {key}"))
    lines = [line + "  " + ", ".join(parts)]
    if phase := _stack_phase():
        lines.append(f"  compose stack: {phase}"[: width - 1])
    running = sorted(state.get("running", {}).values(), key=lambda r: r["since"])
    if running:
        lines.append(f"  running ({len(running)}):")
    for r in running[:12]:
        where = "/".join(x for x in (r.get("worker"), r.get("group")) if x)
        head = f"    {_dur(now - r['since']):>6}  {where:<14} "
        lines.append(head + r["test"][: max(20, width - len(head) - 1)])
    if len(running) > 12:
        lines.append(f"    … and {len(running) - 12} more")
    for f in state.get("failures", [])[-5:]:
        lines.append("  " + _c("31", f[: width - 3]))
    return lines


def _summary(log: Path) -> list[str]:
    """The `short test summary info` part and the last line of the pytest output."""
    text = log.read_text(encoding="utf-8", errors="replace").splitlines()
    start = next((i for i, ln in enumerate(text) if "short test summary info" in ln), None)
    tail = text[start:] if start is not None else text[-1:]
    return tail[-60:]


def run_pytest(suite: str, args: list[str], extra: list[str], log_dir: Path) -> int:
    status = log_dir / f"{suite}.status.json"
    log = log_dir / f"{suite}.log"
    env = {**os.environ, "SM_TEST_STATUS": str(status)}
    cmd = [sys.executable, "-m", "pytest", "-p", "tests.progress", "-rfE", *args, *extra]
    print(_c("1", f"== {suite}: ") + " ".join(cmd[1:]))
    for w in _requirements(suite):
        print("  " + _c("33", "! " + w))
    print(f"  log: {log}")
    started = time.time()
    with log.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
        shown = 0
        last_plain = 0.0
        try:
            while proc.poll() is None:
                time.sleep(1.0)
                try:
                    state = json.loads(status.read_text())
                except (OSError, ValueError):
                    state = {}
                width = shutil.get_terminal_size((120, 20)).columns
                lines = _render(suite, state, started, width)
                if TTY:
                    if shown:
                        sys.stdout.write(f"\x1b[{shown}F\x1b[J")
                    sys.stdout.write("\n".join(lines) + "\n")
                    sys.stdout.flush()
                    shown = len(lines)
                elif time.time() - last_plain >= PLAIN_EVERY_S:
                    print(lines[0], flush=True)
                    last_plain = time.time()
        except KeyboardInterrupt:
            # pytest cleans up on SIGINT (compose stack, loop devices, mounts): let it finish that
            print("\n" + _c("33", "interrupted - pytest is cleaning up, wait…"))
            proc.send_signal(signal.SIGINT)
            proc.wait()
    code = proc.returncode
    print(f"  {_dur(time.time() - started)}, pytest exit code {code}")
    for line in _summary(log):
        print("  " + line)
    return code


def run_lint() -> int:
    print(_c("1", "== lint"))
    worst = 0
    for cmd in LINT:
        r = subprocess.run(["uv", "run", *cmd], cwd=ROOT)
        print(
            f"  {' '.join(cmd)}: " + (_c("32", "ok") if r.returncode == 0 else _c("31", "failed"))
        )
        worst = worst or r.returncode
    return worst


def main(argv: list[str]) -> int:
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(
        prog="scripts/test.py",
        description=__doc__.split("\n\n")[0],
        epilog=__doc__.split("\n\n", 2)[2].format(suites=_suites_help()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "suites",
        nargs="+",
        metavar="SUITE",
        help=f"one or more of: {', '.join(LISTED)}, their short names or a word of one-letter names (see below)",
    )
    ap.add_argument(
        "-n",
        dest="workers",
        help="pytest-xdist workers (default: auto; half the CPUs for ui, e2e and slow; one process for heavy and"
        " hardware)",
    )
    ap.add_argument(
        "--compose-after",
        choices=("clean", "stop", "keep"),
        help="what to do with the compose stack this run started (default: clean)",
    )
    ap.add_argument(
        "--keep-going",
        action="store_true",
        help="run the next suites after a failure (always on for not_slow_hardware and no_hardware)",
    )
    ap.add_argument("--log-dir", type=Path, help="where the logs go (default: a new temp dir)")
    only = ap.add_mutually_exclusive_group()
    only.add_argument(
        "--failed",
        action="store_true",
        help="only the tests that failed in the last run (pytest --lf)",
    )
    only.add_argument(
        "--changed",
        action="store_true",
        help="only the test files the changes against the last commit can affect (tests/affected.py)",
    )
    opts = ap.parse_args(argv)
    if opts.changed:
        from tests.affected import affected, changed_files

        try:
            changed = changed_files(ROOT)
        except RuntimeError as e:
            ap.error(str(e))
        targets = affected(ROOT, changed)
        print(
            f"changed: {len(changed)} file(s) -> "
            + (
                "every test (a change no rule covers)"
                if targets is None
                else f"{len(targets)} test file(s)"
            )
        )

    suites: list[str] = []
    keep_going = opts.keep_going
    for word in opts.suites:
        try:
            names = resolve(word)
        except ValueError:
            ap.error(f"unknown suite {word!r}: one of {', '.join(LISTED)} or {', '.join(ALIASES)}")
        for name in names:
            suites += GROUPS.get(name, [name])
            keep_going = keep_going or name in GROUPS  # a group reports every part
    log_dir = opts.log_dir or Path(tempfile.mkdtemp(prefix="syncmusic-tests-"))
    log_dir.mkdir(parents=True, exist_ok=True)
    if opts.compose_after:
        os.environ["SD_COMPOSE_AFTER"] = opts.compose_after

    results: list[tuple[str, int]] = []
    for suite in suites:
        if suite == "lint":
            code = run_lint()
        else:
            spec = SUITES[suite]
            workers = opts.workers or spec.workers
            if workers == "half":
                workers = str(max(1, (os.cpu_count() or 2) // 2))
            args = [*(["-n", workers] if workers else []), "-m", spec.marks]
            if opts.failed:
                args += ["--last-failed", "--last-failed-no-failures", "none"]
            # test paths given after -- replace the suite's own
            given = any((ROOT / a.split("::")[0]).exists() for a in extra if not a.startswith("-"))
            paths = [] if given else list(spec.paths)
            if opts.changed and targets is not None and not given:
                paths = [t for t in targets if not spec.paths or t.startswith(spec.paths)]
                if not paths:
                    print(_c("1", f"== {suite}: ") + "no affected test file")
                    results.append((suite, 0))
                    continue
            code = run_pytest(suite, [*args, *paths], extra, log_dir)
        results.append((suite, code))
        if code and not keep_going:
            break
    print()
    for suite, code in results:
        print(
            f"{suite:<12} " + (_c("32", "ok") if code in (0, 5) else _c("31", f"failed ({code})"))
        )
    print(f"logs: {log_dir}")
    return next((code for _, code in results if code not in (0, 5)), 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
