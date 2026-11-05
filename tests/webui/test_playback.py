"""Player contents ▶ in Chromium and Firefox: an ALAC m4a (Firefox cannot decode ALAC: served as MP3), an
AAC m4a and an MP3 really play (currentTime advances); a file that cannot be played shows an error. Temp folders only."""

from __future__ import annotations

import subprocess

import httpx
import pytest

from syncmusic.infra import ffmpeg
from tests.media import make_music
from tests.webui.conftest import run_on
from tests.webui.test_ui_regressions import _folder_device

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.fixture(params=["chromium", "firefox"])
def player_browser(request):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        if request.param == "firefox":
            try:
                b = p.firefox.launch(firefox_user_prefs={"media.autoplay.default": 0})
            except Exception as e:  # noqa: BLE001 - not installed: `uv run playwright install firefox`
                pytest.skip(f"Playwright Firefox: {e}")
        else:
            b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        yield b
        b.close()


DECODE = """async () => {
    const src = document.getElementById('pcAudio').src;
    const data = await (await fetch(src)).arrayBuffer();
    try {
        const buf = await new OfflineAudioContext(2, 44100, 44100).decodeAudioData(data);
        return { ok: true, seconds: buf.duration };
    } catch (e) { return { ok: false, error: String(e) }; }
}"""


def _synced_device(url, app, tmp_path) -> str:
    music = tmp_path / "music"
    make_music(music / "a" / "mp3.mp3", title="Mp3 song", artist="A", album="A")
    make_music(music / "a" / "broken.mp3", title="Broken song", artist="A", album="A")
    wav = make_music(tmp_path / "src.wav", title="x")
    # two AAC songs; the device file of "Alac song" becomes ALAC below (a flash drive takes no ALAC, an iPod does)
    for title in ("Aac song", "Alac song"):
        tags = ["-metadata", f"title={title}", "-metadata", "artist=A", "-metadata", "album=A"]
        out = music / "a" / f"{title}.m4a"
        subprocess.run(
            [ffmpeg.FFMPEG, "-v", "error", "-i", str(wav), "-c:a", "aac", *tags, str(out)],
            check=True,
        )
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    dev = _folder_device(url, tmp_path)
    ids = [i["item_id"] for i in _api(url, "GET", "/libraries/music/items")["items"]]
    _api(
        url,
        "POST",
        f"/devices/{dev}/manual-selection",
        json={"library_id": "music", "ids": ids, "field": "track_id"},
    )
    plan = _api(url, "POST", f"/devices/{dev}/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 120).status == "succeeded"
    return dev


def test_player_contents_plays_alac_aac_and_mp3(web_server, player_browser, tmp_path):
    url, app = web_server
    dev = _synced_device(url, app, tmp_path)
    rows = _api(url, "GET", f"/devices/{dev}/contents")
    assert len(rows) == 4, rows
    alac = make_music(tmp_path / "alac.m4a", title="Alac song", artist="A", album="A")
    on_device = tmp_path / "stick" / next(r["path"] for r in rows if r["title"] == "Alac song")
    on_device.write_bytes(alac.read_bytes())
    page = player_browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(url + f"/#/devices/{dev}/contents")
    page.wait_for_selector(f'#content[data-route="/devices/{dev}/contents"]')
    for title in ("Alac song", "Aac song", "Mp3 song"):
        page.get_by_role("button", name=f"Play {title}").click()
        # the browser's own decoder takes what the player got (headless Firefox has no audio output, so the
        # <audio> element itself cannot be watched there; ALAC failed to decode in Firefox)
        decoded = page.evaluate(DECODE)
        assert decoded["ok"] and decoded["seconds"] > 2.5, (title, decoded)
        if player_browser.browser_type.name == "chromium":
            page.wait_for_function(
                "() => { const a = document.getElementById('pcAudio'); return !a.error && a.currentTime > 0.3; }",
                timeout=30000,
            )
        page.evaluate("() => document.getElementById('pcAudio').pause()")
    # a broken file: an error toast instead of nothing
    broken = next(r for r in rows if r["title"] == "Broken song")
    (tmp_path / "stick" / broken["path"]).write_bytes(b"not audio")
    page.get_by_role("button", name="Play Broken song").click()
    page.get_by_text("Cannot play Broken song in the browser.").wait_for(timeout=15000)
