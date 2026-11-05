"""In-memory Navidrome (Subsonic + the bits of the native API we use) as an httpx transport.

Shapes follow what Navidrome 0.64.2 returns (probed 2026-10-17).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from tests.media import TINY_JPEG


def song(sid: str, title: str, **kw: Any) -> dict[str, Any]:
    data = os.urandom(kw.pop("size", 4_000))
    base = {
        "id": sid,
        "title": title,
        "album": "Альбом",
        "artist": "Скрябін",
        "displayArtist": "Скрябін",
        "displayAlbumArtist": "Скрябін",
        "albumId": "al1",
        "artistId": "ar1",
        "track": 1,
        "discNumber": 1,
        "year": 1988,
        "genre": "Rock",
        "genres": [{"name": "Rock"}],
        "coverArt": f"mf-{sid}",
        "size": len(data),
        "suffix": "mp3",
        "contentType": "audio/mpeg",
        "duration": 200,
        "bitRate": 320,
        "path": f"Скрябін/Альбом/{title}.mp3",
        "created": "2026-01-01T10:00:00Z",
        "type": "music",
        "samplingRate": 44100,
        "channelCount": 2,
        "bitDepth": 0,
    }
    base.update(kw)
    base["_data"] = data
    return base


class FakeNavidrome(httpx.AsyncBaseTransport):
    def __init__(
        self,
        user: str = "admin",
        password: str = "pw",
        native: bool = True,
        server_type: str = "navidrome",
    ) -> None:
        self.user, self.password, self.native = user, password, native
        self.server_type = (
            server_type  # what `ping` & co. say; another Subsonic server: no native API
        )
        self.songs: dict[str, dict[str, Any]] = {}
        self.album_images: dict[str, str] = {}  # album id -> imageHash (native /api/album)
        self.playlists: dict[str, dict[str, Any]] = {}  # id -> {name, entries, rules, changed}
        self.lyrics: dict[str, list[dict[str, Any]]] = {}
        self.last_scan = "2026-01-01T10:00:00Z"
        self.calls: list[str] = []
        self.logins = 0
        self.refuse_logins = 0  # answer this many logins with 429 (Navidrome's login rate limit)
        self.scrobbles: list[tuple[str, int]] = []  # (song id, time ms) of every submitted scrobble
        self.ignore_offset = False  # misbehaving server: always answers the first page
        # real Navidrome re-evaluates a smart playlist (one with "rules") when it is read and bumps its `changed`
        # (seen on a server,/); opt-in so older tests keep their setup
        self.smart_reads_bump_changed = False
        self._reads = 0

    def add(self, s: dict[str, Any]) -> None:
        self.songs[s["id"]] = s
        self.last_scan = f"2026-01-01T10:{len(self.songs):02d}:00Z"

    # --- helpers ---------------------------------------------------------------------------------
    def _ok(self, **payload: Any) -> httpx.Response:
        body = {
            "status": "ok",
            "version": "1.16.1",
            "type": self.server_type,
            "serverVersion": "0.64.2 (fake)",
            "openSubsonic": True,
            **payload,
        }
        return httpx.Response(200, json={"subsonic-response": body})

    def _fail(self, code: int, msg: str, fmt: str | None = "json") -> httpx.Response:
        # Like real Navidrome (0.64.2): errors are HTTP 200; the body is XML unless `f=json` was sent.
        if fmt != "json":
            xml = (
                '<subsonic-response xmlns="http://subsonic.org/restapi" status="failed" '
                f'version="1.16.1"><error code="{code}" message="{msg}"></error></subsonic-response>'
            )
            return httpx.Response(
                200, content=xml.encode(), headers={"content-type": "application/xml"}
            )
        body = {"status": "failed", "version": "1.16.1", "error": {"code": code, "message": msg}}
        return httpx.Response(200, json={"subsonic-response": body})

    @staticmethod
    def _public(s: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in s.items() if not k.startswith("_")}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        q = dict(request.url.params)
        self.calls.append(path.rsplit("/", 1)[-1])
        if path.startswith("/rest/"):
            if q.get("u") != self.user or hashlib.md5(
                (self.password + q.get("s", "")).encode()
            ).hexdigest() != q.get("t"):
                return self._fail(40, "Wrong username or password", q.get("f"))
            return self._rest(path[len("/rest/") :], q)
        if not self.native:
            return httpx.Response(404)
        if path == "/auth/login":
            self.logins += 1
            if self.refuse_logins:
                self.refuse_logins -= 1
                return httpx.Response(429, text="Too Many Requests")
            creds = json.loads(request.content)
            if (creds.get("username"), creds.get("password")) != (self.user, self.password):
                return httpx.Response(401, json={"error": "Invalid username or password"})
            return httpx.Response(200, json={"token": "jwt"})
        if request.headers.get("x-nd-authorization") != "Bearer jwt":
            return httpx.Response(401)
        if path == "/api/song":
            start = 0 if self.ignore_offset else int(q["_start"])
            rows = sorted(self.songs.values(), key=lambda s: s["id"])[
                start : start + int(q["_end"]) - int(q["_start"])
            ]
            return httpx.Response(
                200,
                json=[
                    {
                        "id": s["id"],
                        "path": s.get("_real_path", s["path"]),
                        "lyrics": json.dumps(self.lyrics[s["id"]])
                        if s["id"] in self.lyrics
                        else "[]",
                        "hasCoverArt": s.get("_has_cover", True),
                        "codec": s.get("_codec", "MP3"),
                        "size": s["size"],
                        "albumId": s.get("albumId"),
                        "updatedAt": "2026-01-02T00:00:00Z",
                    }
                    for s in rows
                ],
            )
        if path == "/api/album":
            ids = sorted({s["albumId"] for s in self.songs.values() if s.get("albumId")})
            start = 0 if self.ignore_offset else int(q["_start"])
            rows = ids[start : start + int(q["_end"]) - int(q["_start"])]
            return httpx.Response(
                200,
                json=[{"id": a, "imageHash": self.album_images.get(a, "h0")} for a in rows],
            )
        if path == "/api/playlist":
            return httpx.Response(
                200,
                json=[
                    {"id": k, "name": p["name"], "rules": p.get("rules")}
                    for k, p in self.playlists.items()
                ],
            )
        return httpx.Response(404)

    def _rest(self, ep: str, q: dict[str, str]) -> httpx.Response:
        if ep == "ping":
            return self._ok()
        if ep == "getScanStatus":
            return self._ok(
                scanStatus={"scanning": False, "count": len(self.songs), "lastScan": self.last_scan}
            )
        if ep == "startScan":
            return self._ok(scanStatus={"scanning": True, "count": len(self.songs)})
        if ep == "getIndexes":
            return self._ok(indexes={"lastModified": 1, "index": []})
        if ep == "search3":
            assert q.get("query") == ""
            off, n = int(q.get("songOffset", 0)), int(q.get("songCount", 20))
            off = 0 if self.ignore_offset else off
            page = [self._public(s) for s in list(self.songs.values())[off : off + n]]
            return self._ok(searchResult3={"song": page} if page else {})
        if ep == "getPlaylists":
            return self._ok(
                playlists={
                    "playlist": [
                        {
                            "id": k,
                            "name": p["name"],
                            "songCount": len(p["entries"]),
                            "changed": p.get("changed", "x"),
                        }
                        for k, p in self.playlists.items()
                    ]
                }
                if self.playlists
                else {}
            )
        if ep == "getPlaylist":
            p = self.playlists.get(q["id"])
            if p is None:
                return self._fail(70, "not found")
            if self.smart_reads_bump_changed and p.get("rules") is not None:
                self._reads += 1
                p["changed"] = f"2026-01-02T00:00:{self._reads:02d}.000000000Z"
            return self._ok(
                playlist={
                    "id": q["id"],
                    "name": p["name"],
                    "entry": [self._public(self.songs[i]) for i in p["entries"]],
                }
            )
        if ep == "download":
            s = self.songs.get(q["id"])
            if s is None:
                return self._fail(70, "not found", q.get("f"))
            return httpx.Response(
                200, content=s["_data"], headers={"content-type": s["contentType"]}
            )
        if ep == "getCoverArt":
            if q["id"].removeprefix("mf-") in self.songs:
                return httpx.Response(
                    200, content=TINY_JPEG, headers={"content-type": "image/jpeg"}
                )
            return httpx.Response(404)
        if ep == "getSong":
            s = self.songs.get(q.get("id", ""))
            if s is None:
                return self._fail(70, "Song not found", q.get("f"))
            return self._ok(song=self._public(s))
        if ep == "scrobble":
            s = self.songs.get(q.get("id", ""))
            if s is None:
                return self._ok()  # like Navidrome 0.64.2: an unknown id is accepted and dropped
            if q.get("submission", "true") == "true":
                s["playCount"] = int(s.get("playCount") or 0) + 1
                ms = int(q.get("time") or 0)
                s["played"] = (
                    datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")
                )
                self.scrobbles.append((q["id"], ms))
            return self._ok()
        if ep in ("star", "unstar"):
            s = self.songs.get(q.get("id", ""))
            if s is None:
                return self._fail(70, "data not found", q.get("f"))
            if ep == "star":
                s["starred"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            else:
                s.pop("starred", None)
            return self._ok()
        if ep == "setRating":
            s = self.songs.get(q.get("id", ""))
            if s is None:
                return self._fail(70, "data not found", q.get("f"))
            rating = int(q["rating"])
            if not 0 <= rating <= 5:
                return self._fail(0, "Invalid rating", q.get("f"))
            if rating:
                s["userRating"] = rating
            else:
                s.pop("userRating", None)
            return self._ok()
        if ep == "getLyricsBySongId":
            return self._ok(lyricsList={"structuredLyrics": self.lyrics.get(q["id"], [])})
        return self._fail(0, f"unknown endpoint {ep}")
