# SPDX-License-Identifier: GPL-3.0-or-later
"""Downloading the Compás app zip from SyncMusic's release: checksum, zip read back, nothing kept on failure."""

import asyncio
import hashlib
import io
import zipfile

import httpx
import pytest

from syncmusic.infra import client_apps
from syncmusic.infra.updates import Release
from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


GOOD = _zip({"syncmusic/syncmusic-compas": b"\x7fELF app", "syncmusic/app.json": b"{}"})


def _releases(assets: dict[str, dict[str, bytes]], sums_for: bytes | None = None):
    """One release per tag with its assets and a SHA256SUMS; `sums_for` replaces every checksummed body."""
    files: dict[str, bytes] = {}
    out = []
    for tag, named in assets.items():
        sums = "".join(
            f"{hashlib.sha256(b if sums_for is None else sums_for).hexdigest()}  {n}\n"
            for n, b in named.items()
        )
        for n, b in {**named, "SHA256SUMS": sums.encode()}.items():
            files[f"/{tag}/{n}"] = b
        out.append(
            Release(
                tag,
                False,
                None,
                [],
                None,
                [
                    {"name": n, "url": f"https://dl.test/{tag}/{n}", "size": 1}
                    for n in [*named, "SHA256SUMS"]
                ],
            )
        )

    def handler(request):
        return httpx.Response(200, content=files[request.url.path])

    return out, httpx.MockTransport(handler)


def test_the_newest_app_zip_of_any_release_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(client_apps, "running_binary", lambda: None)
    releases, transport = _releases(
        {
            "0.4.0": {"syncmusic-compas-1.2.0.zip": GOOD, "other.zip": b"x"},
            "0.3.0": {"syncmusic-compas-1.1.0.zip": GOOD},
        }
    )
    got = asyncio.run(client_apps.install(releases, "syncmusic-compas", tmp_path, transport))
    assert got.version == "1.2.0" and got.zip == tmp_path / "tools" / "syncmusic-compas-1.2.0.zip"
    assert {f.name for f in got.files} == {"syncmusic-compas", "app.json"}
    assert client_apps.newest("syncmusic-compas", tmp_path).version == "1.2.0"
    assert client_apps.offer("syncmusic-compas", "1.1.0", tmp_path) is not None


@pytest.mark.parametrize(
    ("body", "sums_for", "error"),
    [
        (GOOD, b"other", "checksum"),
        (_zip({"a/../evil": b"x"}), None, "not a usable app zip"),
        (b"not a zip", None, "not a usable app zip"),
    ],
    ids=["checksum", "path-escape", "not-a-zip"],
)
def test_a_bad_download_keeps_nothing(tmp_path, body, sums_for, error):
    releases, transport = _releases({"0.4.0": {"syncmusic-compas-1.2.0.zip": body}}, sums_for)
    with pytest.raises(client_apps.AppDownloadError, match=error):
        asyncio.run(client_apps.install(releases, "syncmusic-compas", tmp_path, transport))
    assert list((tmp_path / "tools").iterdir()) == []


def test_no_release_offers_the_app(tmp_path):
    with pytest.raises(client_apps.AppDownloadError, match="no SyncMusic release"):
        asyncio.run(client_apps.install([], "syncmusic-compas", tmp_path))
    with pytest.raises(client_apps.AppDownloadError, match="unknown app"):
        asyncio.run(client_apps.install([], "nope", tmp_path))


async def test_settings_api_status_and_download(env, monkeypatch):  # noqa: F811
    app, c = env
    monkeypatch.setattr(client_apps, "running_binary", lambda: None)
    got = (await c.get("/api/v1/settings/compas-app")).json()
    assert got["version"] is None and got["app"] == "syncmusic-compas"
    releases, transport = _releases({"0.4.0": {"syncmusic-compas-1.2.0.zip": GOOD}})

    async def fake_releases(repo):
        return releases

    real = client_apps.install
    monkeypatch.setattr(app.update_checker, "releases", fake_releases)
    monkeypatch.setattr(
        client_apps, "install", lambda rel, name, data: real(rel, name, data, transport)
    )
    r = await c.post("/api/v1/settings/compas-app/download")
    assert r.status_code == 400 and "turned off" in r.json()["message"]
    monkeypatch.setattr(app.config, "update_repo", "example/SyncMusic")
    r = await c.post("/api/v1/settings/compas-app/download")
    assert r.status_code == 202, r.text
    job = await app.jobs.wait(r.json()["id"])
    assert job.status == "succeeded", job.error
    assert job.result["version"] == "1.2.0"
    got = (await c.get("/api/v1/settings/compas-app")).json()
    assert got["version"] == "1.2.0" and got["path"].endswith("syncmusic-compas-1.2.0.zip")
