# SPDX-License-Identifier: GPL-3.0-or-later
"""Rule presets: common rules in one click, with a few parameters.

A preset builds an ordinary `Rule` (Navidrome-compatible where the fields allow it); the rule builder appends it to
the rules being edited, where it can be changed like any other. A preset is offered for a set of libraries when one
of them serves its media kind and has its fields. "These artists or albums" starts empty: the builder
fills it from "Pick from library".
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from syncmusic.core.models import MediaKind, Rule


@dataclass(frozen=True)
class PresetParam:
    name: str
    label: str
    default: int
    min: int = 1
    max: int = 100_000


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    kind: MediaKind
    build: Callable[
        [dict[str, int]], dict[str, Any]
    ]  # parameters → the rule's fields (without the id)
    fields: frozenset[str] = frozenset()  # rule fields the libraries must have
    params: tuple[PresetParam, ...] = ()
    pick: bool = False  # the builder fills the rule from "Pick from library"

    def values(self, given: dict[str, Any] | None) -> dict[str, int]:
        """The parameters with defaults filled in; a missing, non-integer or out-of-range value is refused."""
        given = given or {}
        unknown = set(given) - {p.name for p in self.params}
        if unknown:
            raise ValueError(
                f"unknown parameter(s) for preset {self.id!r}: {', '.join(sorted(unknown))}"
            )
        out: dict[str, int] = {}
        for p in self.params:
            v = given.get(p.name, p.default)
            if isinstance(v, bool) or not isinstance(v, int | float) or v != int(v):
                raise ValueError(f"{p.label}: expected a whole number")
            if not p.min <= int(v) <= p.max:
                raise ValueError(f"{p.label}: expected {p.min} to {p.max}")
            out[p.name] = int(v)
        return out

    def rule(self, given: dict[str, Any] | None = None, rule_id: str | None = None) -> Rule:
        data = self.build(self.values(given))
        return Rule(
            id=rule_id or f"preset-{self.id}-{uuid.uuid4().hex[:8]}", kind=[self.kind], **data
        )


def _favourites(p: dict[str, int]) -> dict[str, Any]:
    stars = p["stars"]
    return {
        "name": f"Favourites: starred or rated {stars} stars or more",
        "criteria": {"any": [{"is": {"loved": True}}, {"gt": {"rating": stars - 1}}]},
    }


PRESETS: dict[str, Preset] = {
    x.id: x
    for x in (
        Preset(
            "all-music",
            "All music",
            "Every track, with the library's playlists.",
            MediaKind.MUSIC,
            lambda p: {"name": "All music", "criteria": {"all": []}, "include_playlists": True},
        ),
        Preset(
            "favourites",
            "Favourites",
            "Starred tracks and those rated at least this many stars.",
            MediaKind.MUSIC,
            _favourites,
            frozenset({"loved", "rating"}),
            (PresetParam("stars", "Stars", 4, 1, 5),),
        ),
        Preset(
            "new",
            "New in the library",
            "Tracks added in the last N days.",
            MediaKind.MUSIC,
            lambda p: {
                "name": f"Added in the last {p['days']} days",
                "criteria": {"inTheLast": {"dateadded": p["days"]}},
            },
            frozenset({"dateadded"}),
            (PresetParam("days", "Days", 30, 1, 3650),),
        ),
        Preset(
            "most-played",
            "Most played",
            "The N tracks played most often.",
            MediaKind.MUSIC,
            lambda p: {
                "name": f"The {p['tracks']} most played tracks",
                "criteria": {"gt": {"playcount": 0}},
                "sort": "playcount",
                "order": "desc",
                "limit": p["tracks"],
            },
            frozenset({"playcount"}),
            (PresetParam("tracks", "Tracks", 500, 1, 100_000),),
        ),
        Preset(
            "picked",
            "These artists or albums",
            "Pick artists, albums, genres or playlists from the library.",
            MediaKind.MUSIC,
            lambda p: {"name": "These artists or albums", "criteria": {"any": []}},
            frozenset({"artist", "album"}),
            pick=True,
        ),
        Preset(
            "books-in-progress",
            "Audiobooks I'm listening to",
            "Books started and not finished yet.",
            MediaKind.AUDIOBOOK,
            lambda p: {
                "name": "Audiobooks I'm listening to",
                "criteria": {"all": [{"gt": {"position": 0}}, {"is": {"finished": False}}]},
            },
            frozenset({"position", "finished"}),
        ),
        Preset(
            "latest-episodes",
            "Latest podcast episodes",
            "The newest N episodes of every podcast.",
            MediaKind.PODCAST,
            lambda p: {
                "name": f"The latest {p['episodes']} episodes of every podcast",
                "criteria": {"latestEpisodes": {"per_series": p["episodes"]}},
            },
            frozenset({"series"}),
            (PresetParam("episodes", "Episodes per podcast", 3, 1, 1000),),
        ),
        Preset(
            "unplayed-episodes",
            "Newest unplayed episodes",
            "The newest N episodes of every podcast that are not played yet: a played one leaves, the next comes.",
            MediaKind.PODCAST,
            lambda p: {
                "name": f"The newest {p['episodes']} unplayed episodes of every podcast",
                "criteria": {"latestEpisodes": {"per_series": p["episodes"], "unplayed": True}},
            },
            frozenset({"series", "finished"}),
            (PresetParam("episodes", "Episodes per podcast", 5, 1, 1000),),
        ),
    )
}


def available(
    preset: Preset, libraries: Iterable[tuple[Iterable[MediaKind], Iterable[str]]]
) -> bool:
    """Whether one of the libraries (each as its media kinds and rule fields) serves the preset's kind and fields."""
    for kinds, fields in libraries:
        if preset.kind in set(kinds) and preset.fields <= set(fields):
            return True
    return False


