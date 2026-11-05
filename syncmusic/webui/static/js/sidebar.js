// Main navigation (docs/ui-prototype): the sidebar on wide screens, the same sidebar as a drawer behind
// the burger button on phones. Libraries is a split control - its label shows/hides a link per library (with no
// libraries the dropdown offers Add library instead: no Add library item at the end of the menu any
// more), the button next to it opens All libraries. At the bottom one card per connected device opens that device; it shows whether
// the device is available and, while a job runs on it, the job's progress. Several devices may sync at once:
// every card follows its own job, states are words (waiting / syncing / failed / synced), not colour only, and the
// Devices item carries a badge - the percent of the one running sync, or how many devices sync.
// brand, nav, the Libraries dropdown with a type icon and a health dot per library, 'Connected' device cards.
import { html, $, setHtml, bytes } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { get, post, onEvent } from "./api.js";
import { etaText, isCheck, stepText } from "./views/common.js";
import { icon, typeIcon } from "./icons.js";
import { badge, updateLine, toast, dialog } from "./ui.js";
import { health, onHealth, checkUnknown } from "./libhealth.js";
import { unreadCount, onUnread } from "./bell.js";

addStrings({
  "nav.all_libraries": "All libraries",
  "nav.libraries_toggle": "Show or hide the libraries",
  "nav.add_library": "Add library",
  "nav.menu": "Menu",
  "nav.open_device": "Open {name}",
  "nav.open_job": "Sync status of {name}",
  "side.update_now": "Update {name} now",
  "side.update_started": "Update of {name} started",
  "side.ready": "Connected",
  "side.lib_ok": "Reachable",
  "side.lib_checking": "Checking…",
  "side.lib_off": "Disabled",
  "side.syncing": "Syncing",
  "side.planning": "Planning…",
  "side.checking": "Checking files…",
  "side.left": "{left} left",
  "side.clock_soon": "Ready in a few seconds",
  "side.updating": "Updating…",
  "side.update_failed": "Update failed",
  "side.connected": "Connected",
  "sync_all.button": "Sync all connected",
  "sync_all.title": "Sync all connected devices",
  "sync_all.text": "A full sync puts new music on every connected device; an update only refreshes tags, ratings, positions and playlists. A device whose plan needs a review waits for you.",
  "sync_all.full": "Full sync",
  "sync_all.update": "Update only",
  "sync_all.started": "Syncing {n} devices",
  "sync_all.started_one": "Syncing {n} device",
  "sync_all.none": "No connected device can be synced now",
  "sync_all.skipped": "Skipped: {list}",
  "sync_all.reason.not connected": "not connected",
  "sync_all.reason.ejected": "ejected",
  "sync_all.reason.busy": "busy",
  "sync_all.reason.no library": "no library",
  "sync_all.reason.file system damaged": "file system damaged",
  "sync_all.finished": "Sync all finished: {done} done, {waiting} waiting for a review, {failed} failed",
  "side.cancelling": "Cancelling…",
  "side.ejecting": "Ejecting…",
  "side.ejected": "Ejected - unplug it",
  "side.no_medium": "Turn on USB storage on the player",
  "side.clock": "Setting the clock…",
  "side.waiting": "Waits for you",
  "side.no_libraries": "No templates",
  "side.space": "{used} / {capacity} used",
  "side.space_label": "Used space on {name}",
  "side.error": "Error",
  "side.queued": "Waiting: {message}",
  "side.failed": "Last sync failed",
  "side.synced": "Connected · synced",
  "side.wiped": "Wiped - not synced yet",
  "side.formatted": "Formatted - not synced yet",
  "side.partial": "Last sync partial · some items failed",
  "nav.devices_syncing": "{n} devices syncing",
  "nav.devices_syncing_one": "{n} device syncing",
  "nav.devices_one": "{name}: {pct}%",
  "nav.needs": "{n} device problems need you - the Dashboard lists them and everything else",
  "side.collapse": "Collapse the sidebar",
  "side.expand": "Expand the sidebar",
  "nav.needs_one": "1 device problem needs you - the Dashboard lists it and everything else",
});

export const NAV = [
  { id: "dashboard", path: "/", icon: "dashboard" },
  { id: "libraries", path: "/libraries", icon: "libraries" },
  { id: "devices", path: "/devices", icon: "devices" },
  { id: "templates", path: "/templates", icon: "templates" },
  { id: "proctemplates", path: "/processor-templates", icon: "processors" },
  { id: "settings", path: "/settings", icon: "settings" },
  { id: "notifications", path: "/notifications", icon: "bell" }, // besides the top-bar bell
  { id: "activity", path: "/activity", icon: "pulse" }, // what SyncMusic did
];

