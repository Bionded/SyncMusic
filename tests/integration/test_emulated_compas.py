# SPDX-License-Identifier: GPL-3.0-or-later
"""HiBy player with the Compás firmware on an emulated whole-disk exFAT card, against the compose Audiobookshelf:
an audiobook synced with its library position written into the Audiobooks plugin's state file, a position moved on
the player goes back to the library, the plugin's own lines survive byte for byte and a re-sync changes nothing.
The user's ABS progress is restored afterwards."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from syncmusic.core.models import ActionKind, MediaKind
from syncmusic.core.sync import RegisterRequest
from syncmusic.devices.HibyR1 import compas_feedback as cf
from tests.contracts.harness import compas_tree
from tests.emulated import _populate, abs_config, attach, needs_sudo, running_app, sh
from tests.factories import own_rules
from tests.integration import abs_progress

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

FOREIGN = (
    "P\tOther%20Book\tch1.mp3\t12\t600\t0\t1700000000\t0\t0\nB\tOther%20Book\tch1.mp3\t5\tmark\n"
)


def _player_card(root: Path) -> None:
    """What the firmware and its Audiobooks plugin left on the card."""
    compas_tree(root)
    (root / ".plugins").mkdir(exist_ok=True)
    (root / cf.AUDIOBOOK_STATE).write_text(FOREIGN)


async def test_compas_audiobook_positions_both_ways(services, tmp_path):
    with attach("emulated_compas", 512) as disk:
        sh("sudo", "-n", "mkfs.exfat", "-L", "R1", disk.dev)
        sh("udevadm", "settle", "--timeout=10")
        await _populate(disk.dev, tmp_path / "build", _player_card)
        async with running_app(tmp_path, [disk], [abs_config(services)]) as (app, [blk]):
            abs_lib = await app.sync.library("abs")
            saved = await abs_progress.save(abs_lib)
            try:
                books = [
                    t
                    for t in (await abs_lib.snapshot()).tracks.values()
                    if t.kind is MediaKind.AUDIOBOOK and (t.duration_s or 0) > 300
                ]
                assert books, "an audiobook is needed"
                book = min(books, key=lambda t: t.size_bytes)
                await abs_lib.set_position(book.ref, 30.0, False)
                rule = {
                    "id": "b",
                    "criteria": {
                        "all": [{"is": {"title": book.title}}, {"is": {"album": book.album}}]
                    },
                    "kind": ["audiobook"],
                }
                cfg = await app.sync.register(
                    RegisterRequest(
                        name="Compás",
                        disk=blk.path,
                        rule_set=await own_rules(app, "Compás", {"rules": [rule]}, ["abs"]),
                        on_connect="manual",
                    )
                )
                assert cfg.type == "hiby_compas", cfg.type

                async def sync(**kw):
                    app.sync.forget_library("abs")
                    pc = await app.sync.plan(cfg.id)
                    job = await app.jobs.wait((await app.sync.apply(pc.plan.id, **kw)).id)
                    assert job.status == "succeeded", job.error
                    return pc.plan

                await sync(first_sync="keep")
                mount = (await app.sync.open_device(cfg.id)).mount
                state = (mount / cf.AUDIOBOOK_STATE).read_text()
                assert state.startswith(FOREIGN.splitlines()[0]), (
                    state
                )  # the plugin's own line untouched
                assert FOREIGN.splitlines()[1] in state.splitlines()
                pos = cf.audiobook_positions(mount)
                ours = {k: v for k, v in pos.items() if "Other Book" not in k}
                assert len(ours) == 1 and list(ours.values())[0][0] == pytest.approx(30, abs=1), pos
                rel = next(iter(ours))

                # moved on the player, later than the last sync: goes up to ABS
                key, file = cf._audiobook_key(rel)  # type: ignore[misc]
                lines = state.splitlines()
                for i, line in enumerate(lines):
                    p = line.split("\t")
                    if p[0] == "P" and cf.unquote(p[1]) == key:
                        p[3], p[6] = "95", str(int(time.time()) + 5)
                        lines[i] = "\t".join(p)
                (mount / cf.AUDIOBOOK_STATE).write_text("".join(f"{x}\n" for x in lines))
                await asyncio.sleep(6)
                await sync()
                lib = await app.sync.library("abs")
                assert (await lib.get_position(book.ref)).position_s == pytest.approx(95, abs=1)

                plan = await sync()  # no ping-pong, nothing to copy
                assert {x.kind for x in plan.actions} <= {ActionKind.KEEP}
                assert (await lib.get_position(book.ref)).position_s == pytest.approx(95, abs=1)
                mount = (await app.sync.open_device(cfg.id)).mount
                assert (
                    FOREIGN.splitlines()[1] in (mount / cf.AUDIOBOOK_STATE).read_text().splitlines()
                )
            finally:
                await abs_progress.restore(await app.sync.library("abs"), saved)
