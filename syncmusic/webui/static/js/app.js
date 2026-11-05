// Shell: navigation (sidebar.js: sidebar on wide screens, a drawer behind the burger on phones), routing, live connection state, and the
// delegated event handling for views (data-action on click/submit, data-change on change).
import { html, $, setHtml } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { route, match } from "./router.js";
import { get, connectEvents, reconnectEvents, onEvent, onLiveState } from "./api.js";
import { showLogin } from "./login.js";
import { errorBox, toast, errorMessage, skeleton, emptyState, confirmLeave } from "./ui.js";
import { NAV, renderNav, initSidebar, refreshSidebar } from "./sidebar.js";
import { initServerUrls } from "./forms.js";
import { initBell } from "./bell.js";
import { initSearch } from "./search.js";
import { icon, logo } from "./icons.js";
import { ejectedText } from "./views/common.js";

addStrings({
  "app.auto_ejected": "Ejected - you can unplug {name}.",
  "live.banner": "Lost the connection to the SyncMusic service. Showing the last known state; retrying by itself.",
  "live.retry": "Retry now",
  "theme.btn": "Theme: {mode} (click for {next})",
  "theme.dark": "dark",
  "theme.light": "light",
  "theme.system": "system",
});

route("/", () => import("./views/dashboard.js"));
route("/welcome", () => import("./views/welcome.js"));
route("/libraries", () => import("./views/libraries.js"));
route("/libraries/new", () => import("./views/libraries.js"));
route("/libraries/:id", () => import("./views/libraries.js"));
route("/libraries/:id/:mode", () => import("./views/libraries.js"));
route("/devices", () => import("./views/devices.js"));
route("/devices/register", () => import("./views/register.js"));
route("/devices/:id", () => import("./views/devices.js"));
route("/devices/:id/plan", () => import("./views/plan.js"));
route("/devices/:id/plan/:planId", () => import("./views/plan.js"));
route("/devices/:id/sync/:jobId", () => import("./views/progress.js"));
route("/devices/:id/:tab", () => import("./views/devices.js"));
route("/templates", () => import("./views/templates.js"));
route("/templates/new", () => import("./views/templates.js"));
route("/templates/:id", () => import("./views/templates.js"));
route("/jobs/:id", () => import("./views/job.js"));
route("/processor-templates", () => import("./views/proctemplates.js"));
route("/processor-templates/new", () => import("./views/proctemplates.js"));
route("/processor-templates/:id", () => import("./views/proctemplates.js"));
route("/settings", () => import("./views/settings.js"));
route("/logs", () => import("./views/logs.js"));
route("/notifications", () => import("./views/notifications.js"));
route("/activity", () => import("./views/activity.js"));

const content = $("#content");
let current = null; // {mod, ctx}

// the top bar: breadcrumbs on wide screens - the section, then the screen (a view may give its own trail
// with `crumbs(ctx)`: [{label, href}]) - and the page title alone on phones
function setCrumbs(mod, ctx) {
  let trail = [];
  if (mod && ctx) {
    const title = (mod.title && mod.title(ctx)) || t(`nav.${mod.nav}`);
    if (mod.crumbs) trail = mod.crumbs(ctx) || [];
    if (!trail.length) {
      const sec = NAV.find((n) => n.id === mod.nav);
      if (sec && t(`nav.${sec.id}`) !== title) trail.push({ label: t(`nav.${sec.id}`), href: `#${sec.path}` });
      trail.push({ label: title });
    }
  }
  const last = trail.length - 1;
  setHtml($("#crumbs"), trail.map((c, i) => html`${i ? html`<span class="sep" aria-hidden="true">/</span>` : ""}${i < last && c.href
    ? html`<a href="${c.href}">${c.label}</a>` : html`<span ${i === last ? html`aria-current="page"` : ""}>${c.label}</span>`}`));
  $("#pageTitle").textContent = trail.length ? trail[last].label : "SyncMusic";
}

let seq = 0;
// a re-render asked while the screen is still being drawn waits for that draw (and then runs once): one that
// cancelled it instead would let a busy server's stream of events (every job's progress) keep a slow screen from
// ever being shown
let drawing = false;
let again = false;

