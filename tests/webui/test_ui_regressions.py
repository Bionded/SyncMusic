"""Web UI regressions: each test reproduces a defect once found by hand on a temp instance. Temp folders only;
the test media is never touched."""

from __future__ import annotations

import asyncio
import re
import time

import httpx
import pytest

from syncmusic.facade import AppFacade
from tests.media import make_music
from tests.webui.conftest import animations_done, no_horizontal_scroll, open_page, run_on, settle

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _folder_library(url, tmp_path) -> str:
    music = tmp_path / "music"
    make_music(music / "Скрябін" / "Зозуля.mp3", title="Зозуля", artist="Скрябін", album="Зірка")
    r = httpx.post(
        f"{url}/api/v1/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
        timeout=60,
    )
    assert r.status_code == 201, r.text
    return "music"


def test_navigation_shows_loading_instead_of_the_previous_screen(
    web_server, chrome, tmp_path, monkeypatch
):
    """A slow screen (a Navidrome Browse took ~9 s) must not leave the previous screen on display, with
    controls that no longer do anything."""
    slow = AppFacade.library_items

    async def slow_items(self, *a, **kw):
        await asyncio.sleep(4)
        return await slow(self, *a, **kw)

    monkeypatch.setattr(AppFacade, "library_items", slow_items)
    url, _ = web_server
    lib = _folder_library(url, tmp_path)
    page = open_page(chrome, url + "/#/settings")
    page.wait_for_selector('#content[data-route="/settings"]')
    page.evaluate(f"location.hash = '#/libraries/{lib}/browse'")
    # the settings are gone at once, not only when the slow browse view has loaded
    page.wait_for_function(
        "document.querySelector('#content h1')?.innerText !== 'Settings'"
        " && !document.querySelector('#content').innerText.includes('Cache size limit')",
        timeout=10_000,
    )
    heading = page.evaluate("document.querySelector('#content h1')?.innerText ?? ''")
    shown = page.evaluate("document.querySelector('#content').innerText")
    assert heading != "Settings" and "Cache size limit" not in shown, (heading, shown[:200])
    page.wait_for_selector(f'#content[data-route="/libraries/{lib}/browse"]', timeout=15000)
    assert not page.errors


def test_dashboard_add_a_library_opens_the_form(web_server, chrome, tmp_path):
    # the design: Add a library is the empty dashboard's action
    url, _ = web_server
    page = open_page(chrome, url + "/#/")
    page.wait_for_selector('#content[data-route="/"]')
    page.locator("#content").get_by_role("link", name="Add a library").click()
    page.wait_for_function("location.hash !== '#/'")
    assert page.evaluate("location.hash") == "#/libraries/new"
    assert not page.errors


def test_test_connection_uses_the_values_in_the_form(web_server, chrome, tmp_path):
    url, _ = web_server
    lib = _folder_library(url, tmp_path)
    page = open_page(chrome, url + f"/#/libraries/{lib}")
    page.wait_for_selector(f'#content[data-route="/libraries/{lib}"]')
    page.fill('input[id$="root"]', str(tmp_path / "does-not-exist"))  # edited, not saved
    page.get_by_role("button", name="Test connection").click()
    page.wait_for_function("!document.querySelector('#libCheck').innerText.includes('…')")
    settle(page)  # no late answer turns it into OK
    assert page.locator("#libCheck .status.ok").count() == 0, page.inner_text("#libCheck")


