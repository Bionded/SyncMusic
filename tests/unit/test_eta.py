"""Job ETA: from recent progress samples - steady, bursty, stalled, restarted, finished."""

from __future__ import annotations

import pytest

from syncmusic.infra.jobs import Eta


def test_needs_time_and_progress_before_an_estimate():
    e = Eta()
    assert e.add(0, 0.0) is None
    assert e.add(5, 0.10) is None  # under MIN_S
    assert e.add(10, 0.10 + 0.0) is not None  # 10 s, 10 % done
    e2 = Eta()
    e2.add(0, 0.0)
    assert e2.add(30, 0.01) is None  # under MIN_PROGRESS


def test_a_large_sync_gets_an_estimate_after_ten_files_and_thirty_seconds():
    """12.7k files need ~250 files for 2 %; ten finished files over 30 s are enough."""
    e = Eta()
    e.add(0, 0.0)
    eta = None
    for i in range(1, 10):  # nine files in 27 s: not yet
        eta = e.add(3 * i, i / 12_700)
    assert eta is None
    assert e.add(28, 10 / 12_700) is None  # ten files, under 30 s
    eta = e.add(31, 11 / 12_700)
    assert eta == pytest.approx((12_700 - 11) * 31 / 11, rel=0.05)  # ~2.8 s per file


def test_steady_rate():
    e = Eta()
    for t in range(0, 61, 5):
        eta = e.add(t, t / 100)  # 1 % per second
    assert eta == pytest.approx(40, abs=1)  # 60 % done, 40 s left


def test_bursty_progress_is_smoothed_and_follows_the_recent_rate():
    e = Eta()
    e.add(0, 0.0)
    e.add(10, 0.2)  # fast start: 2 %/s → 40 s left
    first = e.add(20, 0.4)
    assert first == pytest.approx(30, abs=5)
    # then it slows to 0.2 %/s for 4 minutes: the window forgets the fast start
    f = 0.4
    for t in range(30, 300, 10):
        f += 0.02
        eta = e.add(t, f)
    assert f < 1 and eta == pytest.approx((1 - f) / 0.002, rel=0.25)


def test_no_progress_counts_down_without_new_samples_and_never_goes_negative():
    e = Eta()
    e.add(0, 0.0)
    eta = e.add(20, 0.5)
    assert eta == pytest.approx(20, abs=1)
    assert e.at(25) == pytest.approx(15, abs=1)
    assert e.at(100) == 0.0


def test_going_back_starts_over_and_done_is_zero():
    e = Eta()
    e.add(0, 0.0)
    assert e.add(20, 0.5) is not None
    assert e.add(21, 0.1) is None  # a new phase
    assert e.add(30, 1.0) == 0.0


async def test_job_progress_events_carry_the_eta(monkeypatch):
    import asyncio

    from syncmusic.infra import jobs as jobs_mod
    from syncmusic.infra.events import EventBus

    clock = [1000.0]
    monkeypatch.setattr(jobs_mod.time, "monotonic", lambda: clock[0])
    bus = EventBus()
    q = bus.subscribe()
    runner = jobs_mod.JobRunner(bus)

    async def job(ctx):
        for i in range(1, 6):
            clock[0] += 10
            ctx.progress(i / 10, f"step {i}")
        return None

    info = runner.submit("sync", "sync x", job, device_id="d")
    await runner.wait(info.id)
    etas = []
    while not q.empty():
        ev = q.get_nowait()
        if ev.type == "job.progress":
            etas.append(ev.data["eta_s"])
    assert etas[0] is None and etas[-1] == pytest.approx(50, abs=2)  # 50 % in 50 s
    assert runner.get(info.id).eta_s is None  # finished
    await asyncio.sleep(0)


def test_slow_long_sync_still_gets_an_estimate():
    """Regression: 0.01 % per second moves less than MIN_PROGRESS per window, the estimate must still come."""
    e = Eta()
    eta = None
    for t in range(0, 1200, 5):
        eta = e.add(t, t * 0.0001)
    assert eta == pytest.approx((1 - 1195 * 0.0001) / 0.0001, rel=0.05)
