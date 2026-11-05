# SPDX-License-Identifier: GPL-3.0-or-later
"""What SyncMusic did, as an action log: entries derived from a sync report - listening data sent
to the libraries, playlists written to or removed from the device, playlists saved to a library, files added,
replaced and removed. Entries are structured (a kind and its numbers or names); the web UI words them through
i18n, so the log is the same in every language and old reports get one too."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from syncmusic.core.models import SyncReport

ActivityKind = Literal[
    "sent",  # listening data sent to the libraries: counts {plays, ratings, hearts, positions, finished}
    "playlist_written",  # a playlist created or changed on the device: name, songs
    "playlist_removed",  # a playlist removed from the device: name
    "playlist_to_library",  # a device playlist's edits saved to its library playlist: name, songs
    "playlist_new_in_library",  # a playlist made on the device saved to a library: name
    "files",  # files on the device: counts {add, replace, remove, update_meta}
    "failed",  # items that failed: counts {items}
]

SENT = {
    "plays": "plays_submitted",
    "ratings": "ratings_pushed",
    "hearts": "stars_pushed",
    "positions": "positions_pushed",
    "finished": "finished_pushed",
}
FILES = ("add", "replace", "remove", "update_meta")


class Activity(BaseModel):
    at: datetime
    device_id: str
    report_id: str
    sync_kind: Literal["full", "update"]
    kind: ActivityKind
    name: str = ""
    counts: dict[str, int] = {}
    # where it went: the device's name for device entries, the libraries' names for library
    # entries; "" = unknown, the UI says "the device" / "the library"
    target: str = ""


LIBRARY_KINDS = {"sent", "playlist_to_library", "playlist_new_in_library"}


def report_activity(r: SyncReport, device: str = "", libraries: str = "") -> list[Activity]:
    """The report's actions in the order they happen: what went back to the libraries first, then the device.
    `device` and `libraries` name the targets."""
    at = r.finished_at or r.started_at

    def entry(kind: ActivityKind, name: str = "", counts: dict[str, int] | None = None) -> Activity:
        return Activity(
            at=at,
            device_id=r.device_id,
            report_id=r.id,
            sync_kind=r.kind,
            kind=kind,
            name=name,
            counts={k: v for k, v in (counts or {}).items() if v},
            target=libraries if kind in LIBRARY_KINDS else device,
        )

    out: list[Activity] = []
    rev = r.reverse
    if rev is not None:
        sent = {k: getattr(rev, f) for k, f in SENT.items()}
        if any(sent.values()):
            out.append(entry("sent", counts=sent))
        for name, keys in rev.playlists_pushed.items():
            if name not in rev.playlists_created:
                out.append(entry("playlist_to_library", name, {"songs": len(keys)}))
        out += [entry("playlist_new_in_library", n) for n in rev.playlists_created]
    files = {k: r.counts.get(k, 0) for k in FILES}
    if any(files.values()):
        out.append(entry("files", counts=files))
    out += [entry("playlist_written", n, {"songs": c}) for n, c in r.playlists_written.items()]
    out += [entry("playlist_removed", n) for n in [*r.playlists_dropped, *r.playlists_removed]]
    if r.failures:
        out.append(entry("failed", counts={"items": len(r.failures)}))
    return out
