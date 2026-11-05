# SPDX-License-Identifier: GPL-3.0-or-later
"""Audiobookshelf library: podcasts and audiobooks; a FeedbackSink for positions.

Mapping: an audiobook item becomes one Track per audio file (`{itemId}:{ino}`, `chapter_index` = ABS track
order, which is also the order ABS's whole-book `currentTime` runs through); a podcast episode becomes one
Track (`{itemId}:{episodeId}`). Progress (`/api/me` mediaProgress) is mapped onto `position_s`/`finished`.

Reverse sync writes progress back with `PATCH /api/me/progress/<item>[/<episode>]`: a chapter file's position
becomes the book's `currentTime` (chapter start offset + position); a merged book (`{itemId}` alone, core/books.py)
is the whole item, a part of a
split one (`{itemId}:+{ino}+{ino}…`) the span of its files. ABS has no scrobble API, so plays are
accepted without being stored (a position carries the listening state).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from syncmusic import USER_AGENT
from syncmusic.base.capabilities import LibraryCapabilities
from syncmusic.base.feedback import FeedbackRejected, FeedbackSink, SubmitResult
from syncmusic.base.library import (
    Library,
    LibraryUnavailable,
    ServerUrl,
    body_chunks,
    error_reason,
)
from syncmusic.core.models import (
    HealthStatus,
    ItemRef,
    LibrarySnapshot,
    MediaKind,
    PlayEvent,
    Playlist,
    PositionInfo,
    Track,
)
from syncmusic.core.rules.fields import BOOK_FIELDS

CODEC_FORMATS = {
    "mp3": "mp3",
    "aac": "aac",
    "alac": "alac",
    "flac": "flac",
    "opus": "opus",
    "vorbis": "ogg",
}


class AudiobookshelfSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: ServerUrl
    api_token: SecretStr
    library_ids: list[str] = []  # ABS libraries to expose; empty = every book/podcast library
    verify_tls: bool = True
    timeout_s: float = Field(30.0, gt=0)
    page_size: int = Field(100, ge=1)


@dataclass(frozen=True)
class _Target:
    """The progress entry of an item: an episode, or a book (with the chapter's start and length)."""

    item: str
    episode: str | None
    start: float
    length: float | None  # None: an episode
    duration: float


def _ms(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value) / 1000, UTC) if value else None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip()) or None
    except (TypeError, ValueError):
        return None


def _cover_state(item: dict[str, Any]) -> str:
    """Suffix for `source_state` naming the item's cover (path + size/mtime when it is one of the item's
    files), so a replaced or new cover replaces the device copies. '' without a cover."""
    cover = (item.get("media") or {}).get("coverPath")
    if not cover:
        return ""
    for f in item.get("libraryFiles") or []:
        meta = f.get("metadata") or {}
        if meta.get("path") == cover:
            return f"|cover={cover}:{meta.get('size')}:{meta.get('mtimeMs')}"
    return f"|cover={cover}"


FINISHED_WITHIN_S = 30.0  # a position this close to the end counts as finished


