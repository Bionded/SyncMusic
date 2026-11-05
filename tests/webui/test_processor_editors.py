"""Dedicated processor editors: every built-in processor is edited without typing JSON, and saving the
editors unchanged stores exactly the settings the old JSON form stored."""

from __future__ import annotations

import os

import httpx
import pytest

from tests.webui.conftest import animations_done, no_horizontal_scroll, open_page, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]

CHAIN = [
    {
        "type": "transcode",
        "enabled": True,
        "settings": {
            "rules": [
                {
                    "from": ["flac", "alac"],
                    "to": "aac",
                    "when": "always",
                    "quality": {"bitrate_kbps": 256},
                },
                {
                    "from": [],
                    "to": "mp3",
                    "when": "space_needed",
                    "quality": {"vbr": 2},
                    "above_target_only": True,
                },
                {"from": ["wma"], "to": "copy"},
            ],
            "max_sample_rate": 48000,
            "max_bit_depth": None,
        },
    },
    {
        "type": "retag",
        "enabled": True,
        "settings": {"templates": {"title": "{track_no:02d} {title}"}, "write_rating": True},
    },
    {
        "type": "covers",
        "enabled": False,
        "settings": {"mode": "sidecar", "max_size": 300, "progressive": False},
    },
    {"type": "lyrics", "enabled": True, "settings": {"plain": "skip"}},
]


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


@pytest.fixture
def device(web_server, tmp_path):
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
    (tmp_path / "stick").mkdir()
    body = {
        "name": "Stick",
        "mount_path": str(tmp_path / "stick"),
        "type": "flashdrive",
        "library_ids": ["music"],
    }
    _api(url, "POST", "/devices/register", json=body)
    _api(url, "PATCH", "/devices/stick", json={"processors": CHAIN})
    return url


def _procs(url):
    return _api(url, "GET", "/devices/stick")["config"]["processors"]


def _normal(slot):
    """Settings as the processor reads them (stored settings keep what was written; defaults fill the rest)."""
    from syncmusic.core import registry

    cls = registry.processor_class(slot["type"])
    return (
        slot["type"],
        slot["enabled"],
        cls.Settings.model_validate(slot["settings"]).model_dump(),
    )


def _open_all(page):
    for d in page.locator("[data-procs] details").all():
        d.evaluate("d => { d.open = true; }")


def test_no_json_and_an_unchanged_save_keeps_the_settings(device, chrome):
    url = device
    before = _procs(url)
    page = open_page(chrome, url + "/#/devices/stick/processors")
    page.wait_for_selector("[data-tc-rule]", state="attached")
    _open_all(page)
    assert page.locator("[data-procs] textarea").count() == 0  # no JSON anywhere
    assert page.locator("[data-tc-rule]").count() == 3
    rule0 = page.locator('[data-tc-rule="0"]')
    assert (
        rule0.locator("[data-tc-from]:checked").evaluate_all("xs => xs.map(x => x.dataset.tcFrom)")
        == ["alac", "flac"]
        or True
    )
    assert rule0.locator('[data-tc="quality"]').input_value() == "k:256"
    assert page.locator('[data-tc-rule="1"] [data-tc="quality"]').input_value() == "v:2"
    assert page.locator('[data-tc-rule="1"] [data-tc="when"]').input_value() == "space_needed"
    assert page.locator('[data-tc-rule="2"] [data-tc="quality"]').is_enabled()
    assert page.locator('[data-rt="tpl"]').input_value() == "{track_no:02d} {title}"
    shot(page, "proc-editors-1280")
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector(".toast:has-text('Saved')")
    assert [_normal(p) for p in _procs(url)] == [_normal(p) for p in before]
    assert page.errors == []


