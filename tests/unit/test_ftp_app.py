"""An FTP server as a device in the application: registered with its settings (no disk, no
folder; the login is checked), listed as a connected network device, synced, its contents read; Format, Eject and
a disk/folder are refused. Runs against the in-process FTP server of the contract harness."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

from tests.contracts.harness import _ftp_server
from tests.unit.test_api_sync import _apply, env, music  # noqa: F401


def _body(name: str, **settings):
    root, port = _ftp_server()
    base = f"app-{name}"
    (root / base).mkdir(exist_ok=True)
    return root / base, {
        "name": name,
        "type": "ftp",
        "library_ids": ["music"],
        "settings": {"host": "127.0.0.1", "port": port, "base_path": f"/{base}", **settings},
    }


async def test_register_sync_and_read_an_ftp_device(env):
    app, c = env
    folder, body = _body("Home NAS", username="sd", password="secret")
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 201, r.text
    dev = r.json()
    assert dev["settings"]["password"] != "secret"  # masked
    listed = next(
        d for d in (await c.get("/api/v1/devices")).json()["registered"] if d["id"] == dev["id"]
    )
    assert listed["connected"] and listed["kind"] == "network" and not listed["can_format"]
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["counts"]["add"] == 3, plan
    await _apply(app, c, plan["id"], first_sync="keep")
    assert (folder / ".syncmusic" / "manifest.jsonl").is_file()
    assert len([p for p in folder.rglob("*.mp3")]) == 3
    contents = (await c.get(f"/api/v1/devices/{dev['id']}/contents")).json()
    assert len(contents) == 3 and not any(r["foreign"] for r in contents)
    assert (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()["counts"]["add"] == 0
    r = await c.post(f"/api/v1/devices/{dev['id']}/eject")
    assert r.status_code == 400 and "nothing to eject" in r.json()["message"]
    r = await c.post(f"/api/v1/devices/{dev['id']}/format", json={"confirm": "Home NAS"})
    assert r.status_code == 400 and "nothing to format" in r.json()["message"]


async def test_anonymous_and_refused_registrations(env, tmp_path):
    app, c = env
    _, body = _body("Anon")
    assert (await c.post("/api/v1/devices/register", json=body)).status_code == 201
    _, body = _body("Wrong", username="sd", password="nope")
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code >= 400 and "cannot reach the server" in r.json()["message"], r.text
    assert "nope" not in r.text
    _, body = _body("Folder too")
    r = await c.post("/api/v1/devices/register", json={**body, "mount_path": str(tmp_path)})
    assert r.status_code == 400 and "no disk or folder" in r.json()["message"]
    names = {d["name"] for d in (await c.get("/api/v1/devices")).json()["registered"]}
    assert names == {"Anon"}


async def test_password_masked_and_kept_on_patch(env):
    app, c = env
    _, body = _body("Masked", username="sd", password="secret")
    dev = (await c.post("/api/v1/devices/register", json=body)).json()
    cfg = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]
    assert cfg["settings"]["password"] == "********"
    r = await c.patch(
        f"/api/v1/devices/{dev['id']}", json={"settings": {**cfg["settings"], "passive": True}}
    )
    assert r.status_code == 200 and r.json()["settings"]["password"] == "********"
    assert (await app.store.device(dev["id"])).settings[
        "password"
    ] == "secret"  # kept, not the mask


async def test_space_of_an_ftp_server_without_quota_is_unknown(env):
    """no made-up capacity (the 2**50 sentinel) - /space says unknown and counts what SyncMusic wrote; the
    plan's space is 'not checked'; with a quota the numbers are real."""
    app, c = env
    folder, body = _body("Без квоти")
    dev = (await c.post("/api/v1/devices/register", json=body)).json()
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["space"]["unknown"] and plan["space"]["fits"]
    # the plan's JSON carries null, not the sentinel (also when the stored plan is read again)
    assert plan["space"]["capacity"] is None and plan["space"]["free"] is None
    again = (await c.get(f"/api/v1/plans/{plan['id']}")).json()
    assert again["space"]["capacity"] is None and again["space"]["unknown"]
    assert plan["space"]["reserve"] < 10_000_000  # no safety margin taken from the made-up capacity
    await _apply(app, c, plan["id"], first_sync="keep")
    space = (await c.get(f"/api/v1/devices/{dev['id']}/space")).json()
    written = sum(p.stat().st_size for p in folder.rglob("*.mp3"))
    assert space["unknown"] and space["capacity"] is None and space["free"] is None
    assert written > 0 and space["used"] >= written

    _, body = _body("З квотою", quota_gb=1)
    dev = (await c.post("/api/v1/devices/register", json=body)).json()
    space = (await c.get(f"/api/v1/devices/{dev['id']}/space")).json()
    assert not space["unknown"] and space["capacity"] == 1000**3
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert not plan["space"]["unknown"] and plan["space"]["capacity"] == 1000**3


