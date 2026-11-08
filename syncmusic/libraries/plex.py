# SPDX-License-Identifier: GPL-3.0-or-later
"""Plex Media Server library: the music sections of a Plex server. NOT AVAILABLE in this version: it is not in the
registry (registry.RETIRED_LIBRARIES) and kept for a later video library; a stored Plex library loads as unavailable.

Auth: a manual `X-Plex-Token` (no plex.tv PIN login in this version). Every request carries it as a header with
`Accept: application/json` and the client headers; the token is never logged. `section` picks one music section by
title or key; without it every section of type "artist" is read.

Mapping: a track (`type=10`) becomes a Track (`ratingKey`); `originalTitle` (the track artist) is preferred over
`grandparentTitle`; `userRating` 0–10 → 1–5 stars (`round(userRating / 2)`), `viewCount` / `lastViewedAt` → play
count / last played. Media is the original file (the first part's key with `download=1`), never a transcode. Covers:
the track's `thumb`, else the album's `parentThumb`, scaled by `/photo/:/transcode` for thumbnails.

Reverse sync: a play is one `/:/scrobble` (Plex cannot take the play time: it counts the play "now"), star ratings
via `/:/rate` (stars × 2, -1 clears), device playlists created (`POST /playlists` with a `server://` uri) or replaced
(delete the items, add the new ones). Plex has no favourites and music has no resume positions. The state token
covers the sections' scan dates and the playlists; ratings and plays do not move it, so `snapshot_max_age` reloads.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from syncmusic import USER_AGENT, __version__
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
    Track,
)
from syncmusic.core.rules.fields import MUSIC_FIELDS

CONTAINER_FORMATS = {"m4a": "aac", "mp4": "aac", "oga": "ogg", "wav": "wav"}
LIBRARY_ID = "com.plexapp.plugins.library"


class PlexSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: ServerUrl  # e.g. http://plex.local:32400
    token: SecretStr  # X-Plex-Token
    section: str | None = None  # a music section's title or key; None = every music section
    verify_tls: bool = True
    timeout_s: float = Field(30.0, gt=0)
    page_size: int = Field(500, ge=1)


class PlexError(OSError):
    """Plex refused a request (the message says why, never the token)."""


def _epoch(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def _stars(user_rating: Any) -> int | None:
    """Plex's 0–10 user rating as 1–5 stars; 0 / missing is no rating."""
    try:
        value = float(user_rating or 0)
    except (TypeError, ValueError):
        return None
    return max(1, min(5, round(value / 2))) if value > 0 else None


