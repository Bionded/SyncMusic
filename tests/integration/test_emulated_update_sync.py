"""Update sync per device type on emulated devices with the compose stack's Navidrome: after a full
sync, a metadata change and a new track selected by the rules - the update sync writes the metadata (iPod: in the
iTunesDB; flash drive and Rockbox: the same file rewritten with the new tags) and copies no new file; the next full
sync adds the track."""

from __future__ import annotations

import json

import pytest

from syncmusic.core.sync import RegisterRequest
from tests.emulated import (
    CLASSIC_LAYOUT,
    attach,
    flash,
    ipod,
    navidrome_config,
    needs_sudo,
    rockbox,
    running_app,
)
from tests.factories import own_rules

pytestmark = [pytest.mark.integration, pytest.mark.emulated, needs_sudo]


def _rules(n: int) -> dict:
    return {
        "rules": [
            {"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": n, "as_playlist": "Три"}
        ]
    }


@pytest.mark.parametrize(
    ("kind", "dev_type", "field"),
    [
        ("emulated_flash", "flashdrive", "replace"),
        ("emulated_ipod_classic", "ipod", "update_meta"),
        ("emulated_rockbox_eros", "rockbox", "replace"),
    ],
)
async def test_update_sync_writes_metadata_but_no_new_file(
    services, tmp_path, kind, dev_type, field
):
    layout = None if dev_type == "flashdrive" else CLASSIC_LAYOUT
    with attach(kind, 512, layout=layout) as disk:
        if dev_type == "flashdrive":
            await flash(disk, "vfat", "UPD")
        elif dev_type == "ipod":
            await ipod(disk, tmp_path / "build")
        else:
            await rockbox(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [navidrome_config(services)]) as (app, [blk]):
            req = RegisterRequest(
                name="Upd",
                disk=blk.path,
                type=dev_type,
                rule_set=await own_rules(app, "Upd", _rules(3), ["nd"]),
                on_connect="manual",
                # the emulated Rockbox tree plays MP3 only: "Rockbox: copy" (the default) would skip the FLACs
                processor_template_id="builtin-rockbox" if dev_type == "rockbox" else None,
            )
            cfg = await app.sync.register(req)
            assert cfg.on_connect == "manual" and cfg.update_sync
            pc = await app.sync.plan(cfg.id)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
            assert job.status == "succeeded", job.error
            dev = await app.sync.open_device(cfg.id)

            # Navidrome's tags of one track changed (its file did not), and the rules select a fourth track
            path = dev.mount / ".syncmusic" / "manifest.jsonl"
            lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
            next(x for x in lines if x.get("meta_hash"))["meta_hash"] = "stale"
            path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
            cfg = await app.sync.update_device(
                cfg.model_copy(
                    update={"rule_set": await own_rules(app, cfg.name, _rules(4), ["nd"])}
                )
            )

            job = await app.jobs.wait((await app.sync.update_sync(cfg.id)).id)
            assert job.status == "succeeded", job.error
            counts = job.result["counts"]
            assert (counts["add"], counts[field]) == (0, 1), counts
            assert len((await dev.read_manifest()).items) == 3

            pc = await app.sync.plan(cfg.id)
            assert [a.kind.value for a in pc.plan.actions].count("add") == 1
            assert not any(a.reason.startswith("metadata") for a in pc.plan.actions)
            job = await app.jobs.wait((await app.sync.apply(pc.plan.id)).id)
            assert job.status == "succeeded", job.error
            assert len((await dev.read_manifest()).items) == 4
