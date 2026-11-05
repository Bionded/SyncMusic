"""Pick from the library: the rule builder's searchable checklist of artists, albums, genres and
playlists builds the rule; works with a folder library and a Subsonic (fake Navidrome) one."""

from __future__ import annotations

import os

import httpx
import pytest

from tests.media import make_music
from tests.webui.conftest import no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _register(url, tmp_path, library):
    """A stick linking the template "Mine" (one rule, everything of `library`); its rules are edited in the
    template editor (a device has templates only)."""
    rules = [{"id": "mine", "name": "Mine", "criteria": {"all": []}}]
    _api(url, "POST", "/templates", json={"name": "Mine", "library_ids": [library], "rules": rules})
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Stick",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "rule_set": {"template_ids": ["mine"]},
    }
    _api(url, "POST", "/devices/register", json=body)


def _save(page):
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_selector(".row.highlight")  # back on the list


def _tick(page, tab, value, search=None):
    page.locator(f'[data-pick-field="{tab}"]').click()
    if search is not None:
        page.fill("#pickSearch", search)
    row = page.locator(".pick-row", has=page.locator(".pick-name", has_text=value)).first
    row.locator("input").check()


def _open_picker(page):
    page.locator('[data-rule="rules:0"] [data-action="rbPick"]').click()
    page.wait_for_selector("[data-picker] .pick-row")


def test_pick_two_artists_and_a_genre(web_server, chrome, tmp_path):
    url, _ = web_server
    music = tmp_path / "music"
    for artist, title, genre in (
        ("Скрябін", "Група крові", "Rock"),
        ("Скрябін", "Зозуля", "Rock"),
        ("Тартак", "Орбіт", "Rock"),
        ("Воплі Відоплясова", "Золоте місто", "Folk"),
        ("Zemfira", "Шукала", "Pop"),
    ):
        make_music(
            music / artist / f"{title}.mp3",
            title=title,
            artist=artist,
            album=f"{artist} LP",
            genre=genre,
        )
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    _register(url, tmp_path, "music")
    page = open_page(chrome, url + "/#/templates/mine")
    page.wait_for_selector('[data-rule="rules:0"]')
    _open_picker(page)
    # the most frequent first; a search of 3+ letters asks the service, 1-2 letters filter the loaded list
    assert page.locator(".pick-row .pick-name").first.inner_text() == "Скрябін"
    _tick(page, "artist", "Скрябін")
    _tick(page, "artist", "Тартак", search="тар")
    page.wait_for_function(
        "document.querySelectorAll('.pick-row').length === 2"
    )  # Тартак + the ticked Скрябін
    _tick(page, "genre", "Folk", search="fo")
    assert page.locator('[data-pick-n="artist"]').inner_text() == "(2)"
    _tick(page, "album", "Zemfira LP", search="")
    assert (
        "Zemfira" in page.locator(".pick-row", has_text="Zemfira LP").inner_text()
    )  # the album artist
    page.locator('.pick-row:has-text("Zemfira LP") input').uncheck()
    shot(page, "rule-picker-1280")
    page.locator(".modalfoot").get_by_role("button", name="Add 3 picks").click()
    page.wait_for_selector("[data-picker]", state="detached")
    rows = page.locator('[data-rule="rules:0"] .cond')
    assert rows.count() == 3
    assert page.locator('[data-rule="rules:0"] [data-k="match"]').input_value() == "any"
    _save(page)
    rs = _api(url, "GET", "/templates/mine")
    assert rs["rules"][0]["criteria"] == {
        "any": [
            {"is": {"artist": "Скрябін"}},
            {"is": {"artist": "Тартак"}},
            {"is": {"genre": "Folk"}},
        ]
    }
    res = _api(url, "POST", "/devices/stick/rules/test", json={})
    assert res["items"] == 4  # 2 Скрябін + Тартак + the Folk song
    # a rule that has conditions gets the picks as one "any of" group
    page.goto(url + "/#/templates/mine")
    page.wait_for_selector('[data-rule="rules:0"]')
    page.select_option(
        '[data-rule="rules:0"] [data-group=""] > .rb-group-head [data-k="match"]', "all"
    )
    _open_picker(page)
    _tick(page, "artist", "Zemfira")
    page.locator(".modalfoot").get_by_role("button", name="Add 1 pick").click()
    assert page.locator('[data-rule="rules:0"] .rb-group.nested').count() == 1
    # nothing ticked: Add keeps the dialog open
    _open_picker(page)
    add = page.locator(".modalfoot").get_by_role("button", name="Add", exact=True)
    assert add.get_attribute("data-ready") == "false"  # no "Add 0 picks", looks disabled
    add.click()
    assert page.locator("[data-picker]").count() == 1
    page.keyboard.press("Escape")
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(url + "/#/templates/mine")
    page.wait_for_selector('[data-rule="rules:0"]')
    _open_picker(page)
    _tick(page, "album", "Zemfira LP")
    if folder := os.environ.get(
        "SYNCMUSIC_SHOTS"
    ):  # the dialog as seen (a full-page shot misplaces it)
        page.screenshot(path=os.path.join(folder, "rule-picker-390.png"))
    assert page.evaluate(
        "(() => { const m = document.querySelector('.modal'); return m.scrollWidth <= m.clientWidth + 1; })()"
    )
    page.keyboard.press("Escape")
    assert no_horizontal_scroll(page)
    assert page.errors == []


