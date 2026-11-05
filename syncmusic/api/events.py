# SPDX-License-Identifier: GPL-3.0-or-later
"""Server-sent events (`GET /api/v1/events`): every EventBus event as one `data: {type, ts, data}` message.

Each client has its own bounded queue: a client that does not read fast enough loses events (logged by the bus),
publishers never wait. A comment line every `keepalive_s` keeps proxies from closing an idle stream; the queue is
dropped when the client disconnects. The stream ends when `stop` is set: `ApiServer` sets it as soon as uvicorn
starts shutting down, so an open web UI does not hold the shutdown until its graceful timeout.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from syncmusic.infra.events import Event, EventBus

CLIENT_QUEUE = 256  # events buffered per client
KEEPALIVE_S = 15.0


async def event_stream(
    bus: EventBus,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    types: list[str] | None = None,
    keepalive_s: float = KEEPALIVE_S,
    stop: asyncio.Event | None = None,
) -> AsyncIterator[str]:
    """SSE messages for the events whose type starts with one of `types` (None: all), until the client
    disconnects or `stop` is set."""
    q = bus.subscribe(CLIENT_QUEUE)
    stop = stop or asyncio.Event()
    try:
        yield ": connected\n\n"
        while not stop.is_set() and not await is_disconnected():
            try:
                event = await _next(q, stop, keepalive_s)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is None:
                return  # the server is shutting down
            if types and not event.type.startswith(tuple(types)):
                continue
            yield f"data: {event.model_dump_json()}\n\n"
    finally:
        bus.unsubscribe(q)


async def _next(q: asyncio.Queue[Event], stop: asyncio.Event, timeout: float) -> Event | None:
    """The next event; None once `stop` is set; TimeoutError after `timeout` seconds without either."""
    get = asyncio.ensure_future(q.get())
    halt = asyncio.ensure_future(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {get, halt}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        get.cancel()
        halt.cancel()
    if get in done:
        return get.result()
    if halt in done:
        return None
    raise TimeoutError
