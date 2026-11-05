"""Templates and the rule builder: create with the visual builder (autocomplete from the index), YAML
mode round trip, include + exclude, Test (matches + size), built-ins read-only but copyable, deleting an in-use
template only by copying its rules into the devices, per-device priority order."""

from __future__ import annotations

import socket
import time

import httpx
import pytest
from playwright.sync_api import expect

from tests.media import make_music
from tests.webui.conftest import (
    add_templates,
    animations_done,
    no_horizontal_scroll,
    open_page,
    save_templates,
    settle,
    shot,
)

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.fixture
def server(web_server, tmp_path):
    url, app = web_server
    music = tmp_path / "music"
    for artist, title in (("Скрябін", "Група крові"), ("Скрябін", "Зозуля"), ("Тартак", "Орбіт")):
        make_music(
            music / artist / f"{title}.mp3", title=title, artist=artist, album=f"{artist} album"
        )
    (music / "XXX.m3u").write_text("Скрябін/Група крові.mp3\nТартак/Орбіт.mp3\n")
    (music / "ZZZ.m3u8").write_text("Скрябін/Зозуля.mp3\n")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    return url, app, tmp_path


def test_create_with_builder_yaml_exclude_test(server, chrome):
    url, app, _ = server
    page = open_page(chrome, url + "/#/templates/new")
    page.wait_for_selector('[data-rule="rules:0"]')
    page.fill("#tplName", "Тільки Скрябін")
    page.get_by_role("button", name="Add a condition").click()
    value = page.locator('[data-rule="rules:0"] [data-cond="0"] [data-c="value"]')
    value.fill("Ск")
    value.press("End")
    value.type("р")  # suggestions start after more than 2 characters
    page.wait_for_function(
        "[...document.querySelectorAll('datalist option')].some(o => o.value === 'Скрябін')"
    )  # suggested from the index
    value.fill("Скрябін")
    page.get_by_role("button", name="Test", exact=True).click()
    page.wait_for_selector("#tplResult:has-text('2 items')")

    # an exclude rule
    page.get_by_role("button", name="Add an exclude rule").click()
    page.get_by_role("button", name="Add a condition").nth(1).click()
    ex = page.locator('[data-rule="excludes:0"] [data-cond="0"]')
    ex.locator('[data-c="field"]').select_option("title")
    ex = page.locator('[data-rule="excludes:0"] [data-cond="0"]')  # redrawn for the field's type
    ex.locator('[data-c="value"]').fill("Зозуля")
    page.get_by_role("button", name="Test", exact=True).click()
    page.wait_for_selector("#tplResult:has-text('1 item')")

    # YAML and back
    page.get_by_role("tab", name="YAML").click()
    text = page.locator("[data-rb] textarea")
    assert "Зозуля" in text.input_value() and "contains" in text.input_value()
    text.fill(text.input_value().replace("contains", "startsWith", 1))
    page.get_by_role("tab", name="Rules").click()
    page.wait_for_selector('[data-rule="rules:0"] [data-c="op"]')
    assert (
        page.locator('[data-rule="rules:0"] [data-cond="0"] [data-c="op"]').input_value()
        == "startsWith"
    )
    shot(page, "template-builder-1280")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")  # Save returns to the list
    tpl = next(x for x in _api(url, "GET", "/templates") if x["name"] == "Тільки Скрябін")
    assert tpl["rules"][0]["criteria"] == {"all": [{"startsWith": {"artist": "Скрябін"}}]}
    assert tpl["excludes"][0]["criteria"] == {"all": [{"contains": {"title": "Зозуля"}}]}
    assert page.locator(".row.highlight").get_attribute("data-tpl") == tpl["id"]
    page.set_viewport_size({"width": 375, "height": 800})
    page.goto(url + f"/#/templates/{tpl['id']}")
    page.wait_for_selector('[data-rule="rules:0"]')
    assert no_horizontal_scroll(page)
    assert page.errors == []


