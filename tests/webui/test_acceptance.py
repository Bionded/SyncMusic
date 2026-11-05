"""Acceptance of the web UI (→ 176, the developer notes M8): the whole flow at phone width through the UI only -
webhook in Settings, add a folder library, register a folder device in the wizard, a rule in the builder, plan,
first-sync choice, apply, report - the webhook firing; the same flow from the CLI; desktop mode with a graceful
window close. `uv run pytest -m e2e`. The API is used only to check results."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from tests.media import make_music
from tests.unit.test_api_sync import music  # noqa: F401 - fixture of the CLI server
from tests.unit.test_cli_api import server as cli_server  # noqa: F401 - fixture
from tests.unit.test_cli_api import test_devices_sync_jobs_logs as _cli_flow
from tests.webui.conftest import (
    add_templates,
    no_horizontal_scroll,
    open_page,
    poll,
    save_templates,
    shot,
)
from tests.webui.test_desktop_window import (
    test_desktop_window_shows_the_ui_and_close_stops_the_app as _desktop_flow,
)
from tests.webui.xvfb import display  # noqa: F401 - fixture

pytestmark = [pytest.mark.e2e, pytest.mark.ui, pytest.mark.slow]


@pytest.fixture
def webhook():
    """A local receiver; yields (url, list of received JSON payloads)."""
    got: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            got.append(json.loads(body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/hook", got
    srv.shutdown()


def test_phone_width_web_flow_and_webhook(web_server, chrome, webhook, tmp_path):
    url, app = web_server
    hook_url, received = webhook
    songs = tmp_path / "songs"
    make_music(songs / "Скрябін" / "Зозуля.mp3", title="Зозуля", artist="Скрябін", album="Зірка")
    make_music(songs / "Скрябін" / "Зірка.mp3", title="Зірка", artist="Скрябін", album="Зірка")
    make_music(songs / "Тартак" / "Орбіт.mp3", title="Орбіт", artist="Тартак", album="Сигнал")
    (tmp_path / "player").mkdir()
    page = open_page(chrome, url, 390, 844)

    class _Nav:  # the burger drawer of
        def __init__(self, name: str) -> None:
            self.sel = "#nav [data-nav-all]" if name == "libraries" else f"#nav [data-nav={name}]"

        def click(self) -> None:
            page.locator("#burger").click()
            page.wait_for_function("document.body.classList.contains('menu-open')")
            page.locator(self.sel).click()

    nav = _Nav

    # webhook in Settings
    nav("settings").click()
    page.wait_for_selector('input[id$="webhook_url"]')
    page.fill('input[id$="webhook_url"]', hook_url)
    page.get_by_role("button", name="Save").first.click()
    page.wait_for_selector(".toast:has-text('Settings saved')")

    # library
    nav("libraries").click()
    page.get_by_role("link", name="Add a library").first.click()
    page.locator('[data-lib-type="folder"]').click()
    page.wait_for_selector('input[id$="root"]')
    page.fill("#libName", "Пісні")
    page.fill('input[id$="root"]', str(songs))
    page.get_by_role("button", name="Save and scan").click()
    page.wait_for_selector(".toast:has-text('added and reachable')")

    # device: registration wizard for a folder
    nav("devices").click()
    page.get_by_role("link", name="Register a device").click()
    page.wait_for_selector("#regPath:visible")
    page.fill("#regPath", str(tmp_path / "player"))
    page.fill("#regName", "Плеєр")
    # no template yet; nothing is preselected, so the processors are chosen too
    page.select_option("#regProcTemplate", "builtin-flashdrive")
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.includes('/overview')")
    dev = httpx.get(f"{url}/api/v1/devices").json()["registered"][0]
    assert dev["name"] == "Плеєр"
    cfg = httpx.get(f"{url}/api/v1/devices/{dev['id']}").json()["config"]
    assert cfg["library_ids"] == [] and cfg["rule_set"]["template_ids"] == []

    # rules: a template "Скрябін" (artist contains Скрябін) in the template editor, then linked on the device's Rules tab,
    # tested and saved (a device has templates only)
    page.goto(url + "/#/templates/new")
    page.wait_for_selector('[data-rule="rules:0"]')
    page.fill("#tplName", "Скрябін")
    page.locator('[data-rule="rules:0"] [data-k="name"]').fill("Скрябін")
    page.get_by_role("button", name="Add a condition").click()
    page.locator('[data-rule="rules:0"] [data-cond="0"] [data-c="value"]').fill("Скрябін")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    tpl = next(x for x in httpx.get(f"{url}/api/v1/templates").json() if x["name"] == "Скрябін")
    assert tpl["rules"][0]["criteria"] == {"all": [{"contains": {"artist": "Скрябін"}}]}
    page.goto(f"{url}/#/devices/{dev['id']}/rules")
    page.wait_for_selector("[data-tpls]")
    add_templates(page, tpl["id"])
    page.wait_for_selector(f'[data-tpl-sum="{tpl["id"]}"]:has-text("2 items")')
    save_templates(page)
    cfg = httpx.get(f"{url}/api/v1/devices/{dev['id']}").json()["config"]
    assert cfg["rule_set"]["template_ids"] == [tpl["id"]]
    assert no_horizontal_scroll(page)

    # plan, first-sync choice, apply, report
    page.locator('.tabs a:has-text("Overview")').click()
    page.locator("[data-sync]").click()
    page.wait_for_selector('[data-count="add"] strong')
    assert page.locator('[data-count="add"] strong').inner_text() == "2"
    page.check('input[name="firstSync"][value="keep"]')
    page.get_by_role("button", name="Apply").first.click()
    page.wait_for_selector("[data-report]", timeout=60_000)
    assert page.locator("[data-report]").get_attribute("data-outcome") == "succeeded"
    shot(page, "acceptance-report-390")
    assert sorted(p.name for p in (tmp_path / "player").rglob("*.mp3")) == [
        "Зозуля.mp3",  # sorted by code point: "о" (U+043E) before "і" (U+0456)
        "Зірка.mp3",
    ]
    assert no_horizontal_scroll(page)

    # webhook fired
    poll(
        page,
        lambda: any(p["event"] == "sync.finished" for p in received),
        what="the sync.finished webhook",
    )
    assert page.errors == []


def test_cli_flow(cli_server, tmp_path):  # noqa: F811
    _cli_flow(cli_server, tmp_path)


def test_desktop_window_close(display, tmp_path):  # noqa: F811
    _desktop_flow(display, tmp_path)
