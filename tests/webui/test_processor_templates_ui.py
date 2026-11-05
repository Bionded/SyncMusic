"""Processor templates in the web UI (list + editor): create one from the
sidebar entry, Save opens it with its list item current, link it on a device's Processors tab, edit the template and
the device follows, detach, built-ins are read-only and copyable, deleting a linked one detaches the devices. Temp
folders only."""

from __future__ import annotations

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import link_rules, no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _device(url, tmp_path):
    (tmp_path / "stick").mkdir()
    return _api(
        url,
        "POST",
        "/devices/register",
        json={"name": "Stick", "mount_path": str(tmp_path / "stick"), "type": "flashdrive"},
    )


@pytest.mark.parametrize("width", [1280, 390])
def test_create_link_follow_detach(web_server, chrome, tmp_path, width):
    url, _ = web_server
    dev = _device(url, tmp_path)
    page = open_page(chrome, url + "/#/", width=width)
    if width < 600:
        page.locator("#burger").click()
    page.locator('[data-nav="proctemplates"]').click()
    page.wait_for_selector('[data-ptpl="builtin-ipod"]')
    assert "iPod: ALAC/AAC" in page.locator('[data-ptpl="builtin-ipod"]').inner_text()
    page.get_by_role("link", name="New processor template").click()
    page.wait_for_selector("#ptplName")
    page.fill("#ptplName", "Тільки теги")
    page.locator("#procAdd").select_option("retag")
    page.locator('[data-action="procAdd"]').click()
    page.wait_for_selector('[data-proc="0"]')
    page.locator('[data-action="procSave"]').click()
    # Save opens the new template, its list item current; on the phone the editor only
    page.wait_for_selector('.ptpl-item[aria-current="page"][data-ptpl]', state="attached")
    item = page.locator('.ptpl-item[aria-current="page"]')
    tid = item.get_attribute("data-ptpl")
    assert "Тільки теги" in item.text_content() and "Not used" in item.text_content()
    assert page.locator(f'[data-ptpl-editor="{tid}"] [data-proc="0"]').is_visible()
    assert page.locator(".ptpl-list").is_visible() == (width >= 760)
    # link it on the device
    page.goto(url + f"/#/devices/{dev['id']}/processors")
    page.wait_for_selector("[data-proc-template] #procTpl")
    page.locator("#procTpl").select_option(tid)
    assert page.locator("#procLink").is_checked()
    page.locator('[data-action="procApply"]').click()
    page.wait_for_selector(f'[data-linked="{tid}"]')
    cfg = _api(url, "GET", f"/devices/{dev['id']}")["config"]
    assert cfg["processor_template_id"] == tid and [s["type"] for s in cfg["processors"]] == [
        "transcode",  # the fallback Convert every chain gets
        "retag",
    ]
    # the template changes: the device follows
    page.goto(url + f"/#/processor-templates/{tid}")
    page.wait_for_selector("#procAdd")
    page.locator("#procAdd").select_option("lyrics")
    page.locator('[data-action="procAdd"]').click()
    page.wait_for_selector('[data-proc="1"]')
    page.locator('[data-action="procSave"]').click()
    page.wait_for_selector("[data-ptpl-used]")
    assert "Linked to Stick" in page.locator("[data-ptpl-used]").inner_text()
    assert "1 device" in page.locator('.ptpl-item[aria-current="page"]').text_content()
    assert page.locator('[data-proc="1"]').count() == 1  # the saved chain is shown
    cfg = _api(url, "GET", f"/devices/{dev['id']}")["config"]
    assert [s["type"] for s in cfg["processors"]] == ["transcode", "retag", "lyrics"]
    # detach on the device
    page.goto(url + f"/#/devices/{dev['id']}/processors")
    page.wait_for_selector(f'[data-linked="{tid}"]')
    page.locator('[data-action="procDetach"]').click()
    page.wait_for_function("() => !document.querySelector('[data-linked]')")
    assert _api(url, "GET", f"/devices/{dev['id']}")["config"]["processor_template_id"] is None
    assert no_horizontal_scroll(page)
    assert page.errors == []


