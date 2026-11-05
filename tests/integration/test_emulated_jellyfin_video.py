"""Jellyfin's movies and episodes through a real sync (compose profile `jellyfin`): onto an emulated flash drive as
they are and onto an emulated iPod Classic converted by its video processor; a second sync changes nothing."""

from __future__ import annotations

import pytest

from syncmusic.core.models import LibraryConfig, MediaKind
from syncmusic.core.sync import RegisterRequest
from tests.emulated import CLASSIC_LAYOUT, attach, flash, ipod, needs_sudo, running_app
from tests.factories import own_rules
from tests.integration import services
from tests.integration.services import ENV

pytestmark = [
    pytest.mark.integration,
    pytest.mark.emulated,
    pytest.mark.slow,
    needs_sudo,
    pytest.mark.skipif(
        "JELLYFIN_URL" not in ENV or not services.docker(), reason="no Jellyfin test setup"
    ),
]

RULES = {"rules": [{"id": "v", "criteria": {"all": []}, "kind": ["video"]}]}


@pytest.fixture(scope="module")
def jellyfin(compose_stack):  # the stack (its network) stays up while Jellyfin runs on it
    services.jellyfin_up()
    yield services.jellyfin_api_key()
    services.jellyfin_down()


def _config(api_key: str) -> LibraryConfig:
    settings = {"url": ENV["JELLYFIN_URL"], "username": ENV["JELLYFIN_USER"], "api_key": api_key}
    return LibraryConfig(id="jf", type="jellyfin", name="Jellyfin", settings=settings)


async def _sync_twice(app, blk, name: str) -> tuple[dict, object]:
    req = RegisterRequest(
        name=name, disk=blk.path, rule_set=await own_rules(app, name, RULES, ["jf"])
    )
    cfg = await app.sync.register(req)
    pc = await app.sync.plan(cfg.id)
    job = await app.jobs.wait((await app.sync.apply(pc.plan.id, first_sync="keep")).id)
    assert job.status == "succeeded", job.error
    assert job.result["counts"]["add"] == len(services.JELLYFIN_VIDEOS), job.result
    dev = await app.sync.open_device(cfg.id)
    items = (await dev.read_manifest()).items
    again = await app.sync.plan(cfg.id)
    assert {a.kind for a in again.plan.actions} == {"keep"}, again.plan.actions
    return items, dev


async def test_movie_and_episodes_reach_a_flash_drive(jellyfin, tmp_path):
    with attach("emulated_flash", 256) as disk:
        await flash(disk, "vfat", "VIDEO")
        async with running_app(tmp_path, [disk], [_config(jellyfin)]) as (app, [blk]):
            items, dev = await _sync_twice(app, blk, "Video flash")
            paths = sorted(e.path for e in items.values())
            assert all(p.startswith("Videos/") for p in paths), paths
            assert any("Тестовий серіал" in p for p in paths)
            assert {e.kind for e in items.values()} == {MediaKind.VIDEO}


async def test_movie_and_episodes_reach_an_ipod_converted(jellyfin, tmp_path):
    from pygpod import Database as IPodDB

    with attach("emulated_ipod_classic", 1024, layout=CLASSIC_LAYOUT) as disk:
        await ipod(disk, tmp_path / "build")
        async with running_app(tmp_path, [disk], [_config(jellyfin)]) as (app, [blk]):
            items, dev = await _sync_twice(app, blk, "Video Classic")
            assert len(items) == len(services.JELLYFIN_VIDEOS)
            # without metadata providers Jellyfin names each item after its file
            tracks = {t.title: t for t in IPodDB(str(dev.mount)).tracks}
            assert tracks["Тестовий серіал S01E02"].tvshow == "Тестовий серіал", sorted(tracks)
            assert tracks["Тестовий серіал S01E02"].episode_number == 2
            assert not tracks["Тестовий фільм (2001)"].tvshow
            assert all(str(t.ipod_path).lower().endswith((".m4v", ".mp4")) for t in tracks.values())
