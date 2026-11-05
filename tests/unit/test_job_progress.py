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
