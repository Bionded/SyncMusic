# SPDX-License-Identifier: GPL-3.0-or-later
"""compas-sdtool: the Compás player's own library code as a separate program, run on the card after a sync.

compas-sdtool is part of Compás Tools (https://github.com/Starnished66/compas-tools, `sdtool/`, GPL-3.0) and is built
from the Compás player's sources (https://github.com/Starnished66/compas-player). SyncMusic does not link it: it runs
`compas-sdtool scan <card>` so the player's library is built on the computer and the music is there right after
unplugging, then patches hearts and play counts into it (compas_stats). Its output is JSON lines on stdout:
`{"event": "progress", "phase", "done", "total"}`, then `{"event": "done", ...}` or `{"event": "error", "code",
"message"}`.

Where it is looked for: the path set in Settings, next to the SyncMusic binary, then `<data dir>/tools/`. A tool that
is missing, does not run or writes another `database_format` than the card's player is not used (copy only).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from syncmusic.infra import self_update
from syncmusic.infra.self_update import running_binary
from syncmusic.infra.updates import Release

NAME = "compas-sdtool"
INFO_TIMEOUT_S = 15.0
SCAN_TIMEOUT_S = 3600.0  # a first scan of a full card reads every file's tags
SOURCE_URL = "https://github.com/Starnished66/compas-tools"
LICENSE = "GPL-3.0"

Progress = Callable[[str, int, int], None]  # phase, done, total


_setting: str | None = None  # AppSettings.compas_sdtool_path


def configure(path: str | None) -> None:
    """The Settings path (None: look in the usual places); set whenever the settings change."""
    global _setting
    _setting = path


def configured() -> str | None:
    return _setting


class SdtoolError(Exception):
    """The tool failed, timed out or answered something unexpected."""


@dataclass(frozen=True)
class ToolInfo:
    path: Path
    version: str  # sdtool_version
    build: str  # "tools-<rev>+player-<rev>"
    database_format: int


def arch() -> str:
    m = platform.machine().lower()
    return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(m, m)


def asset_name() -> str:
    """The release asset for this machine."""
    return f"{NAME}-linux-{arch()}"


def candidates(setting: str | None, data_dir: Path) -> list[Path]:
    """Where the tool may be, in order: the Settings path (only that one when set), next to the SyncMusic binary,
    the data dir's tools folder."""
    if setting:
        return [Path(setting).expanduser()]
    dirs = [d.parent for d in (running_binary(),) if d is not None] + [data_dir / "tools"]
    return [d / n for d in dirs for n in (NAME, asset_name())]


def locate(setting: str | None, data_dir: Path) -> Path | None:
    """The first executable candidate, None when there is none."""
    for p in candidates(setting, data_dir):
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