class AudiobookshelfLibrary(Library, FeedbackSink):
    rule_fields = BOOK_FIELDS
    sink_name = "audiobookshelf"
    type_name = "audiobookshelf"
    account_fields = ("api_token",)
    display_name = "Audiobookshelf"
    Settings: ClassVar[type[BaseModel]] = AudiobookshelfSettings
    capabilities = LibraryCapabilities(
        kinds={MediaKind.AUDIOBOOK, MediaKind.PODCAST},
        supports_positions=True,
        supports_playlists=True,  # ABS collections
    )
    # tests inject an httpx.MockTransport here before the first request
    transport: httpx.AsyncBaseTransport | None = None

    def __init__(self, config: Any, ctx: Any) -> None:
        super().__init__(config, ctx)
        self._client: httpx.AsyncClient | None = None

    @property
    def s(self) -> AudiobookshelfSettings:
        settings: AudiobookshelfSettings = self.settings  # type: ignore[assignment]
        return settings

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.s.url.rstrip("/"),
                verify=self.s.verify_tls,
                timeout=self.s.timeout_s,
                transport=self.transport,
                headers={
                    "Authorization": f"Bearer {self.s.api_token.get_secret_value()}",
                    "User-Agent": USER_AGENT,
                },
                follow_redirects=True,
            )
        return self._client

    async def get(self, path: str, **params: Any) -> Any:
        r = await self.client().get(path, params=params)
        r.raise_for_status()
        return r.json()

    # --- Library API ----------------------------------------------------------------------------

    async def check(self) -> HealthStatus:
        try:
            r = await self.client().get("/api/me")
        except httpx.HTTPError as e:
            return HealthStatus(
                ok=False,
                message=f"Audiobookshelf not reachable at {self.s.url}: "
                f"{error_reason(e, self.s.timeout_s)}",
            )
        if r.status_code in (401, 403):
            return HealthStatus(ok=False, message="Audiobookshelf rejected the API token")
        if r.status_code != 200:
            return HealthStatus(ok=False, message=f"Audiobookshelf /api/me: HTTP {r.status_code}")
        try:
            missing = set(self.s.library_ids) - {
                lib["id"] for lib in await self._libraries(all_=True)
            }
        except (httpx.HTTPError, ValueError, KeyError) as e:
            return HealthStatus(
                ok=False, message=f"Audiobookshelf libraries: {error_reason(e, self.s.timeout_s)}"
            )
        if missing:
            return HealthStatus(
                ok=False, message=f"unknown ABS libraries: {', '.join(sorted(missing))}"
            )
        return HealthStatus(ok=True, details={"user": r.json().get("username")})

    async def _libraries(self, all_: bool = False) -> list[dict[str, Any]]:
        libs = [
            lib
            for lib in (await self.get("/api/libraries")).get("libraries", [])
            if lib.get("mediaType") in ("book", "podcast")
        ]
        if all_ or not self.s.library_ids:
            return libs
        return [lib for lib in libs if lib["id"] in self.s.library_ids]

    async def _items(self, library_id: str, **params: Any) -> tuple[list[dict[str, Any]], int]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 0
        while True:
            body = await self.get(
                f"/api/libraries/{library_id}/items", limit=self.s.page_size, page=page, **params
            )
            results = [r for r in body.get("results", []) if str(r.get("id")) not in seen]
            seen.update(str(r.get("id")) for r in results)
            out.extend(results)
            total = int(body.get("total", len(out)))
            # a page without new items guards against a server that ignores `page` (would loop forever)
            if not results or len(out) >= total:
                return out, total
            page += 1

    async def _progress(self) -> list[dict[str, Any]]:
        me = await self.get("/api/me")
        return [p for p in me.get("mediaProgress", []) if isinstance(p, dict)]

    async def state_token(self) -> str:
        parts: list[str] = []
        for lib in await self._libraries():
            body = await self.get(
                f"/api/libraries/{lib['id']}/items", limit=1, page=0, sort="updatedAt", desc=1
            )
            newest = body.get("results") or [{}]
            parts.append(f"{lib['id']}|{body.get('total')}|{newest[0].get('updatedAt')}")
            parts.extend(
                f"{c.get('id')}|{c.get('lastUpdate')}|{len(c.get('books') or [])}"
                for c in await self._collections(lib)
            )
        progress = await self._progress()
        parts.append(str(max((int(p.get("lastUpdate") or 0) for p in progress), default=0)))
        parts.append(str(len(progress)))
        return hashlib.sha1("\n".join(parts).encode()).hexdigest()

    async def snapshot(self) -> LibrarySnapshot:
        token = await self.state_token()
        progress = {
            (p.get("libraryItemId"), p.get("episodeId") or None): p for p in await self._progress()
        }
        sem = asyncio.Semaphore(4)

        async def expanded(item_id: str) -> dict[str, Any]:
            async with sem:
                result: dict[str, Any] = await self.get(f"/api/items/{item_id}", expanded=1)
                return result

        tracks: dict[str, Track] = {}
        playlists: dict[str, Playlist] = {}
        for lib in await self._libraries():
            items, _ = await self._items(lib["id"])
            for item in await asyncio.gather(*(expanded(i["id"]) for i in items)):
                if item.get("isMissing") or item.get("isInvalid"):
                    continue
                if item.get("mediaType") == "podcast":
                    new = self._episodes(item, progress)
                else:
                    new = self._book(item, progress)
                tracks.update((t.ref.item_id, t) for t in new)
            for c in await self._collections(lib):
                pl = self._collection(c, tracks)
                playlists[pl.ref.item_id] = pl
        return LibrarySnapshot(
            library_id=self.id, state_token=token, tracks=tracks, playlists=playlists
        )

    async def _collections(self, lib: dict[str, Any]) -> list[dict[str, Any]]:
        """A book library's collections; a server without them has none."""
        if lib.get("mediaType") != "book":
            return []
        try:
            body = await self.get(f"/api/libraries/{lib['id']}/collections")
        except httpx.HTTPStatusError:
            return []
        return [c for c in body.get("results") or [] if isinstance(c, dict) and c.get("id")]

    def _collection(self, c: dict[str, Any], tracks: dict[str, Track]) -> Playlist:
        """A collection as a playlist: the files of its books in the collection's order."""
        order = {str(b.get("id")): i for i, b in enumerate(c.get("books") or [])}
        refs = sorted(
            (t for t in tracks.values() if t.extra.get("abs_item_id") in order),
            key=lambda t: (order[t.extra["abs_item_id"]], t.chapter_index or 0),
        )
        return Playlist(
            ref=ItemRef(library_id=self.id, item_id=f"collection:{c['id']}"),
            name=str(c.get("name") or c["id"]),
            track_refs=[t.ref for t in refs],
        )

    def _book(self, item: dict[str, Any], progress: dict[Any, dict[str, Any]]) -> list[Track]:
        media = item["media"]
        meta = media.get("metadata", {})
        author = meta.get("authorName") or None
        prog = progress.get((item["id"], None), {})
        current = float(prog.get("currentTime") or 0.0)
        book_finished = bool(prog.get("isFinished"))
        # ABS's own series: `series` stays the book title (path layouts use it)
        series = [x for x in meta.get("series") or [] if isinstance(x, dict) and x.get("name")]
        extra: dict[str, Any] = {"abs_item_id": item["id"]}
        if series:
            extra["abs_series"] = [str(x["name"]) for x in series]
            if seq := series[0].get("sequence"):
                extra["abs_series_sequence"] = str(seq)
        out = []
        tracks = sorted(media.get("tracks") or [], key=lambda t: t.get("index") or 0)
        for n, tr in enumerate(tracks, 1):
            fmeta = tr.get("metadata", {})
            start = float(tr.get("startOffset") or 0.0)
            duration = float(tr.get("duration") or 0.0)
            pos = min(max(current - start, 0.0), duration) if prog else None
            tag_title = (tr.get("metaTags") or {}).get("tagTitle")
            filename = str(fmeta.get("filename") or tr.get("title") or "")
            out.append(
                Track(
                    ref=ItemRef(library_id=self.id, item_id=f"{item['id']}:{tr['ino']}"),
                    kind=MediaKind.AUDIOBOOK,
                    title=tag_title or filename.rsplit(".", 1)[0] or str(meta.get("title")),
                    artist=author,
                    album_artist=author,
                    album=meta.get("title"),
                    album_id=item["id"],
                    track_no=n,
                    year=_int(meta.get("publishedYear")),
                    genres=list(meta.get("genres") or []),
                    duration_s=duration or None,
                    size_bytes=int(fmeta.get("size") or 0),
                    format=CODEC_FORMATS.get(
                        str(tr.get("codec", "")).lower(), str(fmeta.get("ext", "")).lstrip(".")
                    ),
                    codec=tr.get("codec"),
                    bitrate_kbps=int(tr["bitRate"]) // 1000 if tr.get("bitRate") else None,
                    channels=tr.get("channels") or None,
                    date_added=_ms(item.get("addedAt")),
                    date_modified=_ms(fmeta.get("mtimeMs")),
                    path=f"{item.get('relPath', '')}/{fmeta.get('relPath', filename)}".lstrip("/"),
                    has_cover=bool(media.get("coverPath")),
                    series=meta.get("title"),
                    author=author,
                    chapter_index=n,
                    position_s=pos,
                    finished=book_finished or (bool(prog) and current >= start + duration > 0),
                    source_state=f"{fmeta.get('size')}:{fmeta.get('mtimeMs')}{_cover_state(item)}",
                    extra={**extra, "abs_start_offset": start},
                )
            )
        return out

    def _episodes(self, item: dict[str, Any], progress: dict[Any, dict[str, Any]]) -> list[Track]:
        media = item["media"]
        meta = media.get("metadata", {})
        out = []
        for ep in media.get("episodes") or []:
            af = ep.get("audioFile") or {}
            fmeta = af.get("metadata", {})
            prog = progress.get((item["id"], ep["id"]), {})
            out.append(
                Track(
                    ref=ItemRef(library_id=self.id, item_id=f"{item['id']}:{ep['id']}"),
                    kind=MediaKind.PODCAST,
                    title=str(ep.get("title") or fmeta.get("filename") or ep["id"]),
                    artist=meta.get("author"),
                    album=meta.get("title"),
                    album_id=item["id"],
                    track_no=_int(ep.get("episode")),
                    disc_no=_int(ep.get("season")),
                    genres=list(meta.get("genres") or []),
                    duration_s=float(af.get("duration") or ep.get("duration") or 0) or None,
                    size_bytes=int(fmeta.get("size") or ep.get("size") or 0),
                    format=CODEC_FORMATS.get(
                        str(af.get("codec", "")).lower(), str(fmeta.get("ext", "")).lstrip(".")
                    ),
                    codec=af.get("codec"),
                    bitrate_kbps=int(af["bitRate"]) // 1000 if af.get("bitRate") else None,
                    channels=af.get("channels") or None,
                    date_added=_ms(ep.get("addedAt")),
                    date_modified=_ms(fmeta.get("mtimeMs")),
                    comment=ep.get("subtitle") or None,
                    path=f"{item.get('relPath', '')}/{fmeta.get('relPath', '')}".strip("/") or None,
                    has_cover=bool(media.get("coverPath")),
                    series=meta.get("title"),
                    author=meta.get("author"),
                    position_s=float(prog["currentTime"])
                    if prog.get("currentTime") is not None
                    else None,
                    finished=bool(prog.get("isFinished")),
                    published=_ms(ep.get("publishedAt")),
                    source_state=f"{fmeta.get('size')}:{fmeta.get('mtimeMs')}{_cover_state(item)}",
                    extra={
                        "abs_item_id": item["id"],
                        "abs_episode_id": ep["id"],
                        "abs_ino": af.get("ino"),
                    },
                )
            )
        return out

    async def _file_url(self, item_id: str) -> str:
        abs_item, _, sub = item_id.partition(":")
        if not sub:
            raise KeyError(f"not an ABS track id: {item_id!r}")
        ino = sub
        item = await self.get(f"/api/items/{abs_item}", expanded=1)
        for ep in item.get("media", {}).get("episodes") or []:
            if ep.get("id") == sub:
                ino = str((ep.get("audioFile") or {}).get("ino"))
        return f"/api/items/{abs_item}/file/{ino}/download"

    async def open_media(self, item_id: str) -> AsyncIterator[bytes]:
        try:
            url = await self._file_url(item_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:  # the server is down, not the item
                raise LibraryUnavailable(
                    f"item {item_id!r}: HTTP {e.response.status_code} (server error)"
                ) from e
            raise KeyError(f"unknown item {item_id!r}: HTTP {e.response.status_code}") from None
        response = await self.client().send(self.client().build_request("GET", url), stream=True)
        if response.status_code != 200:
            await response.aclose()
            if response.status_code >= 500:
                raise LibraryUnavailable(
                    f"download of {item_id!r} failed: HTTP {response.status_code} (server error)"
                )
            raise KeyError(f"download of {item_id!r} failed: HTTP {response.status_code}")

        return body_chunks(response, item_id)

    async def open_cover(self, item_id: str) -> bytes | None:
        abs_item = item_id.partition(":")[0]
        try:
            r = await self.client().get(f"/api/items/{abs_item}/cover", params={"raw": 1})
        except httpx.HTTPError:
            return None
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            return None
        return r.content

    # --- FeedbackSink -------------------------------------------------------------------------

    async def submit_plays(self, plays: list[PlayEvent]) -> SubmitResult:
        """ABS has no scrobble API: plays of its own items are accepted and not stored."""
        result = SubmitResult()
        for play in plays:
            if play.ref.library_id == self.id:
                result.accepted.append(play)
            else:
                result.rejected.append((play, f"not an item of library {self.id!r}"))
        return result

    async def set_rating(self, ref: ItemRef, rating: int | None) -> None:
        """ABS keeps no per-user ratings (capabilities.supports_ratings is False): nothing to do."""

    async def _target(self, ref: ItemRef) -> _Target:
        """Where the item's progress lives in ABS. Raises FeedbackRejected when the item is gone."""
        if ref.library_id != self.id:
            raise FeedbackRejected(f"{ref.key()} is not an item of library {self.id!r}")
        abs_item, _, sub = ref.item_id.partition(":")
        try:
            item = await self.get(f"/api/items/{abs_item}", expanded=1)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise FeedbackRejected(f"{ref.key()}: not found in Audiobookshelf") from e
            raise
        media = item.get("media") or {}
        episode = next((e for e in media.get("episodes") or [] if e.get("id") == sub), None)
        if episode is not None:
            af = episode.get("audioFile") or {}
            duration = float(af.get("duration") or episode.get("duration") or 0.0)
            return _Target(abs_item, sub, 0.0, None, duration)
        tracks = media.get("tracks") or []
        if "+" in sub:  # a part of a split merged book (core/books.py): the span of its files
            inos = set(sub.split("+")) - {""}
            span = [t for t in tracks if str(t.get("ino")) in inos]
            if not span:
                raise FeedbackRejected(f"{ref.key()}: no such audio files in Audiobookshelf")
            start = min(float(t.get("startOffset") or 0.0) for t in span)
            length = sum(float(t.get("duration") or 0.0) for t in span)
            total = float(media.get("duration") or 0.0) or sum(
                float(t.get("duration") or 0.0) for t in tracks
            )
            return _Target(abs_item, None, start, length, total)
        track = next((t for t in tracks if str(t.get("ino")) == sub), None)
        if track is None and (sub or not tracks):
            raise FeedbackRejected(f"{ref.key()}: no such episode or audio file in Audiobookshelf")
        duration = float(media.get("duration") or 0.0) or max(
            (float(t.get("startOffset") or 0) + float(t.get("duration") or 0) for t in tracks),
            default=0.0,
        )
        if track is None:  # the book as one merged file (core/books.py): the whole item
            return _Target(abs_item, None, 0.0, duration, duration)
        start, length = float(track.get("startOffset") or 0.0), float(track.get("duration") or 0.0)
        return _Target(abs_item, None, start, length, duration)

    async def get_position(self, ref: ItemRef) -> PositionInfo | None:
        """The user's progress in the item's own time (a chapter: relative to its start, within the chapter) and
        its `lastUpdate`;
        None when there is none or the item is gone (`set_position` then reports that)."""
        try:
            target = await self._target(ref)
        except FeedbackRejected:
            return None
        key = (target.item, target.episode)
        prog = next(
            (
                p
                for p in await self._progress()
                if (p.get("libraryItemId"), p.get("episodeId") or None) == key
            ),
            None,
        )
        if prog is None or prog.get("currentTime") is None:
            return None
        position = float(prog["currentTime"]) - target.start
        if (
            target.length is not None
        ):  # a chapter: before it 0, past it its end, like the synced bookmarks
            position = min(max(position, 0.0), target.length)
        return PositionInfo(
            position_s=position,
            finished=bool(prog.get("isFinished")),
            updated_at=_ms(prog.get("lastUpdate")),
        )

    async def position_scope(self, ref: ItemRef) -> str:
        """The chapters of a book share the book's progress; an episode (or a gone item) is its own scope."""
        try:
            target = await self._target(ref)
        except FeedbackRejected:
            return ref.key()
        return ref.key() if target.length is None else f"{self.id}/{target.item}"

    async def scope_positions(self) -> dict[str, PositionInfo]:
        """Every progress entry of the user: a book under `<library>/<item>` (its book time), an episode under its
        item key `<library>/<item>:<episode>` - the scopes of `position_scope`. One request."""
        out: dict[str, PositionInfo] = {}
        for p in await self._progress():
            item, episode = p.get("libraryItemId"), p.get("episodeId")
            if not item or p.get("currentTime") is None:
                continue
            out[f"{self.id}/{item}:{episode}" if episode else f"{self.id}/{item}"] = PositionInfo(
                position_s=float(p["currentTime"]),
                finished=bool(p.get("isFinished")),
                updated_at=_ms(p.get("lastUpdate")),
            )
        return out

    async def set_position(self, ref: ItemRef, position_s: float, finished: bool) -> None:
        """PATCH the user's progress. Book: chapter start + position as the book's currentTime. Finished when
        the device says so (a book: its last chapter) or within FINISHED_WITHIN_S of the end. Raises on failure:
        FeedbackRejected when the item is gone (HTTP 404, no such episode/file), anything else is transient."""
        target = await self._target(ref)
        duration = target.duration
        if target.length is None:  # an episode
            current = max(position_s, 0.0)
            path = f"/api/me/progress/{target.item}/{target.episode}"
        else:
            length = target.length
            current = target.start + (length if finished else min(max(position_s, 0.0), length))
            finished = False  # a finished chapter is not a finished book, unless it ends the book
            path = f"/api/me/progress/{target.item}"
        done = finished or (duration > 0 and current >= duration - FINISHED_WITHIN_S)
        body = {
            "currentTime": current,
            "duration": duration,
            "progress": 1.0 if done else (min(current / duration, 1.0) if duration else 0.0),
            "isFinished": done,
        }
        r = await self.client().patch(path, json=body)
        r.raise_for_status()
        if done:
            # ABS 2.37 resets isFinished when the same update moves currentTime of an existing entry (a seek
            # un-finishes), so the flag goes in an update of its own
            r = await self.client().patch(path, json={"isFinished": True, "progress": 1.0})
            r.raise_for_status()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def __repr__(self) -> str:  # never show the token
        return f"AudiobookshelfLibrary(id={self.id!r}, url={self.s.url!r})"
