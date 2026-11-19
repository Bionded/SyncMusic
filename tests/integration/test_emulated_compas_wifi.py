# SPDX-License-Identifier: GPL-3.0-or-later
"""One Compás card synced over USB, then by its Wi-Fi app in the player, then over USB again: the device stays one
record, the Wi-Fi sync writes the card's manifest, and neither side sees the other's files as unknown, copies them
again or doubles them."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from syncmusic.api.server import create_api
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import ActionKind, LibraryConfig
from syncmusic.core.sync import RegisterRequest
from syncmusic.facade import AppFacade
from syncmusic.infra.blockdev import mount as mnt
from syncmusic.reference_client import ReferenceClient
from tests.contracts.harness import compas_tree
from tests.emulated import (
    _populate,
    attach,
    block_device,
    emulated_guard,
    needs_sudo,
    replug,
    running_app,
    sh,
    unplug,
)
from tests.factories import own_rules
from tests.media import make_music

pytestmark = [pytest.mark.emulated, needs_sudo]

ALL = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}, "include_playlists": True}]}
ONE = {
    "rules": [
        {"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": 1, "include_playlists": True}
    ]
}


class CompasApp(ReferenceClient):
    """Like the app in the player: it keeps playlists where the player's own are, `Playlists/<name>.m3u`."""

    def _target(self, rel: str):
        if rel.startswith("Playlists/") and rel.lower().endswith(".m3u8"):
            rel = rel[:-1]
        return super()._target(rel)


def _playlists(root) -> list[str]:
    d = root / "Playlists"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def _tracks(root) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.mp3"))


async def test_one_card_over_usb_wifi_and_usb_again(tmp_path):
    music = tmp_path / "music"
    for i, title in enumerate(("Один", "Два", "Три"), 1):
        make_music(
            music / "Кобза" / f"0{i} {title}.mp3",
            title=title,
            artist="Кобза",
            album="Перший",
            tracknumber=str(i),
        )
    (music / "Мікс.m3u").write_text(
        "Кобза/01 Один.mp3\nКобза/02 Два.mp3\nКобза/03 Три.mp3\n", encoding="utf-8"
    )
    lib = LibraryConfig(id="music", type="folder", name="Music", settings={"root": str(music)})
    with attach("emulated_compas", 256) as disk:
        sh("sudo", "-n", "mkfs.exfat", "-L", "R1", disk.dev)
        sh("udevadm", "settle", "--timeout=10")
        await _populate(disk.dev, tmp_path / "build", compas_tree)
        async with running_app(tmp_path, [disk], [lib]) as (app, [blk]):
            cfg = await app.sync.register(
                RegisterRequest(
                    name="R1",
                    disk=blk.path,
                    rule_set=await own_rules(app, "R1", ONE, ["music"]),
                    on_connect="manual",
                )
            )
            assert cfg.type == "hiby_compas"

            async def usb_sync(**kw):
                pc = await app.sync.plan(cfg.id)
                job = await app.jobs.wait((await app.sync.apply(pc.plan.id, **kw)).id)
                assert job.status == "succeeded", job.error
                return pc.plan

            await usb_sync(first_sync="keep")
            uid = (await app.store.device(cfg.id)).identity.manifest_device_id
            assert uid

            async def in_player(work):
                """The card in the player (not on USB): the app reaches it over Wi-Fi."""
                unplug(disk)
                await app.sync.on_disk_removed(blk.name)
                replug(disk)  # the server is not told: the card sits in the player
                m = await mnt.mount(emulated_guard(), disk.dev, "build", tmp_path / "player")
                try:
                    return await work(m.path)
                finally:
                    await mnt.unmount(m)
                    unplug(disk)

            api = create_api(AppFacade(app), manage_lifecycle=False)
            async with AsyncClient(transport=ASGITransport(app=api), base_url="http://t") as c:
                r = await c.patch("/api/v1/settings", json={"clients_allowed": True})
                assert r.status_code == 200, r.text

                async def pair_and_sync(card):
                    rc = CompasApp(card, http=c)
                    rc.formats, rc.card_uid = ["mp3"], uid

                    async def approve() -> None:
                        for _ in range(600):
                            if reqs := (await c.get("/api/v1/clients/requests")).json():
                                break
                            await asyncio.sleep(0.05)
                        assert [d["id"] for d in reqs[0]["same_card"]] == [cfg.id]
                        r = await c.post(
                            f"/api/v1/clients/requests/{reqs[0]['id']}/approve",
                            json={"device_id": cfg.id},
                        )
                        assert r.status_code == 200, r.text

                    task = asyncio.create_task(approve())
                    assert (
                        await asyncio.wait_for(rc.request_pairing("http://t/", "R1"), 30) == cfg.id
                    )
                    await task
                    await own_rules(app, "R1", ALL, ["music"])  # the player gets more now
                    out = await rc.sync()
                    # two tracks + the playlist, now with all three; the USB-written copy is rewritten, not deleted
                    assert out == {"fetched": 3, "deleted": 0, "failed": 0, "generated": 0}, out
                    on_card = manifest_io.read(card)
                    assert on_card is not None and on_card.header.device_uid == uid
                    assert sorted(e.path for e in on_card.items.values()) == _tracks(card)
                    assert len(_tracks(card)) == 3
                    assert _playlists(card) == ["Мікс.m3u"]
                    return rc

                rc = await in_player(pair_and_sync)
                assert len(await app.store.devices()) == 1

                # back on USB: everything is its own, nothing to copy, nothing doubled
                replug(disk)
                blk = block_device(disk)
                await app._disk_added(blk)
                plan = await usb_sync()
                assert {a.kind for a in plan.actions} <= {ActionKind.KEEP}, [
                    (a.kind, a.ref) for a in plan.actions if a.kind is not ActionKind.KEEP
                ]
                dev = await app.sync.open_device(cfg.id)
                assert len(_tracks(dev.mount)) == 3
                assert _playlists(dev.mount) == ["Мікс.m3u"]

                # in the player again: the Wi-Fi sync has nothing to fetch
                async def again(card):
                    rc.folder = card
                    out = await rc.sync()
                    assert out["failed"] == 0 and out["fetched"] == 0, out
                    assert len(_tracks(card)) == 3
                    assert _playlists(card) == ["Мікс.m3u"]

                await in_player(again)
