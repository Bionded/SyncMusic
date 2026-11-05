"""In-memory Audiobookshelf API (shapes as returned by ABS 2.37.1)."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from tests.media import TINY_JPEG


class FakeABS(httpx.AsyncBaseTransport):
    def __init__(self, token: str = "tok") -> None:
        self.token = token
        self.libraries = [
            {"id": "lb", "name": "Books", "mediaType": "book"},
            {"id": "lp", "name": "Pods", "mediaType": "podcast"},
            {"id": "le", "name": "Ebooks", "mediaType": "ebook"},
        ]
        self.items: dict[str, dict[str, Any]] = {}  # id -> expanded item
        self.files: dict[tuple[str, str], bytes] = {}  # (item, ino) -> bytes
        self.progress: list[dict[str, Any]] = []
        self.patches: list[
            tuple[str, str | None, dict[str, Any]]
        ] = []  # PATCH /api/me/progress calls
        self.collections: list[dict[str, Any]] = []  # book library `lb`
        self.clock = 1_700_000_000_000
        self.ignore_page = False  # misbehaving server: always answers the first page

    # --- builders ----------------------------------------------------------------------------------
    def add_book(
        self,
        item_id: str,
        title: str,
        files: list[tuple[str, float]],
        author: str = "Автор",
        series: tuple[tuple[str, str | None], ...] = (),
    ) -> None:
        tracks, offset = [], 0.0
        for i, (name, duration) in enumerate(files, 1):
            ino = f"{item_id}-{i}"
            data = os.urandom(3_000 + i)
            self.files[(item_id, ino)] = data
            tracks.append(
                {
                    "index": i,
                    "ino": ino,
                    "startOffset": offset,
                    "duration": duration,
                    "codec": "mp3",
                    "bitRate": 192000,
                    "channels": 2,
                    "metaTags": {"tagTitle": name.rsplit(".", 1)[0]},
                    "metadata": {
                        "filename": name,
                        "ext": ".mp3",
                        "relPath": name,
                        "size": len(data),
                        "mtimeMs": 1,
                    },
                }
            )
            offset += duration
        self.clock += 1000
        self.items[item_id] = {
            "id": item_id,
            "libraryId": "lb",
            "mediaType": "book",
            "relPath": title,
            "addedAt": self.clock,
            "updatedAt": self.clock,
            "isMissing": False,
            "isInvalid": False,
            "media": {
                "metadata": {
                    "title": title,
                    "authorName": author,
                    "genres": ["Fantasy"],
                    "publishedYear": "2021",
                    "series": [
                        {"id": f"ser-{name}", "name": name, "sequence": seq} for name, seq in series
                    ],
                },
                "coverPath": "/meta/cover.jpg",
                "tracks": tracks,
            },
        }

    def add_collection(self, coll_id: str, name: str, book_ids: list[str]) -> None:
        self.clock += 1000
        books = [{"id": b} for b in book_ids]
        self.collections.append(
            {
                "id": coll_id,
                "name": name,
                "libraryId": "lb",
                "books": books,
                "lastUpdate": self.clock,
            }
        )

    def add_podcast(self, item_id: str, title: str, episodes: list[tuple[str, str]]) -> None:
        eps = []
        for n, (ep_id, ep_title) in enumerate(episodes, 1):
            ino = f"{item_id}-{ep_id}"
            data = os.urandom(2_000 + n)
            self.files[(item_id, ino)] = data
            eps.append(
                {
                    "id": ep_id,
                    "title": ep_title,
                    "episode": str(n),
                    "season": "1",
                    "publishedAt": 1743948000000 + n,
                    "addedAt": 1,
                    "audioFile": {
                        "ino": ino,
                        "codec": "aac",
                        "duration": 100.0,
                        "bitRate": 128000,
                        "metadata": {
                            "filename": f"{ep_title}.m4a",
                            "ext": ".m4a",
                            "relPath": f"{ep_title}.m4a",
                            "size": len(data),
                            "mtimeMs": 1,
                        },
                    },
                }
            )
        self.clock += 1000
        self.items[item_id] = {
            "id": item_id,
            "libraryId": "lp",
            "mediaType": "podcast",
            "relPath": title,
            "addedAt": self.clock,
            "updatedAt": self.clock,
            "media": {
                "metadata": {"title": title, "author": "Host", "genres": ["Podcasts"]},
                "coverPath": None,
                "episodes": eps,
            },
        }

    def set_progress(
        self, item_id: str, current: float, finished: bool = False, episode: str | None = None
    ) -> None:
        self.clock += 1000
        self.progress = [
            p for p in self.progress if (p["libraryItemId"], p["episodeId"]) != (item_id, episode)
        ]
        self.progress.append(
            {
                "libraryItemId": item_id,
                "episodeId": episode,
                "currentTime": current,
                "isFinished": finished,
                "lastUpdate": self.clock,
            }
        )

    # --- HTTP --------------------------------------------------------------------------------------
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        if path == "/ping":
            return httpx.Response(200, json={"success": True})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, text="Unauthorized")
        if path == "/api/me":
            return httpx.Response(
                200, json={"id": "u", "username": "tester", "mediaProgress": self.progress}
            )
        if m := re.fullmatch(r"/api/me/progress/([^/]+)(?:/([^/]+))?", path):
            if request.method != "PATCH":
                return httpx.Response(404)
            item = self.items.get(m.group(1))
            episode = m.group(2)
            if item is None or (
                episode is not None
                and not any(e["id"] == episode for e in item["media"].get("episodes") or [])
            ):
                return httpx.Response(404, text="Item not found")
            body = json.loads(request.content)
            key = (m.group(1), episode)
            old = next(
                (p for p in self.progress if (p["libraryItemId"], p["episodeId"]) == key), None
            )
            entry = dict(old or {"currentTime": 0.0, "isFinished": False})
            entry.update(body)
            if (
                old is not None
                and "currentTime" in body
                and body["currentTime"] != old["currentTime"]
            ):
                entry["isFinished"] = (
                    False  # like ABS 2.37: moving the position of an entry un-finishes it
                )
            self.set_progress(m.group(1), float(entry["currentTime"]), entry["isFinished"], episode)
            self.progress[-1].update({k: entry[k] for k in ("duration", "progress") if k in entry})
            self.patches.append((m.group(1), episode, body))
            return httpx.Response(200, text="OK")
        if path == "/api/libraries":
            return httpx.Response(200, json={"libraries": self.libraries})
        if m := re.fullmatch(r"/api/libraries/([^/]+)/collections", path):
            rows = [c for c in self.collections if c["libraryId"] == m.group(1)]
            return httpx.Response(200, json={"results": rows, "total": len(rows)})
        if m := re.fullmatch(r"/api/libraries/([^/]+)/items", path):
            rows = [i for i in self.items.values() if i["libraryId"] == m.group(1)]
            if q.get("sort") == "updatedAt":
                rows.sort(key=lambda i: i["updatedAt"], reverse=q.get("desc") == "1")
            limit, page = int(q.get("limit", 0)) or len(rows) or 1, int(q.get("page", 0))
            page = 0 if self.ignore_page else page
            chunk = rows[page * limit : (page + 1) * limit]
            minified = [{k: v for k, v in i.items() if k != "media"} for i in chunk]
            return httpx.Response(
                200, json={"results": minified, "total": len(rows), "limit": limit, "page": page}
            )
        if m := re.fullmatch(r"/api/items/([^/]+)", path):
            item = self.items.get(m.group(1))
            return httpx.Response(200, json=item) if item else httpx.Response(404, text="Not Found")
        if m := re.fullmatch(r"/api/items/([^/]+)/file/([^/]+)/download", path):
            data = self.files.get((m.group(1), m.group(2)))
            return httpx.Response(200, content=data) if data else httpx.Response(404)
        if m := re.fullmatch(r"/api/items/([^/]+)/cover", path):
            item = self.items.get(m.group(1))
            if item and item["media"].get("coverPath"):
                return httpx.Response(
                    200, content=TINY_JPEG, headers={"content-type": "image/jpeg"}
                )
            return httpx.Response(404)
        return httpx.Response(404)