async def _run(
    tool: Path, args: list[str], timeout: float, progress: Progress | None = None
) -> list[dict[str, Any]]:
    """Run the tool; its JSON lines (the progress ones go to `progress` as they come)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(tool),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError as e:
        raise SdtoolError(f"{NAME} does not start: {e}") from e
    events: list[dict[str, Any]] = []

    async def read() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            events.append(ev)
            if progress is not None and ev.get("event") == "progress":
                progress(
                    str(ev.get("phase", "")), int(ev.get("done") or 0), int(ev.get("total") or 0)
                )

    try:
        await asyncio.wait_for(read(), timeout)
        await asyncio.wait_for(proc.wait(), 10)
    except TimeoutError as e:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise SdtoolError(f"{NAME} {args[0]} took longer than {int(timeout)} s") from e
    except BaseException:  # cancelled: the tool must not go on writing the card
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    err = next((e for e in events if e.get("event") == "error"), None)
    if err is not None:
        raise SdtoolError(f"{NAME} {args[0]}: {err.get('message') or err.get('code')}")
    if proc.returncode:
        stderr = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
        raise SdtoolError(f"{NAME} {args[0]} exited with {proc.returncode}: {stderr[-300:]}")
    return events


async def info(tool: Path, card: Path) -> ToolInfo:
    """Version, build and the database format the tool writes (`info` on the card)."""
    events = await _run(tool, ["info", str(card)], INFO_TIMEOUT_S)
    data = next((e for e in reversed(events) if "database_format" in e), None)
    if data is None:
        raise SdtoolError(f"{NAME} info said nothing about its database format")
    try:
        return ToolInfo(
            tool,
            str(data.get("sdtool_version", "")),
            str(data.get("build", "")),
            int(data["database_format"]),
        )
    except (TypeError, ValueError) as e:
        raise SdtoolError(f"{NAME} info: unexpected answer {data!r}") from e


async def scan(
    tool: Path, card: Path, progress: Progress | None = None, timeout: float = SCAN_TIMEOUT_S
) -> dict[str, Any]:
    """Index the card like the player does (only new and changed files are read); the `done` event."""
    events = await _run(tool, ["scan", str(card)], timeout, progress)
    done = next((e for e in reversed(events) if e.get("event") == "done"), None)
    if done is None:
        raise SdtoolError(f"{NAME} scan ended without a result")
    return dict(done)


def unusable(tool: ToolInfo, card_format: int | None) -> str | None:
    """Why the tool must not write this card's library (None: it may): another database format than the player's."""
    if card_format is not None and card_format != tool.database_format:
        return (
            f"{NAME} {tool.version} writes library format {tool.database_format}, the player uses {card_format}: "
            "a newer compas-sdtool is needed"
        )
    return None


# --- download from SyncMusic's own GitHub release (Settings → Devices, the Compás device page) ----------------


def pick(
    releases: list[Release], running: str
) -> tuple[Release, dict[str, Any], dict[str, Any], str] | None:
    """(release, binary asset, metadata asset, SHA256SUMS url) for this machine: the running version's release
    first, else the newest release that has it."""
    name = asset_name()
    ordered = sorted(releases, key=lambda r: r.version != running)  # stable: newest first otherwise
    for r in ordered:
        by_name = {str(a.get("name")): a for a in r.assets}
        sums = by_name.get("SHA256SUMS", {}).get("url")
        if name in by_name and f"{name}.json" in by_name and sums:
            return r, by_name[name], by_name[f"{name}.json"], str(sums)
    return None


async def install(
    releases: list[Release],
    running: str,
    data_dir: Path,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolInfo:
    """Download compas-sdtool for this machine into `<data dir>/tools/`, checked three ways before it is kept: the
    release's SHA256SUMS, that it runs here (the architecture) and that its build and database format are the
    ones the release says. Raises SdtoolError (nothing is kept then)."""
    found = pick(releases, running)
    if found is None:
        raise SdtoolError(f"no SyncMusic release offers {asset_name()} for this computer")
    _release, binary, meta_asset, sums = found
    folder = data_dir / "tools"
    folder.mkdir(parents=True, exist_ok=True)
    part, meta_part = folder / f"{asset_name()}.download", folder / f"{asset_name()}.json.download"
    try:
        await self_update.download(binary, sums, part, transport)
        await self_update.download(meta_asset, sums, meta_part, transport)
        meta = json.loads(meta_part.read_text("utf-8"))
        part.chmod(0o755)
        card = folder / ".probe-card"
        (card / ".compas").mkdir(parents=True, exist_ok=True)
        try:
            got = await info(part, card)
        finally:
            for f in sorted(card.rglob("*"), reverse=True):
                f.rmdir() if f.is_dir() else f.unlink()
            card.rmdir()
        if got.build != meta.get("build") or got.database_format != meta.get("database_format"):
            raise SdtoolError(
                f"the downloaded {NAME} is {got.build} (format {got.database_format}), the release says "
                f"{meta.get('build')} (format {meta.get('database_format')}): not installed"
            )
        final = folder / asset_name()
        part.replace(final)
        meta_part.replace(folder / f"{asset_name()}.json")
    except self_update.Refused as e:
        raise SdtoolError(str(e)) from e
    except (OSError, ValueError) as e:
        raise SdtoolError(f"{NAME} not installed: {e}") from e
    finally:
        for f in (part, meta_part):
            f.unlink(missing_ok=True)
    return ToolInfo(final, got.version, got.build, got.database_format)