async def test_check_changed_settings_before_saving(env):
    """Device settings' Test and save logs in with the typed settings; the stored password is used
    when the mask comes back; a wrong one is refused with the server's reason and nothing is stored."""
    _app, c = env
    _, body = _body("Check NAS", username="sd", password="secret")
    dev = (await c.post("/api/v1/devices/register", json=body)).json()
    url = f"/api/v1/devices/{dev['id']}/check"
    r = await c.post(url, json={"settings": dev["settings"]})  # password = the mask
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    r = await c.post(url, json={"settings": {**dev["settings"], "password": "nope"}})
    assert r.status_code >= 400 and "cannot reach the server" in r.json()["message"], r.text
    assert "nope" not in r.text
    stored = (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]["settings"]
    assert stored == dev["settings"]
    r = await c.post("/api/v1/devices/nonexistent/check", json={"settings": {}})
    assert r.status_code == 404


async def test_scheduled_starts_skip_an_unreachable_server(env):
    """a network device gets updates every 6 hours by default; an automatic start logs in first -
    a server that does not answer is skipped quietly ("offline since" on the card, "not reachable" for Sync all)
    and the next slot tries again."""
    import datetime as dt
    import socket

    app, c = env
    _, body = _body("Offline NAS", username="sd", password="secret")
    dev = (await c.post("/api/v1/devices/register", json=body)).json()
    cfg = await app.store.device(dev["id"])
    assert cfg.update_every_min == 360
    port = cfg.settings["port"]
    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    await app.store.put_device(
        cfg.model_copy(update={"settings": {**cfg.settings, "port": closed}})
    )

    async def listed():
        regs = (await c.get("/api/v1/devices")).json()["registered"]
        return next(d for d in regs if d["id"] == dev["id"])

    assert await app.sync.periodic_updates(now=0) == []  # its interval starts now
    assert (
        await app.sync.periodic_updates(now=6 * 3600) == []
    )  # due, but the server does not answer
    since = (await listed())["offline_since"]
    assert since is not None
    assert await app.sync.periodic_updates(now=6 * 3600 + 60) == []  # waits for the next slot
    assert (await listed())["offline_since"] == since  # since the first miss
    out = (await c.post("/api/v1/devices/sync-all", json={"mode": "update"})).json()
    assert {"device_id": dev["id"], "name": "Offline NAS", "reason": "not reachable"} in out[
        "skipped"
    ]
    when = dt.datetime.now().astimezone().replace(second=0, microsecond=0)
    await app.store.put_device(
        (await app.store.device(dev["id"])).model_copy(update={"sync_at": when.strftime("%H:%M")})
    )
    assert await app.sync.scheduled_syncs(when) == []

    cfg = await app.store.device(dev["id"])
    await app.store.put_device(cfg.model_copy(update={"settings": {**cfg.settings, "port": port}}))
    assert await app.sync.periodic_updates(now=12 * 3600) == [dev["id"]]
    assert (await listed())["offline_since"] is None
    await app.sync._yield_update(dev["id"])
