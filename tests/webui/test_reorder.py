"""Drag and drop to reorder: include rules in a template and processors in a chain move by their
grip with the mouse, by touch (pointer events) and by Alt+↑ / Alt+↓; the saved order is the order on screen.
Device templates: tests/webui/test_templates.py::test_device_priority_order."""

from __future__ import annotations

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _names(page, sel, field):
    return [x.input_value() if field else x.inner_text() for x in page.locator(sel).all()]


# a touch drag as a phone sends it: pointer events of type "touch" on the grip, then over the target row
TOUCH_DRAG = """([grip, target]) => {
  const g = document.querySelector(grip), t = document.querySelector(target);
  const at = (el) => { const r = el.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; };
  const ev = (type, el, p) => el.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true,
    pointerId: 7, pointerType: "touch", isPrimary: true, clientX: p.x, clientY: p.y }));
  const a = at(g), b = at(t);
  ev("pointerdown", g, a); ev("pointermove", g, b); ev("pointerup", g, b);
}"""


def test_template_rules_reorder(web_server, chrome, tmp_path):
    url, _ = web_server
    (tmp_path / "music").mkdir()
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "music",
            "type": "folder",
            "name": "Music",
            "settings": {"root": str(tmp_path / "music")},
        },
    )
    rules = [
        {"id": f"r{i}", "name": name, "criteria": {"all": []}}
        for i, name in enumerate(("Скрябін", "Тартак", "Антитіла"))
    ]
    tpl = _api(
        url, "POST", "/templates", json={"name": "Order", "library_ids": ["music"], "rules": rules}
    )
    page = open_page(chrome, url + f"/#/templates/{tpl['id']}")
    page.wait_for_selector('[data-rule="rules:2"] [data-grip]')
    names = lambda: _names(page, '[data-rule^="rules:"] [data-k="name"]', True)  # noqa: E731
    # mouse: the third rule's grip onto the first rule
    page.locator('[data-rule="rules:2"] [data-grip]').drag_to(page.locator('[data-rule="rules:0"]'))
    assert names() == ["Антитіла", "Скрябін", "Тартак"]
    assert page.locator(".ed-actions.dirty, [data-tpl-actions].dirty").count() == 1  # unsaved
    # keyboard: the focus stays on the moved rule's grip
    assert page.evaluate("document.activeElement.closest('[data-rule]').dataset.rule") == "rules:0"
    page.keyboard.press("Alt+ArrowDown")
    assert names() == ["Скрябін", "Антитіла", "Тартак"]
    page.keyboard.press("Alt+ArrowDown")
    assert names() == ["Скрябін", "Тартак", "Антитіла"]
    page.keyboard.press("Alt+ArrowDown")  # already last: nothing happens
    assert names() == ["Скрябін", "Тартак", "Антитіла"]
    # touch
    page.evaluate(TOUCH_DRAG, ['[data-rule="rules:0"] [data-grip]', '[data-rule="rules:1"]'])
    assert names() == ["Тартак", "Скрябін", "Антитіла"]
    assert (
        page.locator("[data-grip]").first.evaluate("e => getComputedStyle(e).touchAction") == "none"
    )
    shot(page, "template-rules-reorder-1280")
    page.set_viewport_size({"width": 390, "height": 800})
    assert no_horizontal_scroll(page)
    shot(page, "template-rules-reorder-390")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    saved = _api(url, "GET", f"/templates/{tpl['id']}")
    assert [r["name"] for r in saved["rules"]] == ["Тартак", "Скрябін", "Антитіла"]
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_processors_reorder(web_server, chrome, tmp_path, width):
    url, _ = web_server
    (tmp_path / "ipod" / "iPod_Control" / "iTunes").mkdir(parents=True)
    body = {"name": "iPod", "mount_path": str(tmp_path / "ipod"), "type": "ipod"}
    dev = _api(url, "POST", "/devices/register", json=body)["id"]
    before = [p["type"] for p in _api(url, "GET", f"/devices/{dev}")["config"]["processors"]]
    assert len(before) >= 3
    page = open_page(chrome, url + f"/#/devices/{dev}/processors", width=width)
    page.wait_for_selector('[data-proc="2"] [data-grip]')
    order = lambda: _names(page, "[data-proc] .proc-what .name", False)  # noqa: E731
    shown = order()
    if width == 1280:
        page.locator('[data-proc="2"] [data-grip]').drag_to(page.locator('[data-proc="0"]'))
    else:
        page.locator('[data-proc="0"]').scroll_into_view_if_needed()
        page.evaluate(TOUCH_DRAG, ['[data-proc="2"] [data-grip]', '[data-proc="0"]'])
    moved = [shown[2], shown[0], shown[1], *shown[3:]]
    assert order() == moved
    assert page.locator('[data-proc="0"] .num').inner_text() == "1"
    page.locator('[data-proc="0"] [data-grip]').focus()
    page.keyboard.press("Alt+ArrowDown")
    moved = [moved[1], moved[0], *moved[2:]]
    assert order() == moved
    assert page.evaluate("document.activeElement.closest('[data-proc]').dataset.proc") == "1"
    assert no_horizontal_scroll(page)
    shot(page, f"processors-reorder-{width}")
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector(".toast:has-text('Saved')")
    after = [p["type"] for p in _api(url, "GET", f"/devices/{dev}")["config"]["processors"]]
    assert after == [
        before[0],
        before[2],
        before[1],
        *before[3:],
    ]  # dragged 3rd to the top, then one down
    assert page.errors == []
