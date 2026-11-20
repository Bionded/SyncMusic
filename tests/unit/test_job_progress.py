"""JobContext.progress: what goes to the job log."""

from concurrent.futures import ThreadPoolExecutor

from syncmusic.infra.events import EventBus
from syncmusic.infra.jobs import JobContext, JobInfo


def test_log_false_updates_the_status_but_not_the_log():
    info = JobInfo(id="j", kind="sync", name="Sync")
    with ThreadPoolExecutor(1) as pool:
        ctx = JobContext(info, EventBus(), pool)
        ctx.progress(0.1, "Combining “Book” - 0.1 of 1.5 h")
        for i in range(2, 20):
            ctx.progress(i / 100, f"Combining “Book” - 0.{i} of 1.5 h", log=False)
    assert len(info.log) == 1
    assert info.message.endswith("0.19 of 1.5 h") and info.progress == 0.19


async def test_slot_waiters_are_woken_by_a_kept_task():
    import asyncio

    from syncmusic.infra.events import EventBus
    from syncmusic.infra.jobs import JobRunner

    runner = JobRunner(EventBus(), max_workers=1)
    runner._slots_changed = asyncio.Condition()
    woke = asyncio.Event()

    async def waiter():
        async with runner._slots_changed:
            await runner._slots_changed.wait()
        woke.set()

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    runner.set_device_job_limit(2)
    assert len(runner._wakers) == 1
    await asyncio.wait_for(woke.wait(), 2)
    await t
    assert not runner._wakers


def test_changing_the_job_limit_outside_the_loop_does_not_raise():
    import asyncio

    from syncmusic.infra.events import EventBus
    from syncmusic.infra.jobs import JobRunner

    runner = JobRunner(EventBus(), max_workers=1)
    runner._slots_changed = asyncio.Condition()
    runner.set_device_job_limit(1)