def test_a_default_is_editable_copy_and_delete_in_use(server, chrome):
    """a library's default template is editable (then "Default · edited"); Copy opens an unsaved draft."""
    url, app, tmp_path = server
    page = open_page(chrome, url + "/#/templates")
    page.wait_for_selector('[data-tpl="default.music.all"]:has-text("Default")')
    count = len(_api(url, "GET", "/templates"))
    page.locator('[data-tpl="default.music.all"]').get_by_role("button", name="Copy").click()
    # an unsaved draft: nothing is created until Save
    page.wait_for_function("location.hash === '#/templates/new?copy=default.music.all'")
    page.wait_for_selector("#tplName:not([readonly])")
    assert page.locator("#tplName").input_value() == "Copy of All music"
    assert len(_api(url, "GET", "/templates")) == count
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    copy = next(x for x in _api(url, "GET", "/templates") if x["name"] == "Copy of All music")
    assert copy["id"] == "copy-of-all-music" and len(_api(url, "GET", "/templates")) == count + 1
    assert copy["default_of"] is None
    # the default itself is edited in place
    page.goto(url + "/#/templates/default.music.all")
    page.wait_for_selector("#tplName:not([readonly])")
    page.fill("#tplName", "Everything")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector('[data-tpl="default.music.all"]:has-text("Default · edited")')
    assert _api(url, "GET", "/templates/default.music.all")["name"] == "Everything"

    (tmp_path / "stick").mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={
            "name": "Stick",
            "mount_path": str(tmp_path / "stick"),
            "type": "flashdrive",
            "rule_set": {"template_ids": [copy["id"]]},
        },
    )
    page.goto(url + "/#/templates")
    row = page.locator(f'[data-tpl="{copy["id"]}"]')
    page.wait_for_selector(f'[data-tpl="{copy["id"]}"]:has-text("On 1 device")')
    assert row.locator("[data-used]").get_attribute("title") == "Stick"
    row.get_by_role("button", name="Delete").click()
    page.wait_for_selector(".modal:has-text('is in use')")
    page.locator(".modal").get_by_role("button", name="Cancel").click()
    assert any(x["id"] == copy["id"] for x in _api(url, "GET", "/templates"))
    row.get_by_role("button", name="Delete").click()
    page.fill("#typedName", copy["name"])  # in use: its name typed
    page.locator(".modal").get_by_role("button", name="Unlink and delete").click()
    page.wait_for_selector(".toast:has-text('deleted')")
    assert not any(x["id"] == copy["id"] for x in _api(url, "GET", "/templates"))
    rs = _api(url, "GET", "/devices/stick")["config"]["rule_set"]
    assert rs["template_ids"] == [] and rs["rules"] == []  # unlinked, nothing copied

    # device Templates tab: link two templates of one library; their order is the priority
    kino = [{"id": "skryabin", "name": "Скрябін", "criteria": {"is": {"artist": "Скрябін"}}}]
    _api(
        url, "POST", "/templates", json={"name": "Скрябін", "library_ids": ["music"], "rules": kino}
    )
    page.goto(url + "/#/devices/stick/rules")
    page.wait_for_selector("[data-tpls]")
    add_templates(page, "default.music.all", "скрябін")
    rows = page.locator("[data-rules] [data-tpl-row]")
    assert rows.first.get_attribute("data-tpl-row") == "default.music.all"  # in the order added
    # drag the second one above the first, down and up again by keyboard buttons
    page.locator('[data-tpl-row="скрябін"] [data-grip]').drag_to(
        page.locator('[data-tpl-row="default.music.all"]')
    )
    assert rows.first.get_attribute("data-tpl-row") == "скрябін"
    page.locator('[data-tpl-row="скрябін"] [data-action="rulesMove"][data-d="1"]').click()
    assert rows.first.get_attribute("data-tpl-row") == "default.music.all"
    # the saved priority is the order on screen
    page.locator('[data-tpl-row="скрябін"] [data-action="rulesMove"][data-d="-1"]').click()
    assert rows.first.get_attribute("data-tpl-row") == "скрябін"
    assert rows.first.locator(".dt-rank b").inner_text() == "1"
    save_templates(page)
    rs = _api(url, "GET", "/devices/stick")["config"]["rule_set"]
    assert rs["template_ids"] == ["скрябін", "default.music.all"]
    assert rs["priority"] == ["skryabin", "default.music.all"]
    assert page.errors == []


