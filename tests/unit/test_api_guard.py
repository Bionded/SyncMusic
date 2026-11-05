"""Request guard: DNS rebinding, CSRF / cross-site WebSocket, body size."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from syncmusic.api.guard import RequestGuard, host_allowed, origin_matches


def app(allowed: set[str] | None = None, max_body: int = 1000) -> TestClient:
    api = FastAPI()

    @api.get("/x")
    async def get() -> str:
        return "ok"

    @api.post("/x")
    async def post(body: dict[str, object]) -> str:
        return "ok"

    @api.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_text("hi")
        await socket.close()

    api.add_middleware(RequestGuard, allowed_hosts=allowed, max_body=max_body)
    return TestClient(api)


def test_host_names():
    allowed = {"pi5"}
    for ok in ("10.0.0.5:8765", "[::1]:8765", "localhost:8765", "PI5:8765", "127.0.0.1"):
        assert host_allowed(ok, allowed), ok
    for bad in ("evil.example:8765", "", "127.0.0.1.evil.example"):
        assert not host_allowed(bad, allowed), bad
    assert host_allowed("anything", {"*"})


def test_origin_matching():
    assert origin_matches("http://pi5:8765", "pi5:8765")
    assert origin_matches("https://pi5", "pi5")
    assert not origin_matches("http://evil.example", "pi5:8765")
    assert not origin_matches("http://pi5:9999", "pi5:8765")
    assert not origin_matches("null", "pi5:8765")


def test_dns_rebinding_is_refused():
    c = app({"pi5"})
    assert c.get("/x", headers={"host": "evil.example:8765"}).status_code == 421
    assert c.get("/x", headers={"host": "pi5:8765"}).status_code == 200
    assert c.get("/x", headers={"host": "10.0.0.2:8765"}).status_code == 200


def test_cross_site_writes_are_refused_reads_and_same_origin_pass():
    c = app()
    h = {"host": "pi5:8765"}
    assert c.post("/x", json={}, headers={**h, "origin": "http://evil.example"}).status_code == 403
    assert c.post("/x", json={}, headers={**h, "sec-fetch-site": "cross-site"}).status_code == 403
    assert c.post("/x", json={}, headers={**h, "origin": "http://pi5:8765"}).status_code == 200
    assert c.post("/x", json={}, headers=h).status_code == 200  # curl / CLI: no Origin
    assert c.get("/x", headers={**h, "origin": "http://evil.example"}).status_code == 200


def test_cross_site_websocket_is_refused():
    c = app()
    with (
        pytest.raises(WebSocketDisconnect),
        c.websocket_connect("/ws", headers={"origin": "http://evil.example"}),
    ):
        pass
    with c.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_text() == "hi"


def test_big_bodies_are_refused():
    c = app(max_body=100)
    assert c.post("/x", content=b"{" + b" " * 200 + b"}").status_code == 413

    def chunks():
        yield b"{"
        yield b" " * 200
        yield b"}"

    # a chunked body over the limit ends the request (500 instead of a parsed body)
    assert c.post("/x", content=chunks()).status_code >= 400
