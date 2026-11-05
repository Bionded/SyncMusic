"""Processors tab: a chain that makes no sense shows a warning with a one-click fix."""

from __future__ import annotations

import pytest

from tests.webui.conftest import no_horizontal_scroll, open_page, shot
from tests.webui.test_plan_flow import _api, device  # noqa: F401 - fixture

pytestmark = [pytest.mark.ui, pytest.mark.slow]


@pytest.mark.parametrize("width", [1280, 390])
def test_chain_warnings_and_fix(device, chrome, width):  # noqa: F811
    url, _app, _stick = device
    page = open_page(chrome, url + "/#/devices/stick/processors", width)
    page.wait_for_selector("[data-procs] [data-proc]")
    assert page.locator("[data-chain-lint]").count() == 0  # the suggested chain is fine
    # switch the tag step off: the warning appears without a save
    retag = page.locator("[data-proc]", has=page.locator(".name", has_text="Normalise tags"))
    retag.locator("[data-proc-enabled]").uncheck()
    warn = page.locator('[data-chain-lint] [data-lint="no_tags"]')
    warn.wait_for()
    assert "library's" in warn.inner_text()
    shot(page, f"chain-lint-{width}")
    assert no_horizontal_scroll(page)
    warn.get_by_role("button", name="Switch it on").click()
    page.wait_for_function("!document.querySelector('[data-chain-lint]')")
    assert retag.locator("[data-proc-enabled]").is_checked()
    assert [e for e in page.errors if "Failed to load resource" not in e] == []
