# SPDX-License-Identifier: GPL-3.0-or-later
"""Subsonic-compatible library - Navidrome, Gonic, Airsonic, Ampache, … - over the Subsonic/OpenSubsonic API (type `subsonic`, `navidrome`).

Everything needed for a snapshot comes from Subsonic (`search3` with an empty query, paged). When the server is
Navidrome (its `ping` says `type: navidrome`; setting `use_native_api`, "Navidrome extras (auto)"), the native
Navidrome API (`/api/*`, JWT) is used for what Subsonic lacks - lyrics presence, real file paths, codec,
modification time and smart playlist rules - in `_native_*` helpers that fail soft: when the native API is
off, unavailable or the server is not Navidrome the snapshot is still complete, only those fields stay at their
defaults.

As a FeedbackSink: plays go to Subsonic `scrobble` (one call per play, so each play is accepted or
failed on its own), ratings to `setRating`; Navidrome has no position API.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
from syncmusic.base.progress import report_progress
from syncmusic.core.models import (
    HealthStatus,
    ItemRef,
    LibrarySnapshot,
    Lyrics,
    MediaKind,
    PlayEvent,
    Playlist,
    Rule,
    Track,
)
from syncmusic.core.rules.fields import MUSIC_FIELDS
from syncmusic.core.rules.text import rule_from_nsp

log = logging.getLogger(__name__)

API_VERSION = "1.16.1"
CLIENT_NAME = "SyncMusic"


class NativeUnavailable(Exception):
    """The native API is there but refused or failed for now (login rate limit, server error)."""


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return min(float(r.headers.get("retry-after", "")), 30.0)
    except ValueError:
        return None


class SubsonicError(Exception):
    """A failed Subsonic call (`code` as in the Subsonic spec: 40 = wrong credentials, 70 = not found)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"Subsonic error {code}: {message}")
        self.code = code


def _is_error_content_type(ctype: str) -> bool:
    """Content types Navidrome uses for error bodies, never for media files."""
    return ctype.startswith("text/") or ctype.endswith(("/json", "/xml", "+json", "+xml"))


class SubsonicSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: ServerUrl
    username: str
    password: SecretStr
    verify_tls: bool = True
    page_size: int = Field(500, ge=1)
    # Navidrome extras (auto): lyrics flag, real paths, codec, smart playlist rules - used only when the server is
    # Navidrome
    use_native_api: bool = True
    timeout_s: float = Field(30.0, gt=0)


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _format(song: dict[str, Any]) -> str:
    suffix = str(song.get("suffix") or "").lower()
    if suffix in ("m4a", "m4b", "mp4", "aac"):
        return "aac"  # Subsonic does not tell AAC from ALAC; the native API's codec refines this
    if suffix in ("oga", "ogg"):
        return "ogg"
    return suffix


