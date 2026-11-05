# SPDX-License-Identifier: GPL-3.0-or-later
"""Three-way merge of a playlist edited on the device and in the library.

Lists hold item keys in playlist order (duplicates allowed). `base` is what the last sync wrote to the device
(`Manifest.playlist_refs`). Rules: removes from both sides apply, adds from both sides are kept (union), and the
order is the one of the side that reordered; if both sides reordered differently the merge is a conflict (None) and
the device's name-conflict policy decides.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal


def _common_order(seq: list[str], keep: Counter[str]) -> list[str]:
    """`seq` restricted to the multiset `keep` (first occurrences win)."""
    left = Counter(keep)
    out = []
    for k in seq:
        if left[k] > 0:
            out.append(k)
            left[k] -= 1
    return out


def reordered(base: list[str], side: list[str]) -> bool:
    """Whether `side` changed the relative order of the items it kept from `base`."""
    shared = Counter(base) & Counter(side)
    return _common_order(base, shared) != _common_order(side, shared)


def changed(base: list[str], side: list[str]) -> bool:
    return base != side


def merge(
    base: list[str],
    device: list[str],
    library: list[str],
    prefer: Literal["device", "library"] | None = None,
) -> list[str] | None:
    """The merged playlist, or None when both sides reordered it differently and no side is `prefer`red (then the
    preferred side's order is used with the union of adds and removes)."""
    if not changed(base, device):
        return list(library)
    if not changed(base, library) or device == library:
        return list(device)
    dev_re, lib_re = reordered(base, device), reordered(base, library)
    if dev_re and lib_re:
        if prefer is None:
            return None
        lib_re = prefer == "library"
        dev_re = not lib_re
    b = Counter(base)
    removed = (b - Counter(device)) + (
        b - Counter(library)
    )  # an item removed on either side goes (once per removal)
    kept = b - removed
    # the side that reordered gives the order of the kept base items; otherwise the library's
    order_side = device if dev_re else library
    out = _common_order(order_side, kept)
    # adds: an occurrence beyond the base's count is new; added at the end of its side it goes to the end, otherwise
    # after the entry it follows on its side (first when there is none)
    for side in (device, library):
        seen: Counter[str] = Counter()
        new = []
        for k in side:
            seen[k] += 1
            new.append(seen[k] > b[k])
        tail = len(side)
        while tail and new[tail - 1]:
            tail -= 1
        prev: str | None = None
        for i, k in enumerate(side):
            if new[i]:
                if i >= tail:
                    out.append(k)
                else:
                    out.insert(len(out) - out[::-1].index(prev) if prev in out else 0, k)
            prev = k
    return out
