# SPDX-License-Identifier: GPL-3.0-or-later
"""Default rule templates per library: when a library is added, its basic templates exist at once.

They replace the global built-ins. A default is generated from code for its library (so an upgrade can
improve it) until the user edits it: then it is stored like any user template (and backed up). A deleted
default is remembered per library and not generated again. Removing the library removes its untouched defaults.

What a library gets depends on its type: Navidrome (Subsonic) the former built-ins plus "new in the last 30 days";
Audiobookshelf books and podcasts by listening state; Jellyfin all videos, all movies and all series; a folder
"All …" for each media kind it holds.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from syncmusic.core.models import LibraryConfig, MediaKind, Rule, RuleTemplate
from syncmusic.core.rules.presets import PRESETS

PREFIX = "default."
_EPOCH = dt.datetime(2026, 10, 23, tzinfo=dt.UTC)  # generated: no edit time


def default_id(library_id: str, key: str) -> str:
    return f"{PREFIX}{library_id}.{key}"


def _template(lib: LibraryConfig, key: str, name: str, rule: dict[str, Any]) -> RuleTemplate:
    tid = default_id(lib.id, key)
    return RuleTemplate(
        id=tid,
        name=name,
        library_ids=[lib.id],
        rules=[Rule(id=tid, **rule)],
        default_of=lib.id,
        updated_at=_EPOCH,
    )


def _preset(key: str, **params: int) -> dict[str, Any]:
    return PRESETS[key].rule(params, rule_id="x").model_dump(exclude={"id"})


def _all(kind: MediaKind, name: str) -> dict[str, Any]:
    return {"name": name, "kind": [kind], "criteria": {"all": []}}


_KIND_NAMES = {
    MediaKind.MUSIC: "All music",
    MediaKind.AUDIOBOOK: "All audiobooks",
    MediaKind.PODCAST: "All podcasts",
    MediaKind.VIDEO: "All videos",
}


def _music_server() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        ("all", "All music", {**_preset("all-music"), "name": "Everything, with the playlists"}),
        (
            "all-playlists",
            "All playlists",
            {
                "name": "Tracks of every playlist, with the playlists",
                "kind": [MediaKind.MUSIC],
                "criteria": {"inPlaylist": {"any": True}},
                "include_playlists": True,
            },
        ),
        ("favourites", "Favourites", _preset("favourites", stars=4)),
        ("most-popular", "Most popular", _preset("most-played", tracks=500)),
        ("new", "New in the last 30 days", _preset("new", days=30)),
    ]


def _audiobook_server() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        ("all-books", "All books", _all(MediaKind.AUDIOBOOK, "Every audiobook")),
        ("all-podcasts", "All podcasts", _all(MediaKind.PODCAST, "Every podcast episode")),
        (
            "unfinished-books",
            "Unfinished books",
            {
                "name": "Audiobooks not finished yet",
                "kind": [MediaKind.AUDIOBOOK],
                "criteria": {"is": {"finished": False}},
            },
        ),
        ("books-in-progress", "Books in progress", _preset("books-in-progress")),
        (
            "unlistened-episodes",
            "Unlistened podcast episodes",
            {
                "name": "Podcast episodes not started",
                "kind": [MediaKind.PODCAST],
                "criteria": {"all": [{"is": {"finished": False}}, {"is": {"position": 0}}]},
            },
        ),
        ("latest-episodes", "Latest podcast episodes", _preset("latest-episodes", episodes=3)),
        (
            "unplayed-episodes",
            "Newest unplayed podcast episodes",
            _preset("unplayed-episodes", episodes=5),
        ),
    ]


def _video_server() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        ("all", "All videos", _all(MediaKind.VIDEO, "Every movie and episode")),
        (
            "all-movies",
            "All movies",
            {
                "name": "Every movie",
                "kind": [MediaKind.VIDEO],
                "criteria": {"isMissing": {"series": True}},
            },
        ),
        (
            "all-series",
            "All series",
            {
                "name": "Every episode of every series",
                "kind": [MediaKind.VIDEO],
                "criteria": {"isPresent": {"series": True}},
            },
        ),
    ]


def _folder(lib: LibraryConfig) -> list[tuple[str, str, dict[str, Any]]]:
    """ "All" = every file of the folder's default kind (named after it; with subfolders of other kinds it
    takes only its own kind), plus one per other kind its subfolders hold."""
    settings = lib.settings or {}
    default = MediaKind(settings.get("default_kind", MediaKind.MUSIC))
    others = {MediaKind(k) for k in (settings.get("subfolder_kinds") or {}).values()} - {default}
    rule: dict[str, Any] = {
        "name": "Every file of the folder",
        "criteria": {"all": []},
        "include_playlists": True,
    }
    if others:
        rule = {**_all(default, f"Every {default.value} file"), "include_playlists": True}
    rows = [("all", _KIND_NAMES[default], rule)]
    for k in (MediaKind.MUSIC, MediaKind.AUDIOBOOK, MediaKind.PODCAST, MediaKind.VIDEO):
        if k in others:
            rows.append((f"all-{k.value}", _KIND_NAMES[k], _all(k, f"Every {k.value} file")))
    return rows


def generated(lib: LibraryConfig) -> list[RuleTemplate]:
    """The default templates of `lib` as the code defines them now (none for an unknown type)."""
    if lib.type == "subsonic":
        rows = _music_server()
    elif lib.type == "audiobookshelf":
        rows = _audiobook_server()
    elif lib.type == "jellyfin":
        rows = _video_server()
    elif lib.type == "folder":
        rows = _folder(lib)
    else:
        rows = []
    return [_template(lib, key, name, rule) for key, name, rule in rows]