class SubsonicLibrary(Library, FeedbackSink):
    rule_fields = MUSIC_FIELDS | {"series", "published"}  # podcasts: series = the channel
    type_name = "subsonic"
    account_fields = ("username", "password")
    # the reverse sync's journal keys plays by this name: it stays what it was before the rename
    sink_name = "navidrome"
    display_name = "Subsonic-compatible"
    display_hint = "Navidrome, Gonic, Airsonic, Ampache, …"
    Settings: ClassVar[type[BaseModel]] = SubsonicSettings
    capabilities = LibraryCapabilities(
        kinds={MediaKind.MUSIC, MediaKind.PODCAST},
        supports_ratings=True,
        supports_scrobble=True,
        supports_playlists=True,
        supports_starred=True,
        group_covers=True,
    )
    # the state token does not see rating/play-count/star changes → reload at least this often
    snapshot_max_age = timedelta(minutes=30)  # = the browsing refresh
    # tests inject an httpx.MockTransport here before the first request
    transport: httpx.AsyncBaseTransport | None = None
    native_retry_s: tuple[float, ...] = (
        1,
        2,
        4,
        8,
        8,
    )  # login backoff (Navidrome's limit window: 20 s)

    def __init__(self, config: Any, ctx: Any) -> None:
        super().__init__(config, ctx)
        self._client: httpx.AsyncClient | None = None
        self._jwt: str | None = None
        self._login_lock = asyncio.Lock()
        self._cover_ids: dict[str, str] = {}  # item_id -> coverArt id
        self._token_parts: list[str] | None = None  # the last state token's parts (DEBUG diff)
        self._navidrome: bool | None = (
            None  # the server said it is Navidrome (ping `type`); None = not asked yet
        )

    @property
    def s(self) -> SubsonicSettings:
        settings: SubsonicSettings = self.settings  # type: ignore[assignment]
        return settings

    # --- HTTP ----------------------------------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.s.url.rstrip("/"),
                verify=self.s.verify_tls,
                timeout=self.s.timeout_s,
                transport=self.transport,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self._client

    def _auth(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.s.password.get_secret_value() + salt).encode()).hexdigest()
        return {"u": self.s.username, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT_NAME}

    async def call(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """One Subsonic JSON call; returns the `subsonic-response` body or raises SubsonicError."""
        r = await self.client().get(
            f"/rest/{endpoint}", params={**self._auth(), "f": "json", **params}
        )
        r.raise_for_status()
        body: dict[str, Any] = r.json()["subsonic-response"]
        if body.get("status") != "ok":
            err = body.get("error") or {}
            raise SubsonicError(int(err.get("code", 0)), str(err.get("message", "unknown error")))
        return body

    async def _login(self) -> None:
        """One login at a time; Navidrome rate-limits /auth/login (HTTP 429), so a refused or failing login is
        retried with backoff, then NativeUnavailable."""
        async with self._login_lock:
            if self._jwt is not None:
                return
            for delay in (*self.native_retry_s, None):
                r = await self.client().post(
                    "/auth/login",
                    json={
                        "username": self.s.username,
                        "password": self.s.password.get_secret_value(),
                    },
                )
                if r.status_code == 429 or r.status_code >= 500:
                    if delay is None:
                        raise NativeUnavailable(f"native API login: HTTP {r.status_code}")
                    await asyncio.sleep(_retry_after(r) or delay)
                    continue
                r.raise_for_status()
                self._jwt = str(r.json()["token"])
                return

    async def _native(self, path: str, **params: Any) -> Any:
        """GET on the native API with a cached JWT (re-login once on 401). A transient failure (429, 5xx) is
        NativeUnavailable: the snapshot fails instead of silently missing the native fields."""
        for attempt in (0, 1):
            await self._login()
            r = await self.client().get(
                f"/api/{path}", params=params, headers={"x-nd-authorization": f"Bearer {self._jwt}"}
            )
            if r.status_code == 401 and attempt == 0:
                self._jwt = None
                continue
            if r.status_code == 429 or r.status_code >= 500:
                raise NativeUnavailable(f"native API /api/{path}: HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        return None  # pragma: no cover

    # --- Library API ----------------------------------------------------------------------------

    async def check(self) -> HealthStatus:
        try:
            body = await self.call("ping")
        except SubsonicError as e:
            return HealthStatus(ok=False, message=str(e), details={"code": e.code})
        except (httpx.HTTPError, ValueError, KeyError) as e:
            return HealthStatus(
                ok=False,
                message=f"Subsonic server not reachable at {self.s.url}: {error_reason(e, self.s.timeout_s)}",
            )
        return HealthStatus(
            ok=True,
            details={"server": body.get("type"), "version": body.get("serverVersion")},
        )

    async def state_token(self) -> str:
        scan = (await self.call("getScanStatus")).get("scanStatus", {})
        indexes = (await self.call("getIndexes")).get("indexes", {})
        playlists = (await self.call("getPlaylists")).get("playlists", {}).get("playlist", [])
        # Navidrome re-evaluates a smart playlist on every getPlaylist and bumps its `changed` even when its
        # content stays the same: leave it out for smart playlists, `snapshot_max_age` covers them.
        smart = await self._native_smart_ids()
        # only with the native API: without it a smart playlist can't be told apart, and reading it bumps `changed`
        native = await self._uses_native()
        parts = [
            str(scan.get("lastScan")),
            str(scan.get("count")),
            str(indexes.get("lastModified")),
            *sorted(
                [
                    f"{p['id']}|{'' if str(p['id']) in smart else p.get('changed')}|{p.get('songCount')}"
                    + (
                        ""
                        if not native or str(p["id"]) in smart
                        else f"|{await self._order(str(p['id']))}"
                    )
                    for p in playlists
                ]
            ),
        ]
        last, self._token_parts = self._token_parts, parts
        if last is not None and last != parts and log.isEnabledFor(logging.DEBUG):
            # which part moved (a token that changes on every poll reloads the snapshot every time)
            log.debug(
                "library %s: state token changed: %s -> %s",
                self.id,
                sorted(set(last) - set(parts)),
                sorted(set(parts) - set(last)),
            )
        return hashlib.sha1("\n".join(parts).encode()).hexdigest()

    async def _order(self, playlist_id: str) -> str:
        """A fingerprint of a regular playlist's track order: Navidrome's web UI reorders a playlist without
        bumping its `changed` or `songCount`, so the token has to look at the entries (native API only)."""
        try:
            body = await self.call("getPlaylist", id=playlist_id)
        except SubsonicError as e:
            if e.code != 70:
                raise
            # deleted between getPlaylists and here: the next token no longer lists it
            return "gone"
        entries = body.get("playlist", {}).get("entry", [])
        return hashlib.sha1(",".join(str(e["id"]) for e in entries).encode()).hexdigest()[:12]

    async def snapshot(self) -> LibrarySnapshot:
        by_id: dict[str, dict[str, Any]] = {}
        offset = 0
        # the song count for planning progress; a server without it shows items only
        scan = (await self.call("getScanStatus")).get("scanStatus", {})
        total = int(scan.get("count") or 0) or None
        while True:
            body = await self.call(
                "search3",
                query="",
                songCount=self.s.page_size,
                songOffset=offset,
                artistCount=0,
                albumCount=0,
            )
            page = body.get("searchResult3", {}).get("song", [])
            before = len(by_id)
            by_id.update((str(s["id"]), s) for s in page)
            # a short page ends the list; a page without new songs guards against a server that ignores
            # songOffset (it would page forever)
            report_progress(len(by_id), total)
            if len(page) < self.s.page_size or len(by_id) == before:
                break
            offset += len(page)
        songs = list(by_id.values())
        tracks = {str(s["id"]): self._track(s) for s in songs}
        self._cover_ids = {
            i: str(s["coverArt"])
            for i, s in ((str(s["id"]), s) for s in songs)
            if s.get("coverArt")
        }
        if await self._uses_native():
            await self._native_enrich(tracks)
        playlists = await self._playlists(set(tracks))
        # the token is taken after the playlist reads: without the native API smart playlists are not known, and
        # reading them above bumped their `changed`
        token = await self.state_token()
        return LibrarySnapshot(
            library_id=self.id, state_token=token, tracks=tracks, playlists=playlists
        )

    def _track(self, s: dict[str, Any]) -> Track:
        genres = [str(g["name"]) for g in s.get("genres") or [] if g.get("name")]
        if not genres and s.get("genre"):
            genres = [str(s["genre"])]
        rating = s.get("userRating")
        is_podcast = s.get("type") == "podcast" or s.get("mediaType") == "podcast"
        return Track(
            ref=ItemRef(library_id=self.id, item_id=str(s["id"])),
            kind=MediaKind.PODCAST if is_podcast else MediaKind.MUSIC,
            title=str(s.get("title") or ""),
            artist=s.get("displayArtist") or s.get("artist"),
            album_artist=s.get("displayAlbumArtist") or None,
            album=s.get("album"),
            album_id=s.get("albumId"),
            artist_id=s.get("artistId"),
            track_no=s.get("track") or None,
            disc_no=s.get("discNumber") or None,
            year=s.get("year") or None,
            genres=genres,
            duration_s=float(s["duration"]) if s.get("duration") else None,
            size_bytes=int(s.get("size") or 0),
            format=_format(s),
            bitrate_kbps=s.get("bitRate") or None,
            sample_rate=s.get("samplingRate") or None,
            bit_depth=s.get("bitDepth") or None,
            channels=s.get("channelCount") or None,
            rating=int(rating) if rating else None,
            starred=bool(s.get("starred")),
            play_count=int(s.get("playCount") or 0),
            last_played=_dt(s.get("played")),
            date_added=_dt(s.get("created")),
            comment=s.get("comment") or None,
            path=s.get("path"),
            has_cover=bool(s.get("coverArt")),
            series=s.get("album") if is_podcast else None,
            published=_dt(s.get("publishDate")) if is_podcast else None,
            source_state=f"{s.get('size')}:{s.get('duration')}:{s.get('bitRate')}",
            extra={k: s[k] for k in ("bpm", "musicBrainzId") if s.get(k)},
        )

    async def _native_rows(self, resource: str) -> list[dict[str, Any]]:
        """Every row of a native list endpoint (`song`, `album`), paged."""
        offset, step = 0, max(self.s.page_size, 500)
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        while True:
            page = await self._native(resource, _start=offset, _end=offset + step, _sort="id")
            ids = {str(row.get("id")) for row in page or []}
            if not ids - seen:
                break  # empty page, or a server that ignores _start
            seen |= ids
            rows += page
            if len(page) < step:
                break
            offset += step
        return rows

    async def _native_enrich(self, tracks: dict[str, Track]) -> None:
        """Lyrics flag, real path, codec, cover flag and mtime from `/api/song` (fail soft).

        `source_state` = size + updatedAt of the song + the album's `imageHash`: replacing, adding or removing
        a cover image changes the hash but not the song row (checked on 0.64.2). Editing an external
        `.lrc` changes nothing Navidrome exposes (lyrics are read at request time): not detected."""
        try:
            try:
                images = {
                    str(a.get("id")): str(a.get("imageHash") or "")
                    for a in await self._native_rows("album")
                }
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                images = {}
            for row in await self._native_rows("song"):
                t = tracks.get(str(row.get("id")))
                if t is None:
                    continue
                t.has_lyrics = row.get("lyrics") not in (None, "", "[]")
                t.has_cover = bool(row.get("hasCoverArt", t.has_cover))
                t.path = row.get("path") or t.path
                if str(row.get("codec", "")).lower() == "alac":
                    t.format = "alac"
                t.date_modified = _dt(row.get("updatedAt"))
                t.source_state = f"{row.get('size')}:{row.get('updatedAt')}"
                image = images.get(str(row.get("albumId")))
                if image:
                    t.source_state += f"|img={image}"
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return

    async def _playlists(self, known: set[str]) -> dict[str, Playlist]:
        smart = await self._native_smart_ids()
        out: dict[str, Playlist] = {}
        for p in (await self.call("getPlaylists")).get("playlists", {}).get("playlist", []):
            pid = str(p["id"])
            refs = await self.get_playlist(pid)
            if refs is None:  # deleted after getPlaylists: not in the snapshot
                continue
            out[pid] = Playlist(
                ref=ItemRef(library_id=self.id, item_id=pid),
                name=str(p.get("name") or pid),
                track_refs=[r for r in refs if r.item_id in known],
                smart=pid in smart,
            )
        return out

    async def _uses_native(self) -> bool:
        """The Navidrome extras: on in the settings and the server is Navidrome (asked once)."""
        if not self.s.use_native_api:
            return False
        if self._navidrome is None:
            body = await self.call("ping")
            self._navidrome = str(body.get("type", "")).casefold() == "navidrome"
        return self._navidrome

    async def _native_playlists(self) -> list[dict[str, Any]]:
        if not await self._uses_native():
            return []
        try:
            rows = await self._native("playlist")
        except (httpx.HTTPError, ValueError, KeyError):
            return []
        return [r for r in rows or [] if isinstance(r, dict)]

    async def _native_smart_ids(self) -> set[str]:
        return {str(r["id"]) for r in await self._native_playlists() if r.get("rules")}

    async def smart_playlist_rules(self) -> list[Rule]:
        """Navidrome smart playlists as rules (native API; empty when unavailable)."""
        rules = []
        for r in await self._native_playlists():
            if r.get("rules"):
                rules.append(
                    rule_from_nsp(
                        {**r["rules"], "name": r.get("name", "")}, rule_id=f"nd-{r['id']}"
                    )
                )
        return rules

    async def open_media(self, item_id: str) -> AsyncIterator[bytes]:
        """Original file via `download` (never `stream`: we transcode ourselves).

        Subsonic reports a failed download as HTTP 200 with an XML/JSON error body, so any
        non-media content type is treated as a failure - an error body must never become a track.
        """
        request = self.client().build_request(
            "GET", "/rest/download", params={**self._auth(), "f": "json", "id": item_id}
        )
        response = await self.client().send(request, stream=True)
        ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if response.status_code != 200 or _is_error_content_type(ctype):
            await response.aread()
            await response.aclose()
            if response.status_code >= 500:  # the server is down, not the item
                raise LibraryUnavailable(
                    f"download of {item_id!r} failed: HTTP {response.status_code} (server error)"
                )
            raise KeyError(
                f"download of {item_id!r} failed: HTTP {response.status_code} {response.text[:200]}"
            )

        return body_chunks(response, item_id)

    async def open_cover(self, item_id: str) -> bytes | None:
        return await self._cover(item_id, None)

    async def open_thumb(self, item_id: str, size: int) -> bytes | None:
        """Navidrome scales it; artist (`ar-<id>`) and playlist (`pl-<id>`) pictures too."""
        return await self._cover(item_id, size)

    async def _cover(self, item_id: str, size: int | None) -> bytes | None:
        cover_id = self._cover_ids.get(item_id, item_id)
        params = {**self._auth(), "id": cover_id, **({"size": str(size)} if size else {})}
        try:
            r = await self.client().get("/rest/getCoverArt", params=params)
        except httpx.HTTPError:
            return None
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            return None
        return r.content

    async def get_lyrics(self, item_id: str) -> Lyrics | None:
        try:
            body = await self.call("getLyricsBySongId", id=item_id)
        except SubsonicError:
            return None
        variants = body.get("lyricsList", {}).get("structuredLyrics", [])
        variants.sort(key=lambda v: not v.get("synced"))  # synced first
        for v in variants:
            lines = v.get("line") or []
            if not lines:
                continue
            lang = v.get("lang") if v.get("lang") not in (None, "", "xxx") else None
            if v.get("synced"):
                return Lyrics(
                    synced=True,
                    lines=[
                        (int(ln.get("start", 0)) / 1000, str(ln.get("value", ""))) for ln in lines
                    ],
                    language=lang,
                )
            return Lyrics(
                synced=False,
                text="\n".join(str(ln.get("value", "")) for ln in lines),
                language=lang,
            )
        return None

    # --- FeedbackSink -------------------------------------------------------------------------

    async def submit_plays(self, plays: list[PlayEvent]) -> SubmitResult:
        """One `scrobble?submission=true&time=<ms>` per play; a failure is reported per play, never raised.

        Navidrome answers `ok` to a scrobble of a song it does not have (and drops it), so every song is looked
        up once with `getSong` first: plays of a song that is gone fail instead of vanishing."""
        result = SubmitResult()
        known: dict[str, str | None] = {}  # item id -> why it cannot take plays (None = it can)
        for play in plays:
            item_id = play.ref.item_id
            if play.ref.library_id != self.id:
                result.rejected.append((play, f"not an item of library {self.id!r}"))
                continue
            try:
                if item_id not in known:
                    known[item_id] = await self._song_problem(item_id)
                if (problem := known[item_id]) is not None:
                    result.rejected.append((play, problem))
                    continue
                await self.call(
                    "scrobble",
                    id=item_id,
                    time=int(play.played_at.timestamp() * 1000),
                    submission="true",
                )
            except SubsonicError as e:
                bucket = result.rejected if e.code == 70 else result.failed
                bucket.append((play, f"{type(e).__name__}: {e}"))
            except (httpx.HTTPError, ValueError, KeyError) as e:  # server down, 5xx, garbled answer
                result.failed.append((play, f"{type(e).__name__}: {e}"))
            else:
                result.accepted.append(play)
        return result

    async def play_count(self, ref: ItemRef) -> int | None:
        """`getSong` → playCount (None for a song Navidrome does not have)."""
        if ref.library_id != self.id:
            return None
        try:
            body = await self.call("getSong", id=ref.item_id)
        except SubsonicError as e:
            if e.code == 70:
                return None
            raise
        return int((body.get("song") or {}).get("playCount") or 0)

    async def _song_problem(self, item_id: str) -> str | None:
        try:
            await self.call("getSong", id=item_id)
        except SubsonicError as e:
            if e.code == 70:
                return f"not found on the server: {e}"
            raise
        return None

    async def set_rating(self, ref: ItemRef, rating: int | None) -> None:
        """`setRating` 1–5; None or 0 clears the rating. Raises on failure (FeedbackRejected: never possible)."""
        if ref.library_id != self.id:
            raise FeedbackRejected(f"{ref.key()} is not an item of library {self.id!r}")
        if rating is not None and not 0 <= rating <= 5:
            raise FeedbackRejected(f"rating must be 0–5, got {rating}")
        try:
            await self.call("setRating", id=ref.item_id, rating=rating or 0)
        except SubsonicError as e:
            if e.code == 70:
                raise FeedbackRejected(f"not found in Navidrome: {e}") from e
            raise

    async def set_starred(self, ref: ItemRef, starred: bool) -> None:
        """`star` / `unstar` the song."""
        if ref.library_id != self.id:
            raise FeedbackRejected(f"{ref.key()} is not an item of library {self.id!r}")
        try:
            await self.call("star" if starred else "unstar", id=ref.item_id)
        except SubsonicError as e:
            if e.code == 70:
                raise FeedbackRejected(f"not found in Navidrome: {e}") from e
            raise

    async def get_playlist(self, playlist_id: str) -> list[ItemRef] | None:
        try:
            body = await self.call("getPlaylist", id=playlist_id)
        except SubsonicError as e:
            if e.code == 70:
                return None
            raise
        entries = (body.get("playlist") or {}).get("entry", [])
        return [ItemRef(library_id=self.id, item_id=str(e["id"])) for e in entries]

    async def playlists_by_name(self) -> dict[str, str] | None:
        rows = (await self.call("getPlaylists")).get("playlists", {}).get("playlist", [])
        return {str(p.get("name") or p["id"]): str(p["id"]) for p in rows}

    async def save_playlist(
        self, name: str, refs: list[ItemRef], playlist_id: str | None = None
    ) -> str:
        """Subsonic `createPlaylist`: with `playlistId` it replaces the songs (order kept), with `name` it creates one
        owned by the library's user. Navidrome refuses smart playlists and other users' ones (code
        50) → FeedbackRejected. Entries of the playlist our snapshot does not know are replaced too."""
        foreign = [r.key() for r in refs if r.library_id != self.id]
        if foreign:
            raise FeedbackRejected(f"not items of library {self.id!r}: {', '.join(foreign[:3])}")
        target = {"playlistId": playlist_id} if playlist_id else {"name": name}
        try:
            body = await self.call("createPlaylist", **target, songId=[r.item_id for r in refs])
        except SubsonicError as e:
            if e.code in (50, 70):
                raise FeedbackRejected(f"Navidrome refused playlist {name!r}: {e}") from e
            raise
        return str((body.get("playlist") or {}).get("id") or playlist_id or "")

    async def set_position(self, ref: ItemRef, position_s: float, finished: bool) -> None:
        """Navidrome has no resume-position API (capabilities.supports_positions is False): nothing to do."""

    async def request_rescan(self) -> bool:
        """Subsonic `startScan` (Navidrome: admin users only, a refusal is not an error)."""
        try:
            await self.call("startScan")
        except (SubsonicError, httpx.HTTPError) as e:
            log.info("library %s: startScan refused: %s", self.id, e)
            return False
        return True

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def __repr__(self) -> str:  # never show the password
        return f"SubsonicLibrary(id={self.id!r}, url={self.s.url!r}, user={self.s.username!r})"


__all__ = ["NativeUnavailable", "SubsonicError", "SubsonicLibrary", "SubsonicSettings"]
