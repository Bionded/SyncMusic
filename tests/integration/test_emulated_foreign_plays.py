# SPDX-License-Identifier: GPL-3.0-or-later
"""Two installations share one stock iPod: plays of an item that belongs to the other installation's library are
not sent by this one; they survive a full sync (iTunesDB rebuild) in the manifest and are sent once by the
installation whose library it is - no loss, no double count."""

from __future__ import annotations

import pytest

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import ItemRef
from syncmusic.core.sync import RegisterRequest
from tests.contracts.harness import _ipod_feedback
from tests.emulated import CLASSIC_LAYOUT, attach, ipod, navidrome_config, needs_sudo, running_app
from tests.factories import own_rules

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]

RULES = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": 2}]}


async def _full_sync(app, device_id):
    pc = await app.sync.plan(device_id)
    job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
    assert job.status == "succeeded", job.error
    return job.result


def _rekey(mount, key, library_id):
    m = manifest_io.read(mount)
    entry = m.items.pop(key)
    ref = ItemRef(library_id=library_id, item_id=entry.ref.item_id)
    m.items[ref.key()] = entry.model_copy(update={"ref": ref})
    manifest_io.write(mount, m)
    return ref.key()


async def test_plays_of_the_other_installations_item_survive_a_rebuild(services, tmp_path):
    with attach("emulated_ipod_classic", 512, layout=CLASSIC_LAYOUT) as disk:
        await ipod(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            req = RegisterRequest(
                name="Два дома",
                disk=blk.path,
                type="ipod",
                rule_set=await own_rules(app, "Два дома", RULES, ["nd"]),
                on_connect="manual",
            )
            cfg = await app.sync.register(req)
            await _full_sync(app, cfg.id)
            dev = await app.sync.open_device(cfg.id)
            mine, other = sorted(manifest_io.read(dev.mount).items)
            # the second item belongs to another installation (a library this one does not have)
            other = _rekey(dev.mount, other, "ext:L9")
            await _ipod_feedback(dev, manifest_io.read(dev.mount))  # 2 plays on every track

            result = await _full_sync(app, cfg.id)
            assert result["reverse"]["plays_submitted"] == 2, result["reverse"]
            m = manifest_io.read(dev.mount)
            assert other in m.items, "another installation's item was removed"
            assert m.items[other].pending_plays == 2
            assert m.items[mine].pending_plays == 0

            # the other installation's view: the item is in its library
            ours = _rekey(dev.mount, other, "nd")
            result = await _full_sync(app, cfg.id)
            assert result["reverse"]["plays_submitted"] == 2, result["reverse"]
            assert manifest_io.read(dev.mount).items[ours].pending_plays == 0
            result = await _full_sync(app, cfg.id)
            assert result["reverse"]["plays_submitted"] == 0, result["reverse"]
