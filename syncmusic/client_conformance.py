# SPDX-License-Identifier: GPL-3.0-or-later
"""Conformance checks of the Wi-Fi client protocol against a running SyncMusic server.

Client authors run it to see what a server answers (and to compare their own client with it); the server's tests run
it to keep the protocol stable. It pairs a throwaway client with a fresh pairing code, so the server must allow
Wi-Fi clients. Checks that need something to download are skipped until the paired client has rules that select at
least one item; give the paired device rules in the web UI and run it again with `--folder` of the first run.

    python -m syncmusic.client_conformance http://server:8765 ABCD-1234 ./conformance-client
    python -m syncmusic.client_conformance http://server:8765 - ./conformance-client   # reuse that pairing

Exit code 0 when no check failed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from syncmusic.reference_client import API, ClientProtocolError, ReferenceClient


class Skip(Exception):
    """The check cannot run on this server state (e.g. nothing to download)."""


@dataclass
class Result:
    name: str
    status: str  # "pass" | "fail" | "skip"
    detail: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.status != "fail" for r in self.results)

    def by_name(self) -> dict[str, str]:
        return {r.name: r.status for r in self.results}


def _expect(cond: bool, what: str) -> None:
    if not cond:
        raise AssertionError(what)


def _error_code(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return ""


class Conformance:
    """The checks, in order; each one gets the paired reference client."""

    def __init__(self, client: ReferenceClient, code: str | None) -> None:
        self.c = client
        self.code = code
        self.view: dict[str, Any] | None = None
        self.fetch: list[dict[str, Any]] = []

    def url(self, path: str) -> str:
        return str(self.c.state["server"]).rstrip("/") + f"/{API}/{path}"

    async def hello(self) -> None:
        r = await self.c.http.get(self.url("hello"))
        _expect(r.status_code == 200, f"hello answered {r.status_code}")
        body = r.json()
        _expect(body["protocol"]["min"] <= 1 <= body["protocol"]["max"], "protocol 1 not offered")
        _expect(bool(body["instance"]["id"]), "no instance id")

    async def pair_rejects_bad_code(self) -> None:
        r = await self.c.http.post(
            self.url("pair"),
            json={
                "code": "0000-0000",
                "client": {"uid": str(uuid.uuid4()), "name": "Conformance (bad code)"},
            },
        )
        _expect(400 <= r.status_code < 500, f"a wrong code answered {r.status_code}")
        _expect(bool(_error_code(r)), "the error has no code")

    async def pair(self) -> None:
        if self.code is None:
            _expect("token" in self.c.state, "no pairing code and no saved pairing")
            return
        device_id = await self.c.pair(self.c.state["server"], self.code, "Conformance client")
        _expect(bool(device_id) and bool(self.c.state.get("token")), "no device id or token")

    async def auth_required(self) -> None:
        r = await self.c.http.get(self.url("me"))
        _expect(r.status_code == 401, f"no token answered {r.status_code}")
        _expect(_error_code(r) == "auth_required", f"code {_error_code(r)!r}")
        r = await self.c.http.get(self.url("me"), headers={"Authorization": "Bearer nope"})
        _expect(r.status_code == 401, f"a wrong token answered {r.status_code}")

    async def me(self) -> None:
        _, body = await self.c._call("GET", "me")
        _expect(body["device_id"] == self.c.state["device_id"], "me is another device")

    async def sync_plan(self) -> None:
        try:
            self.view = await self.c.start(poll_s=1)
        except ClientProtocolError as e:
            if e.code != "failed":
                raise
            raise Skip(f"the server cannot plan for this client yet: {e}") from e
        plan = self.view["plan"]
        _expect(isinstance(plan["fetch"], list) and isinstance(plan["delete"], list), "plan shape")
        self.fetch = await self.c._section(self.view, "fetch")
        for item in self.fetch:
            _expect({"path", "fid", "size", "key"} <= item.keys(), f"fetch item lacks keys: {item}")

    async def plan_paging(self) -> None:
        if len(self.fetch) < 2:
            raise Skip("needs two items to fetch")
        assert self.view is not None
        sid = self.view["session_id"]
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(len(self.fetch) + 1):
            params: dict[str, Any] = {"section": "fetch", "limit": 1}
            if cursor:
                params["cursor"] = cursor
            _, page = await self.c._call("GET", f"sync/{sid}/plan", params=params)
            _expect(len(page["items"]) <= 1, "limit ignored")
            seen += [i["fid"] for i in page["items"]]
            cursor = page["next"]
            if not cursor:
                break
        _expect(seen == [i["fid"] for i in self.fetch], "pages differ from the plan")

    def _first(self) -> dict[str, Any]:
        if not self.fetch:
            raise Skip("nothing to fetch: give the paired client rules that select an item")
        return min(self.fetch, key=lambda i: i["size"])

    async def file_download(self) -> None:
        item = self._first()
        r = await self.c.http.get(self.url(f"files/{item['fid']}"), headers=self.c._headers())
        _expect(r.status_code == 200, f"download answered {r.status_code}")
        _expect(len(r.content) == item["size"], "size differs from the plan")
        _expect(hashlib.sha256(r.content).hexdigest() == item["fid"], "fid is not the SHA-256")

    async def file_range(self) -> None:
        item = self._first()
        if item["size"] < 2:
            raise Skip("file too small for a range")
        half = item["size"] // 2
        headers = self.c._headers() | {"Range": f"bytes={half}-"}
        r = await self.c.http.get(self.url(f"files/{item['fid']}"), headers=headers)
        _expect(r.status_code == 206, f"a range answered {r.status_code}")
        _expect(len(r.content) == item["size"] - half, "range length")

    async def file_unknown(self) -> None:
        r = await self.c.http.get(self.url(f"files/{'0' * 64}"), headers=self.c._headers())
        _expect(r.status_code == 404, f"an unknown file answered {r.status_code}")
        _expect(_error_code(r) == "not_found", f"code {_error_code(r)!r}")

    async def ack_and_finish(self) -> None:
        if self.view is None:
            raise Skip("no session")
        sid = self.view["session_id"]
        failed = 0
        for item in self.fetch:
            if await self.c.download(item):
                await self.c._call(
                    "POST",
                    f"sync/{sid}/ack",
                    json={"done": [{"path": item["path"], "fid": item["fid"]}]},
                )
            else:
                failed += 1
        _expect(failed == 0, f"{failed} downloads failed verification")
        await self.c._call("POST", f"sync/{sid}/finish", json={"status": "complete"})
        self.view = None
        again = await self.c.start(poll_s=1)
        rest = await self.c._section(again, "fetch")
        await self.c._call(
            "POST", f"sync/{again['session_id']}/finish", json={"status": "complete"}
        )
        _expect(not rest, f"{len(rest)} acked items planned again")

    async def events_idempotent(self) -> None:
        key = self.fetch[0]["key"] if self.fetch else "unknown-item"
        ev = self.c.record("play", key, completed=True)
        _, first = await self.c._call("POST", "events", json={"events": [ev]})
        _, second = await self.c._call("POST", "events", json={"events": [ev]})
        _expect(first["acked_seq"] >= ev["seq"], "event not acked")
        _expect(second["acked_seq"] >= ev["seq"], "resent event not acked")
        _expect(second.get("accepted", 0) == 0, "a resent event was accepted twice")
        self.c.state["events"] = []
        self.c.save()

    async def token_rotation(self) -> None:
        old = self.c.state["token"]
        _, body = await self.c._call("POST", "me/token/rotate")
        new = body["token"]
        _expect(bool(new) and new != old, "no new token")
        # the old token stays valid for a grace period, until the new one is used
        r = await self.c.http.get(self.url("me"), headers={"Authorization": f"Bearer {old}"})
        _expect(
            r.status_code == 200,
            f"the old token stopped before the new one was used ({r.status_code})",
        )
        self.c.state["token"] = new
        self.c.save()
        await self.me()
        r = await self.c.http.get(self.url("me"), headers={"Authorization": f"Bearer {old}"})
        _expect(
            r.status_code == 401,
            f"the old token works after the new one was used ({r.status_code})",
        )

    def checks(self) -> list[tuple[str, Callable[[], Awaitable[None]]]]:
        return [
            ("hello", self.hello),
            ("pair_rejects_bad_code", self.pair_rejects_bad_code),
            ("pair", self.pair),
            ("auth_required", self.auth_required),
            ("me", self.me),
            ("sync_plan", self.sync_plan),
            ("plan_paging", self.plan_paging),
            ("file_download", self.file_download),
            ("file_range", self.file_range),
            ("file_unknown", self.file_unknown),
            ("ack_and_finish", self.ack_and_finish),
            ("events_idempotent", self.events_idempotent),
            ("token_rotation", self.token_rotation),
        ]


async def run(client: ReferenceClient, code: str | None) -> Report:
    """Run every check in order; a failed `pair` stops the run (nothing after it can work)."""
    conf = Conformance(client, code)
    report = Report()
    for name, check in conf.checks():
        try:
            await check()
            report.results.append(Result(name, "pass"))
        except Skip as e:
            report.results.append(Result(name, "skip", str(e)))
        except (AssertionError, ClientProtocolError, httpx.HTTPError, KeyError, TimeoutError) as e:
            report.results.append(Result(name, "fail", f"{type(e).__name__}: {e}"))
            if name == "pair":
                break
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="syncmusic-client-conformance", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument("server")
    ap.add_argument(
        "code", help="a pairing code from the server's UI, or - to reuse --folder's pairing"
    )
    ap.add_argument(
        "folder", type=Path, help="where the throwaway client keeps its state and files"
    )
    ap.add_argument("--json", action="store_true", help="print the results as JSON")
    a = ap.parse_args(argv)

    async def go() -> Report:
        c = ReferenceClient(a.folder)
        c.state["server"] = a.server
        try:
            return await run(c, None if a.code == "-" else a.code)
        finally:
            await c.close()

    report = asyncio.run(go())
    if a.json:
        print(json.dumps([r.__dict__ for r in report.results]))
    else:
        for r in report.results:
            print(f"{r.status.upper():5} {r.name}" + (f"  ({r.detail})" if r.detail else ""))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