def test_nested_groups_bound_to_a_library_attach_to_device(server, chrome):
    """library Music: (in playlist XXX and artist Скрябін) or in playlist ZZZ - built visually with a nested
    group, Test shows the matches and size, saved with its library, then linked on a device's Rules tab."""
    url, app, tmp_path = server
    page = open_page(chrome, url + "/#/templates/new")
    page.wait_for_selector('[data-rule="rules:0"]')
    assert page.input_value("#tplLib") == ""  # a new template draws from all libraries
    page.select_option("#tplLib", "music")
    page.fill("#tplName", "Skryabin nested")
    rule = page.locator('[data-rule="rules:0"]')
    assert (
        rule.locator('[data-group=""] > .rb-group-head [data-k="match"]').count() == 0
    )  # nothing to match yet
    rule.locator('[data-action="rbAddGroup"][data-path=""]').click()
    rule.locator('[data-group=""] > .rb-group-head [data-k="match"]').select_option("any")
    group = page.locator('[data-rule="rules:0"] [data-group="0"]')
    group.locator('> .rb-group-head [data-k="match"]').select_option("all")
    group.locator('> .rb-items > [data-cond="0"] [data-c="field"]').select_option("playlist")
    group = page.locator('[data-rule="rules:0"] [data-group="0"]')
    group.locator('> .rb-items > [data-cond="0"] [data-c="value"]').fill("XXX")
    group.locator('[data-action="rbAddCond"][data-path="0"]').click()
    group.locator('> .rb-items > [data-cond="1"] [data-c="value"]').fill("Скрябін")
    page.locator('[data-rule="rules:0"] [data-action="rbAddCond"][data-path=""]').click()
    top = page.locator('[data-rule="rules:0"] [data-group=""] > .rb-items > [data-cond="1"]')
    top.locator('[data-c="field"]').select_option("playlist")
    top = page.locator('[data-rule="rules:0"] [data-group=""] > .rb-items > [data-cond="1"]')
    top.locator('[data-c="value"]').fill("ZZZ")
    page.get_by_role("button", name="Test", exact=True).click()
    page.wait_for_selector("#tplResult:has-text('2 items')")
    found = page.locator("#tplResult").text_content()
    assert "Група крові" in found and "Зозуля" in found and "Орбіт" not in found
    shot(page, "template-nested-1280")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")  # Save returns to the list
    tpl = next(x for x in _api(url, "GET", "/templates") if x["name"] == "Skryabin nested")
    assert tpl["library_ids"] == ["music"]
    assert tpl["rules"][0]["criteria"] == {
        "any": [
            {"all": [{"inPlaylist": {"name": "XXX"}}, {"contains": {"artist": "Скрябін"}}]},
            {"inPlaylist": {"name": "ZZZ"}},
        ]
    }
    # reopened: the nested group is shown again, not the YAML-only warning
    page.goto(url + f"/#/templates/{tpl['id']}")
    page.wait_for_selector('[data-rule="rules:0"] [data-group="0"]')
    assert page.locator(".warningbox").count() == 0

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
        },
    )
    page.goto(url + "/#/devices/stick/rules")
    page.wait_for_selector("[data-tpls]")
    add_templates(page, tpl["id"])
    group = page.locator('[data-rules] [data-group="music"]')
    assert "Music" in group.locator(".dt-ghead").inner_text()
    assert group.locator(f'[data-tpl-row="{tpl["id"]}"]').count() == 1
    save_templates(page)
    assert tpl["id"] in _api(url, "GET", "/devices/stick")["config"]["rule_set"]["template_ids"]
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(url + f"/#/templates/{tpl['id']}")
    page.wait_for_selector('[data-rule="rules:0"] [data-group="0"]')
    assert no_horizontal_scroll(page)
    assert page.errors == []


def test_saving_unchanged_keeps_playlist_id_conditions(server, chrome):
    """A template whose condition names a playlist by id (as Navidrome .nsp files do) must still match the same
    items after it is opened in the builder and saved without a change; every playlist reference form round-trips
    unchanged, an id is shown by the playlist's name, and an edited value is saved by name."""
    url, app, tmp_path = server
    _api(url, "POST", "/libraries/music/refresh")
    pls = {p["name"]: p["id"] for p in _api(url, "GET", "/libraries/music/playlists")}
    criteria = {
        "any": [
            {"all": [{"inPlaylist": {"id": pls["XXX"]}}, {"notInPlaylist": {"id": pls["ZZZ"]}}]},
            {"inPlaylist": {"name": "ZZZ"}},
            {"inPlaylist": {"path": "../XXX.nsp"}},
            {"inPlaylist": {"id": "gone-playlist"}},
            {"all": [{"notInPlaylist": {"any": True}}, {"contains": {"artist": "Тартак"}}]},
        ]
    }
    _api(
        url,
        "POST",
        "/templates",
        json={
            "name": "By id",
            "library_id": "music",
            "rules": [{"id": "r1", "name": "r1", "criteria": criteria}],
        },
    )

    def matches():
        res = _api(url, "POST", "/rules/test", json={"rule_set": {"template_ids": ["by-id"]}})
        return res["items"]

    def saved():
        return _api(url, "GET", "/templates/by-id")["rules"][0]["criteria"]

    assert matches() == 3
    page = open_page(chrome, url + "/#/templates/by-id")
    page.wait_for_selector('[data-rule="rules:0"]')
    first = page.locator('[data-rule="rules:0"] [data-group="0"] [data-cond="0"] [data-c="value"]')
    expect(first).to_have_value(
        "XXX"
    )  # the id is shown by the playlist's name (loaded in the background)
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".toast:has-text('Saved')")
    assert saved() == criteria
    assert matches() == 3

    # an edited value is a playlist name
    page.wait_for_selector('.row.highlight[data-tpl="by-id"]')
    page.goto(url + "/#/templates/by-id")
    first = page.locator('[data-rule="rules:0"] [data-group="0"] [data-cond="0"] [data-c="value"]')
    first.fill("ZZZ")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".toast:has-text('Saved')")
    assert saved()["any"][0]["all"][0] == {"inPlaylist": {"name": "ZZZ"}}
    assert saved()["any"][1:] == criteria["any"][1:]

    # the device Rules tab uses the same builder
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
        },
    )
    # the device's own template keeps them too when saved unchanged
    own = {"id": "own", "name": "own", "criteria": criteria}
    _api(url, "POST", "/templates", json={"id": "own-stick", "name": "Stick - own", "rules": [own]})
    page.goto(url + "/#/templates/own-stick")
    page.wait_for_selector('[data-rule="rules:0"] [data-group="0"]')
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    assert _api(url, "GET", "/templates/own-stick")["rules"][0]["criteria"] == criteria
    assert page.errors == []


