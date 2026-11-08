# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → System → Storage & resources: what the app keeps on disk, by part, and what the process uses in
memory and CPU. Pure functions over folders and /proc; the facade adds the parts it knows from the running app
(media cache accounting, library snapshots in the database, in-memory caches) and runs the clear actions."""

from __future__ import annotations

import gc
import os
import resource
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syncmusic.infra.housekeeping import disk_free


@dataclass
class Part:
    """One thing on disk: its size, where it is and what clearing it does (None: it cannot be cleared here)."""

    id: str
    label: str
    hint: str
    paths: list[Path]
    size: int = 0
    files: int = 0
    action: str | None = None  # the verb of its clear action, e.g. "Clear", "Compact"
    detail: str = ""
    # per device / per library parts that can be cleared one by one: {id, label, size}
    items: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "hint": self.hint,
            "paths": [str(p) for p in self.paths],
            "size": self.size,
            "files": self.files,
            "action": self.action,
            "detail": self.detail,
            "items": self.items,
        }


def tree_size(paths: list[Path]) -> tuple[int, int]:
    """(bytes, files) under these files or folders; a hard-linked file counts once."""
    seen: set[tuple[int, int]] = set()
    total = files = 0

    def add(p: str) -> None:
        nonlocal total, files
        try:
            st = os.lstat(p)
        except OSError:
            return
        if (st.st_dev, st.st_ino) not in seen:
            seen.add((st.st_dev, st.st_ino))
            total += st.st_size
            files += 1

    for path in paths:
        if path.is_file():
            add(str(path))
            continue
        for root, _dirs, names in os.walk(path):
            for name in names:
                add(os.path.join(root, name))
    return total, files


def subfolders(folder: Path) -> list[dict[str, Any]]:
    """Per subfolder (a device or library id): {id, label, size}, biggest first."""
    if not folder.is_dir():
        return []
    out = []
    for d in sorted(folder.iterdir()):
        if d.is_dir():
            size, _ = tree_size([d])
            out.append({"id": d.name, "label": d.name, "size": size})
    return sorted(out, key=lambda i: -int(i["size"]))


def filesystems(paths: list[Path]) -> list[dict[str, Any]]:
    """The file systems holding these paths, each once: {path, total, free}."""
    seen: dict[int, dict[str, Any]] = {}
    for p in paths:
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            dev = os.stat(p).st_dev
            total, free = disk_free(p)
        except OSError:
            continue
        seen.setdefault(dev, {"path": str(p), "total": total, "free": free})
    return list(seen.values())


# --- the process ---------------------------------------------------------------------------------


def _status_kb(pid: int | str, key: str) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(key + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _children(pid: int) -> list[int]:
    try:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text()
    except OSError:
        return []
    out = []
    for c in text.split():
        out += [int(c), *_children(int(c))]
    return out


class CpuMeter:
    """CPU % of this process since the previous reading, and since the start."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self._last = (self.started, self._cpu())
        self._lock = threading.Lock()

    @staticmethod
    def _cpu() -> float:
        r = resource.getrusage(resource.RUSAGE_SELF)
        return r.ru_utime + r.ru_stime

    def read(self) -> dict[str, float]:
        now, cpu = time.monotonic(), self._cpu()
        with self._lock:
            then, before = self._last
            self._last = (now, cpu)
        cores = os.cpu_count() or 1
        recent = (cpu - before) / (now - then) * 100 if now > then else 0.0
        total = cpu / (now - self.started) * 100 if now > self.started else 0.0
        return {
            "cpu_percent": round(recent, 1),
            "cpu_percent_avg": round(total, 1),
            "cpu_seconds": round(cpu, 1),
            "cores": cores,
            "uptime_s": round(now - self.started, 1),
        }


def process_memory() -> dict[str, Any]:
    """RSS of this process and of its children (ffmpeg, workers) in bytes, and the thread count."""
    pid = os.getpid()
    kids = _children(pid)
    return {
        "rss": _status_kb(pid, "VmRSS") * 1024,
        "rss_peak": _status_kb(pid, "VmHWM") * 1024,
        "children": len(kids),
        "children_rss": sum(_status_kb(c, "VmRSS") for c in kids) * 1024,
        "threads": _status_kb(pid, "Threads"),
    }


def trim_memory() -> bool:
    """Give freed heap memory back to the system (glibc `malloc_trim`): Python frees objects into the allocator's
    arenas, which otherwise keep the process's RSS at its peak after a sync. False where there is no glibc."""
    global _LIBC
    if _LIBC is None:
        try:
            import ctypes

            _LIBC = ctypes.CDLL("libc.so.6")
            _LIBC.malloc_trim.argtypes = [ctypes.c_size_t]
        except (OSError, AttributeError):
            _LIBC = False
    if not _LIBC:
        return False
    _LIBC.malloc_trim(0)
    return True


_LIBC: Any = None


def freeze_gc() -> None:
    """After a job: move every surviving object out of the collector's generations (cheap). The long-lived
    snapshots, manifests and caches then are not walked by a full collection during the next sync, which would
    stop the event loop (0.4 s at 15k tracks). Frozen objects are still freed by reference counting; cycles among
    them wait for `collect_frozen`."""
    gc.freeze()


def collect_frozen() -> None:
    """Hourly, between jobs: unfreeze and collect everything once (frees cycles among frozen objects), then
    freeze again."""
    gc.unfreeze()
    gc.collect()
    gc.freeze()
