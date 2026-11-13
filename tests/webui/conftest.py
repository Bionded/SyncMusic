"""Live server with the web UI for browser tests, plus a shared Chromium."""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from syncmusic.api.server import ApiServer, create_api
from syncmusic.app import Application
from syncmusic.facade import AppFacade
from syncmusic.webui import web_ui

# Ports for the test servers: below the kernel's ephemeral range (32768+ on Linux), which outgoing connections
# (the browsers, httpx) and bind(0) draw from - a port picked there can be taken before uvicorn binds it - and one
# block per xdist worker, so the workers never pick the same one.
PORT_BASE = 21000
PORT_BLOCK = 200
_next_port = random.randrange(PORT_BLOCK)


def _free_port() -> int:
    global _next_port
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw")
    base = PORT_BASE + (int(worker) if worker.isdigit() else 0) % 29 * PORT_BLOCK
    for _ in range(PORT_BLOCK):
        port = base + _next_port % PORT_BLOCK
        _next_port += 1
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"no free port in {base}-{base + PORT_BLOCK - 1}")


@pytest.fixture
def web_server(config):
    """(url, Application) of a live server in a thread; `app.test_loop` is the server's event loop."""
    app = Application(config)
    start = app.start

    async def start_and_keep_loop() -> None:
        app.test_loop = asyncio.get_running_loop()
        await start()

    app.start = start_and_keep_loop
    port = _free_port()
    srv = ApiServer(
        uvicorn.Config(
            create_api(AppFacade(app), ui=web_ui()),
            host="127.0.0.1",
            port=port,
            log_config=None,
            lifespan="on",
            timeout_graceful_shutdown=2,  # a last resort: ApiServer ends the event streams
        )
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(1500):
        if srv.started or not thread.is_alive():
            break
        time.sleep(0.02)
    assert srv.started, f"the test server did not start on port {port}"
    yield f"http://127.0.0.1:{port}", app
    srv.should_exit = True
    thread.join(30)


def run_on(app, coro, timeout: float = 30):
    """Run a coroutine on the server's loop (tests in the main thread)."""
    return asyncio.run_coroutine_threadsafe(coro, app.test_loop).result(timeout)


# the UI formats times in the viewer's locale: every page gets the same one on every machine (24-hour clock).
# Playwright emulates it, so it needs no system locale (LANG of a locale that is not installed breaks non-ASCII
# download file names in Chromium)
UI_LOCALE = "en-GB"


# Register shows every step on one page in the tests that fill its fields directly (the step-by-step flow has
# its own test, which removes this)
ALL_STEPS = "try { localStorage.getItem('sm.regSteps') || localStorage.setItem('sm.regSteps', 'all') } catch (e) {}"


class _Browser:
    """The Playwright browser with `locale=UI_LOCALE` as the default of every new page and context."""

    def __init__(self, browser) -> None:
        self._browser = browser

    def new_page(self, **options):
        page = self._browser.new_page(**{"locale": UI_LOCALE, **options})
        page.add_init_script(ALL_STEPS)
        return page

    def new_context(self, **options):
        context = self._browser.new_context(**{"locale": UI_LOCALE, **options})
        context.add_init_script(ALL_STEPS)
        return context

    def __getattr__(self, name):
        return getattr(self._browser, name)


@pytest.fixture(scope="module")
def chrome():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        b = p.chromium.launch()
        yield _Browser(b)
        b.close()


# pages of the running test, closed at its end: a page left open keeps polling a stopped server, and
# Playwright's sync API raises its route handler's error inside a later test's call
_open_pages: list = []


@pytest.fixture(autouse=True)
def _close_pages():
    yield
    while _open_pages:
        page = _open_pages.pop()
        with contextlib.suppress(Exception):  # the browser of a finished module is already gone
            page.context.close()


def open_page(chrome, url: str, width: int = 1280, height: int = 800):
    """A page on `url` with console errors collected in `page.errors` (asserted empty by the tests); closed (with
    its own context) when the test ends."""
    page = chrome.new_page(viewport={"width": width, "height": height})
    _open_pages.append(page)
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    # with the URL: "Failed to load resource" alone does not say which request failed
    page.on(
        "console",
        lambda m: (
            m.type == "error" and page.errors.append(f"{m.text} [{m.location.get('url', '')}]")
        ),
    )
    page.goto(url)
    return page


def no_horizontal_scroll(page) -> bool:
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


QUIET_MS = 600  # longer than the UI's longest debounce (the rule preview's 500 ms)


def settle(page, quiet_ms: int = QUIET_MS, timeout: int = 30_000) -> None:
    """Wait until the page is quiet: no API request in flight and none started or ended for `quiet_ms` (a
    debounced request would have started by then), then two rendered frames. What a test checks after it is what
    the page shows once everything it asked for has arrived - not what a fixed pause happened to catch."""
    page.wait_for_function(
        """q => { const d = document.documentElement.dataset;
                  return d.apiPending === '0' && performance.now() - Number(d.apiLast || 0) > q; }""",
        arg=quiet_ms,
        timeout=timeout,
    )
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")


def requests_done(page, timeout: int = 30_000) -> None:
    """No API request in flight (no quiet period: for pages with a fake clock, whose performance.now() stands)."""
    page.wait_for_function("document.documentElement.dataset.apiPending === '0'", timeout=timeout)


def animations_done(page, timeout: int = 10_000) -> None:
    """Every CSS transition and animation has finished (the sidebar's slide-out, a drawer's slide-in); endless
    ones (a spinner, an indeterminate bar) do not count."""
    page.wait_for_function(
        """document.getAnimations().every((a) =>
             a.playState !== 'running' || a.effect?.getComputedTiming().iterations === Infinity)""",
        timeout=timeout,
    )


def wait_events(page, more_than: int, timeout: int = 15_000) -> None:
    """The page has handled more than `more_than` events of the server's stream (`events_seen` before)."""
    page.wait_for_function(
        "n => Number(document.documentElement.dataset.events) > n", arg=more_than, timeout=timeout
    )


def events_seen(page) -> int:
    return int(page.evaluate("Number(document.documentElement.dataset.events)"))


def poll(page, condition, timeout_s: float = 15.0, what: str = "the condition") -> None:
    """Wait for a condition the test checks itself (a server-side state, requests a route handler saw). It waits
    through the page, so Playwright keeps serving the route handlers meanwhile."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s:.0f} s waiting for {what}")
        page.wait_for_timeout(50)


def shot(page, name: str) -> None:
    """Full-page screenshot into $SYNCMUSIC_SHOTS (for looking at the UI; no-op when unset)."""
    folder = os.environ.get("SYNCMUSIC_SHOTS")
    if folder:
        os.makedirs(folder, exist_ok=True)
        page.screenshot(path=os.path.join(folder, f"{name}.png"), full_page=True)


def link_rules(url: str, device_id: str, rule_set: dict, library_ids=("music",)) -> dict:
    """A device has templates only: store `rule_set`'s rules and excludes as the template
    "<device> rules" (`<device>-rules`; replaced on every call) and link it instead of the device's other templates
    (its own template, with its picks, stays linked); mode, priority and limits go to the device. Returns the
    device config."""
    tid = f"{device_id}-rules"
    rules = {k: rule_set.get(k, []) for k in ("rules", "excludes")}
    body = {"name": f"{device_id} rules", "library_ids": list(library_ids), **rules}
    r = httpx.get(f"{url}/api/v1/templates/{tid}", timeout=60)
    if r.status_code == 404:
        r = httpx.post(f"{url}/api/v1/templates", json={"id": tid, **body}, timeout=60)
    else:
        r = httpx.patch(f"{url}/api/v1/templates/{tid}", json=body, timeout=60)
    assert r.status_code < 400, r.text
    cfg = httpx.get(f"{url}/api/v1/devices/{device_id}", timeout=60).json()["config"]
    own = [t for t in cfg["rule_set"]["template_ids"] if t == f"own-{device_id}"]
    rest = {k: v for k, v in rule_set.items() if k not in ("rules", "excludes", "template_ids")}
    rs = {**cfg["rule_set"], **rest, "template_ids": [tid, *own]}
    r = httpx.patch(f"{url}/api/v1/devices/{device_id}", json={"rule_set": rs}, timeout=60)
    assert r.status_code < 400, r.text
    return r.json()


# the device Templates tab: templates are added in the inline picker, removed with ×, and
# stored by the Save bar
def add_templates(page, *template_ids: str) -> None:
    page.locator('[data-tpls] [data-action="rulesAdd"]').first.click()
    for tid in template_ids:
        page.locator(f'[data-pick="{tid}"]').click()
        page.wait_for_selector(f'[data-rules] [data-tpl-row="{tid}"]', state="attached")
    page.locator("[data-pick-done]").click()


def remove_template(page, template_id: str) -> None:
    page.locator(f'[data-tpl-row="{template_id}"] [data-action="rulesRemove"]').click()
    page.wait_for_selector(f'[data-rules] [data-tpl-row="{template_id}"]', state="detached")


def save_templates(page) -> None:
    page.locator('[data-savebar="save"]').click()
    page.wait_for_selector(".savebar", state="hidden")


def discard_templates(page) -> None:
    if page.locator('[data-savebar="discard"]').is_visible():
        page.locator('[data-savebar="discard"]').click()
        page.wait_for_selector(".savebar", state="hidden")