def test_editor_is_one_column_at_phone_width(server, chrome):
    """At 390 px the editor gets the full width; the action bar (Test / Save) sticks
    to the bottom of the screen, so it is in sight without scrolling."""
    url, _, _ = server
    _api(
        url,
        "POST",
        "/templates",
        json={"name": "Phone", "library_id": "music", "rules": [{"id": "r1", "criteria": {}}]},
    )
    for path in ("/#/templates/new", "/#/templates/phone"):
        page = open_page(chrome, url + path, width=390, height=700)
        page.wait_for_selector('[data-rule="rules:0"]')
        assert page.locator(".rb").bounding_box()["width"] > 300
        for _ in range(3):  # more rules than the screen holds
            page.locator('[data-action="rbAddRule"][data-list="rules"]').click()
        save = page.get_by_role("button", name="Save", exact=True)
        box = save.bounding_box()
        assert box and box["y"] + box["height"] <= 700  # stuck to the bottom, not below the fold
        assert no_horizontal_scroll(page)
        assert page.errors == []
        page.close()


def test_builder_does_not_wait_for_an_unreachable_library(server, chrome):
    """playlist names are loaded in the background, in parallel, only from the libraries in scope - a
    library whose server never answers does not delay any builder view, and an id of a reachable library is still
    shown by name and round-trips."""
    url, _, tmp_path = server
    hang = socket.socket()  # accepts connections (backlog) but never answers
    hang.bind(("127.0.0.1", 0))
    hang.listen(8)
    try:
        _api(
            url,
            "POST",
            "/libraries",
            json={
                "id": "nd-down",
                "type": "navidrome",
                "name": "Down",
                "settings": {
                    "url": f"http://127.0.0.1:{hang.getsockname()[1]}",
                    "username": "u",
                    "password": "p",
                    "timeout_s": 8,
                },
            },
        )
        _api(url, "POST", "/libraries/music/refresh")
        pls = {p["name"]: p["id"] for p in _api(url, "GET", "/libraries/music/playlists")}
        criteria = {"all": [{"inPlaylist": {"id": pls["XXX"]}}]}
        _api(
            url,
            "POST",
            "/templates",
            json={
                "name": "By id",
                "library_id": "music",
                "rules": [{"id": "r1", "name": "r1", "criteria": criteria}],
            },
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
                "library_ids": ["music", "nd-down"],
            },
        )

        page = open_page(chrome, url + "/#/templates")
        page.wait_for_selector("[data-tpl]")
        for path, saved, save_button in (
            (
                "/#/templates/by-id",
                lambda: _api(url, "GET", "/templates/by-id")["rules"][0]["criteria"],
                "Save",
            ),
            ("/#/templates/new", None, None),
        ):
            page.goto(url + path)
            start = time.monotonic()
            page.wait_for_selector('[data-rule="rules:0"]', timeout=5000)
            assert time.monotonic() - start < 2, path
            if saved is None:
                continue
            value = page.locator('[data-rule="rules:0"] [data-cond="0"] [data-c="value"]')
            expect(value).to_have_value(
                "XXX", timeout=3000
            )  # music answered although nd-down hangs
            page.get_by_role("button", name=save_button, exact=True).click()
            page.wait_for_selector(".toast:has-text('Saved')")
            assert saved() == criteria
        assert page.errors == []
    finally:
        hang.close()


def test_empty_condition_is_refused(server, chrome):
    """an untouched new condition (`artist contains ""`) is not saved - inside an "any" group it would
    select the whole library. Save and Test name the problem and mark the empty value; filled in, it saves."""
    url, _, _ = server
    criteria = {"any": [{"inPlaylist": {"name": "ZZZ"}}]}
    _api(
        url,
        "POST",
        "/templates",
        json={"name": "Any", "library_id": "music", "rules": [{"id": "r1", "criteria": criteria}]},
    )
    page = open_page(chrome, url + "/#/templates/any")
    page.wait_for_selector('[data-rule="rules:0"]')
    page.get_by_role("button", name="Add a condition").click()
    empty = page.locator('[data-rule="rules:0"] [data-cond="1"] [data-c="value"]')
    for button in ("Test", "Save"):
        page.get_by_role("button", name=button, exact=True).click()
        page.wait_for_selector(".toast:has-text('A condition has no value')")
        assert empty.get_attribute("aria-invalid") == "true"
        assert empty.evaluate("(el) => el === document.activeElement")
    assert _api(url, "GET", "/templates/any")["rules"][0]["criteria"] == criteria
    empty.fill("Тартак")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".toast:has-text('Saved')")
    saved = _api(url, "GET", "/templates/any")["rules"][0]["criteria"]
    assert saved == {"any": [*criteria["any"], {"contains": {"artist": "Тартак"}}]}
    # refused saves kept the editor open (above); a good one returns to the list, the template highlighted (277)
    page.wait_for_selector('.row.highlight[data-tpl="any"]')
    assert page.errors == []


