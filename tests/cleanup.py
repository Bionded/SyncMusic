"""Remove what an aborted test run of this checkout left behind (`scripts/test-cleanup.sh`).

A normal or failed run cleans up after itself (fixture finalizers); a run killed with SIGKILL, or a crashed
xdist worker, cannot. This removes, in order:

- test processes: daemons whose command line points into the pytest temp root, and orphaned (parent pid 1)
  Playwright browsers and Xvfb servers of the current user;
- the compose stack of this checkout (`--all`: every `syncmusic-it-*` stack and container) with its volumes;
- loop devices whose backing file is an emulated image (their mounts are unmounted first);
- speed limits (cgroup io.max) left on detached loop devices or on the emulated ones;
- the temp dirs and files the tests create.

Only loop devices on images under the emulated image root are ever touched, never another block device, and only
the fixed temp prefixes below, never anything else under /var/tmp. Nothing runs while a test run of this
checkout is still alive (unless `--force`). `--dry-run` prints the plan and changes nothing.

    uv run python -m tests.cleanup [--dry-run] [--all] [--force]
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tests.emulated import IMAGE_ROOT, clear_loop_limit, limited_loops
from tests.integration import services

PYTEST_ROOT = Path("/tmp") / f"pytest-of-{getpass.getuser()}"
TEMP_PATHS = (
    PYTEST_ROOT,
    services.SYNTHETIC,
    services.MEDIA_LOCK,
    services.SMB_DEVICES,
    services.JELLYFIN_MEDIA,
    services.LOCK,
    services.STATUS,
    services.USERS,
    services.KEPT,
    services.GENERATED,
    Path("/var/tmp/sd-hw-ui-fail.png"),
)
PREFIX = "syncmusic-it-"


@dataclass
class Proc:
    pid: int
    ppid: int
    uid: int
    cmdline: list[str]


def select_loops(losetup_json: str, image_root: Path = IMAGE_ROOT) -> list[str]:
    """The loop devices (`/dev/loopN`) backed by an image under `image_root` - nothing else."""
    root = str(image_root).rstrip("/") + "/"
    picked = []
    for d in json.loads(losetup_json or "{}").get("loopdevices", []):
        name = d.get("name") or ""
        back = (d.get("back-file") or "").removesuffix(" (deleted)")
        if name.startswith("/dev/loop") and back.startswith(root):
            picked.append(name)
    return picked


def select_paths(candidates: Iterable[Path]) -> list[Path]:
    """The existing ones of `candidates` that are inside the allowed temp locations of the tests."""
    picked = []
    for p in candidates:
        resolved = p.resolve() if p.exists() or p.is_symlink() else None
        if resolved is None or resolved.is_mount():
            continue
        ok = (
            resolved == PYTEST_ROOT.resolve()
            or (
                resolved.parent == Path("/var/tmp")
                and resolved.name.startswith(("syncmusic-it-", "sd-hw-"))
            )
            or resolved == services.GENERATED.resolve()
        )
        if ok:
            picked.append(p)
    return picked


def is_pytest(cmdline: list[str]) -> bool:
    """A pytest run (or xdist worker): `pytest …`, `python -m pytest …`, `uv run pytest …`."""
    head = cmdline[:4]
    names = [os.path.basename(a) for a in head]
    return any(n in ("pytest", "py.test") for n in names) or any(
        a == "-m" and b == "pytest" for a, b in zip(head, head[1:], strict=False)
    )


def select_procs(procs: Iterable[Proc], uid: int, pytest_root: Path = PYTEST_ROOT) -> list[int]:
    """Test daemons (command line inside the pytest temp root) and orphaned test browsers / Xvfb of `uid`."""
    marker = str(pytest_root) + "/"
    picked = []
    for p in procs:
        if p.uid != uid or not p.cmdline or p.pid == os.getpid():
            continue
        cmd = " ".join(p.cmdline)
        exe = os.path.basename(p.cmdline[0])
        if is_pytest(p.cmdline):
            continue  # a test run itself: refused earlier unless --force, never killed
        if (
            marker in cmd
            or p.ppid == 1
            and ("/ms-playwright/" in p.cmdline[0] or (exe == "Xvfb" and "-displayfd" in cmd))
        ):
            picked.append(p.pid)
    return picked


def _procs() -> list[Proc]:
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().split(b"\0")
            stat = (d / "stat").read_text()
            uid = d.stat().st_uid
        except OSError:
            continue
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        out.append(Proc(int(d.name), ppid, uid, [c.decode(errors="replace") for c in cmd if c]))
    return out


def _live_users() -> set[int]:
    """Pids of test runs of this checkout: the compose stack's users and pytest processes started here."""
    try:
        pids = set(services._users()[0])
    except (OSError, ValueError):
        pids = set()
    root = Path(__file__).resolve().parents[1]
    for p in _procs():
        if p.pid == os.getpid() or p.uid != os.getuid() or not is_pytest(p.cmdline):
            continue
        try:
            if Path(f"/proc/{p.pid}/cwd").resolve() == root:
                pids.add(p.pid)
        except OSError:
            pass
    return pids


