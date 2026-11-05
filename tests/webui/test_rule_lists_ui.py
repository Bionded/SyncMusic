"""Rules builder: `is in` / `is not in` with values as chips."""

from __future__ import annotations

import httpx
import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_templates import server  # noqa: F401 - fixture

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_is_in_chips(server, chrome, width):  # noqa: F811
    url, _app, _ = server
    page = open_page(chrome, url + "/#/templates/new", width=width)
    page.wait_for_selector('[data-rule="rules:0"]')
    page.select_option("#tplLib", "music")
    page.fill("#tplName", "Скрябін і Тартак")
    page.get_by_role("button", name="Add a condition").click()
    cond = page.locator('[data-rule="rules:0"] [data-cond="0"]')
    cond.locator('[data-c="field"]').select_option("artist")
    cond = page.locator('[data-rule="rules:0"] [data-cond="0"]')
    cond.locator('[data-c="op"]').select_option("in")
    cond = page.locator('[data-rule="rules:0"] [data-cond="0"]')
    box = cond.locator("[data-chip-input]")
    box.fill("Скрябін, Тартак,")  # a comma adds a chip
    assert cond.locator(".vchip").count() == 2
    box.fill('"Rock, Paper"')
    box.press("Enter")  # Enter too; a quoted value keeps its comma
    texts = [x.strip() for x in cond.locator(".vchip").all_inner_texts()]
    assert texts == ["Скрябін", "Тартак", "Rock, Paper"], texts
    cond.get_by_role("button", name="Remove Rock, Paper").click()
    assert cond.locator(".vchip").count() == 2
    page.get_by_role("button", name="Test", exact=True).click()
    page.wait_for_selector("#tplResult:has-text('3 items')")
    assert no_horizontal_scroll(page)
    shot(page, f"rule-in-chips-{width}")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")
    tpl = next(
        x for x in httpx.get(f"{url}/api/v1/templates").json() if x["name"] == "Скрябін і Тартак"
    )
    assert tpl["rules"][0]["criteria"] == {"all": [{"in": {"artist": ["Скрябін", "Тартак"]}}]}
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_preview_per_rule(server, chrome, width):  # noqa: F811
    """the template preview shows every rule with its count and examples, flags a rule matching
    nothing, mixes the sample over the rules and downloads every match as CSV."""
    url, _app, _ = server
    rules = [
        {"id": "k", "name": "Скрябін", "criteria": {"all": [{"is": {"artist": "Скрябін"}}]}},
        {"id": "s", "name": "Тартак", "criteria": {"all": [{"is": {"artist": "Тартак"}}]}},
        {"id": "n", "name": "Ніхто", "criteria": {"all": [{"is": {"artist": "Ніхто"}}]}},
    ]
    r = httpx.post(
        f"{url}/api/v1/templates",
        json={"name": "Три правила", "rules": rules, "library_ids": ["music"]},
    )
    assert r.status_code == 201, r.text
    page = open_page(chrome, url + f"/#/templates/{r.json()['id']}", width=width)
    box = page.locator("[data-tpl-preview]")
    box.locator('[data-preview-rule="s"]').wait_for()
    assert "1 item" in box.locator('[data-preview-rule="s"]').inner_text()
    assert box.locator('[data-preview-rule="n"] [data-rule-nothing]').count() == 1
    sample = box.locator("[data-preview-sample]").inner_text()
    assert "Скрябін" in sample and "Тартак" in sample
    with page.expect_download() as info:
        box.get_by_role("button", name="Download all matches").click()
    assert info.value.suggested_filename == "Три правила-matches.csv"
    assert no_horizontal_scroll(page)
    shot(page, f"preview-per-rule-{width}")
    assert page.errors == []
