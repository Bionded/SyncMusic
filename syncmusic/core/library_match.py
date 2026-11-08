# SPDX-License-Identifier: GPL-3.0-or-later
"""Which of our libraries is a library another instance put items from onto a device.

A device carries, per library that synced to it, the library's source id and a few probe items (ids with
title and duration). Ours matches when the source id is the same, when the user confirmed the pairing
before (an alias), or - for the same library type - when nearly all probe items exist in our library with
the same title and duration; that last case is a guess the user confirms once."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel

from syncmusic.core.models import LibraryConfig, LibrarySnapshot

PROBE_SIZE = 5
PROBE_SHARE = 0.8  # 4 of 5
DURATION_SLACK_S = 2.0


class ProbeItem(BaseModel):
    item_id: str
    title: str
    duration_s: float | None = None


class ForeignLibrary(BaseModel):
    """A library as a device describes it (no credentials)."""

    source_id: str | None = None
    type: str
    name: str = ""
    url_hint: str = ""
    probe: list[ProbeItem] = []


class LibraryMatch(BaseModel):
    library_id: str
    how: Literal["source_id", "alias", "probe"]
    # the pairing is a guess: ask the user once, then store it as an alias
    confirm: bool = False


def probe_items(snapshot: LibrarySnapshot, item_ids: list[str]) -> list[ProbeItem]:
    """Up to PROBE_SIZE probe items of `item_ids` (those on the device), spread over the list."""
    ids = [i for i in item_ids if i in snapshot.tracks]
    if len(ids) > PROBE_SIZE:
        step = len(ids) / PROBE_SIZE
        ids = [ids[int(k * step)] for k in range(PROBE_SIZE)]
    return [
        ProbeItem(
            item_id=i, title=snapshot.tracks[i].title, duration_s=snapshot.tracks[i].duration_s
        )
        for i in ids
    ]


def _same(probe: ProbeItem, snapshot: LibrarySnapshot) -> bool:
    track = snapshot.tracks.get(probe.item_id)
    if track is None or track.title.casefold().strip() != probe.title.casefold().strip():
        return False
    if probe.duration_s is None or track.duration_s is None:
        return True
    return abs(track.duration_s - probe.duration_s) <= DURATION_SLACK_S


def probe_hits(foreign: ForeignLibrary, snapshot: LibrarySnapshot) -> int:
    return sum(_same(p, snapshot) for p in foreign.probe)


def match_library(
    foreign: ForeignLibrary,
    ours: list[LibraryConfig],
    snapshots: dict[str, LibrarySnapshot],
    aliases: dict[str, str],
) -> LibraryMatch | None:
    """Our library behind `foreign`, or None (it belongs to another instance's library: leave its items alone).
    `snapshots`: ours by library id (those not loaded are not probed)."""
    by_id = {c.id: c for c in ours}
    if foreign.source_id:
        for c in ours:
            if foreign.source_id in (c.source_id, c.source_id_now):
                return LibraryMatch(library_id=c.id, how="source_id")
        alias = aliases.get(foreign.source_id)
        if alias in by_id:
            return LibraryMatch(library_id=alias, how="alias")
    if not foreign.probe:
        return None
    need = max(1, math.ceil(len(foreign.probe) * PROBE_SHARE))
    scored = sorted(
        (
            (probe_hits(foreign, snapshots[c.id]), c.id)
            for c in ours
            if c.type == foreign.type and c.id in snapshots
        ),
        reverse=True,
    )
    if not scored or scored[0][0] < need:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None  # two of ours fit equally: not a guess to offer
    return LibraryMatch(library_id=scored[0][1], how="probe", confirm=True)
