"""In-memory Plex Media Server API (the JSON shapes of /library, /playlists, /:/scrobble, /:/rate)."""

from __future__ import annotations

import re
from typing import Any

import httpx

from tests.media import TINY_JPEG

MACHINE_ID = "machine0001"


def track(rating_key: str, title: str, *, album: str = "Група крові", artist: str = "Скрябін",
          size: int = 1000, index: int = 1, plays: int = 0, user_rating: float | None = None,
          container: str = "mp3", codec: str | None = None, original_title: str | None = None,
          thumb: bool = True) -> dict[str, Any]:  # fmt: skip
    return {
        "ratingKey": rating_key,
        "key": f"/library/metadata/{rating_key}",
        "type": "track",
        "title": title,
        "grandparentTitle": artist,
        **({"originalTitle": original_title} if original_title else {}),
        "parentTitle": album,
        "parentRatingKey": f"al-{album}",
        "grandparentRatingKey": f"ar-{artist}",
        "index": index,
        "parentIndex": 1,
        "parentYear": 1988,
        "duration": 200_000,
        "addedAt": 1_790_000_000,
        "updatedAt": 1_790_000_100,
        **({"thumb": f"/library/metadata/{rating_key}/thumb/1"} if thumb else {}),
        "parentThumb": f"/library/metadata/al-{album}/thumb/1",
        **({"userRating": user_rating} if user_rating is not None else {}),
        "viewCount": plays,
        **({"lastViewedAt": 1_790_500_000} if plays else {}),
        "Genre": [{"tag": "Rock"}],
        "Media": [
            {
                "container": container,
                "audioCodec": codec or container,
                "bitrate": 320,
                "audioChannels": 2,
                "Part": [
                    {
                        "key": f"/library/parts/{rating_key}/1/file.{container}",
                        "size": size,
                        "file": f"/music/{artist}/{album}/{index:02d} {title}.{container}",
                    }
                ],
            }
        ],
    }


