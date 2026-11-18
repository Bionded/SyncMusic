# SPDX-License-Identifier: GPL-3.0-or-later
"""A slow event subscriber loses progress events, never the others, and is warned about once a minute."""

from __future__ import annotations

import logging

import pytest

from syncmusic.infra.events import EventBus


async def test_progress_is_coalesced_and_other_events_survive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()
    q = bus.subscribe(queue_size=5)
    bus.publish("device.connected", device_id="a")
    with caplog.at_level(logging.WARNING):
        for i in range(500):
            bus.publish("job.progress", n=i)
        bus.publish("job.finished", job="j")
    got = [q.get_nowait() for _ in range(q.qsize())]
    assert [e.type for e in got] == ["device.connected", "job.progress", "job.finished"]
    assert got[1].data["n"] == 499  # the latest progress is kept
    warnings = [r for r in caplog.records if "slow event subscriber" in r.message]
    assert len(warnings) == 1


async def test_fast_subscriber_gets_everything() -> None:
    bus = EventBus()
    q = bus.subscribe(queue_size=10)
    for i in range(10):
        bus.publish("job.progress", n=i)
    assert q.qsize() == 10


async def test_newest_progress_of_each_job_is_kept() -> None:
    bus = EventBus()
    q = bus.subscribe(queue_size=4)
    bus.publish("job.progress", job_id="one", n=0)
    bus.publish("job.progress", job_id="two", n=0)
    for i in range(1, 5):
        bus.publish("job.progress", job_id="one", n=i)
    got = [q.get_nowait() for _ in range(q.qsize())]
    assert [(e.data["job_id"], e.data["n"]) for e in got] == [("two", 0), ("one", 3), ("one", 4)]


async def test_queue_full_of_other_events_keeps_the_newest() -> None:
    bus = EventBus()
    q = bus.subscribe(queue_size=3)
    bus.publish("job.progress", job_id="one", n=0)
    for i in range(5):
        bus.publish("device.connected", n=i)
    got = [q.get_nowait() for _ in range(q.qsize())]
    assert [e.data["n"] for e in got] == [2, 3, 4]