def test_pick_a_playlist_from_navidrome(web_server, chrome, tmp_path, monkeypatch):
    from syncmusic.libraries.subsonic import SubsonicLibrary
    from tests.fake_navidrome import FakeNavidrome, song

    url, _ = web_server
    nd = FakeNavidrome(native=False)
    nd.add(song("s1", "Група крові"))
    nd.add(song("s2", "Зозуля"))
    nd.add(
        song(
            "s3",
            "Орбіт",
            artist="Тартак",
            displayArtist="Тартак",
            genre="Pop",
            genres=[{"name": "Pop"}],
        )
    )
    nd.playlists["p1"] = {"name": "Дорога", "entries": ["s1", "s3"]}
    monkeypatch.setattr(SubsonicLibrary, "transport", nd)
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "nd",
            "type": "subsonic",
            "name": "Navidrome",
            "settings": {"url": "http://nd.test", "username": "admin", "password": "pw"},
        },
    )
    _register(url, tmp_path, "nd")
    page = open_page(chrome, url + "/#/templates/mine")
    page.wait_for_selector('[data-rule="rules:0"]')
    _open_picker(page)
    _tick(page, "playlist", "Дорога")
    _tick(page, "genre", "Pop")
    page.locator(".modalfoot").get_by_role("button", name="Add 2 picks").click()
    _save(page)
    rs = _api(url, "GET", "/templates/mine")
    assert rs["rules"][0]["criteria"] == {
        "any": [{"is": {"genre": "Pop"}}, {"inPlaylist": {"name": "Дорога"}}]
    }
    assert _api(url, "POST", "/devices/stick/rules/test", json={})["items"] == 2
    assert page.errors == []


