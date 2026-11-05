"""The FTP device against a real FTP server (vsftpd in the compose stack): registered through the app
(the login is checked), music from the compose Navidrome synced, re-run is a no-op, a rule change removes files,
Player contents lists what is on the server; a wrong password is refused at registration."""

from __future__ import annotations

import uuid

import pytest

from syncmusic.app import Application
from syncmusic.core.models import ActionKind
from syncmusic.core.sync import RegisterRequest, SyncError
from syncmusic.infra.config import load_config
from tests.conftest import HARDWARE_TOML
from tests.emulated import navidrome_config
from tests.factories import own_rules

pytestmark = pytest.mark.integration

FIVE = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": 5, "sort": "title"}]}
TWO = {"rules": [{"id": "m", "criteria": {"is": {"kind": "music"}}, "limit": 2, "sort": "title"}]}


@pytest.fixture
async def app(services, tmp_path):
    cfg = load_config(
        env={},
        overrides={
            "data_dir": tmp_path / "data",
            "cache_dir": tmp_path / "cache",
            "mount_root": tmp_path / "mnt",
            "device_allowlist": HARDWARE_TOML,
            "mode": "daemon",
            "detect_devices": False,
        },
    )
    a = Application(cfg)
    await a.start()
    await a.store.put_library(navidrome_config(services))
    yield a
    await a.stop()


def _ftp(services, **over):
    base = f"/sd-it-{uuid.uuid4().hex[:6]}"
    return {
        "host": services["FTP_HOST"],
        "port": int(services["FTP_PORT"]),
        "username": services["FTP_USER"],
        "password": services["FTP_PASSWORD"],
        "base_path": base,
        **over,
    }


async def _apply(a, plan_id, **kw):
    job = await a.jobs.wait((await a.sync.apply(plan_id, **kw)).id)
    assert job.status == "succeeded", job.error
    return job.result


async def test_sync_to_a_real_ftp_server(app, services):
    req = RegisterRequest(
        name="NAS",
        type="ftp",
        rule_set=await own_rules(app, "NAS", FIVE, ["nd"]),
        on_connect="manual",
        settings=_ftp(services),
    )
    cfg = await app.sync.register(req)
    pc = await app.sync.plan(cfg.id)
    assert pc.plan.count(ActionKind.ADD) == 5, pc.plan.warnings
    rep = await _apply(app, pc.plan.id, first_sync="keep")
    assert rep["status"] == "succeeded" and not rep["failures"], rep["failures"]
    dev = await app.sync.open_device(cfg.id)
    m = await dev.read_manifest()
    assert len(m.items) == 5
    assert {e.path for e in m.items.values()} <= {i.path for i in (await dev.read_contents()).items}
    pc = await app.sync.plan(cfg.id)  # re-run: nothing to do
    assert {x.kind for x in pc.plan.actions} == {ActionKind.KEEP}
    cfg = await app.sync.update_device(
        cfg.model_copy(update={"rule_set": await own_rules(app, cfg.name, TWO, ["nd"])})
    )
    pc = await app.sync.plan(cfg.id)
    assert pc.plan.count(ActionKind.REMOVE) == 3
    await _apply(app, pc.plan.id, confirm=True)
    rows = await app.sync.contents(cfg.id)
    assert len(rows) == 2 and not any(r["foreign"] for r in rows)


async def test_wrong_password_refused(app, services):
    req = RegisterRequest(name="Bad", type="ftp", settings=_ftp(services, password="wrong"))
    with pytest.raises(SyncError, match="cannot reach the server"):
        await app.sync.register(req)