def test_autocomplete_shows_only_the_latest_query(server, chrome):
    """a slow answer for an older query does not overwrite the suggestions of the newer one."""
    url, _, _ = server
    page = chrome.new_page(viewport={"width": 1280, "height": 800})
    page.add_init_script(
        """
        const realFetch = window.fetch;
        window.fetch = async (input, init) => {
          const u = String(input instanceof Request ? input.url : input);
          if (u.includes("/rules/values") && new URL(u, location.href).searchParams.get("q") === "Скр") {
            window.__slowAsked = true;
            await new Promise((r) => setTimeout(r, 1500));
            window.__slowAnswered = true;
            return new Response(JSON.stringify([{ value: "STALE", count: 9 }]),
              { headers: { "Content-Type": "application/json" } });
          }
          return realFetch(input, init);
        };
        """
    )
    page.goto(url + "/#/templates/new")
    page.wait_for_selector('[data-rule="rules:0"]')
    page.get_by_role("button", name="Add a condition").click()
    value = page.locator('[data-rule="rules:0"] [data-cond="0"] [data-c="value"]')
    value.fill("Скр")
    value.dispatch_event("input")
    page.wait_for_function("window.__slowAsked === true")  # the slow request for "Скр" is under way
    value.fill("Скрябін")
    value.dispatch_event("input")
    page.wait_for_function(
        "[...document.querySelectorAll('datalist option')].some(o => o.value === 'Скрябін')"
    )
    page.wait_for_function("window.__slowAnswered === true")  # the answer for "Скр" has arrived
    settle(page)
    values = page.evaluate("[...document.querySelectorAll('datalist option')].map(o => o.value)")
    assert "STALE" not in values and "Скрябін" in values


def test_libraries_one_several_all_and_copies_of_defaults(server, chrome):
    """a template draws from one, several or all libraries; a copy of a library's default keeps
    its library and shows no 'older version' warning; Test and suggestions use the chosen libraries."""
    url, app, tmp_path = server
    other = tmp_path / "other"
    make_music(
        other / "Воплі Відоплясова" / "Місто.mp3",
        title="Місто",
        artist="Воплі Відоплясова",
        album="a",
    )
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "other", "type": "folder", "name": "Other", "settings": {"root": str(other)}},
    )
    # a copy of a library's default keeps its library
    for lib in ("music", "other"):
        page = open_page(chrome, url + f"/#/templates/default.{lib}.all")
        page.wait_for_selector("[data-rb]")
        assert page.input_value("#tplLib") == lib
        page.goto(url + "/#/templates")
        page.locator(f'[data-tpl="default.{lib}.all"]').get_by_role("button", name="Copy").click()
        page.wait_for_function(f"location.hash.endsWith('copy=default.{lib}.all')")
        page.wait_for_selector("#content[data-route] #tplLib")
        assert page.input_value("#tplLib") == lib
        text = page.inner_text("#content")
        assert "older version" not in text and "No library" not in text, lib
        assert page.errors == []
    page.get_by_role("button", name="Save", exact=True).click()  # the last draft
    tid = page.locator(".row.highlight").get_attribute("data-tpl")
    assert _api(url, "GET", f"/templates/{tid}")["library_ids"] == ["other"]

    page = open_page(chrome, url + "/#/templates/new")
    page.wait_for_selector('[data-rule="rules:0"]')
    page.fill("#tplName", "Two libraries")
    page.get_by_role("button", name="Test", exact=True).click()
    page.wait_for_selector("#tplResult:has-text('4 items')")  # all: 3 + 1
    page.select_option("#tplLib", "other")
    page.get_by_role("button", name="Test", exact=True).click()
    page.wait_for_selector("#tplResult:has-text('1 item')")
    # suggestions come from the chosen library only
    page.get_by_role("button", name="Add a condition").click()
    value = page.locator('[data-rule="rules:0"] [data-cond="0"] [data-c="value"]')
    value.fill("Воп")
    page.wait_for_function(
        "[...document.querySelectorAll('datalist option')].some(o => o.value === 'Воплі Відоплясова')"
    )
    value.fill("Скр")
    settle(page)  # past the debounce and its answer
    assert not page.evaluate(
        "[...document.querySelectorAll('datalist option')].some(o => o.value === 'Скрябін')"
    )
    page.locator('[data-rule="rules:0"] [aria-label="Remove this condition"]').click()
    page.select_option("#tplLib", "")
    page.get_by_role("button", name="Test", exact=True).click()
    page.wait_for_selector("#tplResult:has-text('4 items')")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")  # Save returns to the list
    tpl = next(x for x in _api(url, "GET", "/templates") if x["name"] == "Two libraries")
    assert tpl["library_ids"] == []
    # one library per template; one that still draws from several keeps them as one choice
    _api(url, "PATCH", f"/templates/{tpl['id']}", json={"library_ids": ["music", "other"]})
    page.goto(url + f"/#/templates/{tpl['id']}")
    page.wait_for_selector("#content[data-route] #tplLib")
    assert page.input_value("#tplLib") == "music,other"
    page.select_option("#tplLib", "")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".toast:has-text('Template saved')")
    assert _api(url, "GET", f"/templates/{tpl['id']}")["library_ids"] == []
    assert page.errors == []


