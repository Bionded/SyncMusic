"""Against the real compose Audiobookshelf on an emulated Classic: a multi-file audiobook listened across a
chapter boundary on the iPod. The book is one position on ABS: the newest chapter bookmark decides (whatever order the
device rows come in), the sync's own push of one chapter is not a newer library position for the other, nothing is
logged as a conflict, and a re-sync changes nothing. The user's ABS progress is restored afterwards."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from syncmusic.core import manifest as manifest_io
from syncmusic.core import models
from syncmusic.core import sync as sync_module
from syncmusic.core.models import MediaKind
from syncmusic.core.sync import RegisterRequest
from tests.emulated import CLASSIC_LAYOUT, abs_config, attach, ipod, needs_sudo, running_app
from tests.factories import own_rules
from tests.integration import abs_progress

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]


@pytest.fixture(autouse=True)
def chapter_files(monkeypatch):
    """A book as one file per chapter on the iPod, no merge (the compose book is split into parts)."""
    for module in (models, sync_module):
        monkeypatch.setattr(module, "COMBINE_AUDIOBOOKS_TYPES", frozenset())


async def test_a_book_listened_across_a_chapter_boundary_keeps_the_later_chapter(
    services, tmp_path, caplog
):
    from pygpod import Database as IPodDB

    with attach("emulated_ipod_classic", 1024, layout=CLASSIC_LAYOUT) as disk:
        await ipod(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [abs_config(services)]) as (app, [blk]):
            abs_lib = await app.sync.library("abs")
            saved = await abs_progress.save(abs_lib)
            try:
                books: dict[str, list] = {}
                for t in (await abs_lib.snapshot()).tracks.values():
                    if t.kind is MediaKind.AUDIOBOOK and (t.duration_s or 0) > 300:
                        books.setdefault(t.album_id or "", []).append(t)
                multi = [sorted(b, key=lambda t: t.chapter_index or 0) for b in books.values()]
                multi = [b for b in multi if len(b) >= 2]
                assert multi, "a multi-file audiobook is needed"
                book = min(multi, key=lambda b: sum(t.size_bytes for t in b[:2]))[:2]
                ch1, ch2 = book
                await abs_lib.set_position(ch1.ref, 30.0, False)
                titles = [{"is": {"title": ch.title}} for ch in book]  # only these two chapters
                rule = {
                    "id": "b",
                    "criteria": {"all": [{"is": {"album": ch1.album}}, {"any": titles}]},
                    "kind": ["audiobook"],
                }
                req = RegisterRequest(
                    name="Book Classic",
                    disk=blk.path,
                    rule_set=await own_rules(app, "Book Classic", {"rules": [rule]}, ["abs"]),
                    on_connect="manual",
                )
                cfg = await app.sync.register(req)

                async def sync(**kw):
                    app.sync.forget_library("abs")
                    pc = await app.sync.plan(cfg.id)
                    job = await app.jobs.wait((await app.sync.apply(pc.plan.id, **kw)).id)
                    assert job.status == "succeeded", job.error
                    return job.result["reverse"] if job.result else {}

                async def bookmarks(set_: dict | None = None) -> dict[str, float]:
                    """Chapter key -> iPod bookmark in s; `set_` = {chapter key: (seconds, played at)}."""
                    mount = (await app.sync.open_device(cfg.id)).mount
                    items = manifest_io.read(mount).items
                    db = IPodDB(str(mount))
                    by_dbid = {str(t.dbid): t for t in db.tracks}
                    for key, (s, played) in (set_ or {}).items():
                        t = by_dbid[items[key].device_ref]
                        t.bookmark_time, t.time_played = int(s * 1000), played
                    if set_:
                        db.save()
                    return {
                        ch.ref.key(): by_dbid[items[ch.ref.key()].device_ref].bookmark_time / 1000
                        for ch in book
                    }

                async def abs_at(ch) -> float:
                    lib = await app.sync.library("abs")
                    return (await lib.get_position(ch.ref)).position_s

                await sync(first_sync="keep")
                assert (await bookmarks())[ch1.ref.key()] == pytest.approx(30.0, abs=1)

                # chapter 1 to its end half an hour ago, then chapter 2 to 100 s ten minutes ago
                now = datetime.now().replace(
                    tzinfo=UTC
                )  # the iPod keeps wall-clock time labelled UTC
                end1 = (ch1.duration_s or 0) - 5
                await bookmarks(
                    {
                        ch1.ref.key(): (end1, now - timedelta(minutes=30)),
                        ch2.ref.key(): (100.0, now - timedelta(minutes=10)),
                    }
                )
                caplog.clear()
                with caplog.at_level(logging.INFO):
                    rev = await sync()
                assert rev["positions_superseded"] == 0, rev
                assert not [r for r in caplog.records if "position conflict" in r.getMessage()]
                assert await abs_at(ch2) == pytest.approx(100.0, abs=1)
                assert (await bookmarks())[ch2.ref.key()] == pytest.approx(100.0, abs=1)

                # back to chapter 1 on the iPod (newest now): the book goes back to it
                await asyncio.sleep(2)  # the iPod keeps whole seconds: later than the last push
                await bookmarks({ch1.ref.key(): (40.0, datetime.now().replace(tzinfo=UTC))})
                rev = await sync()
                assert rev["positions_superseded"] == 0, rev
                assert await abs_at(ch1) == pytest.approx(40.0, abs=1)
                assert (await bookmarks())[ch1.ref.key()] == pytest.approx(40.0, abs=1)

                # no ping-pong
                rev = await sync()
                assert rev["positions_pushed"] == rev["positions_superseded"] == 0, rev
                assert await abs_at(ch1) == pytest.approx(40.0, abs=1)
            finally:
                await abs_progress.restore(await app.sync.library("abs"), saved)