def test_builtin_readonly_copy_and_delete_linked(web_server, chrome, tmp_path):
    url, _ = web_server
    dev = _device(url, tmp_path)
    page = open_page(chrome, url + "/#/processor-templates/builtin-rockbox")
    page.wait_for_selector('[data-proc="0"]')
    assert page.locator("#ptplName").get_attribute("readonly") is not None
    assert page.locator('[data-action="procSave"]').count() == 0
    assert page.locator("fieldset[disabled] [data-proc-enabled]").count() >= 1
    count = len(_api(url, "GET", "/processor-templates"))
    assert page.locator("[data-ptpl-readonly]").is_visible()
    assert page.locator('[data-action="ptplRemove"]').count() == 0
    page.locator('.ptpl-head [data-action="ptplCopy"]').click()
    # an unsaved draft
    page.wait_for_function("location.hash === '#/processor-templates/new?copy=builtin-rockbox'")
    page.wait_for_selector('[data-action="procSave"]')
    assert page.locator("#ptplName").input_value() == "Copy of Rockbox: MP3 320"
    assert len(_api(url, "GET", "/processor-templates")) == count
    page.fill("#ptplName", "Mini MP3")  # renamed before Save: the id follows the name
    assert page.locator("[data-ptpl-draft]").is_visible()
    page.locator('[data-action="procSave"]').click()
    page.wait_for_function("location.hash === '#/processor-templates/mini-mp3'")
    tid = page.locator('.ptpl-item[aria-current="page"]').get_attribute("data-ptpl")
    assert tid == "mini-mp3" and len(_api(url, "GET", "/processor-templates")) == count + 1
    _api(url, "PATCH", f"/devices/{dev['id']}", json={"processor_template_id": tid})
    page.goto(url + f"/#/processor-templates/{tid}")
    page.wait_for_selector('.ptpl-foot [data-action="ptplRemove"]')
    page.locator('.ptpl-foot [data-action="ptplRemove"]').click()
    modal = page.locator(".modal")
    modal.wait_for()
    assert "It is linked to Stick" in modal.inner_text()
    modal.get_by_role("button", name="Detach and delete").click()
    page.wait_for_function("location.hash === '#/processor-templates'")
    cfg = _api(url, "GET", f"/devices/{dev['id']}")["config"]
    assert cfg["processor_template_id"] is None and cfg["processors"][0]["type"] == "transcode"
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_processors_tab_suggests_a_template(web_server, chrome, tmp_path, width):
    """with libraries attached, the Processors tab shows the best template for them and its reason;
    Apply links it, then the line is gone (the device already follows it). Nothing switches by itself."""
    url, _ = web_server
    music = tmp_path / "music"
    for title in ("Група крові", "Зозуля"):
        make_music(music / "Скрябін" / f"{title}.mp3", title=title, artist="Скрябін", album="A")
    lib = {"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}}
    _api(url, "POST", "/libraries", json=lib)
    dev = _device(url, tmp_path)
    link_rules(url, dev["id"], {"rules": [{"id": "all"}]})
    sg = _api(url, "POST", "/devices/suggest-processor-template", json={"device_id": dev["id"]})
    assert sg["template_id"] and sg["reason"]["files"] > 0
    tpl = {"name": "Тільки теги", "processors": [{"type": "retag"}]}
    other = _api(url, "POST", "/processor-templates", json=tpl)["id"]
    assert other != sg["template_id"]
    _api(url, "PATCH", f"/devices/{dev['id']}", json={"processor_template_id": other})
    page = open_page(chrome, url + f"/#/devices/{dev['id']}/processors", width=width)
    line = page.locator(f'[data-proc-suggest="{sg["template_id"]}"]')
    line.wait_for()
    assert "Suggested:" in line.inner_text()
    shot(page, f"proc-suggest-{width}")
    assert _api(url, "GET", f"/devices/{dev['id']}")["config"]["processor_template_id"] == other
    page.locator('[data-action="procApplySuggested"]').click()
    page.wait_for_selector(f'[data-linked="{sg["template_id"]}"]')
    assert page.locator("[data-proc-suggest]").count() == 0
    cfg = _api(url, "GET", f"/devices/{dev['id']}")["config"]
    assert cfg["processor_template_id"] == sg["template_id"]
    assert no_horizontal_scroll(page)
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_processors_tab_offers_generated_chains(web_server, chrome, tmp_path, width):
    """The Processors tab lists the generated chains (size vs free space, best ticked); Use
    makes the ticked one the device's own steps."""
    url, _ = web_server
    music = tmp_path / "music"
    for title in ("Група крові", "Зозуля"):
        make_music(music / "Скрябін" / f"{title}.flac", title=title, artist="Скрябін", album="A")
    lib = {"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}}
    _api(url, "POST", "/libraries", json=lib)
    dev = _device(url, tmp_path)
    link_rules(url, dev["id"], {"rules": [{"id": "all"}]})
    page = open_page(chrome, url + f"/#/devices/{dev['id']}/processors", width=width)
    card = page.locator("[data-proc-variants]")
    card.wait_for()
    assert page.locator('input[name="procVariant"]:checked').count() == 1
    assert "Best" in card.inner_text()
    shot(page, f"proc-variants-{width}")
    last = page.locator("[data-proc-variants] [data-variant]").last
    label = last.inner_text()
    last.click()
    page.locator('[data-action="procUseVariant"]').click()
    page.wait_for_selector(".toast")
    cfg = _api(url, "GET", f"/devices/{dev['id']}")["config"]
    assert cfg["processor_template_id"] is None and cfg["processors"]
    assert "Lossless" in label or "lossless" in label
    assert no_horizontal_scroll(page)
    assert page.errors == []