def test_templates_grouped_by_library_and_edited_defaults_on_library_removal(server, chrome):
    """the list groups templates by library (several / all libraries last); removing a library whose
    default was edited asks whether to keep that template."""
    url, app, tmp_path = server
    other = tmp_path / "other"
    make_music(
        other / "Воплі Відоплясова" / "Місто.mp3",
        title="Місто",
        artist="Воплі Відоплясова",
        album="a",
    )
    lib = {"id": "other", "type": "folder", "name": "Інша", "settings": {"root": str(other)}}
    _api(url, "POST", "/libraries", json=lib)
    _api(url, "POST", "/templates", json={"name": "Mixed", "library_ids": [], "rules": []})
    _api(url, "PATCH", "/templates/default.other.all", json={"name": "Усе інше"})
    page = open_page(chrome, url + "/#/templates")
    page.wait_for_selector('[data-tpl-group="other"]')
    groups = page.locator("[data-tpl-group] h2").all_text_contents()
    assert groups == ["Music", "Інша", "Several or all libraries"]
    assert page.locator('[data-tpl-group="music"] [data-tpl="default.music.all"]').count() == 1
    other_row = page.locator('[data-tpl-group="other"] [data-tpl="default.other.all"]')
    assert "Default · edited" in other_row.inner_text()
    assert page.locator('[data-tpl-group=""] [data-tpl="mixed"]').count() == 1
    shot(page, "templates-grouped-1280")
    page.set_viewport_size({"width": 390, "height": 800})
    animations_done(page)  # the sidebar's slide-out after the resize
    assert no_horizontal_scroll(page)
    shot(page, "templates-grouped-390")
    page.set_viewport_size({"width": 1280, "height": 800})
    # remove the library: keep its edited default
    page.goto(url + "/#/libraries/other")
    # hash navigation: the templates list (its own Delete buttons) must be gone first
    page.wait_for_selector("[data-tpl-group]", state="detached")
    page.wait_for_selector('[data-action="remove"]')
    page.locator('[data-action="remove"]').click()
    dlg = page.locator(".modal")
    page.wait_for_selector(".modal:has-text('“Усе інше”')")
    shot(page, "library-remove-edited-1280")
    dlg.get_by_role("button", name="Remove, keep the templates").click()
    page.locator("#typedName").fill(_api(url, "GET", "/libraries/other")["name"])  # the typed name
    page.locator(".modal").get_by_role("button", name="Remove library").click()
    page.wait_for_function("location.hash === '#/libraries'")
    kept = _api(url, "GET", "/templates/усе-інше")
    assert kept["name"] == "Усе інше" and kept["default_of"] is None and kept["library_ids"] == []
    page.goto(url + "/#/templates")
    page.wait_for_selector('[data-tpl-group=""] [data-tpl="усе-інше"]')
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_template_editor_v2(server, chrome, width):
    """Template editor shell: back, the name as the heading, the badge, a note linking the
    devices a change affects, the Library select, Rules / Pick from library / YAML, the action bar with Discard once
    something changed; long Cyrillic names, light theme."""
    url, app, tmp_path = server
    long = "Дуже довга назва шаблону для перевірки переносів і обрізання"
    tpl = _api(
        url,
        "POST",
        "/templates",
        json={
            "name": long,
            "library_ids": ["music"],
            "rules": [
                {
                    "id": "r1",
                    "name": "Скрябін",
                    "criteria": {"all": [{"is": {"artist": "Скрябін"}}]},
                }
            ],
        },
    )
    (tmp_path / "stick").mkdir()
    _api(
        url,
        "POST",
        "/devices/register",
        json={
            "name": "Флешка Кухня",
            "mount_path": str(tmp_path / "stick"),
            "type": "flashdrive",
            "rule_set": {"template_ids": [tpl["id"]]},
        },
    )
    page = open_page(chrome, url + f"/#/templates/{tpl['id']}", width=width)
    page.wait_for_selector('[data-rule="rules:0"]')
    assert page.input_value("#tplName") == long
    assert page.locator("[data-tpl-badge]").inner_text().strip() == "Own"
    note = page.locator("[data-tpl-note]")
    assert "Флешка Кухня" in note.inner_text()
    assert (
        note.get_by_role("link", name="Флешка Кухня").get_attribute("href").startswith("#/devices/")
    )
    assert page.input_value("#tplLib") == "music"
    assert page.locator("[data-dirty]").is_hidden()
    assert no_horizontal_scroll(page)
    shot(page, f"template-editor-{width}")

    # YAML and back: the same rules
    page.get_by_role("tab", name="YAML").click()
    page.wait_for_selector(".rb textarea.codebox")
    assert "Скрябін" in page.input_value(".rb textarea.codebox")
    page.get_by_role("tab", name="Rules").click()
    page.wait_for_selector('[data-rule="rules:0"]')

    # Pick from library: the ticked artist becomes an include rule; the editor is now unsaved
    page.fill("#tplName", "Скрябін і Тартак")
    page.get_by_role("tab", name="Pick from library").click()
    page.wait_for_selector("[data-tpl-pick]")
    shot(page, f"template-editor-pick-{width}")
    page.locator('[data-action="tplPick"]').click()
    page.wait_for_selector("[data-picker] .pick-row")
    page.locator(".pick-row", has=page.locator(".pick-name", has_text="Тартак")).first.locator(
        "input"
    ).check()
    page.get_by_role("button", name="Add 1 pick").click()
    page.wait_for_selector('[data-rule="rules:1"]')
    assert page.input_value("#tplName") == "Скрябін і Тартак"  # kept across the redraw
    assert page.locator("[data-dirty]").is_visible()
    # "Unsaved changes" next to it (on a phone Discard alone says so: the bar stays one row)
    assert page.locator("[data-dirty-text]").is_visible() == (width > 640)
    assert page.locator("[data-tpl-actions]").bounding_box()["height"] < 80
    shot(page, f"template-editor-dirty-{width}")

    # leaving with unsaved changes asks first (the shared leave guard); Cancel keeps the edit
    here = page.evaluate("location.hash")
    page.evaluate("location.hash = '#/devices'")
    dialog = page.locator(".modal")
    dialog.wait_for()
    assert "Leave without saving?" in dialog.inner_text()
    dialog.get_by_role("button", name="Cancel").click()
    page.wait_for_function(f"location.hash === {here!r}")
    assert page.locator('[data-rule="rules:1"]').count() == 1

    # Discard: back to the stored template
    page.locator('[data-action="discard"]').click()
    page.wait_for_selector('[data-rule="rules:0"]')
    page.wait_for_function("!document.querySelector('[data-rule=\"rules:1\"]')")
    assert page.input_value("#tplName") == long
    assert page.locator("[data-dirty]").is_hidden()

    # an untouched library default: the Default badge, no note
    page.evaluate("localStorage.setItem('syncmusic.theme', 'light')")
    page.goto(url + "/#/templates/default.music.all")
    page.reload()
    page.wait_for_selector("[data-tpl-badge]")
    assert page.locator("[data-tpl-badge]").inner_text().strip() == "Default"
    assert page.locator("[data-tpl-note]").count() == 0
    assert no_horizontal_scroll(page)
    shot(page, f"template-editor-default-light-{width}")
    page.evaluate("localStorage.removeItem('syncmusic.theme')")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_rule_skip_playlists_opt_out(server, chrome, width):
    """'Don't put these playlists on the device' replaces the old include_playlists checkbox 1:1 -
    in Order and limits of every include rule, unchecked by default; ticking it saves skip_playlists."""
    url, _, _ = server
    tpl = _api(
        url,
        "POST",
        "/templates",
        json={
            "name": "Плейлисти",
            "library_ids": ["music"],
            "rules": [
                {"id": "r1", "name": "З плейлиста", "criteria": {"inPlaylist": {"name": "Mix"}}},
                {"id": "r2", "name": "Усе"},
            ],
        },
    )
    page = open_page(chrome, url + f"/#/templates/{tpl['id']}", width=width)
    page.wait_for_selector('[data-rule="rules:1"]')
    assert (
        page.locator('[data-rule="rules:1"] [data-rc-limits] [data-k="skip_playlists"]').count()
        == 1
    )
    page.locator('[data-rule="rules:0"] [data-rc-limits] summary').click()
    box = page.locator('[data-rule="rules:0"] [data-rc-limits] [data-k="skip_playlists"]')
    expect(box).to_be_visible()
    assert not box.is_checked()
    assert no_horizontal_scroll(page)
    shot(page, f"template-editor-skip-playlists-{width}")
    box.check()
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    rules = _api(url, "GET", f"/templates/{tpl['id']}")["rules"]
    assert rules[0]["skip_playlists"] is True and not rules[1].get("skip_playlists")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_template_editor_limits_and_preview(server, chrome, width):
    """a rule's Order and limits (sort, at most N tracks / GB / hours, a device playlist) are
    saved into the rule; the Preview column follows the edits (a side column at 1280, below at 390)."""
    url, _, _ = server
    tpl = _api(
        url,
        "POST",
        "/templates",
        json={"name": "Скрябін", "library_ids": ["music"], "rules": [{"id": "r1", "name": "Усе"}]},
    )
    page = open_page(chrome, url + f"/#/templates/{tpl['id']}", width=width)
    page.wait_for_selector('[data-rule="rules:0"]')
    preview = page.locator("[data-tpl-preview]")
    expect(preview.locator("[data-preview-headline]")).to_contain_text("3 items")
    assert "In Music · 1 include rule, 0 exclude rules" in preview.inner_text()
    side = preview.bounding_box()["x"] > page.locator(".ed-body").bounding_box()["x"] + 300
    assert side == (width == 1280)
    shot(page, f"template-editor-preview-{width}")

    # a condition narrows the preview
    page.get_by_role("button", name="Add a condition").click()
    page.locator('[data-rule="rules:0"] [data-cond="0"] [data-c="value"]').fill("Скрябін")
    expect(preview.locator("[data-preview-headline]")).to_contain_text("2 items")

    # order and limits: at most 1 track, highest year first, a playlist on the device
    box = page.locator('[data-rule="rules:0"] [data-rc-limits]')
    box.locator("summary").click()
    box.locator('[data-k="sort"]').select_option("year")
    box.locator('[data-k="desc"]').check()
    box.locator('[data-k="limit_n"]').fill("1")
    box.locator('[data-k="limit_unit"]').select_option("tracks")
    box.locator('[data-k="as_pl_on"]').check()
    assert box.locator('[data-k="as_playlist"]').is_visible()
    expect(preview.locator("[data-preview-headline]")).to_contain_text("1 item")
    assert no_horizontal_scroll(page)
    shot(page, f"template-editor-limits-{width}")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    rule = _api(url, "GET", f"/templates/{tpl['id']}")["rules"][0]
    assert (rule["sort"], rule["order"], rule["limit"], rule["limit_size"]) == (
        "year",
        "desc",
        1,
        None,
    )
    assert rule["as_playlist"] == "Усе"  # the rule's name when the playlist name is left empty

    # GB, then nothing matches: the preview says how to loosen it
    page.goto(url + f"/#/templates/{tpl['id']}")
    page.wait_for_selector('[data-rule="rules:0"]')
    box = page.locator('[data-rule="rules:0"] [data-rc-limits]')
    assert box.evaluate("d => d.open")  # a rule with a limit opens its box
    box.locator('[data-k="limit_n"]').fill("2.5")
    box.locator('[data-k="limit_unit"]').select_option("GB")
    page.locator('[data-rule="rules:0"] [data-cond="0"] [data-c="value"]').fill("немає такого")
    expect(preview.locator("[data-preview-empty]")).to_be_visible()
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    rule = _api(url, "GET", f"/templates/{tpl['id']}")["rules"][0]
    assert (rule["limit"], rule["limit_size"]) == (None, "2.5GB")

    # "New random order" comes with a random sort and is saved
    page.goto(url + f"/#/templates/{tpl['id']}")
    page.wait_for_selector('[data-rule="rules:0"]')
    box = page.locator('[data-rule="rules:0"] [data-rc-limits]')
    assert not box.locator("[data-reshuffle]").is_visible()
    box.locator('[data-k="sort"]').select_option("random")
    box.locator('[data-k="random_period"]').select_option("week")
    shot(page, f"template-editor-reshuffle-{width}")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    rule = _api(url, "GET", f"/templates/{tpl['id']}")["rules"][0]
    assert (rule["sort"], rule["random_period"]) == ("random", "week")
    assert page.errors == []


