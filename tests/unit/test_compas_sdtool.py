# SPDX-License-Identifier: GPL-3.0-or-later
"""The compas-sdtool wrapper: locating it, its JSON-lines output, failures, the format gate."""

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from syncmusic.devices.HibyR1 import compas_feedback as cf
from syncmusic.devices.HibyR1 import compas_sdtool as sd
from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures


def _fake(tmp_path: Path, body: str, name: str = "compas-sdtool") -> Path:
    tool = tmp_path / name
    tool.write_text("#!/bin/sh\n" + body)
    tool.chmod(0o755)
    return tool


OK = """case "$1" in
info) echo '{"sdtool_version":"1.1.0","build":"tools-a+player-b","database_format":8,"generation":0,"songs":0}' ;;
scan) echo '{"event":"progress","phase":"tags","done":1,"total":2}'
      echo 'not json'
      echo '{"event":"progress","phase":"tags","done":2,"total":2}'
      echo '{"event":"done","command":"scan","files":2,"songs":2,"written":true}' ;;
esac
"""


def test_locate_order(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNCMUSIC_BINARY", raising=False)
    data = tmp_path / "data"
    assert sd.locate(None, data) is None
    (data / "tools").mkdir(parents=True)
    in_data = _fake(data / "tools", OK, sd.asset_name())
    assert sd.locate(None, data) == in_data
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("SYNCMUSIC_BINARY", str(bindir / "syncmusic"))
    beside = _fake(bindir, OK)
    assert sd.locate(None, data) == beside
    assert sd.locate(str(in_data), data) == in_data  # the Settings path wins, and only it
    assert sd.locate(str(tmp_path / "nope"), data) is None
    beside.chmod(0o644)
    assert sd.locate(None, data) == in_data  # not executable: skipped


def test_info_scan_and_progress(tmp_path):
    tool = _fake(tmp_path, OK)
    got = asyncio.run(sd.info(tool, tmp_path))
    assert (got.version, got.build, got.database_format) == ("1.1.0", "tools-a+player-b", 8)
    seen = []
    done = asyncio.run(sd.scan(tool, tmp_path, lambda *a: seen.append(a)))
    assert done["songs"] == 2 and seen == [("tags", 1, 2), ("tags", 2, 2)]
    assert sd.unusable(got, 8) is None and sd.unusable(got, None) is None
    assert "format 8" in sd.unusable(got, 9)


@pytest.mark.parametrize(
    ("body", "msg"),
    [
        ("""echo '{"event":"error","code":"usage","message":"no card"}'; exit 2""", "no card"),
        ("exit 3", "exited with 3"),
        ("sleep 5", "longer than"),
        ("echo '{}'", "without a result"),
    ],
)
def test_failures(tmp_path, body, msg):
    tool = _fake(tmp_path, body)
    with pytest.raises(sd.SdtoolError, match=msg):
        asyncio.run(sd.scan(tool, tmp_path, timeout=1))


def test_a_missing_binary(tmp_path):
    with pytest.raises(sd.SdtoolError, match="does not start"):
        asyncio.run(sd.info(tmp_path / "missing", tmp_path))


REAL = os.environ.get("COMPAS_SDTOOL") or shutil.which("compas-sdtool")


@pytest.mark.skipif(not REAL, reason="set COMPAS_SDTOOL to a built compas-sdtool")
def test_the_real_tool_indexes_a_card(tmp_path):
    from tests.media import make_music

    (tmp_path / ".compas").mkdir()
    make_music(
        tmp_path / "Music" / "Гурт" / "01 Пісня.mp3", title="Пісня", artist="Гурт", album="Альбом"
    )
    tool = Path(REAL)
    assert asyncio.run(sd.info(tool, tmp_path)).database_format == 8
    done = asyncio.run(sd.scan(tool, tmp_path))
    assert done["songs"] == 1
    assert [e.path for e in cf.read_tagcache(tmp_path)] == ["Music/Гурт/01 Пісня.mp3"]


async def _sync_card(app, c, mount):
    from tests.unit.test_api_sync import _apply

    body = {
        "name": mount.name,
        "type": "hiby_compas",
        "mount_path": str(mount),
        "library_ids": ["music"],
    }
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 201, r.text
    plan = (await c.post(f"/api/v1/devices/{r.json()['id']}/plan", json={})).json()
    return await _apply(app, c, plan["id"], first_sync="keep")


async def test_sync_runs_the_tool_or_says_it_is_missing(env, tmp_path, monkeypatch):  # noqa: F811
    from tests.contracts.harness import compas_tree

    app, c = env
    monkeypatch.delenv("SYNCMUSIC_BINARY", raising=False)
    calls = tmp_path / "calls"
    tool = _fake(tmp_path, f'echo "$1" >> {calls}\n' + OK)
    sd.configure(str(tool))
    try:
        report = await _sync_card(app, c, compas_tree(tmp_path / "a"))
    finally:
        sd.configure(None)
    assert calls.read_text().split() == ["info", "scan"]
    assert not any("compas-sdtool" in w for w in report["warnings"])
    report = await _sync_card(app, c, compas_tree(tmp_path / "b"))  # no tool anywhere
    assert any("compas-sdtool was not found" in w for w in report["warnings"])


@pytest.mark.skipif(not REAL, reason="set COMPAS_SDTOOL to a built compas-sdtool")
async def test_sync_builds_the_players_library(env, tmp_path):  # noqa: F811
    from tests.contracts.harness import compas_tree

    app, c = env
    sd.configure(REAL)
    try:
        mount = compas_tree(tmp_path / "card")
        report = await _sync_card(app, c, mount)
    finally:
        sd.configure(None)
    assert report["status"] == "succeeded", report
    indexed = {e.path for e in cf.read_tagcache(mount)}
    on_card = {p.relative_to(mount).as_posix() for p in (mount / "Music").rglob("*.mp3")}
    assert on_card and on_card <= indexed


def _release_server(binary: bytes, meta: dict, sums_for: bytes | None = None):
    import hashlib
    import json as _json

    import httpx

    from syncmusic.infra.updates import Release

    name = sd.asset_name()
    meta_raw = _json.dumps(meta).encode()
    sums = "".join(
        f"{hashlib.sha256(b).hexdigest()}  {n}\n"
        for n, b in ((name, binary if sums_for is None else sums_for), (f"{name}.json", meta_raw))
    )
    files = {f"/{name}": binary, f"/{name}.json": meta_raw, "/SHA256SUMS": sums.encode()}

    def handler(request):
        return httpx.Response(200, content=files[request.url.path])

    assets = [{"name": n.strip("/"), "url": f"https://dl.test{n}", "size": 1} for n in files]
    rel = Release("0.3.0", False, None, [], None, assets)
    return [rel], httpx.MockTransport(handler)


INFO_ONLY = """echo '{"sdtool_version":"1.1.0","build":"tools-a+player-b","database_format":8}'\n"""


@pytest.mark.parametrize(
    ("meta", "bad_sum", "error"),
    [
        ({"build": "tools-a+player-b", "database_format": 8}, False, None),
        ({"build": "tools-a+player-b", "database_format": 8}, True, "checksum"),
        ({"build": "tools-z+player-b", "database_format": 8}, False, "release says"),
    ],
)
def test_install_from_the_release(tmp_path, meta, bad_sum, error):
    binary = ("#!/bin/sh\n" + INFO_ONLY).encode()
    releases, transport = _release_server(binary, meta, b"other" if bad_sum else None)
    data = tmp_path / "data"
    run = sd.install(releases, "0.3.0", data, transport)
    tools = data / "tools"
    if error:
        with pytest.raises(sd.SdtoolError, match=error):
            asyncio.run(run)
        assert sorted(p.name for p in tools.iterdir()) == []  # nothing kept
        return
    got = asyncio.run(run)
    assert got.path == tools / sd.asset_name() and got.database_format == 8
    assert sd.locate(None, data) == got.path
    assert sorted(p.name for p in tools.iterdir()) == [sd.asset_name(), f"{sd.asset_name()}.json"]


def test_no_release_has_the_tool():
    from syncmusic.infra.updates import Release

    with pytest.raises(sd.SdtoolError, match="no SyncMusic release"):
        asyncio.run(sd.install([Release("0.3.0", False, None, [], None, [])], "0.3.0", Path("/x")))


async def test_settings_api_status_and_download(env, tmp_path, monkeypatch):  # noqa: F811
    app, c = env
    monkeypatch.delenv("SYNCMUSIC_BINARY", raising=False)
    got = (await c.get("/api/v1/settings/compas-sdtool")).json()
    assert got["error"] == "not found" and got["license"] == "GPL-3.0"
    assert "compas-tools" in got["source_url"] and got["asset"] == sd.asset_name()
    r = await c.patch("/api/v1/settings", json={"compas_sdtool_path": "/usr/bin/ls"})
    assert r.status_code in (400, 422)

    binary = ("#!/bin/sh\n" + INFO_ONLY).encode()
    releases, _transport = _release_server(
        binary, {"build": "tools-a+player-b", "database_format": 8}
    )

    async def fake_releases(repo):
        return releases

    real_install = sd.install
    monkeypatch.setattr(app.update_checker, "releases", fake_releases)
    monkeypatch.setattr(
        sd, "install", lambda rel, running, data: real_install(rel, running, data, _transport)
    )
    r = await c.post("/api/v1/settings/compas-sdtool/download")
    # tests run without a release repo
    assert r.status_code == 400 and "turned off" in r.json()["message"]
    monkeypatch.setattr(app.config, "update_repo", "example/SyncMusic")
    r = await c.post("/api/v1/settings/compas-sdtool/download")
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    got = (await c.get("/api/v1/settings/compas-sdtool")).json()
    assert (
        got["error"] is None and got["database_format"] == 8 and got["build"] == "tools-a+player-b"
    )
