"""Save and restore the test user's Audiobookshelf progress around reverse-sync tests.

Progress entries that did not exist before are deleted (`DELETE /api/me/progress/<id>`), changed ones are PATCHed
back to their saved values. Only the test server's user progress is touched, never the test media.
"""

from __future__ import annotations

from typing import Any

from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary

FIELDS = ("currentTime", "duration", "progress", "isFinished", "hideFromContinueListening")


def _key(p: dict[str, Any]) -> tuple[str, str | None]:
    return p["libraryItemId"], p.get("episodeId") or None


async def save(lib: AudiobookshelfLibrary) -> dict[tuple[str, str | None], dict[str, Any]]:
    return {_key(p): p for p in await lib._progress()}


async def restore(
    lib: AudiobookshelfLibrary, saved: dict[tuple[str, str | None], dict[str, Any]]
) -> None:
    for p in await lib._progress():
        key = _key(p)
        old = saved.get(key)
        if old is None:
            r = await lib.client().delete(f"/api/me/progress/{p['id']}")
            r.raise_for_status()
        elif any(old.get(f) != p.get(f) for f in FIELDS):
            path = "/".join(x for x in ("/api/me/progress", key[0], key[1]) if x)
            r = await lib.client().patch(path, json={f: old.get(f) for f in FIELDS})
            r.raise_for_status()


def same(a: dict[Any, dict[str, Any]], b: dict[Any, dict[str, Any]]) -> bool:
    return a.keys() == b.keys() and all(a[k].get(f) == b[k].get(f) for k in a for f in FIELDS)
