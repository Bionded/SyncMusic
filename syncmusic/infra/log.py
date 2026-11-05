# SPDX-License-Identifier: GPL-3.0-or-later
"""Structured logging to stdout (journald) and a rotating file in the data dir.

records logged while a job runs carry its `job` and `device` (a context variable set by the job runner,
added by `ContextFilter`), `parse_log` reads the file back into entries for the Logs page, and `BusLogHandler`
publishes INFO+ records as `log.entry` events (live tail).
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import json
import logging
import logging.handlers
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

LOG_BACKUPS = 5
_SECRET_RE = re.compile(r"(?i)\b(password|passwd|token|api_token|secret|p|t|s)=([^&\s]+)")


def redact(text: str) -> str:
    """Remove credentials from URLs / key=value strings (Subsonic u/p/t/s params, tokens)."""
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)


# {"job": ..., "device": ...} of the job the current task/thread works for (set by JobRunner)
log_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "log_context", default=None
)


class ContextFilter(logging.Filter):
    """Adds the job/device of `log_context` to every record (`record.ctx`)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.ctx = log_context.get() or {}
        return True


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    out: dict[str, Any] = dict(getattr(record, "ctx", None) or {})
    extra = getattr(record, "kv", None)
    if isinstance(extra, dict):
        out.update(extra)
    return out


class KVFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = redact(record.getMessage())
        line = f"ts={self.formatTime(record)} level={record.levelname} logger={record.name} msg={msg!r}"
        line += "".join(f" {k}={redact(str(v))!r}" for k, v in _extras(record).items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        data.update({k: redact(str(v)) for k, v in _extras(record).items()})
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "kv", log_dir: Path | None = None) -> None:
    formatter: logging.Formatter = JSONFormatter() if fmt == "json" else KVFormatter()
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(ContextFilter())
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "syncmusic.log", maxBytes=5_000_000, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        fh.addFilter(ContextFilter())
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "aiosqlite", "PIL", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --- reading the log back -----------------------------------------------------------------------------

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_KV_HEAD = re.compile(r"^ts=(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+) level=(\w+) logger=(\S+) msg=")
_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'" + r'|"(?:[^"\\]|\\.)*"')
_KEY = re.compile(r" (\w+)=")


def _unquote(text: str) -> tuple[str, str] | None:
    """The Python string literal at the start of `text` and the rest."""
    m = _QUOTED.match(text)
    if not m:
        return None
    try:
        value = ast.literal_eval(m.group(0))
    except (ValueError, SyntaxError):
        return None
    return str(value), text[m.end() :]


def _parse_line(line: str) -> dict[str, Any] | None:
    """One log line (kv or json format) → {ts, level, logger, msg, job, device, ...}; None for a continuation line
    (a traceback) or anything else."""
    if line.startswith("{"):
        try:
            data = json.loads(line)
        except ValueError:
            return None
        if not isinstance(data, dict) or "level" not in data:
            return None
        return {k: str(v) for k, v in data.items()}
    m = _KV_HEAD.match(line)
    if not m:
        return None
    got = _unquote(line[m.end() :])
    if got is None:
        return None
    entry: dict[str, Any] = {
        "ts": m.group(1),
        "level": m.group(2),
        "logger": m.group(3),
        "msg": got[0],
    }
    rest = got[1]
    while (k := _KEY.match(rest)) and (v := _unquote(rest[k.end() :])):
        entry[k.group(1)] = v[0]
        rest = v[1]
    return entry


def parse_log(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Entries of a log file; lines that do not start an entry (tracebacks) go to the previous entry's `exc`."""
    out: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.rstrip("\n")
        entry = _parse_line(line)
        if entry is not None:
            entry["raw"] = line
            out.append(entry)
        elif out and line:
            prev = out[-1]
            prev["exc"] = f"{prev['exc']}\n{line}" if prev.get("exc") else line
            prev["raw"] += "\n" + line
    return out


def matches(
    entry: dict[str, Any],
    level: str | None = None,
    device: str | None = None,
    job: str | None = None,
    query: str = "",
    step: str | None = None,
) -> bool:
    if level and LEVELS.get(entry.get("level", ""), 0) < LEVELS.get(level.upper(), 0):
        return False
    if step and entry.get("step") != step:  # the sync step a job was in
        return False
    if device and entry.get("device") != device:
        return False
    if job and entry.get("job") != job:
        return False
    words = query.casefold().split()
    text = f"{entry.get('msg', '')} {entry.get('logger', '')} {entry.get('exc', '')}".casefold()
    return all(w in text for w in words)


class BusLogHandler(logging.Handler):
    """Publishes INFO+ records as `log.entry` events on the bus (live tail of the Logs page); thread-safe."""

    def __init__(
        self, bus: Any, loop: asyncio.AbstractEventLoop, level: int = logging.INFO
    ) -> None:
        super().__init__(level)
        self.bus = bus
        self.loop = loop
        self.addFilter(ContextFilter())

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(
            "syncmusic.infra.events"
        ):  # "queue full" warnings would feed themselves
            return
        try:
            entry: dict[str, Any] = {
                "ts": logging.Formatter().formatTime(record),
                "level": record.levelname,
                "logger": record.name,
                "msg": redact(record.getMessage()),
                **{k: redact(str(v)) for k, v in _extras(record).items()},
            }
            if record.exc_info:
                entry["exc"] = redact(logging.Formatter().formatException(record.exc_info))
            self.loop.call_soon_threadsafe(lambda: self.bus.publish("log.entry", **entry))
        except RuntimeError:  # the loop is closed (shutdown)
            pass
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)
