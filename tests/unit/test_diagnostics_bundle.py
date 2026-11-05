# ruff: noqa: F811 - fixtures imported from test_api_sync

"""The diagnostics bundle: a zip with the expected parts and no secret value anywhere."""

from __future__ import annotations

import io
import zipfile

from syncmusic.core.models import LibraryConfig
from syncmusic.infra.bundle import scrub
from tests.unit.test_api_sync import env, music  # noqa: F401

PASSWORD = "s3cret-Navi-pass"
WEBHOOK = "https://hooks.example/token-abc123xyz"


def test_scrub():
    assert scrub(f"pw={PASSWORD} again {PASSWORD}", [PASSWORD, ""]) == "pw=******** again ********"


async def test_bundle_has_parts_and_no_secrets(env):
    app, c = env
    await app.store.put_library(
        LibraryConfig(
            id="navi",
            type="subsonic",
            name="Navi",
            settings={"url": "http://navi.local:4533", "username": "u", "password": PASSWORD},
        )
    )
    s = await app.store.app_settings()
    await app.store.put_app_settings(s.model_copy(update={"webhook_url": WEBHOOK}))
    # a secret that slipped into the log is scrubbed as well
    app.config.logs.mkdir(parents=True, exist_ok=True)
    (app.config.logs / "syncmusic.log").write_text(f"oops {PASSWORD} {WEBHOOK}\n")

    r = await c.get("/api/v1/diagnostics/bundle")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "syncmusic-diagnostics-" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {
        "system.json",
        "config.json",
        "settings.json",
        "libraries.json",
        "devices.json",
        "checks.json",
        "reports.json",
        "log-tail.txt",
    } <= names
    text = "".join(z.read(n).decode() for n in names)
    assert PASSWORD not in text and "token-abc123xyz" not in text
    assert "navi.local" in text  # the library host stays
    assert not any(n.endswith((".db", ".mp3", ".flac")) for n in names)
