"""long (Cyrillic) names and long status texts stay inside their boxes - the job view's header button, the
sidebar card's 'Last sync partial · some items failed' pill, the Templates list's 'Picks of <device>' badge and its
group heading count - at 1280 and 390 px."""

from __future__ import annotations

import os

import pytest

from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, run_on, shot
from tests.webui.test_device_cards import _api

pytestmark = [pytest.mark.ui, pytest.mark.slow]

NAME = "Стік b - Довга кирилична назва пристрою"
LIB = "Папка Тест - дуже довга назва бібліотеки"

# every element that must not be wider than its box (scrollWidth > clientWidth means clipped or overflowing)
INSIDE = """(sel) => [...document.querySelectorAll(sel)].filter((el) => el.offsetParent !== null)
  .map((el) => { const r = el.getBoundingClientRect(), p = el.parentElement.getBoundingClientRect();
    return { sel, over: el.scrollWidth > el.clientWidth + 1 || r.right > p.right + 1 || r.right > innerWidth + 1 }; })
  .filter((x) => x.over)"""


def _partial_device(url, app, tmp_path) -> tuple[str, str]:
    music = tmp_path / "music"
    make_music(music / "a.mp3", title="Пісня А", artist="Океан Ельзи", album="Земля")
    make_music(music / "b.mp3", title="Пісня Б", artist="Океан Ельзи", album="Земля")
    body = {"id": "papka", "type": "folder", "name": LIB, "settings": {"root": str(music)}}
    _api(url, "POST", "/libraries", json=body)
    (tmp_path / "stick").mkdir()
    body = {
        "name": NAME,
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "rule_set": {"template_ids": ["default.papka.all"]},
    }
    dev = _api(url, "POST", "/devices/register", json=body)["id"]
    items = _api(url, "GET", "/libraries/papka/items")["items"]
    _api(
        url,
        "POST",
        f"/devices/{dev}/manual-selection",
        json={"library_id": "papka", "ids": [items[0]["item_id"]]},
    )
    plan = _api(url, "POST", f"/devices/{dev}/plan", json={})
    os.chmod(music / "b.mp3", 0)  # one item fails → "partial"
    try:
        job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
        run_on(app, app.jobs.wait(job["id"]), 60)
    finally:
        os.chmod(music / "b.mp3", 0o644)
    return dev, job["id"]


@pytest.mark.parametrize("width", [1280, 390])
def test_long_names_stay_inside_their_boxes(web_server, chrome, tmp_path, width):
    url, app = web_server
    dev, job = _partial_device(url, app, tmp_path)

    page = open_page(chrome, f"{url}/#/devices/{dev}/sync/{job}", width)
    open_btn = page.locator(".job-head > a.btn")
    open_btn.wait_for()
    assert open_btn.inner_text() == "Open device"  # the name is in the heading already
    assert page.evaluate(INSIDE, ".job-head .btn") == []
    shot(page, f"long-names-job-{width}")

    if width == 1280:  # the sidebar is a drawer on the phone
        page.goto(f"{url}/#/")
        card = page.locator(f'[data-device-card="{dev}"][data-state="partial"]')
        card.wait_for(timeout=15_000)
        # measured in one go on the card in the page: it re-renders on device / job events (like 463)
        page.wait_for_function(
            """(sel) => {
              const card = document.querySelector(sel), pill = card?.querySelector('.status');
              return !!pill && pill.getBoundingClientRect().right <= card.getBoundingClientRect().right + 1;
            }""",
            arg=f'[data-device-card="{dev}"][data-state="partial"]',
        )
        # a wrapped pill is tall enough for its lines (not clipped by the rounded box)
        assert page.evaluate(INSIDE, "[data-device-card] .status") == []
        page.wait_for_function(
            "(sel) => { const el = document.querySelector(sel); return !!el && el.scrollHeight <= el.clientHeight + 1; }",
            arg=f'[data-device-card="{dev}"] .status',
        )
        shot(page, f"long-names-sidebar-{width}")

    page.goto(f"{url}/#/templates")
    own = page.locator(f'[data-tpl="own-{dev}"]')
    own.wait_for()
    assert f"Picks of {NAME}" in own.inner_text()
    assert page.evaluate(INSIDE, ".tpl-badge-link") == []
    assert page.evaluate(INSIDE, ".tpl-group-head span") == []
    assert no_horizontal_scroll(page)
    shot(page, f"long-names-templates-{width}")
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
