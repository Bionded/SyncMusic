# SPDX-License-Identifier: GPL-3.0-or-later
"""Re-link managed items whose library ids changed.

A library server that was reinstalled or whose library was re-created (Navidrome, Jellyfin, Audiobookshelf) serves
the same files under new item ids. Without this step every managed item of that library would turn into an orphan
and the next sync would copy the whole library again next to it. `relink` finds, per library, the manifest entries
whose id the library no longer has and the library items no entry holds, pairs them by the metadata the entry was
written with (kind, title, artist, album, track and disc number) and the duration (±2 s), and moves the entries to
the new ids: the files stay, their listening-data baselines go along. Only one-to-one pairs are re-linked; an entry
that fits several new items (or an item several entries) is left as it is.
"""

from __future__ import annotations

from collections.abc import Mapping

from syncmusic.core.models import ItemRef, LibrarySnapshot, Manifest, ManifestEntry, Track
from syncmusic.core.planner import meta_snapshot

MATCH_FIELDS = ("kind", "title", "artist", "album", "track_no", "disc_no")
DURATION_TOLERANCE_S = 2.0


def _sig(meta: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(meta.get(f, "").casefold().strip() for f in MATCH_FIELDS)


def _close(entry: ManifestEntry, track: Track) -> bool:
    a, b = entry.duration_s, track.duration_s
    return a is None or b is None or abs(a - b) <= DURATION_TOLERANCE_S


def find_relinks(manifest: Manifest, snapshots: Mapping[str, LibrarySnapshot]) -> dict[str, str]:
    """Old item key -> new item key for the entries that can be moved (see the module doc)."""
    out: dict[str, str] = {}
    for lib_id, snap in snapshots.items():
        if not snap.tracks:  # an empty library (unmounted share, rescan glitch) is no new library
            continue
        stale = [
            (k, e)
            for k, e in manifest.items.items()
            if e.ref.library_id == lib_id and e.ref.item_id not in snap.tracks and e.meta
        ]
        if not stale:
            continue
        fresh: dict[tuple[str, ...], list[Track]] = {}
        for t in snap.tracks.values():
            if t.ref.key() not in manifest.items:
                fresh.setdefault(_sig(meta_snapshot(t)), []).append(t)
        claims: dict[str, list[str]] = {}  # new key -> old keys that fit it
        pick: dict[str, str] = {}
        for key, entry in stale:
            fits = [t for t in fresh.get(_sig(entry.meta), []) if _close(entry, t)]
            if len(fits) == 1:
                new = fits[0].ref.key()
                pick[key] = new
                claims.setdefault(new, []).append(key)
        out.update({old: new for old, new in pick.items() if len(claims[new]) == 1})
    return out


def apply_relinks(manifest: Manifest, relinks: Mapping[str, str]) -> None:
    """Move the entries (and the playlist bases that name them) to their new keys, in place."""
    if not relinks:
        return
    items: dict[str, ManifestEntry] = {}
    for key, entry in manifest.items.items():
        new = relinks.get(key)
        if new is None:
            items[key] = entry
        else:
            items[new] = entry.model_copy(update={"ref": ItemRef.parse(new), "orphan": False})
    manifest.items = items
    manifest.playlist_refs = {
        name: [relinks.get(k, k) for k in keys] for name, keys in manifest.playlist_refs.items()
    }