def _folder_device(url, tmp_path, name: str = "Stick") -> str:
    (tmp_path / "stick").mkdir()
    r = httpx.post(
        f"{url}/api/v1/devices/register",
        json={"name": name, "mount_path": str(tmp_path / "stick"), "type": "flashdrive"},
        timeout=60,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def test_checkbox_sits_next_to_its_label(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url + "/#/libraries/new?type=subsonic")
    page.wait_for_selector("label.checkrow input[type=checkbox]")
    # the box is ~13 px wide; its label text must start right after it (it started ~650 px later)
    gaps = page.evaluate(
        """() => [...document.querySelectorAll('#content label.checkrow')].map(l => {
            const box = l.querySelector('input').getBoundingClientRect();
            const r = document.createRange(); r.selectNodeContents(l);
            const lefts = [...r.getClientRects()].map(x => x.left).filter(x => x > box.left + 1);
            return [l.innerText.trim(), Math.round((lefts.length ? Math.min(...lefts) : box.left) - box.left)];
        })"""
    )
    assert gaps and all(gap < 40 for _, gap in gaps), gaps


def test_phone_device_tabs_show_the_active_tab(web_server, chrome, tmp_path):
    url, _ = web_server
    dev = _folder_device(url, tmp_path)
    page = open_page(chrome, url + f"/#/devices/{dev}/settings", width=390, height=844)
    page.wait_for_selector(f'#content[data-route="/devices/{dev}/settings"]')
    settle(page)
    animations_done(page)  # the tab strip scrolled the active tab into view
    active = page.locator(f'#content a.tab[href="#/devices/{dev}/settings"]')
    box = active.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390, box


def test_reverse_only_device_is_not_called_synced(web_server, chrome, tmp_path):
    url, _ = web_server
    dev = _folder_device(url, tmp_path)
    job = httpx.post(f"{url}/api/v1/devices/{dev}/update", timeout=60).json()
    for _ in range(100):
        if httpx.get(f"{url}/api/v1/jobs/{job['id']}", timeout=60).json()["status"] == "succeeded":
            break
        time.sleep(0.1)
    page = open_page(chrome, url + "/#/")
    page.wait_for_selector('#content[data-route="/"]')
    settle(page)
    card = page.locator(f'aside a[href="#/devices/{dev}"]').first.inner_text()
    assert "synced" not in card.lower(), card


def _manual_selection(url, tmp_path) -> tuple[str, dict]:
    """A folder library, a folder device and one song sent to it by hand (the 'Manual selection' rule, of the device's own template); returns the device id and that template."""
    lib = _folder_library(url, tmp_path)
    dev = _folder_device(url, tmp_path)
    items = httpx.get(f"{url}/api/v1/libraries/{lib}/items", timeout=60).json()["items"]
    r = httpx.post(
        f"{url}/api/v1/devices/{dev}/manual-selection",
        json={"library_id": lib, "ids": [items[0]["item_id"]], "field": "track_id"},
        timeout=60,
    )
    assert r.status_code < 400, r.text
    return dev, httpx.get(f"{url}/api/v1/templates/own-{dev}", timeout=60).json()


def test_device_rules_keep_the_manual_selection(web_server, chrome, tmp_path):
    """The device's own template opened and saved in the template editor keeps its hand-picked lists."""
    url, _ = web_server
    dev, before = _manual_selection(url, tmp_path)
    assert "track_id" in str(before["rules"])
    # and the exclude rule "Player contents -> Remove selected" writes
    excludes = [
        {
            "id": "removed-from-device",
            "name": "Removed from the device",
            "criteria": {"any": [{"in": {"track_id": ["gone-1", "gone-2"]}}]},
        }
    ]
    r = httpx.patch(f"{url}/api/v1/templates/own-{dev}", json={"excludes": excludes}, timeout=60)
    assert r.status_code < 400, r.text
    before = r.json()
    page = open_page(chrome, url + f"/#/templates/own-{dev}")
    page.wait_for_selector('[data-rule="rules:0"]')
    # shown as a hand-picked list, not as a condition row the builder would rewrite
    assert page.locator("[data-hand-picked='1']").count() == 1
    assert page.locator("[data-hand-picked='2']").count() == 1
    bad = []
    page.on("response", lambda r: r.status >= 400 and bad.append(f"{r.status} {r.url}"))
    with page.expect_response(lambda r: "/rules/test" in r.url) as resp:
        page.get_by_role("button", name="Test", exact=True).click()
    assert resp.value.status == 200, resp.value.text()
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    after = httpx.get(f"{url}/api/v1/templates/own-{dev}", timeout=60).json()
    assert not bad and after["rules"] == before["rules"], (bad, after["rules"])
    assert after["excludes"] == before["excludes"]


def test_rule_builder_media_labels_are_translated(web_server, chrome, tmp_path):
    url, _ = web_server
    dev, _ = _manual_selection(url, tmp_path)
    page = open_page(
        chrome, url + f"/#/templates/own-{dev}"
    )  # a fresh load, Libraries never opened
    page.wait_for_selector('[data-rule="rules:0"]')
    page.locator("#content details > summary", has_text="Order and limits").first.click()
    text = page.inner_text("#content")
    assert "libs.kind." not in text, text[:400]


def test_plan_of_a_device_without_libraries_links_to_its_rules(web_server, chrome, tmp_path):
    url, _ = web_server
    dev = _folder_device(
        url, tmp_path
    )  # registered before any library exists: nothing pulls from one
    page = open_page(chrome, url + f"/#/devices/{dev}/plan")
    page.wait_for_function("document.querySelector('#content')?.dataset.route?.includes('/plan')")
    settle(page)
    link = page.locator(f'#content a[href="#/devices/{dev}/rules"]')  # its templates
    assert link.count() > 0, page.inner_text("#content")
    assert "Retry" not in page.inner_text("#content")
    # the dashboard and the sidebar say so instead of offering a plan review
    page.goto(url + "/#/")
    page.wait_for_selector(f'#content [data-no-libraries="{dev}"]')
    assert page.locator(f'#content [data-review="{dev}"]').count() == 0
    card = page.locator(f'[data-device-card="{dev}"]')
    assert (
        card.get_attribute("data-state") == "no-libraries"
        and "no templates" in card.inner_text().lower()
    )
    page.goto(url + f"/#/devices/{dev}")
    page.wait_for_selector("#content [data-no-libraries]")
    assert page.locator("#content [data-sync]").get_attribute("aria-disabled") == "true"


# Every screen of the UI: the sweep below asserts what the user sees, not only "no exception".
SCREENS = (
    "/",
    "/libraries",
    "/libraries/new",
    "/libraries/{lib}",
    "/libraries/{lib}/browse",
    "/devices",
    "/devices/register",
    "/devices/{dev}/overview",
    "/devices/{dev}/contents",
    "/devices/{dev}/rules",
    "/devices/{dev}/processors",
    "/devices/{dev}/settings",
    "/devices/{dev}/history",
    "/templates",
    "/templates/default.music.all",
    "/templates/new",
    "/settings",
    "/logs",
)
RAW_KEY = re.compile(
    r"\b(?:common|libs|dev|nav|rb|rules|tpl|plan|set|logs|dash|side|reg|proc)\.[a-z_]+(?:\.[a-z_]+)?\b"
)


def _visit_every_screen(url, chrome, tmp_path, width: int, fresh: bool = False):
    """(path, page text with every <details> open) per screen; asserts each screen is its own and error-free.
    `fresh`: a new page per screen (what a bookmark or reload shows), else one page navigated like a user."""
    dev, _ = _manual_selection(url, tmp_path)
    size = {"width": width, "height": 844 if width < 700 else 900}
    page = open_page(chrome, url + "/#/", **size)
    seen = []
    for pattern in SCREENS:
        path = pattern.format(lib="music", dev=dev)
        if fresh:
            assert not page.errors, page.errors
            page.close()
            page = open_page(chrome, url + f"/#{path}", **size)
        else:
            page.evaluate(f"location.hash = '#{path}'")
        page.wait_for_selector(f'#content[data-route="{path}"]', timeout=15000)
        settle(page)  # views that mount widgets after rendering (rule builder)
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        settle(page)  # an opened <details> may load its content
        text = page.inner_text("#content")
        assert "Something went wrong" not in text, (path, text[:300])
        # the top bar names the screen: breadcrumbs on wide screens, the page title on phones
        assert page.inner_text("#crumbs" if width >= 760 else "#pageTitle").strip(), path
        if width < 700:
            assert no_horizontal_scroll(page), path
        seen.append((path, text))
    assert not page.errors, page.errors
    return seen


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("fresh", [False, True])
def test_every_screen_renders_itself_without_errors(web_server, chrome, tmp_path, width, fresh):
    url, _ = web_server
    _visit_every_screen(url, chrome, tmp_path, width, fresh)


def test_no_screen_shows_raw_i18n_keys(web_server, chrome, tmp_path):
    url, _ = web_server
    raw = {
        p: sorted(set(RAW_KEY.findall(t)))
        for p, t in _visit_every_screen(url, chrome, tmp_path, 1280, fresh=True)
    }
    assert not {p: k for p, k in raw.items() if k}, raw


def test_leaving_a_plan_that_is_still_computing_keeps_the_new_screen(
    web_server, chrome, tmp_path, monkeypatch
):
    """Planning takes seconds on a real iPod: a user who leaves #/devices/<id>/plan meanwhile (e.g. for the Rules tab)
    must stay where they went; the finished plan must not pull them back to #/devices/<id>/plan/<plan id>."""
    plan = AppFacade.plan

    async def slow_plan(self, *a, **kw):
        await asyncio.sleep(3)
        return await plan(self, *a, **kw)

    monkeypatch.setattr(AppFacade, "plan", slow_plan)
    url, _ = web_server
    dev, _ = _manual_selection(url, tmp_path)
    page = open_page(chrome, url + f"/#/devices/{dev}/plan")
    page.wait_for_function(
        "document.documentElement.dataset.apiPending !== '0'"
    )  # the plan is under way
    page.evaluate(f"location.hash = '#/devices/{dev}/rules'")
    page.wait_for_selector(f'#content[data-route="/devices/{dev}/rules"]', timeout=15000)
    settle(page)  # the plan is done by now
    assert page.evaluate("location.hash") == f"#/devices/{dev}/rules"
    assert page.locator(f'#content[data-route="/devices/{dev}/rules"]').count() == 1


def test_leaving_a_screen_while_its_action_runs_keeps_the_new_screen(
    web_server, chrome, monkeypatch
):
    """The same for actions that navigate after an await: Save of a new template returns to the list when
    done, but not if the user has gone to another screen meanwhile."""
    add = AppFacade.add_template

    async def slow_add(self, *a, **kw):
        await asyncio.sleep(2)
        return await add(self, *a, **kw)

    monkeypatch.setattr(AppFacade, "add_template", slow_add)
    url, _ = web_server
    before = len(httpx.get(f"{url}/api/v1/templates", timeout=60).json())
    page = open_page(chrome, url + "/#/templates/new")
    page.wait_for_selector("#tplName")
    page.fill("#tplName", "Slow")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_function(
        "document.documentElement.dataset.apiPending !== '0'"
    )  # the save is under way
    page.evaluate("location.hash = '#/settings'")
    page.wait_for_selector('#content[data-route="/settings"]', timeout=15000)
    settle(page)  # the save is done by now
    assert page.evaluate("location.hash") == "#/settings"
    assert (
        len(httpx.get(f"{url}/api/v1/templates", timeout=60).json()) == before + 1
    )  # the template was saved


def test_phone_rules_tab_with_a_template_of_another_library_fits(web_server, chrome, tmp_path):
    """Rules tab at 390 px: a template of another library with a long name; the row must wrap instead of
    widening the page (the 'not attached' badge is gone since)."""
    url, _ = web_server
    dev, _ = _manual_selection(url, tmp_path)
    other = tmp_path / "pods"
    make_music(other / "Show" / "Епізод.mp3", title="Епізод", artist="Show", album="Show")
    r = httpx.post(
        f"{url}/api/v1/libraries",
        json={"id": "pods", "type": "folder", "name": "Pods", "settings": {"root": str(other)}},
        timeout=60,
    )
    assert r.status_code == 201, r.text
    body = {"name": "Тільки подкасти", "library_ids": ["pods"], "rules": [{"id": "a", "name": "a"}]}
    r = httpx.post(f"{url}/api/v1/templates", json=body, timeout=60)
    assert r.status_code == 201, r.text
    page = open_page(chrome, url + f"/#/devices/{dev}/rules", width=390, height=844)
    page.wait_for_selector(f'#content[data-route="/devices/{dev}/rules"]')
    # no "not attached" warning any more: linking it brings its library
    page.locator('[data-tpls] [data-action="rulesAdd"]').first.click()
    page.wait_for_selector("[data-pick]:has-text('Тільки подкасти')")
    assert no_horizontal_scroll(page)


def test_add_library_with_an_empty_folder_names_the_field_not_the_model(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url + "/#/libraries/new?type=folder")  # Subsonic is the default since
    page.wait_for_selector('#content[data-route="/libraries/new"]')
    page.get_by_role("button", name="Save and scan").click()
    page.wait_for_function(
        "document.querySelectorAll('#content :invalid').length || document.querySelector('.toast')"
    )
    settle(page)
    shown = " ".join(page.locator(".toast").all_inner_texts()) + page.inner_text("#content")
    marked = page.evaluate("document.querySelectorAll('#content :invalid').length")
    assert marked or "Folder" in shown, shown
    assert "FolderSettings" not in shown and "Value error" not in shown, shown
    assert "Folder is required" in shown, shown  # the field by its label, marked
    assert page.evaluate("document.activeElement.getAttribute('aria-invalid')") == "true"
    r = httpx.post(
        f"{url}/api/v1/libraries",
        json={"id": "x", "type": "folder", "name": "X", "settings": {"root": ""}},
    )
    assert (
        r.status_code == 400 and r.json()["message"] == "invalid settings: root: must not be empty"
    ), r.text


def test_built_in_templates_say_1_rule(web_server, chrome, tmp_path):
    """a count of 1 is singular ('1 rule', not '1 rules'). The one-rule templates are the
    defaults of a library, so the test adds one."""
    url, _ = web_server
    make_music(tmp_path / "music" / "a.mp3", title="A", artist="B")
    r = httpx.post(
        f"{url}/api/v1/libraries",
        json={
            "id": "m",
            "type": "folder",
            "name": "M",
            "settings": {"root": str(tmp_path / "music")},
        },
        timeout=60,
    )
    assert r.status_code == 201, r.text
    page = open_page(chrome, url + "/#/templates")
    page.wait_for_selector('#content[data-route="/templates"]')
    text = page.locator("#content").inner_text()
    assert "1 rule ·" in text or "1 rule\n" in text, text
    assert "1 rules" not in text


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def test_items_of_a_removed_library_are_orphans_and_can_be_removed(web_server, tmp_path):
    """A song sent by hand is on the device; its library is detached and removed. The song must show as an orphan and
    Player contents -> Remove must work although the device has no library left."""
    url, app = web_server
    dev, _ = _manual_selection(url, tmp_path)
    plan = _api(url, "POST", f"/devices/{dev}/plan", json={})
    job = _api(url, "POST", f"/plans/{plan['id']}/apply", json={"first_sync": "keep"})
    assert run_on(app, app.jobs.wait(job["id"]), 60).status == "succeeded"
    # detached: no template draws from the library any more
    _api(url, "PATCH", f"/devices/{dev}", json={"rule_set": {"template_ids": []}})
    _api(url, "DELETE", f"/templates/own-{dev}")  # its picks name the library
    _api(url, "DELETE", "/libraries/music")
    rows = _api(url, "GET", f"/devices/{dev}/contents")
    rows = rows if isinstance(rows, list) else rows["items"]
    assert len(rows) == 1 and rows[0]["orphan"] is True, rows
    r = httpx.post(
        f"{url}/api/v1/devices/{dev}/contents/remove", json={"keys": [rows[0]["key"]]}, timeout=60
    )
    assert r.status_code == 202, r.text
    assert run_on(app, app.jobs.wait(r.json()["id"]), 60).status == "succeeded"
    assert not list((tmp_path / "stick").rglob("*.mp3"))


def test_register_type_change_shows_no_commas_between_processors(web_server, chrome):
    url, _ = web_server
    page = open_page(chrome, url + "/#/devices/register")
    page.wait_for_selector('#content[data-route="/devices/register"] input[name="regType"]')
    page.locator(".reg-type").last.click()  # type option cards
    page.wait_for_function("document.querySelectorAll('#regProcs input[type=checkbox]').length > 1")
    assert page.locator("#regProcs input[type=checkbox]").count() > 1
    # the processor names (checkbox labels), not the template choice's help sentences
    labels = page.eval_on_selector_all(
        "#regProcs label:has(input[type=checkbox])", "ls => ls.map(l => l.innerText)"
    )
    assert labels and not [x for x in labels if "," in x], labels


def test_sync_settings_megabytes_label_has_one_unit(web_server, chrome, tmp_path):
    url, _ = web_server
    dev, _ = _manual_selection(url, tmp_path)
    page = open_page(chrome, url + f"/#/devices/{dev}/sync")
    page.wait_for_selector("[data-sync-form]")
    page.locator(".ds-adv summary").click()  # the design: the margin is under Advanced
    text = page.inner_text("#content")
    assert "Minimum safety margin (MB)" in text
    assert "(MB) (MB)" not in text


def test_rules_test_lists_rules_by_name(web_server, chrome, tmp_path):
    url, _ = web_server
    dev, _ = _manual_selection(
        url, tmp_path
    )  # one rule: id manual-selection, name "Manual selection"
    page = open_page(chrome, url + f"/#/devices/{dev}/rules")
    # the Templates tab counts per template (it had a per-rule Test before)
    page.wait_for_selector("[data-tpl-sum]:has-text('1 item')")
    text = page.inner_text("#content")
    assert "manual-selection" not in text, text


def test_overview_storage_after_eject_shows_no_error_box(web_server, chrome, tmp_path):
    """After Eject the device still counts as connected until udev reports the powered-off
    disk gone, so the Overview asks for /space and gets 409 not_connected ('was ejected: unplug it ...'). The Storage
    card must show the neutral text, not 'Something went wrong'. Simulated with a routed 409 on a folder device."""
    url, _ = web_server
    dev, _ = _manual_selection(url, tmp_path)
    page = chrome.new_page(viewport={"width": 1280, "height": 900})
    page.route(
        f"**/api/v1/devices/{dev}/space",
        lambda route: route.fulfill(
            status=409,
            content_type="application/json",
            body='{"error": "not_connected", "message": "Stick was ejected: unplug it and connect it again"}',
        ),
    )
    page.goto(url + f"/#/devices/{dev}/overview")
    page.wait_for_selector(f'#content[data-route="/devices/{dev}/overview"]')
    settle(page)
    text = page.inner_text("#content")
    assert "Something went wrong" not in text, text[:400]


def test_ejected_device_asks_nothing_and_says_ejected(web_server, chrome, tmp_path):
    """while GET /devices reports a device `ejected` (powered off, disk not gone yet), the Overview does not
    request /space (no 409 in the console), shows the neutral Storage text and the badge 'Ejected - unplug it'."""
    import json

    url, _ = web_server
    dev, _ = _manual_selection(url, tmp_path)
    page = chrome.new_page(viewport={"width": 1280, "height": 900})
    space_calls: list[str] = []

    def devices(route):
        body = route.fetch().json()
        for d in body["registered"]:
            d["ejected"] = d["id"] == dev
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/v1/devices", devices)
    page.on("request", lambda r: space_calls.append(r.url) if r.url.endswith("/space") else None)
    page.goto(url + f"/#/devices/{dev}/overview")
    page.wait_for_selector(f'#content[data-route="/devices/{dev}/overview"]')
    settle(page)
    text = page.inner_text("#content")
    assert "Ejected - unplug it" in text, text[:400]
    assert "Capacity is shown while the device is connected" in text
    assert space_calls == []
    assert page.locator("[data-action=eject]").is_disabled()