# --- generated names follow their number ---------------------------------------------------------------
# A rule whose name is exactly what its preset generated ("The latest 3 episodes of every podcast") is renamed when
# its number changes in the builder; a name the user typed (anything else) is never touched.


def _find(criteria: Any, op: str, field: str) -> Any:
    """The value of the first `{op: {field: value}}` in a criteria tree (None when there is none)."""
    if isinstance(criteria, dict):
        for k, v in criteria.items():
            if k == op and isinstance(v, dict) and field in v:
                return v[field]
            if (found := _find(v, op, field)) is not None:
                return found
    elif isinstance(criteria, list):
        for x in criteria:
            if (found := _find(x, op, field)) is not None:
                return found
    return None


def _plus_one(v: Any) -> Any:
    return v + 1 if isinstance(v, int) and not isinstance(v, bool) else None


# preset id → its one parameter read back from a rule
_READ_BACK: dict[str, Callable[[Rule], Any]] = {
    "favourites": lambda r: _plus_one(_find(r.criteria, "gt", "rating")),
    "new": lambda r: _find(r.criteria, "inTheLast", "dateadded"),
    "most-played": lambda r: r.limit,
    "latest-episodes": lambda r: _find(r.criteria, "latestEpisodes", "per_series"),
    "unplayed-episodes": lambda r: _find(r.criteria, "latestEpisodes", "per_series"),
}
_SENTINEL = 987654321


def _pattern(preset: Preset) -> re.Pattern[str]:
    name = str(preset.build({preset.params[0].name: _SENTINEL})["name"])
    return re.compile("^" + re.escape(name).replace(str(_SENTINEL), r"\d+") + "$")


_PATTERNS = {pid: _pattern(PRESETS[pid]) for pid in _READ_BACK}


def follow_name(rule: Rule) -> Rule:
    """The rule renamed after its number when its name is a preset's generated one with another number."""
    for pid, pattern in _PATTERNS.items():
        if not pattern.match(rule.name or ""):
            continue
        value = _READ_BACK[pid](rule)
        if not isinstance(value, int) or isinstance(value, bool):
            return rule
        name = str(PRESETS[pid].build({PRESETS[pid].params[0].name: value})["name"])
        return rule if name == rule.name else rule.model_copy(update={"name": name})
    return rule