class FakePlex(httpx.AsyncBaseTransport):
    def __init__(self, token: str = "token") -> None:
        self.token = token
        self.items: dict[str, dict[str, Any]] = {}
        self.playlists: dict[str, tuple[str, list[str]]] = {}  # ratingKey -> (title, item ids)
        self.smart: set[str] = set()
        self.updated = 1_790_000_000  # the music section's updatedAt, moved by add()
        self.playlist_version: dict[str, int] = {}
        self.requests: list[str] = []
        self.scrobbled: list[str] = []
        self.headers: list[httpx.Headers] = []
        self.fail_downloads_with: int | None = None

    def add(self, item: dict[str, Any]) -> None:
        self.items[item["ratingKey"]] = item
        self.updated += 1

    def _container(self, **body: Any) -> httpx.Response:
        return httpx.Response(200, json={"MediaContainer": body})

    def _touch(self, pid: str) -> None:
        self.playlist_version[pid] = self.playlist_version.get(pid, 0) + 1

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        self.requests.append(f"{request.method} {path}")
        self.headers.append(request.headers)
        if request.headers.get("x-plex-token") != self.token:
            return httpx.Response(401)
        if path == "/":
            return self._container(friendlyName="fake", version="1.41.0")
        if path == "/identity":
            return self._container(machineIdentifier=MACHINE_ID, version="1.41.0")
        if path == "/library/sections":
            return self._container(Directory=[
                {"key": "1", "title": "Music", "type": "artist", "updatedAt": self.updated, "scannedAt": self.updated},
                {"key": "2", "title": "Movies", "type": "movie", "updatedAt": 5},
            ])  # fmt: skip
        if path == "/library/sections/1/all":
            return self._page(request, list(self.items.values()))
        if path == "/library/sections/1/refresh":
            return httpx.Response(200)
        if m := re.fullmatch(r"/library/metadata/([^/]+)", path):
            it = self.items.get(m.group(1))
            return self._container(Metadata=[it]) if it else httpx.Response(404)
        if m := re.fullmatch(r"/library/metadata/([^/]+)/thumb(/\d+)?", path):
            known = m.group(1) in self.items or m.group(1).startswith("al-")
            return self._jpeg() if known else httpx.Response(404)
        if path == "/photo/:/transcode":
            m = re.fullmatch(r"/library/metadata/([^/]+)/thumb(/\d+)?", q.get("url", ""))
            known = m is not None and (m.group(1) in self.items or m.group(1).startswith("al-"))
            return self._jpeg() if known else httpx.Response(404)
        if m := re.fullmatch(r"/library/parts/([^/]+)/1/file\.\w+", path):
            if self.fail_downloads_with:
                return httpx.Response(self.fail_downloads_with)
            it = self.items.get(m.group(1))
            if it is None or q.get("download") != "1":
                return httpx.Response(404)
            size = it["Media"][0]["Part"][0]["size"]
            return httpx.Response(200, content=b"\0" * size, headers={"content-type": "audio/mpeg"})
        if path == "/:/scrobble":
            it = self.items.get(q.get("key", ""))
            if it is None or q.get("identifier") != "com.plexapp.plugins.library":
                return httpx.Response(404)
            it["viewCount"] = int(it.get("viewCount") or 0) + 1
            self.scrobbled.append(q["key"])
            return httpx.Response(200)
        if path == "/:/rate" and request.method == "PUT":
            it = self.items.get(q.get("key", ""))
            if it is None:
                return httpx.Response(404)
            rating = float(q["rating"])
            if rating < 0:
                it.pop("userRating", None)
            else:
                it["userRating"] = rating
            return httpx.Response(200)
        if path == "/playlists" and request.method == "GET":
            return self._container(size=len(self.playlists), Metadata=[
                {"ratingKey": pid, "title": name, "playlistType": "audio", "smart": pid in self.smart,
                 "leafCount": len(ids), "updatedAt": 1_790_000_000 + self.playlist_version.get(pid, 0)}
                for pid, (name, ids) in self.playlists.items()
            ])  # fmt: skip
        if path == "/playlists" and request.method == "POST":
            ids = self._uri_ids(q.get("uri", ""))
            if ids is None or q.get("type") != "audio":
                return httpx.Response(400)
            pid = f"pl{len(self.playlists) + 1}"
            self.playlists[pid] = (q["title"], ids)
            return self._container(Metadata=[{"ratingKey": pid, "title": q["title"]}])
        if m := re.fullmatch(r"/playlists/([^/]+)/items", path):
            pid = m.group(1)
            if pid not in self.playlists:
                return httpx.Response(404)
            name, ids = self.playlists[pid]
            if request.method == "GET":
                rows = [self.items[i] for i in ids if i in self.items]
                return self._page(request, rows)
            if request.method == "DELETE":
                self.playlists[pid] = (name, [])
                self._touch(pid)
                return httpx.Response(200)
            if request.method == "PUT":
                new = self._uri_ids(q.get("uri", ""))
                if new is None:
                    return httpx.Response(400)
                self.playlists[pid] = (name, ids + new)
                self._touch(pid)
                return self._container(leafCountAdded=len(new))
        return httpx.Response(404)

    @staticmethod
    def _uri_ids(uri: str) -> list[str] | None:
        prefix = f"server://{MACHINE_ID}/com.plexapp.plugins.library/library/metadata/"
        if not uri.startswith(prefix):
            return None
        return [i for i in uri[len(prefix) :].split(",") if i]

    def _page(self, request: httpx.Request, rows: list[dict[str, Any]]) -> httpx.Response:
        start = int(request.headers.get("x-plex-container-start", 0))
        size = int(request.headers.get("x-plex-container-size", 10**9))
        page = rows[start : start + size]
        return self._container(size=len(page), totalSize=len(rows), offset=start, Metadata=page)

    @staticmethod
    def _jpeg() -> httpx.Response:
        return httpx.Response(200, content=TINY_JPEG, headers={"content-type": "image/jpeg"})
