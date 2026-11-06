"""Full UI click-through: every screen at phone and desktop width, every control pressed.

`CONTROLS` lists every `data-action` of the web UI sources (per view module) with how it is exercised: `click` = pressed
by the generic pass below on a seeded instance (a folder library with a playlist, a synced folder device, a second
device, a template, a finished job); a dialog it opens is answered as given (`cancel` or `ok`); `elsewhere` names the
test that exercises it in a state this fixture does not have (a running job, a plan that does not fit, a disk). A new
`data-action` in a view fails `test_every_control_is_listed` until it is added here.

The generic pass opens every screen fresh for each control, presses it (a form: its submit button), answers a dialog,
and requires: no script error, no horizontal page scroll (also with the dialog open), the screen still renders.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from syncmusic.core.detection import ConnectedDisk
from syncmusic.devices._filedevice import FileDeviceMixin
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from tests.media import make_music
from tests.webui.conftest import (
    events_seen,
    no_horizontal_scroll,
    open_page,
    requests_done,
    run_on,
    settle,
    shot,
    wait_events,
)

pytestmark = [pytest.mark.ui, pytest.mark.slow]

JS = Path(__file__).parents[2] / "syncmusic" / "webui" / "static" / "js"

CLICK, CANCEL, OK = "click", "cancel", "ok"

# "module:action" → (how, dialog answer or the test that covers it)
CONTROLS: dict[str, tuple[str, str]] = {
    # shared
    "ui:retry": ("elsewhere", "test_click_through.py::test_error_box_retry"),
    "rulebuilder:rbAddRule": (CLICK, ""),
    "rulebuilder:rbRemoveRule": (CLICK, ""),
    "rulebuilder:rbFlip": (CLICK, ""),
    "rulebuilder:rbAddCond": (CLICK, ""),
    "rulebuilder:rbRemoveCond": (CLICK, ""),
    "rulebuilder:rbChipRemove": ("elsewhere", "test_rule_lists_ui.py::test_is_in_chips"),
    "rulebuilder:rbInsertCond": (CLICK, ""),
    "rulebuilder:rbAddGroup": (CLICK, ""),
    "rulebuilder:rbRemoveGroup": (CLICK, ""),
    "rulebuilder:rbPick": ("elsewhere", "test_rule_picker.py::test_pick_two_artists_and_a_genre"),
    "rulebuilder:rbPreset": (
        "elsewhere",
        "test_rule_preview_ui.py::test_a_preset_is_added_in_one_click",
    ),
    # dashboard
    "dashboard:cancelJob": (
        "elsewhere",
        "test_dashboard.py::test_dashboard (Cancel in place)",
    ),
    "dashboard:gentleOn": ("elsewhere", "test_dashboard.py::test_dashboard_v2_rows"),
    "dashboard:dismissNeed": ("elsewhere", "test_dashboard.py::test_needs_attention_rows"),
    "dashboard:syncAll": ("elsewhere", "test_sync_all_ui.py::test_sync_all_buttons"),
    "processors:procUseVariant": (
        "elsewhere",
        "test_processor_templates_ui.py::test_processors_tab_offers_generated_chains",
    ),
    "plan:planUseSuggested": (
        "elsewhere",
        "test_plan_flow.py::test_plan_review_suggests_processors_for_the_plan",
    ),
    "dashboard:phoneEject": ("elsewhere", "test_dashboard.py (phone cards)"),
    # libraries
    "libraries:add": (CLICK, ""),
    "libraries:save": (CLICK, ""),
    "libraries:checkOne": (CLICK, ""),
    "libraries:refresh": (CLICK, ""),
    "libraries:refreshAll": ("elsewhere", "test_libraries.py::test_library_cards_v2"),
    "libraries:remove": (CLICK, CANCEL),
    "libraries:pickType": (CLICK, ""),
    "libraries:search": (CLICK, ""),
    "libraries:setView": ("elsewhere", "test_libraries.py::test_library_browser_v2"),
    "libraries:clearSel": ("elsewhere", "test_libraries.py::test_library_browser_v2"),
    "libraries:openAdd": ("elsewhere", "test_libraries.py::test_library_browser_v2"),
    # devices (overview, contents, libraries, sync, settings)
    "devices:ignoreDisk": (
        "elsewhere",
        "test_devices.py::test_register_a_detected_disk_from_the_list",
    ),
    "devices:repairFs": ("elsewhere", "test_fs_damaged_ui.py::test_damaged_file_system_banner"),
    "devices:eject": (
        "elsewhere",
        "test_plan_flow.py (eject after a sync); disabled for folder devices",
    ),
    "devices:format": (
        "elsewhere",
        "test_devices.py::test_format_needs_the_typed_name; disk devices only",
    ),
    "devices:bind": ("elsewhere", "test_click_through.py::test_bind_a_folder_device_to_its_disk"),
    "devices:remove": (CLICK, CANCEL),
    "devices:wipe": ("elsewhere", "test_devices.py::test_danger_zone_per_type"),
    "devices:feedbackOpen": ("elsewhere", "test_feedback_ui.py::test_report_line_in_history"),
    "feedback:feedbackOpen": ("elsewhere", "test_feedback_ui.py::test_not_delivered_is_visible"),
    "devices:updateNow": (CLICK, ""),
    "devices:integrityRun": ("elsewhere", "test_integrity_ui.py::test_integrity_check_card"),
    "devices:integrityFix": ("elsewhere", "test_integrity_ui.py::test_integrity_check_card"),
    "devices:gentleOn": (
        "elsewhere",
        "test_device_overview.py::test_storage_failure_offers_gentle_mode_on_the_overview",
    ),
    "contents:pcPlay": (CLICK, ""),
    "contents:pcRefresh": (CLICK, ""),
    "contents:pcView": (
        "elsewhere",
        "test_player_browser.py::test_player_contents_grid_list_sort_and_range_select",
    ),
    "contents:pcSearch": (CLICK, ""),
    "contents:pcRemove": (
        "elsewhere",
        "test_player_browser.py::test_player_contents_tabs_search_and_foreign_items",
    ),  # the selection bar's button shows only with a selection
    "contents:pcClear": (
        "elsewhere",
        "test_player_browser.py::test_player_contents_tabs_search_and_foreign_items",
    ),
    "settings:installUpdate": (
        "elsewhere",
        "test_update_button.py::test_update_button_asks_then_installs",
    ),
    "welcome:wizNext": ("elsewhere", "test_welcome.py::test_first_run_wizard"),
    "welcome:wizBack": ("elsewhere", "test_welcome.py::test_first_run_wizard"),
    "welcome:wizLibType": ("elsewhere", "test_welcome.py::test_first_run_wizard"),
    "welcome:wizSkip": ("elsewhere", "test_welcome.py::test_wizard_skip_and_existing_library"),
    "devices:ftpTest": ("elsewhere", "test_ftp_register.py::test_ftp_test_and_save"),
    "devices:removeOrphans": (CLICK, CANCEL),
    "devices:playLog": (
        "elsewhere",
        "test_devices.py::test_rockbox_play_log_toggle_in_device_settings",
    ),
    # rules tab, processors tab
    # the Templates tab's edits leave unsaved changes (the leave guard would stop the walk) - tested on their own
    "rules:rulesAdd": (
        "elsewhere",
        "test_device_templates_only.py::test_the_picker_adds_removes_and_saves",
    ),
    "rules:rulesMove": (
        "elsewhere",
        "test_templates.py::test_a_default_is_editable_copy_and_delete_in_use",
    ),
    "rules:rulesRemove": (
        "elsewhere",
        "test_device_templates_only.py::test_the_picker_adds_removes_and_saves",
    ),
    "plan:planExport": (
        "elsewhere",
        "test_plan_flow.py::test_the_plan_says_why_and_downloads_the_list",
    ),
    "notifications:open": ("elsewhere", "test_notifications.py::test_bell_list_read_clear"),
    "notifications:clearOne": ("elsewhere", "test_notifications.py::test_bell_list_read_clear"),
    "notifications:clearAll": ("elsewhere", "test_notifications.py::test_bell_list_read_clear"),
    "processors:chainRun": ("elsewhere", "test_chain_preview_ui.py::test_preview_on_one_track"),
    "processors:procFix": ("elsewhere", "test_chain_lint_ui.py::test_chain_warnings_and_fix"),
    "processors:procAdd": (CLICK, ""),
    "processors:procMove": (CLICK, ""),
    "processors:procRemove": (CLICK, ""),
    "processors:procSuggest": (CLICK, ""),
    # the buttons are drawn by js/proceditors.js, handled by the chain editor
    "proceditors:procRowAdd": (
        "elsewhere",
        "test_processor_editors.py::test_edit_every_processor_with_controls",
    ),
    "proceditors:procRowRemove": (
        "elsewhere",
        "test_processor_editors.py::test_edit_every_processor_with_controls",
    ),
    "processors:procSave": (CLICK, ""),
    "processors:procApply": (CLICK, ""),
    "processors:procDetach": (
        "elsewhere",
        "test_processor_templates_ui.py::test_create_link_follow_detach (only shown while linked)",
    ),
    "processors:procApplySuggested": (
        "elsewhere",
        "test_processor_templates_ui.py::test_processors_tab_suggests_a_template",
    ),
    "proctemplates:ptplCopy": (CLICK, ""),
    "proctemplates:ptplRemove": (CLICK, CANCEL),
    "proctemplates:procSave": (CLICK, ""),
    # registration
    "register:register": (CLICK, ""),
    "libraries:libFind": ("elsewhere", "test_discovery_ui.py::test_find_servers_and_shares"),
    "libraries:libPickServer": ("elsewhere", "test_discovery_ui.py::test_find_servers_and_shares"),
    "libraries:libShares": ("elsewhere", "test_discovery_ui.py::test_find_servers_and_shares"),
    "libraries:libPickShare": ("elsewhere", "test_discovery_ui.py::test_find_servers_and_shares"),
    "templates:tplMatchesCsv": ("elsewhere", "test_rule_lists_ui.py::test_preview_per_rule"),
    "picker:pickOpen": ("elsewhere", "test_path_picker.py::test_local_and_smb_library_root"),
    "register:regFind": (
        "elsewhere",
        "test_discovery_ui.py::test_register_an_ftp_device_from_the_search",
    ),
    "register:regPickServer": (
        "elsewhere",
        "test_discovery_ui.py::test_register_an_ftp_device_from_the_search",
    ),
    # plan, progress / report
    "plan:replan": (CLICK, ""),
    "plan:planCancel": (
        "elsewhere",
        "test_plan_progress_ui.py::test_planning_progress_cancel_and_rejoin",
    ),
    "plan:planGroup": (CLICK, ""),
    "plan:noCacheOn": ("elsewhere", "test_plan_flow.py::test_no_cache_suggestion_in_the_plan"),
    "plan:gentleOn": ("elsewhere", "test_plan_flow.py::test_gentle_warnings_in_the_plan"),
    "plan:apply": (CLICK, CANCEL),
    "plan:force": ("elsewhere", "test_plan_flow.py::test_force_needs_two_confirmations"),
    "plan:fitApply": ("elsewhere", "test_plan_flow.py::test_transcode_to_fit"),
    "progress:gentleOn": ("elsewhere", "test_overlays.py::test_gentle_mode_suggestion_with_undo"),
    "progress:cancel": ("elsewhere", "test_click_through.py::test_cancel_a_running_sync"),
    "progress:eject": (
        "elsewhere",
        "test_plan_flow.py (eject from the report); disabled for folder devices",
    ),
    # templates, settings, logs
    "libraries:retry": (
        "elsewhere",
        "test_library_cache.py::test_cached_browsing_updates_and_refresh (the new-data notice)",
    ),
    "job:jobCancel": (
        "elsewhere",
        "test_job_views.py::test_jobs_open_their_views (only while a job runs)",
    ),
    "templates:copy": (CLICK, ""),
    "templates:remove": (CLICK, CANCEL),
    "templates:reset": ("elsewhere", "test_templates_list.py::test_templates_list_v2"),
    "templates:save": (CLICK, ""),
    "templates:test": (CLICK, ""),
    "templates:tplView": (CLICK, ""),
    "templates:tplPick": (
        "elsewhere",
        "test_templates.py::test_template_editor_v2 (in the Pick view)",
    ),
    "templates:discard": (
        "elsewhere",
        "test_templates.py::test_template_editor_v2 (only with unsaved changes)",
    ),
    # the side index is hidden on the phone (a jump menu instead); the save is the unsaved bar's
    "settings:jump": ("elsewhere", "test_settings.py::test_change_every_setting_and_reload"),
    "settings:testWebhook": (CLICK, ""),
    "settings:checkFfmpeg": (CLICK, ""),
    "settings:runDiag": (CLICK, ""),
    "settings:clearCache": ("elsewhere", "test_settings.py::test_cache_and_space"),
    "settings:clearSysCache": ("elsewhere", "test_settings.py::test_system_cache"),
    "settings:factoryReset": ("elsewhere", "test_settings.py::test_maintenance"),
    "settings:restoreCancel": ("elsewhere", "test_settings.py::test_backup_and_restore"),
    "settings:restoreDo": ("elsewhere", "test_settings.py::test_backup_and_restore"),
    "settings:dbBackupNow": ("elsewhere", "test_settings.py::test_db_backups"),
    "settings:dbRestore": ("elsewhere", "test_settings.py::test_db_backups"),
    "settings:checkUpdates": ("elsewhere", "test_settings.py::test_updates"),
    "settings:importCancel": ("elsewhere", "test_settings.py::test_import_export"),
    "settings:importDo": ("elsewhere", "test_settings.py::test_import_export"),
    # switching it here would leave the browser in the light theme for the rest of the walk
    "settings:theme": ("elsewhere", "test_theme.py::test_theme_switch_applies_before_paint"),
    **{
        f"profile:{a}": (
            "elsewhere",
            "test_device_profile_ui.py::test_profile_card_override_and_reset",
        )
        for a in ("profileEdit", "profileCancel", "profileSave", "profileReset", "profileResetAll")
    },
    "logs:filter": (
        "elsewhere",
        "test_logs.py::test_filter_live_and_download (selects apply at once, text on Enter)",
    ),
    "logs:copyExc": (
        "elsewhere",
        "test_logs.py::test_filter_live_and_download (an error line with a traceback)",
    ),
    "settings:accessPassword": ("elsewhere", "test_access_ui.py::test_access_login"),
    "settings:accessToken": ("elsewhere", "test_access_ui.py::test_access_login"),
    "settings:accessRevoke": ("elsewhere", "test_access_ui.py::test_access_revoke_and_sign_out"),
    "settings:accessLogout": ("elsewhere", "test_access_ui.py::test_access_revoke_and_sign_out"),
    "settings:accessEverywhere": (
        "elsewhere",
        "test_access_ui.py::test_access_revoke_and_sign_out",
    ),
    "libraries:addAccount": (
        "elsewhere",
        "test_library_accounts.py::test_library_accounts_and_the_device_pick",
    ),
    "libraries:editAccount": (
        "elsewhere",
        "test_library_accounts.py::test_library_accounts_and_the_device_pick",
    ),
    "libraries:removeAccount": (
        "elsewhere",
        "test_library_accounts.py::test_library_accounts_and_the_device_pick",
    ),
}


def _source_actions() -> set[str]:
    """Every data-action of the UI sources as module:action (a `${a ? "x" : "y"}` form names both)."""
    out = set()
    for f in [*JS.glob("*.js"), *JS.glob("views/*.js")]:
        for m in re.finditer(r'data-action="(\$\{[^}]*\}|[^"$]+)"', f.read_text()):
            names = re.findall(r'"(\w+)"', m.group(1)) if "${" in m.group(1) else [m.group(1)]
            out.update(f"{f.stem}:{n}" for n in names)
    return out


def test_every_control_is_listed():
    found = _source_actions()
    assert found - CONTROLS.keys() == set(), "new controls: add them to CONTROLS"
    assert CONTROLS.keys() - found == set(), "controls that no longer exist"
    elsewhere = [v[1] for v in CONTROLS.values() if v[0] == "elsewhere"]
    for ref in elsewhere:  # the named test exists
        file, _, name = re.split(r"[ ;]", ref)[0].partition("::")
        text = (Path(__file__).parent / file).read_text()
        assert not name or f"def {name}(" in text, ref


def _api(url, method, path, **kw):
    r = httpx.request(method, f"{url}/api/v1{path}", timeout=60, **kw)
    assert r.status_code < 400, r.text
    return r.json() if r.content else None


def _wait_job(url, job_id):
    for _ in range(600):
        if _api(url, "GET", f"/jobs/{job_id}")["status"] not in ("queued", "running"):
            return
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish")


@pytest.fixture
def seeded(web_server, tmp_path):
    url, app = web_server
    music = tmp_path / "music"
    for artist, title in (("Скрябін", "Група крові"), ("Скрябін", "Зозуля"), ("Тартак", "Орбіт")):
        make_music(
            music / artist / f"{title}.mp3", title=title, artist=artist, album=f"{artist} album"
        )
    (music / "XXX.m3u8").write_text("Скрябін/Група крові.mp3\nТартак/Орбіт.mp3\n")
    _api(
        url,
        "POST",
        "/libraries",
        json={"id": "music", "type": "folder", "name": "Music", "settings": {"root": str(music)}},
    )
    kino = [
        {"id": "skryabin", "name": "Скрябін", "criteria": {"any": [{"is": {"artist": "Скрябін"}}]}}
    ]
    _api(
        url, "POST", "/templates", json={"name": "Скрябін", "library_ids": ["music"], "rules": kino}
    )
    for name in ("Stick", "Spare"):
        (tmp_path / name.lower()).mkdir()
        _api(
            url,
            "POST",
            "/devices/register",
            json={
                "name": name,
                "mount_path": str(tmp_path / name.lower()),
                "type": "flashdrive",
                "rule_set": {"template_ids": ["default.music.all", "скрябін"]},
            },
        )
    _api(url, "POST", "/processor-templates", json={"name": "Mine", "processors": []})
    _api(
        url,
        "POST",
        "/templates",
        json={
            "name": "Nested",
            "library_id": "music",
            "rules": [
                {
                    "id": "r1",
                    "name": "r1",
                    "criteria": {
                        "any": [
                            {
                                "all": [
                                    {"inPlaylist": {"name": "XXX"}},
                                    {"contains": {"artist": "К"}},
                                ]
                            },
                            {"is": {"artist": "Тартак"}},
                        ]
                    },
                }
            ],
            "excludes": [{"id": "x1", "name": "x1", "criteria": {"is": {"title": "Зозуля"}}}],
        },
    )
    # a manual item whose source then goes: an orphan on the device (Device settings → Remove orphans)
    extra = music / "Gone" / "Зниклий.mp3"
    make_music(extra, title="Зниклий", artist="Gone", album="Gone")
    _api(url, "POST", "/libraries/music/refresh")
    item = _api(url, "GET", "/libraries/music/items", params={"query": "Зниклий"})["items"][0]
    _api(
        url,
        "POST",
        "/devices/stick/manual-selection",
        json={"library_id": "music", "ids": [item["item_id"]]},
    )
    plan = _api(url, "POST", "/devices/stick/plan")
    job = _api(
        url, "POST", f"/plans/{plan['id']}/apply", json={"confirm": True, "first_sync": "keep"}
    )
    _wait_job(url, job["id"])
    extra.unlink()
    _api(url, "POST", "/libraries/music/refresh")
    return url, job["id"]


def _screens(job_id: str) -> list[tuple[str, str]]:
    """(view module, route) of every screen; `{plan}` is a fresh plan (earlier presses outdate plans)."""
    dev = [
        ("devices", f"/devices/stick/{tab}")
        for tab in ("overview", "contents", "libraries", "settings", "history")
    ]
    return [
        ("dashboard", "/"),
        ("libraries", "/libraries"),
        ("libraries", "/libraries/new"),
        ("libraries", "/libraries/music"),
        ("libraries", "/libraries/music/browse"),
        ("devices", "/devices"),
        ("register", "/devices/register"),
        *dev,
        ("rules", "/devices/stick/rules"),
        ("processors", "/devices/stick/processors"),
        ("plan", "/devices/spare/plan/{plan}"),
        ("progress", f"/devices/stick/sync/{job_id}"),
        ("templates", "/templates"),
        ("templates", "/templates/new"),
        ("templates", "/templates/nested"),
        ("templates", "/templates/default.music.all"),
        ("proctemplates", "/processor-templates"),
        ("proctemplates", "/processor-templates/new"),
        ("proctemplates", "/processor-templates/builtin-ipod"),
        ("proctemplates", "/processor-templates/mine"),
        ("settings", "/settings"),
        ("logs", "/logs"),
    ]


def _key(module: str, action: str) -> str:
    if module in ("rules", "processors") and action in ("reverse", "eject", "updateNow"):
        return f"devices:{action}"  # the device header is on every device tab
    if action.startswith("rb"):
        return f"rulebuilder:{action}"
    if action.startswith("pick") and action != "pickType":  # the path picker lives in js/picker.js
        return f"picker:{action}"
    if action == "retry":
        return "ui:retry"
    if action.startswith("profile"):  # the device profile card lives in views/profile.js
        return f"profile:{action}"
    if action.startswith("pc"):  # Player contents lives in views/contents.js
        return f"contents:{action}"
    if action in ("procRowAdd", "procRowRemove"):  # drawn by js/proceditors.js
        return f"proceditors:{action}"
    # the template page's own Save
    if module == "proctemplates" and action == "procSave":
        return f"{module}:{action}"
    if action.startswith(
        "proc"
    ):  # the chain editor (views/processors.js) also edits processor templates
        return f"processors:{action}"
    return f"{module}:{action}"


def _busy(url) -> bool:
    return any(d["busy"] for d in _api(url, "GET", "/devices")["registered"])


def _job_ids(url) -> list[str]:
    return [j["id"] for j in _api(url, "GET", "/jobs")]


def _open(page, url, route):
    """The screen, freshly loaded (no state left from an earlier press) once no device is busy: an earlier press
    (a library refresh, a reverse sync) starts automatic update syncs, and a screen opened during one
    shows its busy state (Player contents: nothing to press; Device settings without Remove orphans and the danger
    zone) instead of the controls this pass is after. An update that starts while the screen loads makes it load
    again (under load it started right after the check)."""
    for _ in range(3):
        end = time.monotonic() + 30
        while _busy(url) and time.monotonic() < end:
            time.sleep(0.2)
        jobs = _job_ids(url)
        if page.url.endswith(f"#{route}"):
            page.reload()
        else:
            page.goto(f"{url}/#{route}")
        page.wait_for_function(f"document.getElementById('content').dataset.route === {route!r}")
        page.wait_for_selector("#content h1")
        settle(page, quiet_ms=200)  # views that load a second time (live state) settle
        if not _busy(url) and _job_ids(url) == jobs:  # no job came and went meanwhile
            return


# the visible, enabled controls of the content: [data-action] buttons/links, and forms by their submit button
CONTROLS_JS = """() => {
  const vis = (el) => el.offsetParent !== null && !el.matches(':disabled') && el.getAttribute('aria-disabled') !== 'true';
  const out = [];
  document.querySelectorAll('#content [data-action]').forEach((el) => {
    if (el.tagName === 'FORM') {
      const b = el.querySelector('button[type=submit], button:not([type])');
      if (b && vis(b)) out.push(el.dataset.action);
    } else if (vis(el)) out.push(el.dataset.action);
  });
  return out;
}"""

CLICK_JS = """([action, n]) => {
  document.querySelectorAll('[data-click-me]').forEach((el) => el.removeAttribute('data-click-me'));
  const vis = (el) => el.offsetParent !== null && !el.matches(':disabled') && el.getAttribute('aria-disabled') !== 'true';
  const els = [...document.querySelectorAll('#content [data-action]')].filter((el) => el.dataset.action === action)
    .map((el) => el.tagName === 'FORM' ? el.querySelector('button[type=submit], button:not([type])') : el)
    .filter((el) => el && vis(el));
  if (!els[n]) return false;
  els[n].setAttribute('data-click-me', '1');
  return true;
}"""


def _press(page, module: str, action: str, n: int) -> bool:
    """Press the n-th visible `action` control and answer its dialog; False when it is not there."""
    if not page.evaluate(CLICK_JS, [action, n]):
        return False
    page.locator("[data-click-me]").click()
    settle(page, quiet_ms=200)  # the action's request, or its dialog
    modal = page.locator(".modal")
    if modal.count():
        assert no_horizontal_scroll(page), f"{action}: dialog scrolls sideways"
        how, answer = CONTROLS[_key(module, action)]
        buttons = modal.locator(".modalfoot button")
        if answer == OK and buttons.count():
            buttons.last.click()
        else:
            modal.locator('[data-v="__close"]').click()
        page.wait_for_function("!document.querySelector('.modal')", timeout=10_000)
    assert page.locator(".modal").count() == 0, f"{action}: dialog still open"
    return True


def _benign(err: str) -> bool:
    # an action the fixture refuses (e.g. eject of a folder device) answers 4xx: the toast shows it
    return "Failed to load resource" in err and "status of 5" not in err


@pytest.mark.parametrize("width", [390, 1280])
def test_click_through(seeded, chrome, width):
    url, job_id = seeded
    page = open_page(chrome, url, width, 844 if width < 600 else 900)
    open_requests: dict[object, str] = {}  # what the page still waits for (in a failure's message)
    page.on("request", lambda r: open_requests.__setitem__(r, f"{r.method} {r.url}"))
    page.on("requestfinished", lambda r: open_requests.pop(r, None))
    page.on("requestfailed", lambda r: open_requests.pop(r, None))
    seen: set[str] = set()
    pressed: set[str] = set()

    def fresh(route: str) -> str:
        if "{plan}" not in route:
            return route
        return route.format(plan=_api(url, "POST", "/devices/spare/plan")["id"])

    for module, screen in _screens(job_id):
        route = fresh(screen)
        _open(page, url, route)
        shot(page, f"click-{width}-{route.strip('/').replace('/', '-') or 'home'}")
        actions = page.evaluate(CONTROLS_JS)
        for action in actions:
            assert _key(module, action) in CONTROLS, f"{route}: {action} is not in CONTROLS"
        counts: dict[str, int] = {}
        for action in actions:
            n = counts.get(action, 0)
            counts[action] = n + 1
            key = _key(module, action)
            seen.add(key)
            if CONTROLS[key][0] != CLICK or n >= 3:  # three of a kind (rows) are enough
                continue
            route = fresh(screen)
            _open(page, url, route)
            if not _press(page, module, action, n):
                continue  # gone after a fresh load (an earlier press changed the state, e.g. removed a row)
            pressed.add(key)
            errors = [e for e in page.errors if not _benign(e)]
            assert errors == [], f"{route} {action}: {errors}"
            assert no_horizontal_scroll(page), f"{route} {action}"
            # a press may navigate (Plan again shows the planning screen, then the new plan: a new plan takes
            # longer while other workers load the host); the planning screen's heading is swapped for the new
            # plan's, so after a navigation the page settles first and a heading caught mid-swap does not count
            if page.evaluate("location.hash") != "#" + route:
                with contextlib.suppress(PlaywrightTimeout):
                    page.wait_for_selector("#content h1", timeout=60_000)
                settle(page, quiet_ms=500)
            with contextlib.suppress(PlaywrightTimeout):
                page.wait_for_selector("#content h1", timeout=60_000)
            assert page.locator("#content h1").count() == 1, "\n".join(
                [
                    f"{route} {action}: screen gone, now at {page.evaluate('location.hash')}",
                    f"shown: {page.inner_text('#content')[:400]!r}",
                    "requests still open:",
                    *sorted(v for v in open_requests.values() if "/events" not in v),
                ]
            )
    clickable = {k for k, v in CONTROLS.items() if v[0] == CLICK}
    assert clickable - seen == set(), "listed as clicked here but not on any screen"
    assert clickable - pressed == set(), "listed as clicked here but never pressed"


def test_error_box_retry(web_server, chrome):
    """A screen that fails to load shows the error with Retry; Retry loads it again (here: once the library
    exists)."""
    url, _ = web_server
    page = open_page(chrome, url + "/#/libraries/later")
    page.wait_for_selector(".errorbox [data-action='retry']")
    _api(
        url,
        "POST",
        "/libraries",
        json={
            "id": "later",
            "type": "folder",
            "name": "Later",
            "settings": {"root": "/nonexistent"},
        },
    )
    page.locator(".errorbox [data-action='retry']").click()
    page.wait_for_selector("#content h1:has-text('Later')")
    assert page.locator(".errorbox").count() == 0
    assert [e for e in page.errors if not _benign(e)] == []


def test_bind_a_folder_device_to_its_disk(seeded, chrome, web_server):
    """A folder device whose directory is on a connected disk offers "Bind"; it calls bind-disk (the binding itself
    is covered by tests/unit/test_bind_disk.py)."""
    url, app = web_server
    calls: list = []

    async def plug():
        disk = BlockDeviceInfo(name="sdz", path="/dev/sdz", serial="S1", tran="usb")
        app.sync.detector.connected["sdz"] = ConnectedDisk(
            disk=disk, device_id="stick", data_partition="/dev/sdz1"
        )

    async def fake_bind(device_id, disk=None):
        calls.append(device_id)
        return await app.sync.config(device_id)

    run_on(app, plug())
    app.sync.bind_disk = fake_bind
    page = open_page(chrome, url + "/#/devices/stick/settings")
    page.locator('[data-action="bind"]').click()
    page.wait_for_selector(".toast:has-text('Stick')")
    assert calls == ["stick"]
    assert [e for e in page.errors if not _benign(e)] == []


def test_cancel_a_running_sync(seeded, chrome, web_server, monkeypatch):
    """The progress screen of a running sync: Cancel asks first (No keeps it running), then stops the job after the
    current file."""
    url, app = web_server
    put_track = FileDeviceMixin.put_track

    async def slow_put_track(self, s, item):
        await asyncio.sleep(1.0)
        return await put_track(self, s, item)

    monkeypatch.setattr(FileDeviceMixin, "put_track", slow_put_track)
    plan = _api(url, "POST", "/devices/spare/plan")
    job = _api(
        url, "POST", f"/plans/{plan['id']}/apply", json={"confirm": True, "first_sync": "keep"}
    )
    page = open_page(chrome, url + f"/#/devices/spare/sync/{job['id']}")
    page.locator('[data-action="cancel"]').click()
    page.locator(".modal").get_by_role("button", name="Cancel", exact=True).click()  # no
    assert _api(url, "GET", f"/jobs/{job['id']}")["status"] == "running"
    page.locator('[data-action="cancel"]').click()
    page.locator(".modal").get_by_role("button", name="Cancel the sync").click()
    page.wait_for_selector(".toast:has-text('Cancelling')")
    _wait_job(url, job["id"])
    assert _api(url, "GET", f"/jobs/{job['id']}")["status"] == "cancelled"
    assert [e for e in page.errors if not _benign(e)] == []


def test_rerender_of_the_previous_screen_does_not_redraw_the_next(seeded, chrome, web_server):
    """an event the device overview follows queues a re-render (150 ms debounce); opening Player contents
    before it fires must not redraw the new screen (a ticked item stays ticked). The page runs on a fake clock, so
    the queued timer fires exactly when the test lets time pass."""
    url, app = web_server
    page = chrome.new_page(viewport={"width": 1280, "height": 900})
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.clock.install()
    page.goto(url + "/#/devices/stick/overview")
    page.wait_for_selector('[data-action="eject"]')
    # from here on page time moves only on run_for
    page.clock.pause_at(datetime.now(UTC) + timedelta(seconds=2))
    redrawn = page.evaluate("() => (window.__marker = document.querySelector('#content h1'), true)")
    assert redrawn
    seen = events_seen(page)
    app.test_loop.call_soon_threadsafe(lambda: app.bus.publish("device.mounted", device_id="stick"))
    wait_events(page, seen)  # the event arrived; its re-render waits for the (fake) clock
    assert page.evaluate("document.querySelector('#content h1') === window.__marker")
    page.evaluate("location.hash = '#/devices/stick/contents'")
    box = page.locator('input[name="pcItem"]').first
    box.check()
    page.clock.run_for(1000)  # the queued re-render would fire now
    requests_done(page)  # and its requests are answered
    assert box.is_checked()
    assert page.errors == []
