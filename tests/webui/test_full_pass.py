"""Full pass over the web UI (.x → 176): every screen at phone and desktop width with real data - renders a
heading, no horizontal page scroll, no script errors; screenshots into $SYNCMUSIC_SHOTS."""

from __future__ import annotations

import time

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.e2e, pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.fixture
def seeded(web_server, tmp_path):
    url, app = web_server
    music = tmp_path / "music"
    for artist, title in (("Скрябін", "Група крові"), ("Скрябін", "Зозуля"), ("Тартак", "Орбіт")):
        make_music(
            music / artist / f"{title}.mp3", title=title, artist=artist, album=f"{artist} album"
        )
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    (tmp_path / "stick").mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={
            "name": "Stick",
            "mount_path": str(tmp_path / "stick"),
            "type": "flashdrive",
            "library_ids": ["music"],
            "rule_set": {"template_ids": ["default.music.all"]},
        },
    )
    plan = _api(url, "POST", "/devices/stick/plan")
    job = _api(
        url, "POST", f"/plans/{plan['id']}/apply", json={"confirm": True, "first_sync": "keep"}
    )
    for _ in range(300):
        if _api(url, "GET", f"/jobs/{job['id']}")["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    plan2 = _api(url, "POST", "/devices/stick/plan")
    return url, job["id"], plan2["id"]


SCREENS = [
    ("dashboard", "/"),
    ("libraries", "/libraries"),
    ("library-new", "/libraries/new"),
    ("library-settings", "/libraries/music"),
    ("library-browse", "/libraries/music/browse"),
    ("devices", "/devices"),
    ("register", "/devices/register"),
    *[
        (f"device-{tab}", f"/devices/stick/{tab}")
        for tab in ("overview", "libraries", "rules", "processors", "settings", "history")
    ],
    ("plan", "/devices/stick/plan/{plan}"),
    ("report", "/devices/stick/sync/{job}"),
    ("templates", "/templates"),
    ("template-new", "/templates/new"),
    ("template-default", "/templates/default.music.all"),
    ("settings", "/settings"),
]


@pytest.mark.parametrize("width", [390, 1280])
def test_full_pass(seeded, chrome, width):
    url, job_id, plan_id = seeded
    page = open_page(chrome, url, width, 844 if width < 600 else 900)
    for name, path in SCREENS:
        route = path.format(job=job_id, plan=plan_id)
        page.goto(f"{url}/#{route}")
        page.wait_for_function(f"document.getElementById('content').dataset.route === {route!r}")
        page.wait_for_selector("#content h1")
        assert page.locator("#content .errorbox").count() == 0, (
            name,
            page.locator("#content").inner_text()[:300],
        )
        assert no_horizontal_scroll(page), name
        shot(page, f"full-{width}-{name}")
    assert page.errors == []
