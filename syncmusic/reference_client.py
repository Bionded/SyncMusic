# SPDX-License-Identifier: GPL-3.0-or-later
"""Reference Wi-Fi client: pairs with a SyncMusic server, syncs into a folder and sends listening events.

It shows the whole client protocol in one small file that only needs `httpx` and the standard library, so client
authors can read (or copy) it, and the server's tests drive it end to end. It is not a player: what it "plays" are
events you record with `play`/`rate`/`favourite`/`position`.

    python -m syncmusic.reference_client pair http://server:8765 ABCD-1234 ./phone
    python -m syncmusic.reference_client sync ./phone
    python -m syncmusic.reference_client play ./phone <key> --completed
    python -m syncmusic.reference_client events ./phone

State (server, token, inventory, the item key of each synced file, unsent events) lives in `<folder>/.syncmusic-client.json`; a download in progress
is `<file>.part` and resumes with an HTTP Range request on the next sync.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import platform
import uuid
from pathlib import Path
from typing import Any

import httpx

API = "api/client/v1"
STATE = ".syncmusic-client.json"
PART = ".part"
CHUNK = 1 << 20


class ClientProtocolError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        err = body.get("error", {}) if isinstance(body, dict) else {}
        self.status = status
        self.code = err.get("code", "")
        super().__init__(f"{status} {self.code}: {err.get('message', body)}")


class ReferenceClient:
    """One client install syncing into `folder`. `http`: an httpx.AsyncClient to use (tests); else one is made."""

    def __init__(self, folder: Path, http: httpx.AsyncClient | None = None) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state: dict[str, Any] = self._load()
        self._http = http
        self._own_http = http is None
        self.formats: list[str] = ["mp3", "aac", "flac", "ogg", "opus"]

    # --- state --------------------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads((self.folder / STATE).read_text("utf-8"))
        except FileNotFoundError:
            return {"uid": str(uuid.uuid4()), "seq": 0, "events": [], "inventory": {}, "keys": {}}

    def save(self) -> None:
        tmp = self.folder / (STATE + ".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), "utf-8")
        os.replace(tmp, self.folder / STATE)

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60)
        return self._http

    async def close(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()

    def _url(self, path: str) -> str:
        return self.state["server"].rstrip("/") + f"/{API}/{path}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.state['token']}"}

    async def _call(self, method: str, path: str, **kw: Any) -> tuple[int, Any]:
        r = await self.http.request(method, self._url(path), headers=self._headers(), **kw)
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            raise ClientProtocolError(r.status_code, body)
        return r.status_code, body

    def capabilities(self) -> dict[str, Any]:
        st = os.statvfs(self.folder)
        return {
            "formats": self.formats,
            "media_kinds": ["music", "podcast", "audiobook"],
            "storage": {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize},
            "features": {"playlists": True, "play_events": True},
            "layout": {"stores_paths": True},
        }

    # --- pairing ------------------------------------------------------------------------------------

    async def pair(self, server: str, code: str, name: str = "") -> str:
        """Pair with a code shown in the server's UI; returns the device id."""
        self.state["server"] = server
        info = {
            "uid": self.state["uid"],
            "name": name or platform.node()[:64] or "Reference client",
            "os": platform.system()[:32],
            "app": "syncmusic-reference-client",
            "app_version": "1",
        }
        r = await self.http.post(
            self._url("pair"),
            json={"code": code, "client": info, "capabilities": self.capabilities()},
        )
        body = r.json()
        if r.status_code != 201:
            raise ClientProtocolError(r.status_code, body)
        self.state |= {"token": body["token"], "device_id": body["device_id"]}
        self.save()
        return str(body["device_id"])

    # --- sync ---------------------------------------------------------------------------------------

    def _files(self) -> dict[str, int]:
        out = {}
        for p in self.folder.rglob("*"):
            rel = p.relative_to(self.folder).as_posix()
            if p.is_file() and not rel.startswith(STATE) and not rel.endswith(PART):
                out[rel] = p.stat().st_size
        return out

    async def start(self, *, poll_s: float = 2, tries: int = 300) -> dict[str, Any]:
        """Report what the folder holds and wait for the plan (planning / waiting for the user on the server)."""
        inv = [{"path": rel, "size": size} for rel, size in sorted(self._files().items())]
        body = {"capabilities": self.capabilities(), "inventory": {"kind": "full", "items": inv}}
        _, view = await self._call("POST", "sync", json=body)
        for _ in range(tries):
            if view["status"] == "ready":
                return view
            if view["status"] in ("failed", "finished"):
                raise ClientProtocolError(
                    409, {"error": {"code": view["status"], "message": view.get("message", "")}}
                )
            await asyncio.sleep(min(poll_s, view.get("retry_after_s", poll_s)))
            _, view = await self._call("GET", f"sync/{view['session_id']}")
        raise TimeoutError(f"no plan: {view['status']} {view.get('message', '')}")

    async def _section(self, view: dict[str, Any], section: str) -> list[dict[str, Any]]:
        plan = view["plan"]
        items = list(plan[section])
        cursor = plan["next"].get(section)
        while cursor:
            _, page = await self._call(
                "GET",
                f"sync/{view['session_id']}/plan",
                params={"section": section, "cursor": cursor},
            )
            items += page["items"]
            cursor = page["next"]
        return items

    def _target(self, rel: str) -> Path:
        p = (self.folder / rel).resolve()
        if not p.is_relative_to(self.folder.resolve()) or p == self.folder.resolve():
            raise ValueError(f"unsafe path from the server: {rel!r}")
        return p

    async def download(self, item: dict[str, Any], *, stop_after: int | None = None) -> bool:
        """Fetch one file into `<path>.part` (resuming what is there), verify size + SHA-256, move it in place.
        `stop_after`: give up after that many bytes (tests: an interrupted download). True when complete."""
        dest = self._target(item["path"])
        part = dest.with_name(dest.name + PART)
        dest.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        headers = self._headers()
        if have:
            headers |= {"Range": f"bytes={have}-", "If-Range": f'"{item["fid"]}"'}
        async with self.http.stream("GET", self._url(f"files/{item['fid']}"), headers=headers) as r:
            if r.status_code >= 400:
                await r.aread()
                raise ClientProtocolError(r.status_code, r.json() if r.content else {})
            mode = "ab" if r.status_code == 206 else "wb"  # 200: the file changed, start over
            written = 0
            with open(part, mode) as f:  # noqa: ASYNC230 - kept plain for readers
                async for chunk in r.aiter_bytes(CHUNK):
                    if stop_after is not None:
                        chunk = chunk[: stop_after - written]
                    f.write(chunk)
                    written += len(chunk)
                    if stop_after is not None and written >= stop_after:
                        return False
        h = hashlib.sha256()
        with open(part, "rb") as f:  # noqa: ASYNC230
            while chunk := f.read(CHUNK):
                h.update(chunk)
        if part.stat().st_size != item["size"] or h.hexdigest() != item["fid"]:
            part.unlink()
            return False
        os.replace(part, dest)
        return True

    async def sync(self) -> dict[str, int]:
        """One full sync: plan, deletes, downloads (each acked), finish. Returns counts."""
        await self.send_events()
        view = await self.start()
        sid = view["session_id"]
        deletes = await self._section(view, "delete")
        fetch = await self._section(view, "fetch")
        keys: dict[str, str] = self.state.setdefault("keys", {})
        gone = []
        for d in deletes:
            self._target(d["path"]).unlink(missing_ok=True)
            gone.append(d["path"])
            keys.pop(d["path"], None)
        if gone:
            await self._call("POST", f"sync/{sid}/ack", json={"deleted": gone})
        got = failed = 0
        for item in fetch:
            if await self.download(item):
                await self._call(
                    "POST",
                    f"sync/{sid}/ack",
                    json={"done": [{"path": item["path"], "fid": item["fid"]}]},
                )
                if item.get("key"):
                    keys[item["path"]] = item["key"]  # what events about this file refer to
                got += 1
            else:
                failed += 1
        status = "complete" if not failed else "partial"
        await self._call("POST", f"sync/{sid}/finish", json={"status": status})
        self.state["inventory"] = self._files()
        self.save()
        return {"fetched": got, "deleted": len(gone), "failed": failed}

    # --- events -------------------------------------------------------------------------------------

    def record(self, type_: str, key: str, **fields: Any) -> dict[str, Any]:
        """Remember something the user did; sent with the next `send_events` (also offline)."""
        self.state["seq"] += 1
        ev = {
            "eid": str(uuid.uuid4()),
            "seq": self.state["seq"],
            "type": type_,
            "key": key,
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "clock": "ntp",
            **fields,
        }
        self.state["events"].append(ev)
        self.save()
        return ev

    async def send_events(self, batch: int = 1000) -> int:
        """Upload unsent events; drops the ones the server acked. Returns how many it sent."""
        sent = 0
        while self.state["events"]:
            chunk = self.state["events"][:batch]
            _, body = await self._call("POST", "events", json={"events": chunk})
            acked = body["acked_seq"]
            self.state["events"] = [e for e in self.state["events"] if e["seq"] > acked]
            self.save()
            sent += len(chunk)
        return sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="syncmusic-reference-client", description=__doc__.split("\n\n")[0]
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair", help="pair with a code from the server's UI")
    p.add_argument("server")
    p.add_argument("code")
    p.add_argument("folder", type=Path)
    p.add_argument("--name", default="")
    sub.add_parser("sync", help="sync the folder").add_argument("folder", type=Path)
    sub.add_parser("events", help="send unsent events").add_argument("folder", type=Path)
    for name in ("play", "rate", "favourite", "position"):
        q = sub.add_parser(name, help=f"record a {name} event")
        q.add_argument("folder", type=Path)
        q.add_argument("key", help="the item key from the plan")
        if name == "play":
            q.add_argument("--completed", action="store_true")
            q.add_argument("--played-s", type=float)
        elif name == "rate":
            q.add_argument("value", type=int, choices=range(6))
        elif name == "favourite":
            q.add_argument("--off", action="store_true")
        else:
            q.add_argument("position_s", type=float)
            q.add_argument("--finished", action="store_true")
    a = ap.parse_args(argv)

    async def run() -> Any:
        c = ReferenceClient(a.folder)
        try:
            if a.cmd == "pair":
                return {"device_id": await c.pair(a.server, a.code, a.name)}
            if a.cmd == "sync":
                return await c.sync()
            if a.cmd == "events":
                return {"sent": await c.send_events()}
            if a.cmd == "play":
                return c.record("play", a.key, completed=a.completed, played_s=a.played_s)
            if a.cmd == "rate":
                return c.record("rating", a.key, value=a.value)
            if a.cmd == "favourite":
                return c.record("favourite", a.key, on=not a.off)
            return c.record("position", a.key, position_s=a.position_s, finished=a.finished)
        finally:
            await c.close()

    try:
        print(json.dumps(asyncio.run(run())))
    except ClientProtocolError as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
