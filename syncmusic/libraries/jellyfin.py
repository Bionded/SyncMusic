# SPDX-License-Identifier: GPL-3.0-or-later
"""Jellyfin library: the movies and episodes of a Jellyfin server, one way (to the player). Beta.

Auth: the user's password (`/Users/AuthenticateByName` → an access token, kept in memory only), or an API key
(Dashboard → API keys) plus the user whose library is read. Every request carries
`Authorization: MediaBrowser Token="…"`; the key is never logged.

Video only: music, play counts, favourites and positions are not read or sent back (no FeedbackSink). A device that
got music from a Jellyfin library before keeps it (the items become orphans) and its plan says why (`NOTICE`).

Mapping: a `Movie` or `Episode` item becomes a video Track (`Id`): the series, season and episode of an episode
(`SeriesName`, `ParentIndexNumber`, `IndexNumber`), year, genres, frame size and codec of its video stream. Media is
the original file (`/Items/{id}/Download`, else `/Videos/{id}/stream?static=true` for a user without the download
permission) - never a server transcode; the device's video processor converts it where needed. Covers: the item's
Primary image, else its series'. The state token covers the video count and the newest item.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from syncmusic import USER_AGENT, __version__
from syncmusic.base.capabilities import LibraryCapabilities
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
    Track,
)
from syncmusic.core.rules.fields import COMMON_FIELDS

TICKS = 10_000_000  # RunTimeTicks per second
FIELDS = "Path,MediaSources,Genres,DateCreated,ProductionYear,ParentIndexNumber,IndexNumber,Etag,SeriesName"
VIDEO_TYPES = "Movie,Episode"
NOTICE = "Jellyfin now provides video only - attach another library for music"


class JellyfinSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: ServerUrl
    username: str
    api_key: SecretStr | None = None  # with a key the password is not needed
    password: SecretStr | None = None
    verify_tls: bool = True
    timeout_s: float = Field(30.0, gt=0)
    page_size: int = Field(500, ge=1)

    @model_validator(mode="after")
    def _one_credential(self) -> JellyfinSettings:
        if self.api_key is None and self.password is None:
            raise ValueError("set an API key or the user's password")
        return self


class JellyfinError(OSError):
    """Jellyfin refused a request (the message says why, never the key or password)."""


def _date(value: Any) -> datetime | None:
    """Jellyfin's ISO dates have 7 fraction digits ('2026-10-30T06:00:00.1234567Z')."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    head, dot, rest = text.partition(".")
    if dot:
        frac = "".join(c for c in rest if c.isdigit())
        text = f"{head}.{frac[:6]}{rest[len(frac) :]}"
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


