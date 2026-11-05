"""Logs page backend: records carry their job/device, the file is read back into entries (kv and json),
filters by level/device/job/words, download of the raw lines, live `log.entry` events, the job id of a report."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import asyncio
import logging

import pytest

from syncmusic.infra.events import EventBus
from syncmusic.infra.jobs import JobRunner
from syncmusic.infra.log import (
    BusLogHandler,
    ContextFilter,
    JSONFormatter,
    KVFormatter,
    log_context,
    matches,
    parse_log,
)
from tests.unit.test_api_sync import _register, env, music  # noqa: F401 - fixtures

log = logging.getLogger("syncmusic.test.logs")


def _format(
    formatter: logging.Formatter, msg: str, *args, level=logging.INFO, exc=None, **ctx
) -> str:
    token = log_context.set(ctx or None)
    try:
        record = log.makeRecord(log.name, level, __file__, 1, msg, args, exc)
        ContextFilter().filter(record)
        return formatter.format(record)
    finally:
        log_context.reset(token)


@pytest.mark.parametrize("formatter", [KVFormatter(), JSONFormatter()], ids=["kv", "json"])
def test_written_lines_read_back(formatter):
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc = sys.exc_info()
    lines = [
        _format(
            formatter, "copied %s", "Скрябін - 'Група крові' \"live\"", job="j1", device="classic"
        ),
        _format(formatter, "url http://h/rest?u=a&p=secret", level=logging.WARNING),
        _format(formatter, "failed", level=logging.ERROR, exc=exc, job="j2"),
    ]
    text = "\n".join(lines).splitlines()
    entries = parse_log(text)
    assert [e["level"] for e in entries] == ["INFO", "WARNING", "ERROR"]
    first = entries[0]
    assert first["msg"] == "copied Скрябін - 'Група крові' \"live\""
    assert first["job"] == "j1" and first["device"] == "classic"
    assert first["logger"] == "syncmusic.test.logs" and first["ts"].startswith("20")
    assert "secret" not in entries[1]["msg"]  # redacted when written
    assert "ValueError: boom" in entries[2]["exc"] and entries[2]["job"] == "j2"
    assert "device" not in entries[2]


def test_filters():
    e = {
        "level": "WARNING",
        "msg": "Mounted Classic",
        "logger": "x.mount",
        "job": "j1",
        "device": "classic",
    }
    assert (
        matches(e, level="INFO") and matches(e, level="warning") and not matches(e, level="ERROR")
    )
    assert matches(e, device="classic") and not matches(e, device="mini")
    assert matches(e, job="j1") and not matches(e, job="j2")
    assert matches(e, query="classic MOUNT") and not matches(e, query="classic eject")


async def test_job_records_carry_job_and_device_also_in_worker_threads():
    runner = JobRunner(EventBus())
    seen: list[dict] = []

    class Grab(logging.Handler):
        def emit(self, record):
            seen.append({"msg": record.getMessage(), **record.ctx})

    handler = Grab()
    handler.addFilter(ContextFilter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:

        async def job(ctx):
            log.info("in the task")
            await ctx.run_blocking(log.info, "in a worker thread")
            await asyncio.to_thread(log.info, "in to_thread")

        info = runner.submit("sync", "s", job, device_id="classic")
        await runner.wait(info.id)
        log.info("outside")
    finally:
        log.removeHandler(handler)
    want = {"job": info.id, "device": "classic"}
    assert seen[:3] == [
        {"msg": m, **want} for m in ("in the task", "in a worker thread", "in to_thread")
    ]
    assert seen[3] == {"msg": "outside"}


async def test_bus_handler_publishes_info_and_above():
    bus = EventBus()
    q = bus.subscribe()
    handler = BusLogHandler(bus, asyncio.get_running_loop())
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        log.debug("not live")
        log.info("token=abc live")
        await asyncio.to_thread(log.warning, "from a thread")
        await asyncio.sleep(0.05)
    finally:
        log.removeHandler(handler)
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert [e.type for e in events] == ["log.entry", "log.entry"]
    assert events[0].data["msg"] == "token=*** live" and events[0].data["level"] == "INFO"
    assert events[1].data["msg"] == "from a thread"


async def test_job_records_carry_the_sync_step():
    """a sync job's phase names the step of every record logged after it; a plan job's records say plan."""
    runner = JobRunner(EventBus())
    seen: list[dict] = []

    class Grab(logging.Handler):
        def emit(self, record):
            seen.append({"msg": record.getMessage(), **record.ctx})

    handler = Grab()
    handler.addFilter(ContextFilter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:

        async def sync(ctx):
            log.info("before")
            ctx.progress(0.1, None, phase="copy")
            log.info("copying")
            await asyncio.to_thread(log.info, "copying in a thread")
            ctx.progress(0.9, None, phase="database")
            log.info("writing the database")

        async def plan(ctx):
            log.info("planning")

        await runner.wait(runner.submit("sync", "s", sync, device_id="classic").id)
        await runner.wait(runner.submit("plan", "p", plan, device_id="classic").id)
    finally:
        log.removeHandler(handler)
    steps = {x["msg"]: x.get("step") for x in seen}
    assert steps == {
        "before": None,
        "copying": "copy",
        "copying in a thread": "copy",
        "writing the database": "database",
        "planning": "plan",
    }


async def test_entries_api_filters_and_download(env, tmp_path):
    app, c = env
    path = app.config.data_dir / "logs" / "syncmusic.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = KVFormatter()
    path.write_text(
        "\n".join(
            [
                _format(f, "app started"),
                _format(f, "copy 1", level=logging.DEBUG, job="j1", device="classic"),
                _format(f, "copy failed", level=logging.ERROR, job="j1", device="classic"),
                _format(f, "mini mounted", job="j2", device="mini"),
                _format(f, "copied 3 files", job="j2", device="mini", step="copy"),
            ]
        )
        + "\n"
    )
    get = lambda **p: c.get("/api/v1/logs/entries", params=p)  # noqa: E731
    r = (await get()).json()
    assert [e["msg"] for e in r["entries"]] == [
        "app started",
        "copy 1",
        "copy failed",
        "mini mounted",
        "copied 3 files",
    ]
    # the sync step a line was logged in
    r = (await get(step="copy")).json()
    assert [(e["msg"], e["step"]) for e in r["entries"]] == [("copied 3 files", "copy")]
    dl = await c.get("/api/v1/logs/download", params={"step": "copy"})
    assert dl.text.count("\n") == 1 and "copied 3 files" in dl.text
    assert "raw" not in r["entries"][0]
    r = (await get(level="INFO")).json()
    assert r["total"] == 4
    assert [e["msg"] for e in (await get(device="classic")).json()["entries"]] == [
        "copy 1",
        "copy failed",
    ]
    assert [e["msg"] for e in (await get(job="j2")).json()["entries"]] == [
        "mini mounted",
        "copied 3 files",
    ]
    assert [e["msg"] for e in (await get(query="COPY failed")).json()["entries"]] == ["copy failed"]
    r = (await get(limit=1)).json()
    assert r["total"] == 5 and [e["msg"] for e in r["entries"]] == ["copied 3 files"]  # the newest
    assert (await get(level="LOUD")).status_code == 400
    assert (await get(limit=0)).status_code == 400

    r = await c.get("/api/v1/logs/download", params={"job": "j1"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert 'filename="syncmusic-j1.log"' in r.headers["content-disposition"]
    assert r.text.count("\n") == 2 and "copy failed" in r.text and "mini" not in r.text


async def test_a_report_names_its_job(env, tmp_path):
    app, c = env
    dev = await _register(c, tmp_path / "flash")
    job = (await c.post(f"/api/v1/devices/{dev['id']}/update")).json()
    await app.jobs.wait(job["id"])
    (report,) = (await c.get("/api/v1/sync-reports", params={"device": dev["id"]})).json()
    assert report["job_id"] == job["id"]


def test_an_item_failure_keeps_the_sync_step():
    """An item failure logs its own step (put / prepare / a processor) in `kv`; the line must stay under the job's
    sync step, so the Logs page's Step = copy finds it."""
    token = log_context.set({"job": "j1", "device": "classic", "step": "copy"})
    try:
        record = log.makeRecord(
            log.name,
            logging.WARNING,
            __file__,
            1,
            "classic: put failed for X",
            (),
            None,
            extra={"kv": {"item": "X", "item_step": "put"}},
        )
        ContextFilter().filter(record)
        line = KVFormatter().format(record)
    finally:
        log_context.reset(token)
    (entry,) = parse_log([line])
    assert entry["step"] == "copy" and entry["item_step"] == "put"
