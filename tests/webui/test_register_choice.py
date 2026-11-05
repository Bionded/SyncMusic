"""registration offers a real choice for rules and processors - an existing template, a new template
for this device, or this device only. No template is created unless asked. Nothing is preselected: Register without a choice asks for one."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=30, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _register(
    chrome,
    url,
    target,
    name,
    templates=("default.music.all",),
    copy=False,
    proc_mode="template",
    proc_template="builtin-flashdrive",
):
    target.mkdir()
    page = open_page(chrome, url + "/#/devices/register")
    page.wait_for_selector("#regPath:visible")
    page.fill("#regPath", str(target))
    page.fill("#regName", name)
    # nothing preselected: no template chip, no processor choice; Register asks for the processors
    assert page.locator('input[name="tpl"]:checked').count() == 0
    assert page.locator("[data-reg-count]").inner_text() == "None selected"
    assert page.locator('input[name="procMode"]:checked').count() == 0
    assert page.locator("#regProcTemplate").input_value() == ""
    for tid in templates:  # the design: templates are chips
        page.locator(f'.chip:has(input[name="tpl"][value="{tid}"])').click()
    assert page.locator("[data-reg-count]").inner_text() == (
        f"{len(templates)} selected" if templates else "None selected"
    )
    if copy:
        page.check("#regCopy")
    page.get_by_role("button", name="Register").click()
    page.wait_for_selector(".toast >> text=Choose the processors")
    assert "/register" in page.evaluate("location.hash")
    if proc_template:
        page.select_option("#regProcTemplate", proc_template)
    page.check(f'input[name="procMode"][value="{proc_mode}"]')
    shot(page, f"register-{target.name}")
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.includes('/overview')")
    dev = page.evaluate("decodeURIComponent(location.hash.split('/')[2])")
    page.close()
    return _api(url, "GET", f"/devices/{dev}")["config"]


def test_register_template_choices(web_server, chrome, tmp_path):
    url, _ = web_server
    (tmp_path / "music").mkdir()
    lib = {
        "id": "music",
        "type": "folder",
        "name": "Music",
        "settings": {"root": str(tmp_path / "music")},
    }
    _api(url, "POST", "/libraries", json=lib)  # its default templates
    templates = {t["id"] for t in _api(url, "GET", "/templates")}
    procs = {t["id"] for t in _api(url, "GET", "/processor-templates")}

    # existing templates, linked; nothing new is created
    cfg = _register(chrome, url, tmp_path / "a", "Default stick")
    assert cfg["rule_set"]["template_ids"] == ["default.music.all"]
    assert cfg["processor_template_id"] == "builtin-flashdrive"
    assert {t["id"] for t in _api(url, "GET", "/templates")} == templates
    assert {t["id"] for t in _api(url, "GET", "/processor-templates")} == procs

    # a new template of each kind, named after the device, linked
    cfg = _register(chrome, url, tmp_path / "b", "Own stick", copy=True, proc_mode="new")
    new_tpl = {t["id"]: t for t in _api(url, "GET", "/templates")}
    (tid,) = set(new_tpl) - templates
    assert new_tpl[tid]["name"] == "Own stick" and cfg["rule_set"]["template_ids"] == [tid]
    fav = _api(url, "GET", "/templates/default.music.all")
    assert [r["criteria"] for r in new_tpl[tid]["rules"]] == [r["criteria"] for r in fav["rules"]]
    new_procs = {t["id"]: t for t in _api(url, "GET", "/processor-templates")}
    (pid,) = set(new_procs) - procs
    assert new_procs[pid]["name"] == "Own stick" and cfg["processor_template_id"] == pid
    assert new_procs[pid]["device_type"] == "flashdrive"

    # no template yet: no template link, the checked processors (and the form fits a phone)
    page = open_page(chrome, url + "/#/devices/register")
    page.set_viewport_size({"width": 390, "height": 900})
    page.wait_for_selector("#regProcTemplate")
    assert no_horizontal_scroll(page)
    shot(page, "register-390")
    page.close()
    cfg = _register(
        chrome,
        url,
        tmp_path / "c",
        "Plain stick",
        templates=(),
        proc_mode="device",
        proc_template=None,
    )
    assert cfg["rule_set"]["template_ids"] == [] and cfg["processor_template_id"] is None
    assert cfg["processors"]
    assert len(_api(url, "GET", "/templates")) == len(templates) + 1


@pytest.mark.parametrize("width", [1280, 390])
def test_register_v2_type_cards_and_template_chips(web_server, chrome, tmp_path, width):
    """The type is a row of option cards; templates are chips grouped by library; a Rockbox
    player gets music only, so an Audiobookshelf library says so and offers no chips."""
    url, _ = web_server
    (tmp_path / "music").mkdir()
    lib = {
        "id": "music",
        "type": "folder",
        "name": "Музика",
        # a podcast subfolder: the folder's "All podcasts" is not for a Rockbox player
        "settings": {"root": str(tmp_path / "music"), "subfolder_kinds": {"Pods": "podcast"}},
    }
    _api(url, "POST", "/libraries", json=lib)
    abs_lib = {
        "id": "abs",
        "type": "audiobookshelf",
        "name": "Аудіокниги",
        "settings": {"url": "http://127.0.0.1:9", "api_token": "t"},
    }
    _api(url, "POST", "/libraries", json=abs_lib)
    page = open_page(chrome, url + "/#/devices/register", width=width)
    page.wait_for_selector("#regPath:visible")
    groups = page.locator("[data-reg-group]")
    assert groups.count() == 2
    abs_group = groups.filter(has_text="Аудіокниги")
    abs_group.locator(".chip").first.click()
    assert page.locator("[data-reg-count]").inner_text() == "1 selected"
    assert no_horizontal_scroll(page)
    shot(page, f"register-v2-{width}")

    page.locator(".reg-type", has_text="Rockbox").click()
    expect(abs_group.locator("[data-music-only]")).to_have_text("Rockbox players get music only.")
    assert abs_group.locator(".chip").count() == 0
    assert (
        page.locator("[data-reg-count]").inner_text() == "None selected"
    )  # the book pick is dropped
    folder = groups.filter(has_text="Музика")
    assert folder.locator(".chip").all_inner_texts() == ["All music"]
    expect(folder.locator("[data-music-only]")).to_have_text("Rockbox players get music only.")
    # no stray separators after a redraw
    assert "," not in page.locator("#regTemplates").inner_text()
    # "Rockbox: copy" is preselected for a Rockbox player (nothing is preselected for the other types)
    assert page.locator("#regProcTemplate").input_value() == "builtin-rockbox-copy"
    assert page.locator('input[name="procMode"]:checked').get_attribute("value") == "template"
    shot(page, f"register-v2-rockbox-{width}")

    # 4 · First sync: Merge (keep) is chosen; a folder cannot be formatted, so no Format row; the choice is stored
    assert page.locator('input[name="firstSync"]:checked').get_attribute("value") == "keep"
    assert page.locator("#regFormatRow").is_hidden()
    page.locator(".reg-opt", has_text="Adopt what is on the device").click()
    (tmp_path / "rb" / ".rockbox").mkdir(parents=True)
    page.fill("#regPath", str(tmp_path / "rb"))
    page.fill("#regName", "Плеєр на кухні")
    groups.filter(has_text="Музика").locator(".chip").first.click()
    page.check('input[name="procMode"][value="device"]')
    # the steps by their names, as on the Processors tab
    steps = page.locator('input[name="proc"]').evaluate_all(
        "els => els.map(e => e.parentElement.textContent.trim())"
    )
    assert steps and not {"transcode", "retag", "covers", "lyrics"} & set(steps), steps
    assert no_horizontal_scroll(page)
    shot(page, f"register-v2-first-{width}")
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.includes('/overview')")
    dev = page.evaluate("decodeURIComponent(location.hash.split('/')[2])")
    cfg = _api(url, "GET", f"/devices/{dev}")["config"]
    assert cfg["first_sync_choice"] == "adopt" and cfg["type"] == "rockbox"
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_register_suggests_a_processor_template(web_server, chrome, tmp_path, width):
    """Register preselects the processor template that fits, with a reason line; a generated chain
    is offered as a new template and stored only on Register."""
    import json

    from tests.contracts.harness import rockbox_tree
    from tests.media import make_music

    url, _ = web_server
    for i in range(3):
        make_music(tmp_path / "music" / f"{i:02} Пісня.mp3", title=f"Пісня {i}", artist="Скрябін")
    lib = {
        "id": "music",
        "type": "folder",
        "name": "Музика",
        "settings": {"root": str(tmp_path / "music")},
    }
    _api(url, "POST", "/libraries", json=lib)
    page = open_page(chrome, url + "/#/devices/register", width=width)
    page.wait_for_selector("#regPath:visible")
    page.locator(".reg-type", has_text="Rockbox").click()
    page.fill("#regPath", str(rockbox_tree(tmp_path / "rb")))
    page.locator("#regPath").dispatch_event("change")
    page.fill("#regName", "Кухня")
    page.locator('.chip:has(input[name="tpl"])').first.click()
    reason = page.locator("[data-suggest-reason]")
    expect(reason).to_contain_text("copies as is - all 3 files play natively and fit")
    assert page.locator("#regProcTemplate").input_value() == "builtin-rockbox-copy"
    assert no_horizontal_scroll(page)
    shot(page, f"register-suggest-{width}")

    # a generated draft (the scorer is covered by unit tests; here the page's handling of it)
    draft = {
        "name": "Кухня: suggested",
        "device_type": "rockbox",
        "processors": [
            {
                "type": "transcode",
                "enabled": True,
                "settings": {"rules": [{"from": ["opus"], "to": "mp3", "when": "not_supported"}]},
            },
            {"type": "retag", "enabled": True, "settings": {}},
        ],
        "builtin": False,
    }
    answer = {
        "template_id": None,
        "draft": draft,
        "reason": {
            "key": "convert",
            "files": 3,
            "converted": 1,
            "needed": 10**6,
            "free": 10**9,
            "fits": True,
            "generated": True,
            "formats": ["opus"],
        },
        "candidates": [],
    }
    page.route(
        "**/api/v1/devices/suggest-processor-template",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(answer)
        ),
    )
    page.locator('.chip:has(input[name="tpl"])').first.click()  # untick: asks again
    expect(reason).to_contain_text("No template fits; a new one converts opus.")
    assert page.locator("#regProcTemplate").input_value() == "__draft__"
    shot(page, f"register-suggest-draft-{width}")
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.includes('/overview')")
    dev = page.evaluate("decodeURIComponent(location.hash.split('/')[2])")
    cfg = _api(url, "GET", f"/devices/{dev}")["config"]
    tpls = {t["id"]: t for t in _api(url, "GET", "/processor-templates")}
    assert tpls[cfg["processor_template_id"]]["name"] == "Кухня: suggested"
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_register_offers_generated_chains(web_server, chrome, tmp_path, width):
    """Register lists the generated chains with their estimated size vs free space, the best
    one ticked; ticking another uses it, and Register stores its steps as the device's own chain."""
    from tests.contracts.harness import rockbox_tree
    from tests.media import make_music

    url, _ = web_server
    for i in range(3):
        make_music(tmp_path / "music" / f"{i:02} Пісня.flac", title=f"Пісня {i}", artist="Скрябін")
    lib = {
        "id": "music",
        "type": "folder",
        "name": "Музика",
        "settings": {"root": str(tmp_path / "music")},
    }
    _api(url, "POST", "/libraries", json=lib)
    page = open_page(chrome, url + "/#/devices/register", width=width)
    page.wait_for_selector("#regPath:visible")
    page.locator(".reg-type", has_text="Rockbox").click()
    page.fill("#regPath", str(rockbox_tree(tmp_path / "rb")))
    page.locator("#regPath").dispatch_event("change")
    page.fill("#regName", "Варіанти")
    page.locator('.chip:has(input[name="tpl"])').first.click()
    options = page.locator("[data-variants] [data-variant]")
    options.first.wait_for()
    texts = options.all_inner_texts()
    assert len(texts) >= 3 and "Files as they are" in texts[0] and "fits in" in texts[0]
    assert any("Lossless → MP3 320k" in x for x in texts)
    best = page.locator('input[name="regVariant"]:checked')
    assert (
        best.count() == 1
        and "Best" in page.locator(f'[data-variant="{best.input_value()}"]').inner_text()
    )
    assert no_horizontal_scroll(page)
    shot(page, f"register-variants-{width}")
    mp3 = next(i for i, x in enumerate(texts) if "Lossless → MP3 320k" in x)
    page.locator(f'[data-variant="{mp3}"]').click()
    assert page.locator('input[name="procMode"][value="variant"]').is_checked()
    page.get_by_role("button", name="Register").click()
    page.wait_for_function("location.hash.includes('/overview')")
    dev = page.evaluate("decodeURIComponent(location.hash.split('/')[2])")
    cfg = _api(url, "GET", f"/devices/{dev}")["config"]
    assert cfg["processor_template_id"] is None
    rules = [
        r
        for p in cfg["processors"]
        if p["type"] == "transcode"
        for r in p["settings"].get("rules", [])
    ]
    assert any(r.get("to") == "mp3" and r.get("when") == "always" for r in rules)
    assert page.errors == []
