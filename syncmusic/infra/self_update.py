# SPDX-License-Identifier: GPL-3.0-or-later
"""Installing an update of a single-file binary from its GitHub release (Settings → Updates, `syncmusic update`).

Only the binaries update themselves: the systemd service of `syncmusic --install` (install type `service`), and a
standalone or desktop binary someone runs by hand. Docker and source installs only get the notice.

Steps (`install`): download the release's binary for this install and `SHA256SUMS` next to the running binary,
check the checksum, let the new binary unpack itself once (a service: into the root-owned runtime dir; this is also
the test that it starts at all), keep the running binary as `<binary>.previous`, move the new one in place
atomically and mark it on trial (`<binary>.trial`). The caller then restarts: the service exits with
`RESTART_EXIT_CODE` (its unit restarts it), a binary run by hand starts itself again (`os.execv`).

The trial: the launcher (packaging/launcher.in) counts the starts of a binary on trial and, after
`TRIAL_STARTS` starts that never became healthy, moves `.previous` back and leaves `<binary>.rolled-back`. The app
ends the trial once it has run for `HEALTHY_AFTER_S` (`confirm`) and reports either outcome once (`outcome`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from syncmusic import USER_AGENT

RESTART_EXIT_CODE = 75  # the unit has Restart=on-failure
HEALTHY_AFTER_S = 60
TRIAL_STARTS = 3  # matches packaging/launcher.in
SERVICE_RUNTIME = Path(
    "/opt/syncmusic/runtime"
)  # matches packaging/launcher.in and infra/install.py
SUDOERS_DIR = Path("/etc/sudoers.d")
DOWNLOAD_TIMEOUT_S = 600
UNPACK_TIMEOUT_S = 300


class Refused(Exception):
    """The update cannot be installed (the message says why); nothing was changed."""


@dataclass
class Paths:
    binary: Path

    @property
    def previous(self) -> Path:
        return self.binary.with_name(self.binary.name + ".previous")

    @property
    def trial(self) -> Path:
        return self.binary.with_name(self.binary.name + ".trial")

    @property
    def rolled_back(self) -> Path:
        return self.binary.with_name(self.binary.name + ".rolled-back")

    @property
    def download(self) -> Path:
        return self.binary.with_name(f".{self.binary.name}.download")


def running_binary() -> Path | None:
    """The single-file binary this process was started from (set by its launcher), else None."""
    binary = os.environ.get("SYNCMUSIC_BINARY")
    return Path(binary) if binary else None


def expected_sha256(sums: str, name: str) -> str:
    for line in sums.splitlines():
        parts = line.split()
        if (
            len(parts) == 2
            and parts[1].lstrip("*") == name
            and re.fullmatch(r"[0-9a-f]{64}", parts[0])
        ):
            return parts[0]
    raise Refused(f"SHA256SUMS of the release has no line for {name}")


def _remove(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


async def download(
    asset: dict[str, object],
    sums_url: str,
    dest: Path,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """The asset into `dest`, checked against the release's SHA256SUMS; `dest` is removed on any failure."""
    name, url = str(asset.get("name") or ""), str(asset.get("url") or "")
    if not name or not url or not sums_url:
        raise Refused("the release has no download for this install")
    headers = {"User-Agent": USER_AGENT}
    digest = hashlib.sha256()
    try:
        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT_S, transport=transport, follow_redirects=True, headers=headers
        ) as c:
            sums = await c.get(sums_url)
            sums.raise_for_status()
            want = expected_sha256(sums.text, name)
            with dest.open("wb") as f:
                async with c.stream("GET", url) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
                        digest.update(chunk)
    except httpx.HTTPError as e:
        _remove(dest)
        raise Refused(f"the download failed: {type(e).__name__}") from e
    except BaseException:
        _remove(dest)
        raise
    if digest.hexdigest() != want:
        _remove(dest)
        raise Refused(f"checksum mismatch for {name}: not installed")


def runtime_id(binary: Path) -> str:
    with binary.open("rb") as f:
        head = f.read(4096).decode(errors="replace")
    m = re.search(r"^ID=(\S+)$", head, re.M)
    if not m:
        raise Refused("the downloaded file is no SyncMusic binary")
    return m.group(1)


def unpack(new: Path, service: bool) -> str:
    """Let the new binary unpack itself (and so prove it starts); a service's into the root-owned runtime dir.
    Returns its runtime id."""
    new.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "SYNCMUSIC_BINARY"}
    if service:
        env["SYNCMUSIC_RUNTIME_DIR"] = str(SERVICE_RUNTIME)
    try:
        r = subprocess.run(
            [str(new), "--help"],
            env=env,
            capture_output=True,
            text=True,
            timeout=UNPACK_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused(f"the new version does not start: {e}") from e
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()
        raise Refused(f"the new version does not start: {tail[-1] if tail else r.returncode}")
    return runtime_id(new)


def service_helper(runtime_id: str) -> Path:
    return SERVICE_RUNTIME / runtime_id / "python" / "bin" / "syncmusic-priv"


def retarget_sudoers(helper: Path, sudoers: Path = SUDOERS_DIR) -> list[Path]:
    """Point the `--user` rules of `syncmusic --install` at the new runtime's root helper."""
    changed = []
    for rule in sorted(sudoers.glob("syncmusic-*")):
        try:
            text = rule.read_text()
        except OSError:
            continue
        new = re.sub(
            r"NOPASSWD: \S*/runtime/[^/\s]+/python/bin/syncmusic-priv", f"NOPASSWD: {helper}", text
        )
        if new != text:
            tmp = rule.with_name(rule.name + ".tmp")
            tmp.write_text(new)
            tmp.chmod(0o440)
            tmp.replace(rule)
            changed.append(rule)
    return changed


def swap(paths: Paths, new: Path) -> None:
    """The running binary becomes `.previous`, `new` takes its place (atomic), the trial starts."""
    new.chmod(0o755)
    shutil.copy2(paths.binary, paths.previous)
    paths.trial.write_text("0\n")
    paths.rolled_back.unlink(missing_ok=True)
    os.replace(new, paths.binary)


def write_marker(marker: Path, previous: str, target: str) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"from": previous, "to": target, "at": time.time()}))


def outcome(marker: Path, paths: Paths | None, running: str) -> tuple[str, dict[str, str]] | None:
    """After a restart: ("updated", …) when this is the new version (still on trial), ("rolled_back", …) when the
    launcher went back to the previous one; None without an update. A rollback is reported and cleared here."""
    try:
        doc = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    info = {"from": str(doc.get("from", "")), "to": str(doc.get("to", ""))}
    if running == info["to"]:
        return "updated", info
    marker.unlink(missing_ok=True)
    if paths is not None:
        paths.rolled_back.unlink(missing_ok=True)
        paths.trial.unlink(missing_ok=True)
    return "rolled_back", info


def confirm(marker: Path, paths: Paths | None) -> None:
    """The new version ran long enough: end the trial (the launcher no longer rolls back)."""
    marker.unlink(missing_ok=True)
    if paths is not None:
        paths.trial.unlink(missing_ok=True)


def prune_runtimes(keep: set[str], runtime: Path = SERVICE_RUNTIME) -> None:
    """Remove unpacked runtimes of older versions (the current and the previous one stay)."""
    if not runtime.is_dir():
        return
    for d in runtime.iterdir():
        if d.is_dir() and d.name not in keep and not d.name.startswith("."):
            shutil.rmtree(d, ignore_errors=True)