class PlexLibrary(Library, FeedbackSink):
    rule_fields = MUSIC_FIELDS
    type_name = "plex"
    account_fields = ("token",)
    sink_name = "plex"
    display_name = "Plex"
    Settings: ClassVar[type[BaseModel]] = PlexSettings
    capabilities = LibraryCapabilities(
        kinds={MediaKind.MUSIC},
        supports_ratings=True,
        supports_scrobble=True,
        supports_playlists=True,
    )
    snapshot_max_age = timedelta(minutes=30)  # plays and ratings do not move the state token
    # tests inject an httpx transport here before the first request
    transport: httpx.AsyncBaseTransport | None = None

    def __init__(self, config: Any, ctx: Any) -> None:
        super().__init__(config, ctx)
        self._client: httpx.AsyncClient | None = None
        self._part_of: dict[str, str] = {}  # track id -> media part key (the download path)
        self._thumb_of: dict[str, list[str]] = {}  # track id -> its thumb, then the album's
        self._machine_id: str | None = None

    @property
    def s(self) -> PlexSettings:
        settings: PlexSettings = self.settings  # type: ignore[assignment]
        return settings

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.s.url.rstrip("/"),
                verify=self.s.verify_tls,
                timeout=self.s.timeout_s,
                transport=self.transport,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "X-Plex-Token": self.s.token.get_secret_value(),
                    "X-Plex-Client-Identifier": hashlib.sha1(self.id.encode()).hexdigest()[:16],
                    "X-Plex-Product": "SyncMusic",
                    "X-Plex-Version": __version__,
                },
                follow_redirects=True,
            )
        return self._client

    async def _request(
        self, method: str, path: str, headers: dict[str, str] | None = None, **params: Any
    ) -> httpx.Response:
        r = await self.client().request(method, path, params=params, headers=headers)
        if r.status_code == 401:
            raise PlexError("Plex rejected the token")
        return r

    async def get(self, path: str, headers: dict[str, str] | None = None, **params: Any) -> Any:
        r = await self._request("GET", path, headers, **params)
        r.raise_for_status()
        container: dict[str, Any] = (r.json() if r.content else {}).get("MediaContainer") or {}
        return container

    async def _paged(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Every row of a list, page by page (X-Plex-Container-Start / -Size)."""
        out: list[dict[str, Any]] = []
        while True:
            headers = {
                "X-Plex-Container-Start": str(len(out)),
                "X-Plex-Container-Size": str(self.s.page_size),
            }
            body = await self.get(path, headers, **params)
            page = body.get("Metadata") or []
            out.extend(page)
            total = int(body.get("totalSize") or body.get("size") or 0)
            if not page or len(out) >= total:
                return out

    async def _sections(self) -> list[dict[str, Any]]:
        rows = (await self.get("/library/sections")).get("Directory") or []
        music = [d for d in rows if d.get("type") == "artist"]
        if self.s.section is None:
            return music
        want = self.s.section.casefold()
        return [d for d in music if want in (str(d.get("key")), str(d.get("title", "")).casefold())]

    async def source_id(self) -> str | None:
        """`plex:<machineIdentifier>:<section uuid>[,…]`."""
        machine = await self._machine()
        uuids = sorted(str(d.get("uuid") or d.get("key")) for d in await self._sections())
        return f"plex:{machine}:{','.join(uuids)}" if machine and uuids else None

    async def _playlists(self) -> list[dict[str, Any]]:
        return list((await self.get("/playlists", playlistType="audio")).get("Metadata") or [])

    async def _machine(self) -> str:
        if self._machine_id is None:
            self._machine_id = str((await self.get("/identity")).get("machineIdentifier") or "")
        return self._machine_id

    # --- Library API ----------------------------------------------------------------------------

    async def check(self) -> HealthStatus:
        try:
            info = await self.get("/")
            sections = await self._sections()
        except PlexError as e:
            return HealthStatus(ok=False, message=str(e))
        except httpx.HTTPStatusError as e:
            return HealthStatus(ok=False, message=f"Plex: HTTP {e.response.status_code}")
        except httpx.HTTPError as e:
            return HealthStatus(
                ok=False,
                message=f"Plex not reachable at {self.s.url}: {error_reason(e, self.s.timeout_s)}",
            )
        if not sections:
            what = f"no music section {self.s.section!r}" if self.s.section else "no music section"
            return HealthStatus(ok=False, message=f"Plex has {what}")
        return HealthStatus(
            ok=True,
            details={
                "server": info.get("friendlyName"),
                "version": info.get("version"),
                "sections": [d.get("title") for d in sections],
            },
        )

    async def state_token(self) -> str:
        parts = [
            *(
                f"{d.get('key')}|{d.get('updatedAt')}|{d.get('scannedAt')}"
                for d in await self._sections()
            ),
            *sorted(
                f"{p.get('ratingKey')}|{p.get('updatedAt')}|{p.get('leafCount')}"
                for p in await self._playlists()
            ),
        ]
        return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]

    def _track(self, it: dict[str, Any]) -> Track:
        item_id = str(it["ratingKey"])
        media = (it.get("Media") or [{}])[0]
        part = (media.get("Part") or [{}])[0]
        container = str(media.get("container") or part.get("container") or "").lower()
        codec = str(media.get("audioCodec") or "").lower() or None
        fmt = "alac" if codec == "alac" else CONTAINER_FORMATS.get(container, container)
        if part.get("key"):
            self._part_of[item_id] = str(part["key"])
        self._thumb_of[item_id] = [str(x) for x in (it.get("thumb"), it.get("parentThumb")) if x]
        duration = it.get("duration") or media.get("duration")
        return Track(
            ref=ItemRef(library_id=self.id, item_id=item_id),
            title=it.get("title") or item_id,
            artist=it.get("originalTitle") or it.get("grandparentTitle") or None,
            album_artist=it.get("grandparentTitle") or None,
            album=it.get("parentTitle") or None,
            album_id=str(it["parentRatingKey"]) if it.get("parentRatingKey") else None,
            artist_id=str(it["grandparentRatingKey"]) if it.get("grandparentRatingKey") else None,
            track_no=it.get("index"),
            disc_no=it.get("parentIndex"),
            year=it.get("year") or it.get("parentYear"),
            genres=[str(g["tag"]) for g in it.get("Genre") or [] if g.get("tag")],
            duration_s=int(duration) / 1000 if duration else None,
            size_bytes=int(part.get("size") or 0),
            format=fmt or None,
            codec=codec,
            bitrate_kbps=int(media["bitrate"]) if media.get("bitrate") else None,
            channels=media.get("audioChannels"),
            rating=_stars(it.get("userRating")),
            play_count=int(it.get("viewCount") or 0),
            last_played=_epoch(it.get("lastViewedAt")),
            date_added=_epoch(it.get("addedAt")),
            path=part.get("file"),
            has_cover=bool(it.get("thumb") or it.get("parentThumb")),
            source_state=f"{part.get('size')}|{it.get('updatedAt') or ''}",
        )

    async def snapshot(self) -> LibrarySnapshot:
        token = await self.state_token()
        tracks: dict[str, Track] = {}
        for section in await self._sections():
            for it in await self._paged(f"/library/sections/{section['key']}/all", type=10):
                track = self._track(it)
                tracks[track.ref.item_id] = track
        playlists: dict[str, Playlist] = {}
        for p in await self._playlists():
            pid = str(p["ratingKey"])
            entries = await self._paged(f"/playlists/{pid}/items")
            playlists[pid] = Playlist(
                ref=ItemRef(library_id=self.id, item_id=pid),
                name=p.get("title") or pid,
                track_refs=[
                    ItemRef(library_id=self.id, item_id=str(e["ratingKey"]))
                    for e in entries
                    if str(e.get("ratingKey")) in tracks
                ],
            )
        return LibrarySnapshot(
            library_id=self.id, state_token=token, tracks=tracks, playlists=playlists
        )

    async def _metadata(self, item_id: str) -> dict[str, Any] | None:
        """The item now, None when Plex does not have it."""
        r = await self._request("GET", f"/library/metadata/{item_id}")
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        rows = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        return dict(rows[0]) if rows else None

    async def open_media(self, item_id: str) -> AsyncIterator[bytes]:
        """The original file: the media part's key (from the snapshot, else the item's metadata)."""
        key = self._part_of.get(item_id)
        if key is None:
            item = await self._metadata(item_id)
            if item is None:
                raise KeyError(f"download of {item_id!r} failed: not on the Plex server")
            self._track(item)
            key = self._part_of.get(item_id)
            if key is None:
                raise KeyError(f"download of {item_id!r} failed: the item has no media file")
        request = self.client().build_request("GET", key, params={"download": "1"})
        response = await self.client().send(request, stream=True)
        if response.status_code == 200:
            return body_chunks(response, item_id)
        await response.aread()
        await response.aclose()
        if response.status_code >= 500:  # the server is down, not the item
            raise LibraryUnavailable(
                f"download of {item_id!r} failed: HTTP {response.status_code} (server error)"
            )
        raise KeyError(f"download of {item_id!r} failed: HTTP {response.status_code}")

    async def open_cover(self, item_id: str) -> bytes | None:
        return await self._image(item_id, None)

    async def open_thumb(self, item_id: str, size: int) -> bytes | None:
        return await self._image(item_id, size)

    async def _image(self, item_id: str, size: int | None) -> bytes | None:
        thumbs = self._thumb_of.get(item_id)
        if thumbs is None:  # not in a snapshot: an album / artist / playlist id, or a new track
            thumbs = [f"/library/metadata/{item_id}/thumb"]
        for thumb in thumbs:
            try:
                if size:
                    r = await self._request(
                        "GET", "/photo/:/transcode", width=str(size), height=str(size),
                        url=thumb, minSize="1",
                    )  # fmt: skip
                else:
                    r = await self._request("GET", thumb)
            except (httpx.HTTPError, PlexError):
                return None
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                return r.content
        return None

    # --- FeedbackSink ----------------------------------------------------------------------------

    def _own(self, ref: ItemRef) -> str:
        if ref.library_id != self.id:
            raise FeedbackRejected(f"{ref.key()} is not an item of library {self.id!r}")
        return ref.item_id

    async def submit_plays(self, plays: list[PlayEvent]) -> SubmitResult:
        """One scrobble per play (Plex counts it now: the play time cannot be set); failures per play."""
        result = SubmitResult()
        known: dict[str, bool] = {}  # item id -> Plex has it
        for play in plays:
            if play.ref.library_id != self.id:
                result.rejected.append((play, f"not an item of library {self.id!r}"))
                continue
            item_id = play.ref.item_id
            try:
                if item_id not in known:
                    known[item_id] = await self._metadata(item_id) is not None
                if not known[item_id]:
                    result.rejected.append((play, "not found on the Plex server"))
                    continue
                r = await self._request("GET", "/:/scrobble", key=item_id, identifier=LIBRARY_ID)
                r.raise_for_status()
            except (httpx.HTTPError, OSError) as e:
                result.failed.append((play, self.failure_reason(e)))
                continue
            result.accepted.append(play)
        return result

    async def play_count(self, ref: ItemRef) -> int | None:
        if ref.library_id != self.id:
            return None
        item = await self._metadata(ref.item_id)
        return int(item.get("viewCount") or 0) if item is not None else None

    async def set_rating(self, ref: ItemRef, rating: int | None) -> None:
        item_id = self._own(ref)
        if await self._metadata(item_id) is None:
            raise FeedbackRejected(f"{item_id!r} not found on the Plex server")
        value = str(int(rating) * 2) if rating else "-1"
        r = await self._request("PUT", "/:/rate", key=item_id, identifier=LIBRARY_ID, rating=value)
        if r.status_code in (400, 404):
            raise FeedbackRejected(f"Plex refused the rating of {item_id!r}: HTTP {r.status_code}")
        r.raise_for_status()

    async def set_position(self, ref: ItemRef, position_s: float, finished: bool) -> None:
        """Music only: no resume positions (capabilities.supports_positions is False)."""

    async def get_playlist(self, playlist_id: str) -> list[ItemRef] | None:
        r = await self._request("GET", f"/playlists/{playlist_id}/items")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        rows = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        return [ItemRef(library_id=self.id, item_id=str(e["ratingKey"])) for e in rows]

    async def playlists_by_name(self) -> dict[str, str] | None:
        return {
            str(p.get("title") or p["ratingKey"]): str(p["ratingKey"])
            for p in await self._playlists()
            if not p.get("smart")  # a smart playlist cannot take a device's songs
        }

    async def save_playlist(
        self, name: str, refs: list[ItemRef], playlist_id: str | None = None
    ) -> str:
        foreign = [r.key() for r in refs if r.library_id != self.id]
        if foreign:
            raise FeedbackRejected(f"not items of library {self.id!r}: {', '.join(foreign[:3])}")
        ids = ",".join(r.item_id for r in refs)
        uri = f"server://{await self._machine()}/{LIBRARY_ID}/library/metadata/{ids}"
        if playlist_id is None:
            r = await self._request(
                "POST", "/playlists", type="audio", title=name, smart="0", uri=uri
            )
        else:
            r = await self._request("DELETE", f"/playlists/{playlist_id}/items")
            if r.status_code < 400 and refs:
                r = await self._request("PUT", f"/playlists/{playlist_id}/items", uri=uri)
        if r.status_code in (400, 403, 404):
            raise FeedbackRejected(f"Plex refused playlist {name!r}: HTTP {r.status_code}")
        r.raise_for_status()
        if playlist_id is not None:
            return playlist_id
        rows = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        if not rows:
            raise PlexError(f"Plex created playlist {name!r} without an id")
        return str(rows[0]["ratingKey"])

    async def request_rescan(self) -> bool:
        ok = True
        for section in await self._sections():
            r = await self._request("GET", f"/library/sections/{section['key']}/refresh")
            ok = ok and r.status_code in (200, 204)
        return ok

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