// a media element taken out of the page only pauses: its download keeps one of the browser's six connections to
// the server until it is garbage-collected, and a few played files stall every later request - drop them first
function releaseMedia(root) {
  for (const m of root.querySelectorAll("audio, video")) {
    m.pause();
    m.removeAttribute("src");
    m.load();
  }
}

async function show({ focus = true } = {}) {
  const my = ++seq;
  drawing = true;
  again = false;
  try {
    await draw(my, focus);
  } finally {
    if (my === seq) {
      drawing = false;
      if (again) rerender();
    }
  }
}

async function draw(my, focus) {
  const m = match(location.hash);
  if (!m) {
    current = null;
    renderNav(null, "");
    setCrumbs(null, null);
    setHtml(content, emptyState(t("error.notfound"), { action: html`<a class="btn" href="#/">${t("nav.dashboard")}</a>` }));
    return;
  }
  let mod;
  try {
    mod = await m.load();
  } catch (e) {
    if (my === seq) setHtml(content, errorBox(e));
    return;
  }
  // a newer navigation won while this view's module loaded: it must not become `current` (its actions would answer
  // the newer screen's clicks)
  if (my !== seq) return;
  const ctx = { params: m.params, query: m.query, path: m.path, hash: location.hash, rerender: () => rerender(), state: {} };
  const same = current && current.ctx.path === ctx.path;
  // keep view state on a re-render of the same screen; another query (a "Copy" draft vs a blank "New")
  // is another screen
  if (same && current.ctx.hash === ctx.hash) ctx.state = current.ctx.state;
  current = { mod, ctx };
  renderNav(mod.nav || null, ctx.path);
  setCrumbs(mod, ctx);
  // a navigation replaces the previous screen at once: its controls belong to a view that is no longer current
  // and would do nothing while a slow view loads; a re-render of the same route keeps it until ready
  if (!same || !content.innerHTML.trim()) {
    delete content.dataset.route;
    releaseMedia(content);
    setHtml(content, skeleton(4));
  }
  let tpl;
  try {
    tpl = await mod.render(ctx);
  } catch (e) {
    tpl = errorBox(e);
  }
  if (my !== seq) return; // a newer navigation won
  releaseMedia(content);
  setHtml(content, tpl);
  content.dataset.route = ctx.path; // which route is on screen (tests wait for it)
  setCrumbs(mod, ctx); // the view may know more now
  if (mod.mounted) mod.mounted(ctx, content);
  tabStrips(content);
  if (focus) content.focus({ preventScroll: true });
}

// a tab strip wider than the screen (phone): the active tab is scrolled fully into view and the strip fades
// at the side where more tabs are (`data-more` = left / right / both)
function tabStrips(root) {
  for (const strip of root.querySelectorAll(".tabs")) {
    const active = strip.querySelector('[aria-selected="true"]');
    if (active && strip.scrollWidth > strip.clientWidth) {
      const left = active.offsetLeft - strip.offsetLeft, right = left + active.offsetWidth;
      if (left < strip.scrollLeft) strip.scrollLeft = left - 16;
      else if (right > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = right - strip.clientWidth + 16;
    }
    const mark = () => {
      const l = strip.scrollLeft > 2, r = strip.scrollLeft + strip.clientWidth < strip.scrollWidth - 2;
      if (l || r) strip.dataset.more = l && r ? "both" : l ? "left" : "right";
      else delete strip.dataset.more;
    };
    mark();
    strip.addEventListener("scroll", mark, { passive: true });
  }
}

let timer = null;
function rerender() {
  clearTimeout(timer);
  // only the view that asked is drawn again: after a navigation the new screen must not be redrawn (and lose what
  // the user selected or typed) because of an event the previous screen followed
  const asked = current;
  timer = setTimeout(() => {
    if (current !== asked) return;
    if (drawing) again = true;
    else show({ focus: false });
  }, 150);
}

async function runAction(name, el, e) {
  if (name === "retry") { show(); return; }
  const fn = current?.mod.actions?.[name];
  if (!fn) return;
  e.preventDefault();
  if (el.disabled) return;
  try {
    await fn(el, current.ctx, e);
  } catch (err) {
    toast(errorMessage(err), "error");
  }
}

initServerUrls(content); // server URL fields: the http scheme added on leaving, TLS checkbox follows

// a thumbnail without a cover (204) or with a broken one leaves just its placeholder
content.addEventListener("error", (e) => { if (e.target.matches?.(".thumb img, .pc-art img")) e.target.remove(); }, true);

content.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (el && el.tagName !== "FORM") runAction(el.dataset.action, el, e);
});
content.addEventListener("submit", (e) => {
  const el = e.target.closest("form[data-action]");
  if (el) runAction(el.dataset.action, el, e);
  else e.preventDefault();
});
content.addEventListener("change", (e) => {
  const el = e.target.closest("[data-change]");
  if (el) runAction(el.dataset.change, el, e);
});

