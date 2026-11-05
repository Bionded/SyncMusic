# SPDX-License-Identifier: GPL-3.0-or-later
"""Give freed heap memory back to the OS: after a big snapshot reload glibc keeps the freed arenas as its
high-water mark (measured ~67 MB for 12k tracks). `malloc_trim(0)` releases them; a no-op where libc is not
glibc."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)


def _load() -> Callable[[int], int] | None:
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        fn = libc.malloc_trim  # glibc only
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_size_t]
    fn.restype = ctypes.c_int
    return fn


_malloc_trim = _load()


def trim() -> bool:
    """Release free heap memory to the OS; False where unsupported."""
    if _malloc_trim is None:
        return False
    _malloc_trim(0)
    return True