const PHONE = window.matchMedia("(max-width: 759px)");
const DEVICE_JOBS = ["sync"]; // jobs whose outcome the card keeps (the others only show progress)
// results: device id -> status of its last finished sync job; for a sync that succeeded as a job, its report's
// status: "partial" when items failed, "failed" when the verification failed
export const resultOf = (kind, status, outcome) =>
  kind === "sync" && status === "succeeded" && (outcome === "partial" || outcome === "failed") ? outcome : status;
// space: device id -> {capacity, free} of an idle connected device (null: unknown, not shown); dropped after its
// jobs and device events so the next render asks again
// planning: device id -> progress of its plan being made ("Planning… 40 %")
const state = { active: null, path: "/", libs: null, libsOpen: false, devices: [], jobs: new Map(), results: new Map(), space: new Map(), planning: new Map() };

function devicesBadge() {
  const busy = state.devices.filter((d) => d.busy && !d.ejecting && d.update?.state !== "running"); // an update is no sync
  if (!busy.length) return "";
  if (busy.length > 1) {
    const text = t("nav.devices_syncing", { n: busy.length });
    return html`<span class="count" data-devices-badge title="${text}" aria-label="${text}">${busy.length}</span>`;
  }
  const pct = Math.round((state.jobs.get(busy[0].busy)?.progress || 0) * 100);
  const text = t("nav.devices_one", { name: busy[0].name, pct });
  return html`<span class="count" data-devices-badge title="${text}" aria-label="${text}">${pct}%</span>`;
}

function unreadBadge() {
  const n = unreadCount();
  if (!n) return "";
  const text = t("bell.unread_n", { n });
  return html`<span class="count" data-notif-badge title="${text}" aria-label="${text}">${n > 99 ? "99+" : n}</span>`;
}

// the running sync's step under its percent: "Copying 120/400 · 18 MB/s"
function stepLine(job) {
  const text = stepText(job);
  return text ? html`<small class="mini-line" data-card-step>${text}</small>` : "";
}

// the Dashboard item's badge: the device rows of its "Needs you" this sidebar knows from /devices
function needsBadge() {
  const n = state.devices.reduce((sum, d) => sum + [d.error, d.waiting && d.connected && !d.busy, d.no_medium,
    d.feedback_pending || d.feedback_dropped, d.health_warning, d.no_libraries && d.connected].filter(Boolean).length, 0);
  if (!n) return "";
  const text = n === 1 ? t("nav.needs_one") : t("nav.needs", { n });
  return html`<span class="count" data-needs-badge title="${text}" aria-label="${text}">${n > 99 ? "99+" : n}</span>`;
}

function link(n) {
  const extra = n.id === "devices" ? devicesBadge() : n.id === "notifications" ? unreadBadge() : n.id === "dashboard" ? needsBadge() : "";
  return html`<a href="#${n.path}" data-nav="${n.id}" data-tip="${t(`nav.${n.id}`)}" ${n.id === state.active ? html`aria-current="page"` : ""}>
    <span class="ico">${icon(n.icon)}</span><span class="lbl">${t(`nav.${n.id}`)}</span>${extra}</a>`;
}
onUnread(() => renderNav());

function librariesItem() {
  const libs = state.libs || [];
  const open = state.libsOpen;
  const onAll = state.path === "/libraries";
  const add = html`<a href="#/libraries/new" data-lib-add ${state.path === "/libraries/new" ? html`aria-current="page"` : ""}>${icon("plus", { size: 16 })}${t("nav.add_library")}</a>`;
  const sub = open
    ? html`<div class="subnav" id="libLinks">${libs.length ? libs.map((l) => {
      const path = `/libraries/${encodeURIComponent(l.id)}/browse`;
      const h = health.get(l.id);
      const [dot, word] = l.enabled === false ? ["off", t("side.lib_off")] : !h ? ["checking", t("side.lib_checking")] : h.ok ? ["ok", t("side.lib_ok")] : ["error", h.text];
      return html`<a href="#${path}" data-lib="${l.id}" ${state.path.startsWith(path) ? html`aria-current="page"` : ""}>${typeIcon(l.type, "library", 16)}<span class="lib-name">${l.name}</span><span class="hdot ${dot}" data-health-dot="${dot}" title="${word}" role="img" aria-label="${word}"></span></a>`;
    }) : add}</div>`
    : "";
  return html`<div class="nav-split">
      <button type="button" class="nav-library" data-nav="libraries" data-libtoggle ${state.active === "libraries" ? html`data-active="true"` : ""}
        aria-expanded="${open ? "true" : "false"}" aria-controls="libLinks" title="${t("nav.libraries_toggle")}" data-tip="${t("nav.libraries")}">
        <span class="ico">${icon("libraries")}</span><span class="lbl">${t("nav.libraries")}</span><span class="chev">${icon(open ? "up" : "down", { size: 14 })}</span></button>
      <a class="nav-all" href="#/libraries" data-nav-all aria-label="${t("nav.all_libraries")}" title="${t("nav.all_libraries")}" ${onAll ? html`aria-current="page"` : ""}>${icon("list", { size: 16 })}</a>
    </div>${sub}`;
}