def test_pick_library_dropdown(web_server, chrome, tmp_path):
    """the picker browses one library at a time; one library → the dropdown is shown disabled."""
    url, _ = web_server
    for lib, artist in (("rock", "Скрябін"), ("pop", "Zemfira")):
        make_music(
            tmp_path / lib / artist / "a.mp3", title="A", artist=artist, album="LP", genre="X"
        )
        body = {
            "id": lib,
            "type": "folder",
            "name": lib.title(),
            "settings": {"root": str(tmp_path / lib)},
        }
        _api(url, "POST", "/libraries", json=body)
    rules = [{"id": "r", "name": "R", "criteria": {"all": []}}]
    _api(url, "POST", "/templates", json={"name": "One", "library_ids": ["rock"], "rules": rules})
    _api(
        url,
        "POST",
        "/templates",
        json={"name": "Both", "library_ids": ["rock", "pop"], "rules": rules},
    )
    page = open_page(chrome, url + "/#/templates/one")
    page.wait_for_selector('[data-rule="rules:0"]')
    _open_picker(page)
    sel = page.locator("#pickLibrary")
    assert sel.is_disabled() and sel.input_value() == "rock"
    assert "only Rock" in page.locator("[data-picker]").inner_text()
    shot(page, "rule-picker-one-library-1280")
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-picker]", state="detached")
    assert page.errors == []
    page = open_page(chrome, url + "/#/templates/both")  # a fresh page: no leave guard
    page.wait_for_selector('[data-rule="rules:0"]')
    _open_picker(page)
    assert not page.locator("#pickLibrary").is_disabled()
    assert page.locator(".pick-row .pick-name").all_inner_texts() == ["Скрябін"]
    page.select_option("#pickLibrary", "pop")
    page.wait_for_function(
        "document.querySelector('.pick-row .pick-name')?.textContent === 'Zemfira'"
    )
    shot(page, "rule-picker-libraries-1280")
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-picker]", state="detached")
    _open_picker(page)  # the last chosen library is remembered
    assert page.locator("#pickLibrary").input_value() == "pop"
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-picker]", state="detached")
    page.set_viewport_size({"width": 390, "height": 800})
    _open_picker(page)
    assert page.evaluate(
        "(() => { const m = document.querySelector('.modal'); return m.scrollWidth <= m.clientWidth + 1; })()"
    )
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-picker]", state="detached")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_audiobookshelf_picker_offers_books_podcasts_authors(
    web_server, chrome, tmp_path, monkeypatch, width
):
    """an Audiobookshelf library is picked by audiobooks, podcasts, episodes and authors, not by the
    music views; a podcast becomes a `series` condition (all its episodes), an author an `author` one."""
    from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
    from tests.fake_abs import FakeABS

    url, _ = web_server
    abs_ = FakeABS()
    abs_.add_book(
        "b1", "451° за Фаренгейтом", [("01.mp3", 60.0), ("02.mp3", 60.0)], author="Рей Бредбері"
    )
    abs_.add_book(
        "b2", "Mort", [("01.mp3", 60.0)], author="Terry Pratchett", series=(("Discworld", "4"),)
    )
    abs_.add_collection("c1", "Літо", ["b1"])  # collections are the playlists
    abs_.add_podcast(
        "p1", "Повна хронологія Warhammer 40000", [("e1", "Гор Луперкаль"), ("e2", "Ч. 2")]
    )
    monkeypatch.setattr(AudiobookshelfLibrary, "transport", abs_)
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "abs",
            "type": "audiobookshelf",
            "name": "ABS",
            "settings": {"url": "http://abs.test", "api_token": "tok"},
        },
    )
    _register(url, tmp_path, "abs")
    page = open_page(chrome, url + "/#/templates/mine", width=width)
    page.wait_for_selector('[data-rule="rules:0"]')
    _open_picker(page)
    tabs = page.locator("[data-pick-field]").evaluate_all(
        "els => els.map(e => e.dataset.pickField)"
    )
    assert tabs == ["audiobook", "podcast", "episode", "bookseries", "author", "genre", "playlist"]
    names = page.locator(".pick-row .pick-name").all_inner_texts()
    assert sorted(names) == ["451° за Фаренгейтом", "Mort"]  # books only, no podcast
    _tick(page, "podcast", "Повна хронологія Warhammer 40000")
    page.locator('[data-pick-field="episode"]').click()
    page.wait_for_function("document.querySelectorAll('.pick-row').length === 2")
    _tick(page, "bookseries", "Discworld")
    _tick(page, "author", "Terry Pratchett")
    page.locator('[data-pick-field="playlist"]').click()
    page.wait_for_function("document.querySelector('.pick-row .pick-name')?.textContent === 'Літо'")
    no_horizontal_scroll(page)
    shot(page, f"rule-picker-abs-{width}")
    page.locator(".modalfoot").get_by_role("button", name="Add 3 picks").click()
    page.wait_for_selector("[data-picker]", state="detached")
    _save(page)
    rs = _api(url, "GET", "/templates/mine")
    assert rs["rules"][0]["criteria"] == {
        "any": [
            {"is": {"series": "Повна хронологія Warhammer 40000"}},
            {"is": {"bookseries": "Discworld"}},
            {"is": {"author": "Terry Pratchett"}},
        ]
    }
    res = _api(url, "POST", "/devices/stick/rules/test", json={})
    assert res["items"] == 3  # both episodes + Mort
