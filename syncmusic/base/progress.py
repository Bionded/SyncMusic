# SPDX-License-Identifier: GPL-3.0-or-later
"""Progress of a long library call: a library's `snapshot()` may say how far it got with
`report_progress(done, total)`; whoever waits for it sets a callback for its own context with `on_progress`.
A call that waits for something else (a device waiting for its player) says what with `report_status(message)`,
heard through `on_status`. Nobody listening: a no-op."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextvars import ContextVar

ProgressCallback = Callable[[int, int | None], None]

StatusCallback = Callable[[str], None]

_callback: ContextVar[ProgressCallback | None] = ContextVar("library_progress", default=None)
_status: ContextVar[StatusCallback | None] = ContextVar("status", default=None)


def report_progress(done: int, total: int | None = None) -> None:
    """`done` items so far, of `total` if the library knows it."""
    cb = _callback.get()
    if cb is not None:
        cb(done, total)


@contextlib.contextmanager
def on_progress(cb: ProgressCallback) -> Iterator[None]:
    token = _callback.set(cb)
    try:
        yield
    finally:
        _callback.reset(token)


def report_status(message: str) -> None:
    """What the call waits for now, in plain words."""
    cb = _status.get()
    if cb is not None:
        cb(message)


@contextlib.contextmanager
def on_status(cb: StatusCallback) -> Iterator[None]:
    token = _status.set(cb)
    try:
        yield
    finally:
        _status.reset(token)
