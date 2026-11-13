"""A Wi-Fi client end to end over real HTTP: a throwaway daemon with the compose Navidrome as library, the reference
client pairs, the conformance checks pass, it syncs two tracks into a folder, and its play and favourite reach
Navidrome on the next sync (the worker's plays and stars are restored by the `services` fixture)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from syncmusic.client_conformance import run
from syncmusic.reference_client import ReferenceClient
from tests.integration.test_navidrome_local import ENV, navidrome  # noqa: F401 - fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif("NAVIDROME_URL" not in ENV, reason="no Navidrome credentials in .env.test"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def daemon(tmp_path):
    port = _free_port()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("SYNCMUSIC_")},
        "SYNCMUSIC_RUNTIME_DIR": str(tmp_path / "rt"),
        "SYNCMUSIC_DATA_DIR": str(tmp_path / "data"),
        "SYNCMUSIC_CACHE_DIR": str(tmp_path / "cache"),
        "SYNCMUSIC_LOG_DIR": str(tmp_path / "log"),
        "SYNCMUSIC_HOST": "127.0.0.1",
        "SYNCMUSIC_PORT": str(port),
        "SYNCMUSIC_DETECT_DEVICES": "false",
        "SYNCMUSIC_CONFIG": os.devnull,
    }
    log = (tmp_path / "out.log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "syncmusic", "run", "--mode", "daemon"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        end = time.monotonic() + 120
        while True:
            assert proc.poll() is None, (tmp_path / "out.log").read_text(errors="replace")[-2000:]
            try:
                if httpx.get(f"{url}/api/v1/settings", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < end, "the daemon did not answer"
            time.sleep(0.5)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


async def _client(api: httpx.AsyncClient, url: str, folder, template: str) -> ReferenceClient:
    rc = ReferenceClient(folder)
    code = (await api.post("/clients/pairings")).json()["code"]
    device_id = await rc.pair(url + "/", code, folder.name)
    r = await api.patch(
        f"/devices/{device_id}",
        json={
            "rule_set": {"template_ids": [template]},
            "delete_threshold_pct": 100,
        },
    )
    assert r.status_code == 200, r.text
    return rc


async def test_client_over_http_with_navidrome(daemon, navidrome, tmp_path):  # noqa: F811
    async with httpx.AsyncClient(base_url=f"{daemon}/api/v1", timeout=120) as api:
        nd = navidrome.config.settings
        r = await api.post(
            "/libraries",
            json={"id": "nd", "type": "subsonic", "name": "Navidrome", "settings": nd},
        )
        assert r.status_code == 201, r.text
        snap = await navidrome.snapshot()
        picks = sorted(snap.tracks, key=lambda i: snap.tracks[i].size_bytes)[:2]
        r = await api.patch("/settings", json={"clients_allowed": True, "clients_mdns": False})
        assert r.status_code == 200, r.text
        r = await api.post("/templates", json={"name": "Phone", "library_ids": ["nd"], "rules": []})
        assert r.status_code < 300, r.text
        template = r.json()["id"]
        r = await api.post(
            f"/templates/{template}/manual-selection", json={"library_id": "nd", "ids": picks}
        )
        assert r.status_code == 200, r.text

        probe = await _client(api, daemon, tmp_path / "conformance", template)
        try:
            report = await run(probe, None)
        finally:
            await probe.close()
        assert report.ok, report.results
        assert report.by_name()["file_download"] == "pass"

        rc = await _client(api, daemon, tmp_path / "phone", template)
        try:
            assert (await rc.sync())["fetched"] >= 2
            files = sorted(f for f in rc.state["keys"] if f.endswith(".mp3"))
            assert len(files) == 2, files
            before = snap.tracks[picks[0]]
            # which synced file is picks[0]: the one whose key names that item
            key = next((k for k in rc.state["keys"].values() if picks[0] in k), None)
            assert key, rc.state["keys"]
            rc.record("play", key, completed=True)
            rc.record("favourite", key, on=True)
            await rc.sync()  # events first, then the sync's reverse step hands them to Navidrome
            after = (await navidrome.snapshot()).tracks[picks[0]]
            assert after.play_count == before.play_count + 1
            assert after.starred
        finally:
            await rc.close()