def _compose_projects(all_: bool) -> list[str]:
    if not all_:
        return [services.project()]
    out = subprocess.run(
        [*services.docker(), "compose", "ls", "-a", "--format", "json"],
        capture_output=True,
        text=True,
    ).stdout
    return sorted(
        {p["Name"] for p in json.loads(out or "[]") if p["Name"].startswith(PREFIX)}
        | {services.project()}
    )


def _containers(all_: bool) -> list[str]:
    """Stray `syncmusic-it-<id>` containers (the Docker image test) - only with --all."""
    if not all_:
        return []
    out = subprocess.run(
        [*services.docker(), "ps", "-a", "--filter", f"name=^{PREFIX}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    ).stdout
    return [n for n in out.split() if n.startswith(PREFIX) and n.count("-") == 2]


def lift_stale_limits(dry_run: bool = False) -> None:
    """Lift the speed limit (io.max) of every loop device that is no longer attached: a killed run leaves it,
    and it would slow down the next user of that loop number."""
    attached = {
        Path(f"/sys/class/block/{Path(d['name']).name}/dev").read_text().strip()
        for d in json.loads(
            subprocess.run(["losetup", "-J"], capture_output=True, text=True).stdout or "{}"
        ).get("loopdevices", [])
    }
    for maj_min in limited_loops():
        if maj_min not in attached:
            print(("would " if dry_run else "") + f"lift the speed limit of loop {maj_min}")
            if not dry_run:
                clear_loop_limit(maj_min)


def run(dry_run: bool, all_: bool, force: bool) -> int:
    def do(what: str, cmd: list[str]) -> None:
        print(("would " if dry_run else "") + what)
        if not dry_run:
            subprocess.run(cmd, check=False, capture_output=True)

    users = _live_users()
    if users and not force:
        print(
            f"a test run is still active (pids {sorted(users)}); stop it first or pass --force",
            file=sys.stderr,
        )
        return 1

    for pid in select_procs(_procs(), os.getuid()):
        do(f"kill test process {pid}", ["kill", "-9", str(pid)])

    if services.docker():
        for name in _containers(all_):
            do(f"remove container {name}", [*services.docker(), "rm", "-f", "-v", name])
        for project in _compose_projects(all_):
            cmd = [*services.docker(), "compose", "-f", str(services.COMPOSE), "-p", project]
            listed = subprocess.run(
                [*cmd, "ps", "-a", "-q"], capture_output=True, text=True
            ).stdout.strip()
            vols = subprocess.run(
                [
                    *services.docker(),
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if listed or vols:
                do(
                    f"stop compose stack {project} and delete its volumes",
                    [*cmd, "down", "--volumes", "--remove-orphans"],
                )

    loops = select_loops(subprocess.run(["losetup", "-J"], capture_output=True, text=True).stdout)
    for dev in loops:
        out = subprocess.run(
            ["lsblk", "-rn", "-o", "MOUNTPOINTS", dev], capture_output=True, text=True
        ).stdout
        for target in out.split():
            do(f"unmount {target}", ["sudo", "-n", "umount", target])
        do(f"detach {dev}", ["sudo", "-n", "losetup", "-d", dev])

    lift_stale_limits(dry_run)

    if IMAGE_ROOT.is_dir() and not IMAGE_ROOT.is_mount():
        for entry in sorted(IMAGE_ROOT.iterdir()):
            if not entry.is_mount():
                do(f"remove {entry}", ["sudo", "-n", "rm", "-rf", "--one-file-system", str(entry)])

    for p in select_paths(TEMP_PATHS):
        print(("would " if dry_run else "") + f"remove {p}")
        if dry_run:
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
            if p.exists():  # root-owned files from containers
                subprocess.run(
                    ["sudo", "-n", "rm", "-rf", "--one-file-system", str(p)], check=False
                )
        else:
            p.unlink(missing_ok=True)
    return 0


def leftovers() -> list[str]:
    """What a previous run left (for the pytest header): cheap checks only, no Docker call."""
    found = []
    try:
        stack_users = services._users()[0]
    except (OSError, ValueError):
        stack_users = set()
    if not stack_users and services.USERS.exists():
        found.append("the compose stack's user list of a killed run")
    out = subprocess.run(["losetup", "-J"], capture_output=True, text=True).stdout
    if (loops := select_loops(out)) and not stack_users:
        found.append(f"{len(loops)} emulated loop device(s)")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="test-cleanup",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  scripts/test-cleanup.sh --dry-run\n  scripts/test-cleanup.sh\n  scripts/test-cleanup.sh --all",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print what would be removed, change nothing"
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="every syncmusic-it-* stack and container, not only this checkout's",
    )
    ap.add_argument(
        "--force", action="store_true", help="clean up even while a test run seems active"
    )
    a = ap.parse_args(argv)
    return run(a.dry_run, a.all, a.force)


if __name__ == "__main__":
    raise SystemExit(main())