// a card: type icon + name, a status badge, then either the sync's accent bar ("38%" and the time
// left), the used-space bar ("1.2 GB / 29.8 GB"), an indeterminate bar while the clock is set, or the error's reason;
// an automatic update shows only a compact update line ("Updating…" / "Update failed"), no bar
function deviceCard(d) {
  const job = d.busy ? state.jobs.get(d.busy) : null;
  const updating = d.update?.state === "running";
  const syncing = !!d.busy && !updating;
  const queued = syncing && job?.status === "queued";
  const pct = syncing && job && !queued ? Math.round((job.progress || 0) * 100) : null;
  const result = state.results.get(d.id);
  const planning = !syncing ? state.planning.get(d.id) : null;
  const planPct = planning && planning.progress !== null && planning.progress !== undefined ? Math.round(planning.progress * 100) : null;
  // [data-state, badge state, text, badge icon]
  let [st, cls, label, glyph] = ["ready", "ok", t("side.ready"), "plug"];
  if (d.error) [st, cls, label, glyph] = ["error", "error", t("side.error"), null];
  else if (d.ejecting) [st, cls, label, glyph] = ["ejecting", "neutral", t("side.ejecting"), "eject"];
  else if (d.ejected) [st, cls, label, glyph] = ["ejected", "neutral", t("side.ejected"), "eject"];
  else if (d.no_medium) [st, cls, label, glyph] = ["no_medium", "warn", t("side.no_medium"), "plug"];
  else if (d.setting_clock) [st, cls, label, glyph] = ["clock", "neutral", t("side.clock"), "clock"];
  else if (queued) [st, cls, label, glyph] = ["queued", "warn", t("side.queued", { message: job.message || t("side.syncing") }), "clock"];
  else if (syncing) [st, cls, label, glyph] = ["running", job?.cancelling ? "warn" : "busy", job?.cancelling ? t("side.cancelling") : t(isCheck(d.busy_kind) ? "side.checking" : "side.syncing"), job?.cancelling ? "clock" : null];
  else if (planning) [st, cls, label, glyph] = ["planning", "busy", t("side.planning"), null];
  else if (d.waiting) [st, cls, label, glyph] = ["waiting", "warn", t("side.waiting"), "clock"];
  // a wipe / format since the last full sync: its old result no longer describes the device
  else if (d.erased && !d.no_libraries) [st, cls, label, glyph] = ["erased", "neutral", t(`side.${d.erased.how}`), null];
  else if (result === "failed") [st, cls, label, glyph] = ["failed", "error", t("side.failed"), null];
  // nothing to sync yet; before "synced": a device without templates is not in sync with anything (275)
  else if (d.no_libraries) [st, cls, label, glyph] = ["no-libraries", "warn", t("side.no_libraries"), null];
  else if (result === "partial") [st, cls, label, glyph] = ["partial", "warn", t("side.partial"), null];
  else if (result === "succeeded") [st, cls, label, glyph] = ["done", "ok", t("side.synced"), null];
  // the time left, not the current file: "~12 min left" / "Estimating the time left…"
  const eta = pct === null || job?.cancelling ? null : etaText(job.eta_s, true);
  const left = eta === null ? "" : eta ? t("side.left", { left: eta }) : t("eta.estimating");
  const space = pct === null && !planning && idle(d) ? state.space.get(d.id) : null;
  const used = space ? Math.max(0, space.capacity - space.free) : 0;
  const usedPct = space && space.capacity ? Math.round((used / space.capacity) * 100) : 0;
  const upd = !syncing && (updating || d.update?.state === "failed")
    ? updateLine(updating ? "running" : "failed", t(updating ? "side.updating" : "side.update_failed"), { compact: true }) : "";
  // a device with a sync opens that job's status, any other its page
  const href = syncing ? `#/devices/${encodeURIComponent(d.id)}/sync/${encodeURIComponent(d.busy)}`
    : planning ? `#/devices/${encodeURIComponent(d.id)}/plan` : `#/devices/${encodeURIComponent(d.id)}`;
  // Update now: beside the card's link, not inside it (no button in a link); an idle device only
  const now = idle(d) && !d.error && !d.no_libraries && d.update?.state !== "off"
    ? html`<button type="button" class="mini-upd" data-update-now="${d.id}" title="${t("side.update_now", { name: d.name })}" aria-label="${t("side.update_now", { name: d.name })}">${icon("sync", { size: 14 })}</button>` : "";
  // the collapsed sidebar shows only the type icon in a progress ring with a status dot
  const ring = pct ?? (st === "planning" ? planPct : null);
  const tip = `${d.name} · ${label}${ring === null ? "" : ` ${ring}%`}`;
  const rail = html`<span class="mini-rail" aria-hidden="true" style="--p:${ring ?? 0}"${ring === null ? "" : html` data-ring`}>${typeIcon(d.type, "device", 18)}<i class="rail-dot ${cls}"></i></span>`;
  return html`<div class="mini-wrap${now ? " has-upd" : ""}"><a class="device-mini" href="${href}" data-device-card="${d.id}" data-state="${st}" data-tip="${tip}" aria-label="${t(syncing ? "nav.open_job" : "nav.open_device", { name: d.name })}">
    ${rail}<span class="mini-top">${typeIcon(d.type, "device", 18)}<b>${d.name}</b></span>
    <span class="mini-badge" data-card-sub>${badge(cls, label, glyph)}</span>
    ${st === "clock" || (st === "planning" && planPct === null) ? html`<span class="bar indet" role="progressbar" aria-label="${t(st === "clock" ? "side.clock" : "side.planning")}"><i></i></span>` : ""}
    ${st === "planning" && planPct !== null ? html`<span class="bar thin" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${planPct}"><i style="width:${planPct}%"></i></span>` : ""}
    ${pct === null ? "" : html`<span class="bar thin" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></span>`}
    ${space ? html`<span class="space-bar" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${usedPct}" aria-label="${t("side.space_label", { name: d.name })}"><i style="width:${usedPct}%"></i></span>` : ""}
    ${pct !== null ? html`<small class="mini-line"><span class="mono" data-card-pct>${pct}%</span>${left ? html`<span class="mini-left" data-card-left>${left}</span>` : ""}</small>${stepLine(job)}`
      : d.error ? html`<small class="mini-line err" data-card-error title="${d.error}">${d.error}</small>`
      : space ? html`<small class="mini-line" data-space>${t("side.space", { used: bytes(used), capacity: bytes(space.capacity) })}</small>`
      : st === "clock" ? html`<small class="mini-line">${t("side.clock_soon")}</small>`
      : st === "planning" && planPct !== null ? html`<small class="mini-line"><span class="mono" data-card-pct>${planPct}%</span></small>` : ""}
    ${upd}</a>${now}</div>`;
}

const idle = (d) => d.connected && !d.busy && !d.ejecting && !d.ejected && !d.setting_clock && !d.no_medium;

// one request per idle device whose space is not known yet - only one already mounted (or a folder): the sidebar
// never mounts a disk; a failure (directory gone) hides it
async function loadSpace() {
  const ask = state.devices.filter((d) => idle(d) && (d.mounted_at || d.kind === "folder") && !state.space.has(d.id));
  if (!ask.length) return;
  ask.forEach((d) => state.space.set(d.id, null));
  await Promise.all(ask.map((d) => get(`/devices/${encodeURIComponent(d.id)}/space`)
    .then((r) => state.space.set(d.id, r.capacity ? { capacity: r.capacity, free: r.free } : null))
    .catch(() => state.space.set(d.id, null))));
  renderNav();
}

export function renderNav(active = state.active, path = state.path) {
  state.active = active;
  state.path = path;
  const items = NAV.map((n) => (n.id === "libraries" ? librariesItem() : link(n)));
  setHtml($("#nav"), items);
  const cards = state.devices.filter((d) => d.connected);
  setHtml($("#sideBottom"), cards.length ? html`<div class="side-head side-head-row"><span class="lbl">${t("side.connected")}</span>${cards.length > 1
    ? html`<button type="button" class="btn small ghost" data-sync-all data-tip="${t("sync_all.button")}" title="${t("sync_all.button")}" aria-label="${t("sync_all.button")}">${icon("sync", { size: 14 })}</button>` : ""}</div>${cards.map(deviceCard)}` : "");
}

async function loadLibraries() {
  try { state.libs = await get("/libraries"); } catch { state.libs = state.libs || []; }
  renderNav();
  if (state.libsOpen) checkUnknown(state.libs); // the dropdown's health dots (one check per library and page load)
}

// the outcome of each device's last sync job before this page was opened
async function loadResults() {
  let jobs;
  try { jobs = await get("/jobs"); } catch { return; }
  const last = new Map();
  for (const j of jobs) {
    if (!j.device_id || !j.finished_at || !DEVICE_JOBS.includes(j.kind)) continue;
    if (!last.has(j.device_id) || last.get(j.device_id).finished_at < j.finished_at) last.set(j.device_id, j);
  }
  for (const [id, j] of last) if (!state.results.has(id)) state.results.set(id, resultOf(j.kind, j.status, j.result?.status));
  renderNav();
}

async function loadDevices() {
  try { state.devices = (await get("/devices")).registered; } catch { return; }
  for (const id of state.space.keys()) if (!state.devices.some((d) => d.id === id && idle(d))) state.space.delete(id);
  // a job that started before this page was opened: its progress so far
  await Promise.all(state.devices.filter((d) => d.busy && !state.jobs.has(d.busy)).map((d) =>
    get(`/jobs/${encodeURIComponent(d.busy)}`).then((j) => state.jobs.set(d.busy, j)).catch(() => {})));
  renderNav();
  loadSpace();
}

export function setMenu(open) {
  document.body.classList.toggle("menu-open", open);
  $("#burger").setAttribute("aria-expanded", open ? "true" : "false");
  if (open) $("#nav a, #nav button")?.focus();
}

// the collapsed sidebar: a narrow rail of icons on wide screens, remembered per browser; phones keep
// the drawer. Labels stay in the DOM (visually hidden), a floating tip names the item on hover and keyboard focus.
const RAIL = "syncmusic.sidebar";
export const isRail = () => document.body.classList.contains("side-rail");
export function setRail(on) {
  document.body.classList.toggle("side-rail", on);
  try { localStorage.setItem(RAIL, on ? "rail" : "full"); } catch { /* private mode */ }
  const btn = $("#sideCollapse");
  const text = t(on ? "side.expand" : "side.collapse");
  btn.setAttribute("aria-expanded", on ? "false" : "true");
  btn.setAttribute("aria-label", text);
  btn.title = text;
  setHtml(btn, html`${icon(on ? "right" : "left", { size: 16 })}<span class="lbl">${t("side.collapse")}</span>`);
  hideTip();
}

function showTip(el) {
  const text = isRail() && !PHONE.matches ? el.closest("[data-tip]")?.dataset.tip : null;
  const tip = $("#sideTip");
  if (!text) { hideTip(); return; }
  const r = el.closest("[data-tip]").getBoundingClientRect();
  tip.textContent = text;
  tip.hidden = false;
  tip.style.top = `${Math.round(r.top + r.height / 2)}px`;
  tip.style.left = `${Math.round(r.right + 8)}px`;
}
function hideTip() { const tip = $("#sideTip"); if (tip) tip.hidden = true; }

function initRail() {
  let saved = null;
  try { saved = localStorage.getItem(RAIL); } catch { /* private mode */ }
  setRail(saved === "rail");
  $("#sideCollapse").addEventListener("click", () => setRail(!isRail()));
  const side = $("#sidebar");
  side.addEventListener("mouseover", (e) => { if (e.target.closest("[data-tip]")) showTip(e.target); else hideTip(); });
  side.addEventListener("mouseleave", hideTip);
  side.addEventListener("focusin", (e) => showTip(e.target));
  side.addEventListener("focusout", hideTip);
  side.addEventListener("scroll", hideTip);
  // "[" toggles it, outside text fields
  document.addEventListener("keydown", (e) => {
    if (e.key !== "[" || e.ctrlKey || e.metaKey || e.altKey || PHONE.matches) return;
    if (e.target.closest?.("input, textarea, select, [contenteditable=''], [contenteditable='true']") || document.querySelector(".modal-back")) return;
    e.preventDefault();
    setRail(!isRail());
  });
}

/** 'Sync all connected': full sync or update on every connected device, from the sidebar and the
 * dashboard; the skipped ones are named with their reason, the summary comes as a toast when all have ended. */
export async function syncAll() {
  const mode = await dialog({
    title: t("sync_all.title"), message: t("sync_all.text"),
    buttons: [{ label: t("common.cancel"), value: null }, { label: t("sync_all.update"), value: "update" }, { label: t("sync_all.full"), value: "full", kind: "primary" }],
  });
  if (!mode) return;
  try {
    const r = await post("/devices/sync-all", { mode });
    const skipped = r.skipped.map((s) => `${s.name} (${t(`sync_all.reason.${s.reason}`)})`).join(", ");
    const head = r.devices.length ? t("sync_all.started", { n: r.devices.length }) : t("sync_all.none");
    toast(skipped ? `${head}. ${t("sync_all.skipped", { list: skipped })}` : head, r.devices.length ? "ok" : "error");
  } catch (err) { toast(err.message, "error"); }
}

export function initSidebar() {
  initRail();
  $("#nav").addEventListener("click", (e) => {
    const toggle = e.target.closest("[data-libtoggle]");
    if (!toggle) return;
    if (isRail() && !PHONE.matches) { location.hash = "#/libraries"; return; } // no dropdown in the rail
    state.libsOpen = !state.libsOpen;
    renderNav();
    if (state.libsOpen) checkUnknown(state.libs);
    $("[data-libtoggle]").focus();
  });
  $("#sideBottom").addEventListener("click", async (e) => {
    if (e.target.closest("[data-sync-all]")) { syncAll(); return; }
    const btn = e.target.closest("[data-update-now]");
    if (!btn) return;
    const d = state.devices.find((x) => x.id === btn.dataset.updateNow);
    btn.disabled = true;
    try {
      await post(`/devices/${encodeURIComponent(btn.dataset.updateNow)}/update`);
      toast(t("side.update_started", { name: d?.name || btn.dataset.updateNow }), "ok");
    } catch (err) { toast(err.message, "error"); btn.disabled = false; }
  });
  $("#burger").addEventListener("click", () => setMenu(!document.body.classList.contains("menu-open")));
  $("#navBackdrop").addEventListener("click", () => setMenu(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && document.body.classList.contains("menu-open")) { setMenu(false); $("#burger").focus(); }
  });
  window.addEventListener("hashchange", () => setMenu(false));
  PHONE.addEventListener("change", () => setMenu(false));
  onHealth(() => { if (state.libsOpen) renderNav(); });
  onEvent((ev) => {
    if (ev.type === "sync_all.finished") toast(t("sync_all.finished", ev.data), ev.data.failed ? "error" : "ok");
    if (ev.type === "plan.progress" || ev.type === "plan.finished") {
      if (ev.type === "plan.progress") state.planning.set(ev.data.device_id, ev.data);
      else state.planning.delete(ev.data.device_id);
      renderNav();
    } else if (ev.type === "job.progress") {
      // progress events do not carry the status: a job that reports while waiting for a slot is still queued
      const prev = state.jobs.get(ev.data.job_id);
      state.jobs.set(ev.data.job_id, { ...prev, ...ev.data });
      if (state.devices.some((d) => d.busy === ev.data.job_id)) renderNav();
    } else if (ev.type.startsWith("device.") || ev.type.startsWith("sync.") || ev.type.startsWith("job.")) {
      if (ev.type === "job.created") state.jobs.set(ev.data.job_id, { ...ev.data, status: "queued", progress: 0 });
      if (ev.type === "job.started") state.jobs.set(ev.data.job_id, { ...state.jobs.get(ev.data.job_id), status: "running" });
      if (ev.data?.device_id && (ev.type === "job.finished" || ev.type.startsWith("device."))) state.space.delete(ev.data.device_id);
      if (ev.type === "job.finished") {
        state.jobs.delete(ev.data.job_id);
        if (ev.data.device_id && DEVICE_JOBS.includes(ev.data.kind)) state.results.set(ev.data.device_id, resultOf(ev.data.kind, ev.data.status, ev.data.outcome));
      }
      loadDevices();
    }
  });
  refreshSidebar();
}

// after a reconnect (events may have been missed)
export function refreshSidebar() {
  get("/planning").then((ps) => { state.planning = new Map(ps.map((p) => [p.device_id, p])); renderNav(); }).catch(() => {});
  loadLibraries();
  loadDevices();
  loadResults();
}

// views call this after adding, renaming or removing a library (there are no library events)
export const librariesChanged = loadLibraries;
