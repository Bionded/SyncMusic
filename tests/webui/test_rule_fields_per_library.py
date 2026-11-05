"""In the browser: a device's rule editor offers only the fields of its libraries - a Navidrome-only
device has no audiobook `author`, an Audiobookshelf-only one no `album`; a rule that already uses such a field still
shows it, marked."""

from __future__ import annotations

import httpx
import pytest

from syncmusic.core.models import LibraryConfig
from tests.webui.conftest import open_page, run_on

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=30, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _field_options(page) -> list[str]:
    page.wait_for_selector('[data-c="field"]')
    return page.locator('[data-c="field"]').first.locator("option").all_inner_texts()


def test_rule_fields_follow_the_libraries(web_server, chrome, tmp_path):
    url, app = web_server
    dead = "http://127.0.0.1:9"  # never contacted for the field list
    for lid, type_, settings in (
        ("nd", "subsonic", {"url": dead, "username": "u", "password": "p"}),
        ("abs", "audiobookshelf", {"url": dead, "api_token": "t"}),
    ):
        run_on(
            app,
            app.store.put_library(
                LibraryConfig(id=lid, type=type_, name=lid.upper(), settings=settings)
            ),
        )
    for lid, rule in (
        ("nd", {"id": "r", "criteria": {"all": [{"contains": {"artist": ""}}]}}),
        ("abs", {"id": "r", "criteria": {"all": [{"contains": {"album": "x"}}]}}),
    ):
        # rules live in templates: the editor offers the fields of the template's library
        body = {"name": f"Dev {lid}", "library_ids": [lid], "rules": [rule]}
        tpl = _api(url, "POST", "/templates", json=body)["id"]
        page = open_page(chrome, f"{url}/#/templates/{tpl}")
        options = _field_options(page)
        if lid == "nd":
            assert "album" in options and "author" not in options and "chapter" not in options
        else:
            assert "author" in options and "chapter" in options
            assert "album" not in options  # only the used one, marked:
            assert "album - not in these libraries" in options
            assert page.locator('[data-c="field"]').first.input_value() == "album"
        page.close()
