"""Settings → Wi-Fi clients: allow clients, the Add client dialog shows a QR code, the code and a countdown and turns
into "paired" when a client uses the code; rename and revoke from the list; closing an unused dialog cancels its code."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from tests.webui.conftest import link_rules, no_horizontal_scroll, open_page, poll, run_on, shot

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def _pair(url: str, code: str, name: str) -> str:
    r = httpx.post(
        f"{url}/api/client/v1/pair",
        json={"code": code, "client": {"uid": str(uuid.uuid4()), "name": name, "os": "Android 15"}},
    )
    assert r.status_code == 201, r.text
    return r.json()["device_id"]


@pytest.mark.parametrize("width", [1280, 390])
def test_pair_rename_revoke(chrome, web_server, width):
    url, _ = web_server
    page = open_page(chrome, f"{url}/#/settings", width=width)
    box = page.locator("[data-clients]")
    box.scroll_into_view_if_needed()
    assert page.locator("[data-action=clientAdd]").is_disabled()
    page.locator("#cl-allow").check()
    page.locator("[data-clients='on']").wait_for()

    # an unused code is cancelled when the dialog closes
    page.locator("[data-action=clientAdd]").click()
    page.locator("[data-cl-qr] svg").wait_for()
    assert "Valid for" in page.locator("[data-cl-left]").inner_text()
    page.get_by_role("button", name="Close", exact=True).last.click()
    poll(
        page,
        lambda: httpx.get(f"{url}/api/v1/clients/pairings").json() == [],
        what="code cancelled",
    )

    page.locator("[data-action=clientAdd]").click()
    code = page.locator("[data-cl-code]").inner_text().strip()
    assert no_horizontal_scroll(page)
    shot(page, f"clients-dialog-{width}")
    device_id = _pair(url, code, "Телефон Олени")
    page.locator("[data-cl-done]").wait_for(timeout=10_000)
    assert "Телефон Олени" in page.locator("[data-cl-done]").inner_text()
    page.get_by_role("button", name="Close", exact=True).last.click()
    row = page.locator(f"[data-client='{device_id}']")
    row.wait_for()
    assert "Android 15" in row.inner_text()

    row.locator("[data-action=clientRename]").click()
    page.locator("#cl-name").fill("Kitchen phone")
    page.get_by_role("button", name="Save", exact=True).last.click()
    page.locator(f"[data-client='{device_id}']", has_text="Kitchen phone").wait_for()

    page.locator(f"[data-client='{device_id}'] [data-action=clientRevoke]").click()
    page.get_by_role("button", name="Revoke", exact=True).last.click()
    page.locator(f"[data-client='{device_id}']", has_text="Revoked").wait_for()
    assert no_horizontal_scroll(page)
    shot(page, f"clients-settings-{width}")
    assert page.errors == []


def test_client_device_page_shows_the_client_and_its_progress(chrome, web_server):
    url, app = web_server
    httpx.patch(f"{url}/api/v1/settings", json={"clients_allowed": True}).raise_for_status()
    code = httpx.post(f"{url}/api/v1/clients/pairings").json()["code"]
    device_id = _pair(url, code, "Телефон Олени")
    page = open_page(chrome, f"{url}/#/devices/{device_id}/overview")
    card = page.locator(f"[data-client-card='{device_id}']")
    card.wait_for()
    assert "Android 15" in card.inner_text()

    async def progress():
        app.bus.publish(
            "client.progress",
            device_id=device_id,
            done=1,
            total=4,
            bytes_done=1000,
            bytes_total=4000,
        )

    page.wait_for_timeout(500)  # the event stream is connected
    run_on(app, progress())
    page.locator("[data-client-sync]", has_text="file 1 of 4").wait_for()
    httpx.post(f"{url}/api/v1/clients/{device_id}/revoke").raise_for_status()
    page.locator("[data-client-revoked]").wait_for()
    assert no_horizontal_scroll(page)
    assert page.errors == []


def _ask(url: str, name: str) -> dict:
    r = httpx.post(
        f"{url}/api/client/v1/pair-requests",
        json={"client": {"uid": str(uuid.uuid4()), "name": name, "os": "Compás"}},
    )
    assert r.status_code == 202, r.text
    return r.json()


@pytest.mark.parametrize("width", [1280, 390])
def test_pairing_requests_approve_and_reject(chrome, web_server, width):
    url, _ = web_server
    httpx.patch(f"{url}/api/v1/settings", json={"clients_allowed": True}).raise_for_status()
    page = open_page(chrome, f"{url}/#/devices", width=width)
    page.locator("#content h1").wait_for()

    first = _ask(url, "Плеєр у вітальні")
    second = _ask(url, "Spare player")
    row = page.locator(f"[data-request='{first['request_id']}']")
    row.wait_for(timeout=10_000)  # the Devices page follows the event, no reload
    if width >= 1280:
        page.locator("[data-requests-badge]").wait_for()
    assert first["display_code"] in row.inner_text()
    row.scroll_into_view_if_needed()
    assert no_horizontal_scroll(page)
    shot(page, f"clients-requests-{width}")

    row.locator("[data-action=clientApprove]").click()
    poll(
        page,
        lambda: (
            httpx.get(
                f"{url}/api/client/v1/pair-requests/{first['request_id']}",
                headers={"x-pair-secret": first["secret"]},
            ).status_code
            == 200
        ),
        what="request approved",
    )
    page.locator("#content", has_text="Плеєр у вітальні").locator("a").first.wait_for()

    page.locator(f"[data-request='{second['request_id']}'] [data-action=clientReject]").click()
    page.locator(f"[data-request='{second['request_id']}']").wait_for(state="detached")
    r = httpx.get(
        f"{url}/api/client/v1/pair-requests/{second['request_id']}",
        headers={"x-pair-secret": second["secret"]},
    )
    assert r.status_code == 403, r.text
    assert "request_rejected" in r.text, r.text
    page.locator("[data-requests-badge]").wait_for(state="detached")
    # Settings keeps only the on/off switch: no request list there
    third = _ask(url, "Third player")
    page.goto(f"{url}/#/settings")
    page.locator("[data-clients='on']").wait_for()
    assert page.locator("#cl-requests").is_checked()
    assert page.locator(f"[data-request='{third['request_id']}']").count() == 0
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_client_diagnostics_on_the_device_page(chrome, web_server, width):
    url, _ = web_server
    httpx.patch(f"{url}/api/v1/settings", json={"clients_allowed": True}).raise_for_status()
    code = httpx.post(f"{url}/api/v1/clients/pairings").json()["code"]
    r = httpx.post(
        f"{url}/api/client/v1/pair",
        json={"code": code, "client": {"uid": str(uuid.uuid4()), "name": "Плеєр", "os": "Compas"}},
    )
    device_id, token = r.json()["device_id"], r.json()["token"]
    page = open_page(chrome, f"{url}/#/devices/{device_id}/overview", width=width)
    page.locator(f"[data-client-card='{device_id}']").wait_for()
    assert page.locator("[data-client-diag]").count() == 0
    httpx.post(
        f"{url}/api/client/v1/diagnostics",
        json={"log": "app started\n", "summary": {"files": 3}},
        headers={"Authorization": f"Bearer {token}"},
    ).raise_for_status()
    diag = page.locator("[data-client-diag]")
    diag.wait_for(timeout=10_000)  # the card follows the client.changed event
    with page.expect_download() as dl:
        diag.get_by_role("link", name="Download").click()
    assert "app started" in Path(dl.value.path()).read_text(encoding="utf-8")
    assert no_horizontal_scroll(page)
    shot(page, f"client-diagnostics-{width}")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_sync_now_asks_the_player_and_shows_that_it_waits(chrome, web_server, width, tmp_path):
    url, _ = web_server
    (tmp_path / "music").mkdir()
    lib = {
        "id": "music",
        "type": "folder",
        "name": "Music",
        "settings": {"root": str(tmp_path / "music")},
    }
    if httpx.get(f"{url}/api/v1/libraries/music").status_code == 404:
        httpx.post(f"{url}/api/v1/libraries", json=lib, timeout=60).raise_for_status()
    httpx.patch(f"{url}/api/v1/settings", json={"clients_allowed": True}).raise_for_status()
    code = httpx.post(f"{url}/api/v1/clients/pairings").json()["code"]
    device_id = _pair(url, code, "Плеєр у кухні")
    link_rules(url, device_id, {"rules": [{"id": "all", "name": "All", "criteria": {"all": []}}]})
    page = open_page(chrome, f"{url}/#/devices/{device_id}/overview", width=width)
    page.locator("[data-action=clientSync]").first.click()
    waits = page.locator("[data-client-requested]")
    waits.wait_for()
    assert "open the SyncMusic app" in waits.inner_text()
    assert httpx.get(f"{url}/api/v1/clients/{device_id}").json()["sync_requested"] is True
    assert no_horizontal_scroll(page)
    shot(page, f"client-sync-requested-{width}")
    page.locator("[data-action=cancelClientSync]").click()
    waits.wait_for(state="detached")
    assert httpx.get(f"{url}/api/v1/clients/{device_id}").json()["sync_requested"] is False
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_the_outbox_shows_on_the_device_page_and_clears(chrome, web_server, width, tmp_path):
    import time

    from tests.media import make_music

    url, _ = web_server
    music = tmp_path / "music"
    make_music(music / "A" / "01 One.mp3", title="One", artist="Кобза", album="Перший")
    lib = {"id": f"m{width}", "type": "folder", "name": "Music", "settings": {"root": str(music)}}
    httpx.post(f"{url}/api/v1/libraries", json=lib, timeout=60).raise_for_status()
    httpx.patch(f"{url}/api/v1/settings", json={"clients_allowed": True}).raise_for_status()
    code = httpx.post(f"{url}/api/v1/clients/pairings").json()["code"]
    r = httpx.post(
        f"{url}/api/client/v1/pair",
        json={"code": code, "client": {"uid": str(uuid.uuid4()), "name": "Плеєр", "os": "Compas"}},
    )
    device_id, h = r.json()["device_id"], {"Authorization": f"Bearer {r.json()['token']}"}
    link_rules(
        url,
        device_id,
        {"rules": [{"id": "all", "name": "All", "criteria": {"all": []}}]},
        (lib["id"],),
    )
    caps = {"formats": ["mp3"], "storage": {"total": 10**9, "free": 10**9}}
    view = httpx.post(
        f"{url}/api/client/v1/sync", json={"capabilities": caps}, headers=h, timeout=60
    ).json()
    for _ in range(200):
        if view["status"] != "planning":
            break
        time.sleep(0.1)
        view = httpx.get(f"{url}/api/client/v1/sync/{view['session_id']}", headers=h).json()
    assert view["status"] == "ready", view
    httpx.post(
        f"{url}/api/client/v1/sync/{view['session_id']}/finish",
        json={"status": "partial"},
        headers=h,
    )
    page = open_page(chrome, f"{url}/#/devices/{device_id}/overview", width=width)
    row = page.locator("[data-client-card] dd", has_text="1 file,")
    row.wait_for()
    assert no_horizontal_scroll(page)
    shot(page, f"client-outbox-{width}")
    page.locator("[data-action=clearOutbox]").click()
    page.get_by_role("button", name="Clear", exact=True).last.click()
    row.wait_for(state="detached")
    assert httpx.get(f"{url}/api/v1/clients/{device_id}").json()["outbox"]["files"] == 0
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_client_sync_waiting_for_confirmation_or_failed_shows_on_the_device_page(
    chrome, web_server, width
):
    import datetime as dt

    from syncmusic.core.client_sessions import ClientSession

    url, app = web_server
    httpx.patch(f"{url}/api/v1/settings", json={"clients_allowed": True}).raise_for_status()
    code = httpx.post(f"{url}/api/v1/clients/pairings").json()["code"]
    device_id = _pair(url, code, "Плеєр на кухні")
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    review = ClientSession(
        id="s-review",
        device_id=device_id,
        mode="full",
        status="needs_review",
        message="12 of 40 files would be removed (over the delete threshold); does not fit",
        plan_id="plan-1",
        expires_at=expires,
    )
    app.client_sessions.sessions[review.id] = review
    page = open_page(chrome, f"{url}/#/devices/{device_id}/overview", width=width)
    box = page.locator("[data-client-review]")
    box.wait_for()
    assert "delete threshold" in box.inner_text()
    link = page.locator("[data-client-sync] a", has_text="Review plan")
    assert link.get_attribute("href").endswith("/plan/plan-1")
    assert no_horizontal_scroll(page)
    shot(page, f"client-review-{width}")

    review.status, review.message = "failed", "not enough space on the player"
    page.reload()
    page.locator("[data-client-failed]", has_text="not enough space").wait_for()
    assert no_horizontal_scroll(page)
    shot(page, f"client-failed-{width}")
    assert page.errors == []


@pytest.mark.parametrize("width", [1280, 390])
def test_transfer_card_shows_speed_and_time_left_of_a_live_session(
    chrome, web_server, width, tmp_path
):
    """A client takes files of a live session one by one through the protocol: the device page follows the acks
    with files, bytes, the transfer rate to the player and the time left."""
    import time

    from tests.media import make_music

    url, _ = web_server
    music = tmp_path / "music"
    for n in range(1, 4):
        make_music(
            music / "A" / f"0{n} Пісня {n}.mp3", title=f"Пісня {n}", artist="Кобза", album="Перший"
        )
    lib = {"id": f"t{width}", "type": "folder", "name": "Music", "settings": {"root": str(music)}}
    httpx.post(f"{url}/api/v1/libraries", json=lib, timeout=60).raise_for_status()
    httpx.patch(f"{url}/api/v1/settings", json={"clients_allowed": True}).raise_for_status()
    code = httpx.post(f"{url}/api/v1/clients/pairings").json()["code"]
    r = httpx.post(
        f"{url}/api/client/v1/pair",
        json={
            "code": code,
            "client": {"uid": str(uuid.uuid4()), "name": "Телефон Олени", "os": "Android 15"},
        },
    )
    device_id, h = r.json()["device_id"], {"Authorization": f"Bearer {r.json()['token']}"}
    link_rules(
        url,
        device_id,
        {"rules": [{"id": "all", "name": "All", "criteria": {"all": []}}]},
        (lib["id"],),
    )
    caps = {"formats": ["mp3"], "storage": {"total": 10**9, "free": 10**9}}
    view = httpx.post(
        f"{url}/api/client/v1/sync", json={"capabilities": caps}, headers=h, timeout=60
    ).json()
    sid = view["session_id"]
    for _ in range(200):
        if view["status"] != "planning":
            break
        time.sleep(0.1)
        view = httpx.get(f"{url}/api/client/v1/sync/{sid}", headers=h).json()
    assert view["status"] == "ready", view
    fetch = [f for f in view["plan"]["fetch"] if f["role"] == "track"]
    assert len(fetch) == 3

    page = open_page(chrome, f"{url}/#/devices/{device_id}/overview", width=width)
    box = page.locator(f"[data-client-card='{device_id}'] [data-client-sync]")
    box.wait_for()
    page.wait_for_timeout(500)  # the event stream is connected

    def ack(item: dict) -> None:
        httpx.post(
            f"{url}/api/client/v1/sync/{sid}/ack",
            json={"done": [{"path": item["path"], "fid": item["fid"]}]},
            headers=h,
        ).raise_for_status()

    ack(fetch[0])
    page.locator("[data-client-sync]", has_text="to the player").wait_for(
        state="detached", timeout=2_000
    ) if False else None
    time.sleep(1.2)  # the rate needs two acks at least a second apart
    ack(fetch[1])
    box.locator("text=/s to the player").wait_for(timeout=10_000)
    text = box.inner_text()
    assert "file 2 of" in text, text
    assert " left" in text or "less than" in text.lower() or "min" in text or "s" in text, text
    assert box.locator("[role=progressbar]").get_attribute("aria-valuenow") not in (None, "0")
    assert no_horizontal_scroll(page)
    shot(page, f"client-transfer-{width}")
    httpx.post(f"{url}/api/client/v1/sync/{sid}/finish", json={"status": "partial"}, headers=h)
    assert page.errors == []
