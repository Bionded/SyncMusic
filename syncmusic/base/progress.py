# SPDX-License-Identifier: GPL-3.0-or-later
"""Progress of a long library call: a library's `snapshot()` may say how far it got with
`report_progress(done, total)`; whoever waits for it sets a callback for its own context with `on_progress`.
Nobody listening: a no-op."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextvars import ContextVar

ProgressCallback = Callable[[int, int | None], None]

_callback: ContextVar[ProgressCallback | None] = ContextVar("library_progress", default=None)


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