function dispatch(ev) {
  if (!current) return;
  const { mod, ctx } = current;
  if (mod.onEvent) mod.onEvent(ev, ctx, content);
  else if ((mod.refreshOn || []).some((p) => ev.type.startsWith(p))) rerender();
}
onEvent(dispatch);
// Eject after a sync: wherever the user is
onEvent((ev) => {
  if (ev.type === "device.auto_ejected") toast(ejectedText(ev.data, t("app.auto_ejected", { name: ev.data.name })), "ok");
});

let wasUp = null;
function drawLive(up) {
  const pill = $("#livePill");
  pill.classList.toggle("off", !up);
  pill.dataset.liveState = up ? "on" : "off";
  setHtml(pill, html`<span class="live" aria-hidden="true"></span><span>${up ? t("live.on") : t("live.off")}</span>`);
  // while the event stream is down a banner says so; the screen keeps its last state
  const banner = $("#liveBanner");
  banner.hidden = up;
  if (!up && !banner.innerHTML.trim()) {
    setHtml(banner, html`${icon("warn", { size: 16 })}<span>${t("live.banner")}</span><button type="button" class="btn small" data-live-retry>${t("live.retry")}</button>`);
  }
}
$("#liveBanner").addEventListener("click", (e) => { if (e.target.closest("[data-live-retry]")) reconnectEvents(); });

onLiveState((up) => {
  drawLive(up);
  // events may have been missed while disconnected: views that follow events decide whether to reload (a form
  // being filled in must not be wiped)
  if (up && wasUp === false) {
    dispatch({ type: "live.reconnected", data: {} });
    refreshSidebar();
  }
  wasUp = up;
});

$("#brandSub").textContent = t("app.tagline");
setHtml($("#brandMark"), logo(24));
setHtml($("#burger"), icon("menu"));
drawLive(true);
// a screen with unsaved changes asks before it is left; "no" puts the address back
let shownHash = location.hash;
let reverting = false;
window.addEventListener("hashchange", async () => {
  if (reverting) { reverting = false; return; }
  if (!(await confirmLeave())) { reverting = true; location.hash = shownHash; return; }
  shownHash = location.hash;
  clearTimeout(timer);
  show();
});
// the top-bar theme button cycles dark → light → system; Settings shows the same choice
const THEME_ICON = { dark: "moon", light: "sun", system: "monitor" };
function drawThemeBtn() {
  const { modes, mode } = window.syncmusicTheme, now = mode(), next = modes[(modes.indexOf(now) + 1) % modes.length];
  const btn = $("#themeBtn"), label = t("theme.btn", { mode: t(`theme.${now}`), next: t(`theme.${next}`) });
  btn.dataset.mode = now;
  btn.dataset.next = next;
  btn.title = label;
  btn.setAttribute("aria-label", label);
  setHtml(btn, icon(THEME_ICON[now]));
  document.querySelectorAll("#themeCard [data-value]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.value === now)));
}
$("#themeBtn").addEventListener("click", (e) => window.syncmusicTheme.set(e.currentTarget.dataset.next));
document.addEventListener("syncmusic-theme", drawThemeBtn);
drawThemeBtn();
// the optional login: without a session only the login screen
const access = await get("/auth").catch(() => null);
if (access?.enabled && !access.signed_in) showLogin();
else {
  initSidebar();
  initBell();
  initSearch();
  connectEvents();
  show({ focus: false }); // first load: focus stays at the top (skip link first); navigation moves it to the content
}
