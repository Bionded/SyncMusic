"""Builders for synthetic domain objects used across tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from syncmusic.core.models import (
    ItemRef,
    LibrarySnapshot,
    MediaKind,
    Playlist,
    RuleSet,
    RuleTemplate,
    Track,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def track(item_id: str, library_id: str = "lib", **kw: Any) -> Track:
    data: dict[str, Any] = {
        "ref": ItemRef(library_id=library_id, item_id=item_id),
        "title": kw.pop("title", f"Song {item_id}"),
        "size_bytes": kw.pop("size_bytes", 5_000_000),
        "duration_s": kw.pop("duration_s", 200.0),
        "format": kw.pop("format", "mp3"),
    }
    data.update(kw)
    return Track(**data)


def snapshot(tracks: list[Track], library_id: str = "lib", playlists: list[Playlist] | None = None):
    return LibrarySnapshot(
        library_id=library_id,
        state_token="t",
        tracks={t.ref.item_id: t for t in tracks},
        playlists={p.ref.item_id: p for p in playlists or []},
    )


def music_library() -> LibrarySnapshot:
    days = timedelta(days=1)
    ts = [
        track(
            "1",
            title="Back in Black",
            artist="AC/DC",
            album="Back in Black",
            album_id="al1",
            artist_id="ar1",
            year=1980,
            genres=["Rock", "Hard Rock"],
            rating=5,
            starred=True,
            play_count=10,
            last_played=NOW - 2 * days,
            date_added=NOW - 400 * days,
            track_no=6,
            bitrate_kbps=320,
            format="mp3",
        ),
        track(
            "2",
            title="Hells Bells",
            artist="AC/DC",
            album="Back in Black",
            album_id="al1",
            artist_id="ar1",
            year=1980,
            genres=["Rock"],
            rating=3,
            play_count=4,
            track_no=1,
            last_played=NOW - 40 * days,
            date_added=NOW - 400 * days,
            format="mp3",
        ),
        track(
            "3",
            title="Take On Me",
            artist="a-ha",
            album="Hunting High and Low",
            album_id="al2",
            artist_id="ar2",
            year=1985,
            genres=["Pop"],
            rating=None,
            play_count=0,
            date_added=NOW - 5 * days,
            format="flac",
            bit_depth=24,
            sample_rate=96000,
            size_bytes=60_000_000,
            comment="remaster",
        ),
        track(
            "4",
            title="Скрябін - Група крові",
            artist="Скрябін",
            album="Група крові",
            album_id="al3",
            artist_id="ar3",
            year=1988,
            genres=["Rock"],
            rating=4,
            play_count=7,
            last_played=NOW - 10 * days,
            date_added=NOW - 100 * days,
            format="flac",
            compilation=True,
            extra={"mood": "dark"},
        ),
        track(
            "5",
            title="Episode 1",
            kind=MediaKind.PODCAST,
            series="Pod",
            artist="Host",
            published=NOW - 30 * days,
            date_added=NOW - 30 * days,
            format="aac",
        ),
        track(
            "6",
            title="Episode 2",
            kind=MediaKind.PODCAST,
            series="Pod",
            artist="Host",
            published=NOW - 20 * days,
            date_added=NOW - 20 * days,
            format="aac",
        ),
        track(
            "7",
            title="Episode 3",
            kind=MediaKind.PODCAST,
            series="Pod",
            artist="Host",
            published=NOW - 10 * days,
            date_added=NOW - 10 * days,
            format="aac",
            position_s=100,
            finished=False,
        ),
        track(
            "8",
            title="Chapter 1",
            kind=MediaKind.AUDIOBOOK,
            series="Book",
            artist="Author",
            chapter_index=1,
            finished=True,
            format="mp3",
        ),
    ]
    pls = [
        Playlist(
            ref=ItemRef(library_id="lib", item_id="pl1"),
            name="Favourites",
            track_refs=[ts[0].ref, ts[2].ref],
        )
    ]
    return snapshot(ts, playlists=pls)


async def own_rules(
    app: Any, name: str, rule_set: dict[str, Any] | RuleSet, library_ids: list[str] | None = None
) -> RuleSet:
    """A device has templates only: stores `rule_set`'s rules and excludes as the template
    "<name> rules" (id from the name; stored again on every call, so a scenario can change them) and returns the
    device rule set that links it, with its mode, priority and limits."""
    rs = RuleSet.model_validate(rule_set)
    tid = "rules-" + "-".join(name.lower().split())
    tpl = RuleTemplate(
        id=tid,
        name=f"{name} rules",
        library_ids=library_ids or [],
        rules=rs.rules,
        excludes=rs.excludes,
    )
    await app.store.put_template(tpl)
    return rs.model_copy(
        update={"template_ids": [*rs.template_ids, tid], "rules": [], "excludes": []}
    )
