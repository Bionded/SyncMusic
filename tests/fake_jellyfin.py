"""In-memory Jellyfin API (the shapes of Jellyfin 10.10's /Items, /Users, /Playlists)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from tests.media import TINY_JPEG

USER_ID = "u0001"


def audio(item_id: str, title: str, *, album: str = "Група крові", artist: str = "Скрябін", size: int = 1000,
          track: int = 1, plays: int = 0, favourite: bool = False, container: str = "mp3") -> dict[str, Any]:  # fmt: skip
    return {
        "Id": item_id,
        "Name": title,
        "Type": "Audio",
        "Album": album,
        "AlbumId": f"al-{album}",
        "AlbumArtist": artist,
        "Artists": [artist],
        "ArtistItems": [{"Id": f"ar-{artist}", "Name": artist}],
        "IndexNumber": track,
        "ParentIndexNumber": 1,
        "ProductionYear": 1988,
        "Genres": ["Rock"],
        "RunTimeTicks": 2_000_000_000,
        "DateCreated": "2026-10-01T10:00:00.1234567Z",
        "Etag": f"e-{item_id}",
        "Path": f"/music/{artist}/{album}/{track:02d} {title}.{container}",
        "AlbumPrimaryImageTag": "tag",
        "MediaSources": [
            {
                "Size": size,
                "Container": container,
                "Bitrate": 320000,
                "MediaStreams": [
                    {
                        "Type": "Audio",
                        "Codec": container,
                        "SampleRate": 44100,
                        "Channels": 2,
                        "BitRate": 320000,
                    }
                ],
            }
        ],
        "UserData": {
            "PlayCount": plays,
            "IsFavorite": favourite,
            "Played": plays > 0,
            **({"LastPlayedDate": "2026-10-20T08:00:00.0000000Z"} if plays else {}),
        },
    }


def video(item_id: str, title: str, *, series: str | None = None, season: int = 1, episode: int = 1,
          size: int = 2000, container: str = "mkv") -> dict[str, Any]:  # fmt: skip
    """A movie, or an episode of `series`."""
    it: dict[str, Any] = {
        "Id": item_id,
        "Name": title,
        "Type": "Episode" if series else "Movie",
        "ProductionYear": 2001,
        "Genres": ["Drama"],
        "RunTimeTicks": 30_000_000_000,
        "DateCreated": "2026-10-01T10:00:00.1234567Z",
        "Etag": f"e-{item_id}",
        "Path": f"/video/{series or 'Movies'}/{title}.{container}",
        "ImageTags": {"Primary": "tag"},
        "MediaSources": [
            {
                "Size": size,
                "Container": container,
                "Bitrate": 4_000_000,
                "MediaStreams": [
                    {"Type": "Video", "Codec": "h264", "Width": 1280, "Height": 720},
                    {"Type": "Audio", "Codec": "aac", "Channels": 2},
                ],
            }
        ],
        "UserData": {"PlayCount": 0, "IsFavorite": False, "Played": False},
    }
    if series:
        it.update(
            SeriesName=series,
            SeriesId=f"se-{series}",
            ParentIndexNumber=season,
            IndexNumber=episode,
            SeriesPrimaryImageTag="tag",
        )
    return it


class FakeJellyfin(httpx.AsyncBaseTransport):
    def __init__(self, api_key: str = "key", password: str = "pw") -> None:
        self.api_key, self.password = api_key, password
        self.items: dict[str, dict[str, Any]] = {}
        self.playlists: dict[str, tuple[str, list[str]]] = {}  # id -> (name, item ids)
        self.download_allowed = True
        self.requests: list[str] = []
        self.played: list[tuple[str, str]] = []  # (item id, datePlayed) of each "mark played"

    def add(self, item: dict[str, Any]) -> None:
        self.items[item["Id"]] = item

    def _authorised(self, request: httpx.Request) -> bool:
        m = re.search(r'Token="([^"]*)"', request.headers.get("authorization", ""))
        return m is not None and m.group(1) in (self.api_key, "session-token")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        self.requests.append(f"{request.method} {path}")
        if path == "/Users/AuthenticateByName" and request.method == "POST":
            body = json.loads(request.content)
            if body.get("Username") != "admin" or body.get("Pw") != self.password:
                return httpx.Response(401)
            return httpx.Response(
                200, json={"AccessToken": "session-token", "User": {"Id": USER_ID}}
            )
        if not self._authorised(request):
            return httpx.Response(401)
        if path == "/Users":
            return httpx.Response(200, json=[{"Id": USER_ID, "Name": "Admin"}])
        if path == "/System/Info":
            return httpx.Response(200, json={"ServerName": "fake", "Version": "10.10.7"})
        if path == "/Items":
            return httpx.Response(200, json=self._query(q))
        if m := re.fullmatch(r"/Playlists/([^/]+)/Items", path):
            if m.group(1) not in self.playlists:
                return httpx.Response(404)
            ids = self.playlists[m.group(1)][1]
            return httpx.Response(
                200, json={"Items": [self.items[i] for i in ids if i in self.items]}
            )
        if m := re.fullmatch(r"/Items/([^/]+)/Images/Primary", path):
            known = m.group(1) in self.items or m.group(1).startswith(("al-", "se-"))
            return httpx.Response(200, content=TINY_JPEG, headers={"content-type": "image/jpeg"}) if known else httpx.Response(404)  # fmt: skip
        if m := re.fullmatch(r"/Items/([^/]+)/Download", path):
            if not self.download_allowed:
                return httpx.Response(403)
            return self._file(m.group(1))
        if m := re.fullmatch(r"/(?:Audio|Videos)/([^/]+)/stream", path):
            return self._file(m.group(1))
        if m := re.fullmatch(r"/Items/([^/]+)", path):
            it = self.items.get(m.group(1))
            return httpx.Response(200, json=it) if it else httpx.Response(404)
        if (m := re.fullmatch(r"/UserPlayedItems/([^/]+)", path)) and request.method == "POST":
            it = self.items.get(m.group(1))
            if it is None:
                return httpx.Response(404)
            user = it["UserData"]
            user.update(
                PlayCount=user["PlayCount"] + 1, Played=True, LastPlayedDate=q.get("datePlayed")
            )
            self.played.append((m.group(1), q.get("datePlayed", "")))
            return httpx.Response(200, json=user)
        if m := re.fullmatch(r"/UserFavoriteItems/([^/]+)", path):
            it = self.items.get(m.group(1))
            if it is None:
                return httpx.Response(404)
            it["UserData"]["IsFavorite"] = request.method == "POST"
            return httpx.Response(200, json=it["UserData"])
        if path == "/Playlists" and request.method == "POST":
            body = json.loads(request.content)
            pid = f"pl{len(self.playlists) + 1}"
            self.playlists[pid] = (body["Name"], list(body.get("Ids") or []))
            return httpx.Response(200, json={"Id": pid})
        if (m := re.fullmatch(r"/Playlists/([^/]+)", path)) and request.method == "POST":
            if m.group(1) not in self.playlists:
                return httpx.Response(404)
            name, _ = self.playlists[m.group(1)]
            self.playlists[m.group(1)] = (name, list(json.loads(request.content).get("Ids") or []))
            return httpx.Response(204)
        if path == "/Library/Refresh":
            return httpx.Response(204)
        return httpx.Response(404)

    def _file(self, item_id: str) -> httpx.Response:
        it = self.items.get(item_id)
        if it is None:
            return httpx.Response(404)
        size = it["MediaSources"][0]["Size"]
        return httpx.Response(200, content=b"\0" * size, headers={"content-type": "audio/mpeg"})

    def _query(self, q: dict[str, str]) -> dict[str, Any]:
        kind = q.get("IncludeItemTypes")
        if kind == "Playlist":
            rows = [
                {
                    "Id": pid,
                    "Name": name,
                    "Type": "Playlist",
                    "ChildCount": len(ids),
                    "Etag": str(hash(tuple(ids))),
                }
                for pid, (name, ids) in self.playlists.items()
            ]
        else:
            rows = list(self.items.values())
            if kind:
                rows = [r for r in rows if r["Type"] in kind.split(",")]
            if q.get("SortBy") == "DateCreated":
                rows = rows[::-1]
        start, limit = int(q.get("StartIndex", 0)), int(q.get("Limit", 10**9))
        return {"Items": rows[start : start + limit], "TotalRecordCount": len(rows)}