def test_template_preview_cancels_a_stale_request(server, chrome):
    """an edit while a preview request is still running cancels that request; the newest answer
    is shown."""
    url, _, _ = server
    tpl = _api(
        url,
        "POST",
        "/templates",
        json={"name": "Скрябін", "library_ids": ["music"], "rules": [{"id": "r1", "name": "Усе"}]},
    )
    page = open_page(chrome, url + f"/#/templates/{tpl['id']}", width=1280)
    page.wait_for_selector('[data-rule="rules:0"]')
    preview = page.locator("[data-tpl-preview]")
    expect(preview.locator("[data-preview-headline]")).to_contain_text("3 items")
    # the first edit's request is held back in the page until the second edit cancels it
    page.evaluate("""() => {
      const orig = window.fetch; let first = true; window.__aborted = 0;
      window.fetch = (url, opts = {}) => {
        if (!String(url).endsWith("/rules/test")) return orig(url, opts);
        opts.signal?.addEventListener("abort", () => { window.__aborted++; });
        if (!first) return orig(url, opts);
        first = false;
        window.__held = true;
        return new Promise((ok, no) => {
          opts.signal?.addEventListener("abort", () => no(new DOMException("aborted", "AbortError")));
          setTimeout(() => orig(url, opts).then(ok, no), 3000);
        });
      };
    }""")
    page.get_by_role("button", name="Add a condition").click()
    value = page.locator('[data-rule="rules:0"] [data-cond="0"] [data-c="value"]')
    value.fill("Скр")
    page.wait_for_function("window.__held === true")  # past the debounce: the first request is held
    value.fill("Скрябін")
    expect(preview.locator("[data-preview-headline]")).to_contain_text("2 items")
    assert page.evaluate("window.__aborted") >= 1
    assert page.errors == []