class JellyfinLibrary(Library):
    rule_fields = COMMON_FIELDS | {"series", "tracknumber", "discnumber"}
    type_name = "jellyfin"
    account_fields = (
        "username",
        "password",
    )  # with the server's API key the user name alone picks the user
    display_name = "Jellyfin"
    beta = True
    Settings: ClassVar[type[BaseModel]] = JellyfinSettings
    capabilities = LibraryCapabilities(kinds={MediaKind.VIDEO})
    snapshot_max_age = timedelta(minutes=30)
    # tests inject an httpx transport here before the first request
    transport: httpx.AsyncBaseTransport | None = None

    def __init__(self, config: Any, ctx: Any) -> None:
        super().__init__(config, ctx)
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._user_id: str | None = None
        self._login_lock = asyncio.Lock()
        self._series_of: dict[str, str] = {}  # episode id -> series id (the cover fallback)

    @property
    def s(self) -> JellyfinSettings:
        settings: JellyfinSettings = self.settings  # type: ignore[assignment]
        return settings

    def _auth_header(self, token: str | None) -> str:
        device = hashlib.sha1(self.id.encode()).hexdigest()[:16]
        head = f'MediaBrowser Client="SyncMusic", Device="SyncMusic", DeviceId="{device}", Version="{__version__}"'
        return f'{head}, Token="{token}"' if token else head

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

    async def _login(self) -> tuple[str, str]:
        """(token, user id): the API key and the user looked up by name, or a password login."""
        async with self._login_lock:
            if self._token and self._user_id:
                return self._token, self._user_id
            c = self.client()
            if self.s.password is None:  # a password gives a user session (playlist edits need one)
                assert self.s.api_key is not None  # the settings validator wants one of the two
                token = self.s.api_key.get_secret_value()
                r = await c.get("/Users", headers={"Authorization": self._auth_header(token)})
                if r.status_code in (401, 403):
                    raise JellyfinError("Jellyfin rejected the API key")
                r.raise_for_status()
                user = next(
                    (
                        u
                        for u in r.json()
                        if str(u.get("Name", "")).casefold() == self.s.username.casefold()
                    ),
                    None,
                )
                if user is None:
                    raise JellyfinError(f"Jellyfin has no user {self.s.username!r}")
                self._token, self._user_id = token, str(user["Id"])
            else:
                body = {"Username": self.s.username, "Pw": self.s.password.get_secret_value()}
                r = await c.post(
                    "/Users/AuthenticateByName",
                    json=body,
                    headers={"Authorization": self._auth_header(None)},
                )
                if r.status_code in (400, 401, 403):
                    raise JellyfinError("Jellyfin rejected the user name or password")
                r.raise_for_status()
                data = r.json()
                self._token, self._user_id = str(data["AccessToken"]), str(data["User"]["Id"])
            return self._token, self._user_id

    async def _request(self, method: str, path: str, **params: Any) -> httpx.Response:
        token, _ = await self._login()
        r = await self.client().request(
            method, path, params=params, headers={"Authorization": self._auth_header(token)}
        )
        if (
            r.status_code == 401 and self.s.password is not None
        ):  # the session expired: log in again
            self._token = None
            token, _ = await self._login()
            r = await self.client().request(
                method, path, params=params, headers={"Authorization": self._auth_header(token)}
            )
        return r

    async def get(self, path: str, **params: Any) -> Any:
        r = await self._request("GET", path, **params)
        r.raise_for_status()
        return r.json()

    async def _items(self, **params: Any) -> list[dict[str, Any]]:
        """Every item of a query, page by page."""
        _, uid = await self._login()
        out: list[dict[str, Any]] = []
        while True:
            body = await self.get(
                "/Items",
                userId=uid,
                Recursive="true",
                StartIndex=len(out),
                Limit=self.s.page_size,
                **params,
            )
            page = body.get("Items") or []
            out.extend(page)
            if not page or len(out) >= int(body.get("TotalRecordCount") or 0):
                return out

    # --- Library API ----------------------------------------------------------------------------

    async def check(self) -> HealthStatus:
        try:
            info = await self.get("/System/Info")
        except JellyfinError as e:
            return HealthStatus(ok=False, message=str(e))
        except httpx.HTTPStatusError as e:
            return HealthStatus(
                ok=False, message=f"Jellyfin /System/Info: HTTP {e.response.status_code}"
            )
        except httpx.HTTPError as e:
            return HealthStatus(
                ok=False,
                message=f"Jellyfin not reachable at {self.s.url}: {error_reason(e, self.s.timeout_s)}",
            )
        return HealthStatus(
            ok=True,
            details={
                "server": info.get("ServerName"),
                "version": info.get("Version"),
                "user": self.s.username,
            },
        )

    async def source_id(self) -> str | None:
        """`jellyfin:<server id>`: /System/Info's `Id` is unique per server."""
        server = str((await self.get("/System/Info")).get("Id") or "")
        return f"jellyfin:{server}" if server else None

    async def state_token(self) -> str:
        _, uid = await self._login()
        videos = await self.get(
            "/Items", userId=uid, Recursive="true", IncludeItemTypes=VIDEO_TYPES, SortBy="DateCreated",
            SortOrder="Descending", Limit=1, Fields="Etag",
        )  # fmt: skip
        newest = (videos.get("Items") or [{}])[0]
        parts = [str(videos.get("TotalRecordCount")), f"{newest.get('Id')}|{newest.get('Etag')}"]
        return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]

    def _video(self, it: dict[str, Any]) -> Track:
        source = (it.get("MediaSources") or [{}])[0]
        video = next((m for m in source.get("MediaStreams") or [] if m.get("Type") == "Video"), {})
        path = str(it.get("Path") or "")
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        container = ext or str(source.get("Container") or "").lower().split(",")[0]
        episode = it.get("Type") == "Episode"
        series_id = it.get("SeriesId")
        if episode and series_id:
            self._series_of[str(it["Id"])] = str(series_id)
        ticks = it.get("RunTimeTicks")
        bitrate = source.get("Bitrate")
        show = it.get("SeriesName") if episode else None
        extra: dict[str, Any] = {
            k: v
            for k, v in {
                "show": show,
                "season": it.get("ParentIndexNumber") if episode else None,
                "episode": it.get("IndexNumber") if episode else None,
                "width": video.get("Width"),
                "height": video.get("Height"),
            }.items()
            if v is not None
        }
        return Track(
            ref=ItemRef(library_id=self.id, item_id=str(it["Id"])),
            kind=MediaKind.VIDEO,
            title=it.get("Name") or str(it["Id"]),
            series=show,
            track_no=it.get("IndexNumber") if episode else None,
            disc_no=it.get("ParentIndexNumber") if episode else None,
            year=it.get("ProductionYear"),
            genres=list(it.get("Genres") or []),
            duration_s=ticks / TICKS if ticks else None,
            size_bytes=int(source.get("Size") or 0),
            format=container,
            codec=str(video.get("Codec") or "").lower() or None,
            bitrate_kbps=int(bitrate) // 1000 if bitrate else None,
            date_added=_date(it.get("DateCreated")),
            path=path or None,
            has_cover=bool(
                (it.get("ImageTags") or {}).get("Primary") or it.get("SeriesPrimaryImageTag")
            ),
            source_state=f"{source.get('Size')}|{it.get('Etag') or ''}",
            extra=extra,
        )

    async def snapshot(self) -> LibrarySnapshot:
        token = await self.state_token()
        items = await self._items(IncludeItemTypes=VIDEO_TYPES, Fields=FIELDS)
        tracks = {str(it["Id"]): self._video(it) for it in items}
        return LibrarySnapshot(library_id=self.id, state_token=token, tracks=tracks, notice=NOTICE)

    async def open_media(self, item_id: str) -> AsyncIterator[bytes]:
        """The original file: `Download`, else a static stream (a user without the download permission gets 403)."""
        token, _ = await self._login()
        headers = {"Authorization": self._auth_header(token)}
        response: httpx.Response | None = None
        for path, params in (
            (f"/Items/{item_id}/Download", {}),
            (f"/Videos/{item_id}/stream", {"static": "true"}),
        ):
            request = self.client().build_request("GET", path, params=params, headers=headers)
            response = await self.client().send(request, stream=True)
            if response.status_code == 200:
                return body_chunks(response, item_id)
            await response.aread()
            await response.aclose()
            if response.status_code not in (401, 403):
                break
        assert response is not None
        if response.status_code >= 500:  # the server is down, not the item
            raise LibraryUnavailable(
                f"download of {item_id!r} failed: HTTP {response.status_code} (server error)"
            )
        raise KeyError(f"download of {item_id!r} failed: HTTP {response.status_code}")

    async def open_cover(self, item_id: str) -> bytes | None:
        return await self._image(item_id, None)

    async def open_thumb(self, item_id: str, size: int) -> bytes | None:
        """Jellyfin scales it; series ids have Primary images too."""
        return await self._image(item_id, size)

    async def _image(self, item_id: str, size: int | None) -> bytes | None:
        params = {"maxWidth": str(size), "maxHeight": str(size)} if size else {}
        for iid in dict.fromkeys([item_id, self._series_of.get(item_id, item_id)]):
            try:
                r = await self._request("GET", f"/Items/{iid}/Images/Primary", **params)
            except (httpx.HTTPError, JellyfinError):
                return None
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                return r.content
        return None

    async def request_rescan(self) -> bool:
        r = await self._request("POST", "/Library/Refresh")
        return r.status_code in (200, 204)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