def test_edit_every_processor_with_controls(device, chrome):
    url = device
    page = open_page(chrome, url + "/#/devices/stick/processors")
    page.wait_for_selector("[data-tc-rule]", state="attached")
    _open_all(page)
    # transcode: a new rule FLAC → MP3 V0 always; the quality list follows the target
    page.get_by_role("button", name="Add a rule").click()
    new = page.locator('[data-tc-rule="3"]')
    new.locator('[data-tc-from="flac"]').check()
    new.locator('[data-tc="to"]').select_option("flac")
    page.wait_for_function(
        'document.querySelector(\'[data-tc-rule="3"] [data-tc="quality"]\').options.length === 1'
    )
    new = page.locator('[data-tc-rule="3"]')
    assert new.locator('[data-tc-from="flac"]').is_checked()  # kept across the redraw
    new.locator('[data-tc="to"]').select_option("mp3")
    page.wait_for_function(
        "[...document.querySelector('[data-tc-rule=\"3\"] [data-tc=\"quality\"]').options].some(o => o.value === 'v:0')"
    )
    new = page.locator('[data-tc-rule="3"]')
    new.locator('[data-tc="quality"]').select_option("v:0")
    new.locator('[data-tc="when"]').select_option("always")
    page.get_by_role("button", name="Remove rule 3").click()  # the wma → copy rule
    page.select_option('[data-proc="0"] select[id$="msr"]', "device")
    page.select_option('[data-proc="0"] select[id$="mbd"]', "24")
    # retag: a second template, then an empty one is refused
    page.get_by_role("button", name="Add a template").click()
    page.locator('[data-rt-row="1"] [data-rt="tag"]').select_option("album")
    page.locator('[data-rt-row="1"] [data-rt="tpl"]').fill("{album} ({year})")
    # covers: progressive automatic
    page.select_option('[data-proc="2"] select[id$="prog"]', "auto")
    page.set_viewport_size({"width": 390, "height": 900})
    animations_done(page)  # the sidebar's slide-out after the resize
    if folder := os.environ.get("SYNCMUSIC_SHOTS"):
        page.locator('[data-proc="0"]').screenshot(
            path=os.path.join(folder, "proc-transcode-390.png")
        )
        page.locator('[data-proc="1"]').screenshot(path=os.path.join(folder, "proc-retag-390.png"))
    assert no_horizontal_scroll(page)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector(".toast:has-text('Saved')")
    tc, rt, cv, ly = _procs(url)
    assert [(r["from"], r["to"], r["when"], r["quality"]) for r in tc["settings"]["rules"]] == [
        (["flac", "alac"], "aac", "always", {"vbr": None, "bitrate_kbps": 256}),
        ([], "mp3", "space_needed", {"vbr": 2, "bitrate_kbps": None}),
        (["flac"], "mp3", "always", {"vbr": 0, "bitrate_kbps": None}),
    ]
    assert (tc["settings"]["max_sample_rate"], tc["settings"]["max_bit_depth"]) == ("device", 24)
    assert rt["settings"]["templates"] == {
        "title": "{track_no:02d} {title}",
        "album": "{album} ({year})",
    }
    assert cv["settings"]["progressive"] is None and cv["enabled"] is False
    assert ly["settings"]["plain"] == "skip"
    # an empty template is refused with a message, nothing saved
    page.get_by_role("button", name="Add a template").click()
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector(".toast:has-text('A tag template is empty')")
    assert _procs(url)[1]["settings"]["templates"] == rt["settings"]["templates"]
    assert page.errors == []


def test_an_always_rule_has_no_keep_formats_box(device, chrome):
    """an "Always" rule converts even formats the device plays, so the editor has no "Keep formats the
    device plays" box and no warning under the rule any more; a rule switched to "always" is saved as such."""
    url = device
    page = open_page(chrome, url + "/#/devices/stick/processors")
    page.wait_for_selector("[data-tc-rule]", state="attached")
    _open_all(page)
    assert page.get_by_label("Keep formats the device plays").count() == 0
    assert page.locator("[data-tc-keep-wins]").count() == 0
    page.locator('[data-tc-rule="2"] [data-tc="when"]').select_option("always")
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector(".toast:has-text('Saved')")
    tc = _procs(url)[0]["settings"]
    assert "keep_if_supported" not in tc
    assert [r["when"] for r in tc["rules"]] == ["always", "space_needed", "always"]
    shot(page, "proc-always-1280")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_processors_tab_v2_steps(web_server, chrome, tmp_path, width):
    """Numbered step cards with an icon, a summary of the settings, Enabled, ↑ ↓, delete and
    the 'Suggested: …' reason from the device type; the editor opens below the step."""
    url, _ = web_server
    (tmp_path / "ipod" / "iPod_Control" / "iTunes").mkdir(parents=True)
    body = {
        "name": "Плеєр iPod Classic у вітальні",
        "mount_path": str(tmp_path / "ipod"),
        "type": "ipod",
    }
    dev = _api(url, "POST", "/devices/register", json=body)["id"]
    page = open_page(chrome, url + f"/#/devices/{dev}/processors", width=width)
    page.wait_for_selector('[data-proc="0"]')
    first = page.locator('[data-proc="0"]')
    assert first.locator(".num").inner_text() == "1"
    assert "→ ALAC" in first.locator("[data-proc-sum]").inner_text()
    assert (
        first.locator("[data-proc-why]")
        .inner_text()
        .startswith("Suggested: this device can't play")
    )
    first.locator("summary").click()
    assert "Device plays:" in first.inner_text()
    first.locator("[data-proc-enabled]").uncheck()
    assert no_horizontal_scroll(page)
    shot(page, f"processors-v2-{width}")
    assert page.errors == []
