// Dashboard: four stat cards, "Needs you" (plans
// waiting, unknown drives, device errors, devices without templates, a full sync stopped by the device's storage,
// unreachable libraries, failed or cancelled syncs, items that did not fit, storage not shared, system check errors,
// an available update - each with one action, most severe first, some dismissible), active jobs with live progress and Cancel in place (automatic
// updates are quiet rows without a bar), recent jobs (successful updates left out) and library health.
// Everything leads somewhere: stat cards and whole rows are links - a row's main link stretches over the
// row, the links and buttons inside it keep their own targets.
import { html, setHtml, when } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, post, patch } from "../api.js";
import { badge, updateLine, toast, errorMessage, confirm } from "../ui.js";
import { etaText, deviceState, stepText, betaBadge } from "./common.js";
import { icon, typeIcon } from "../icons.js";
import { jobHref, kindText, confirmCancel } from "./job.js";
import { resultOf, syncAll } from "../sidebar.js";
import { health, checkLibrary, checkUnknown } from "../libhealth.js";

addStrings({
  "dash.sub": "{connected} connected · {needs} waiting for you · checked {time}",
  "dash.sub_empty": "Nothing set up yet.",
  "dash.phone_devices": "Connected devices",
  "dash.phone.sync": "Sync",
  "dash.phone.review": "Review & sync",
  "dash.phone.progress": "Show progress",
  "dash.phone.fix": "Open settings",
  "dash.phone.eject": "Eject",
  "dash.phone.eject_title": "Eject {name}?",
  "dash.phone.eject_text": "Unplug it once it says Ejected.",
  "dash.phone.ejected": "{name} ejected - unplug it",
  "dash.all_devices": "All devices",
  "dash.stat.connected": "Connected devices",
  "dash.stat.connected_sub": "{n} registered",
  "dash.stat.attention": "Needs attention",
  "dash.stat.attention_sub": "Plans, errors, warnings",
  "dash.stat.all_clear": "All clear",
  "dash.stat.jobs": "Running jobs",
  "dash.stat.idle": "Idle",
  "dash.stat.libraries": "Libraries",
  "dash.stat.libraries_bad": "{n} need a look",
  "dash.stat.libraries_ok": "All reachable",
  "dash.needs": "Needs you",
  "dash.health_title": "{name}: storage may be failing",
  "dash.sync_failed_title": "{name}: last sync failed",
  "dash.sync_cancelled_title": "{name}: last sync was cancelled",
  "dash.sync_failed_sub": "Open the sync to see what went wrong, then sync again",
  "dash.space_title": "{name}: {n} items did not fit",
  "dash.space_title_one": "{name}: 1 item did not fit",
  "dash.space_sub": "Free space on the device or narrow its templates",
  "dash.no_medium_title": "{name}: storage not shared",
  "dash.no_medium_sub": "Allow USB storage access on the player",
  "dash.update_title": "SyncMusic {version} is available",
  "dash.update_sub": "What changed and how to update: Settings → Updates",
  "dash.check_title": "System check: {label}",
  "dash.open_settings": "Open settings",
  "dash.dismiss": "Dismiss",
  "dash.needs_count": "{n} items",
  "dash.needs_count_one": "1 item",
  "dash.nothing_needs": "Nothing waits for you.",
  "dash.review_title": "{name}: plan ready to review",
  "dash.review_sub": "Nothing is written until you apply.",
  "dash.review": "Review plan",
  "dash.unknown_title": "Unknown drive: {model}",
  "dash.unknown_model": "unnamed drive",
  "dash.register": "Register",
  "dash.error_title": "{name}: {error}",
  "dash.error_sub": "Open the device to see what happened; the log has the details.",
  "dash.open_device": "Open device",
  "dash.health_ack": "Acknowledge",
  "dash.health_acked": "Storage warning acknowledged for {name}: it comes back on a new error.",
  "dash.details": "Details",
  "dash.no_templates_title": "{name} has no templates",
  "dash.no_templates_sub": "Choose what goes on it to sync it.",
  "dash.choose_templates": "Choose templates",
  "dash.gentle_title": "{name}: full sync stopped",
  "dash.gentle_sub": "The device's storage failed during the sync. Gentle write mode writes in small batches with pauses.",
  "dash.gentle_on": "Turn on gentle mode",
  "dash.fb_title": "{name}: listening data not delivered",
  "dash.fb_pending": "{n} plays, ratings or positions wait for their library",
  "dash.fb_pending_one": "{n} play, rating or position waits for its library",
  "dash.fb_dropped": "{n} given up",
  "dash.fb_dropped_one": "{n} given up",
  "dash.fb_show": "Show",
  "dash.gentle_done": "Gentle write mode is on for {name}. It applies from the next time the device is mounted.",
  "dash.gentle_undone": "Gentle write mode is off again for {name}.",
  "dash.lib_bad_title": "{name} is unreachable",
  "dash.open_library": "Open library",
  "dash.jobs": "Active jobs",
  "dash.no_jobs": "No jobs running. Syncs start when a device is plugged in, on its schedule, or from Sync.",
  "dash.job_sub": "{kind} · started {when}",
  "dash.updating": "Updating…",
  "dash.recent_jobs": "Recent jobs",
  "dash.no_recent": "No finished jobs yet.",
  "dash.libraries": "Library health",
  "dash.all_libraries": "All libraries",
  "dash.no_libraries": "No libraries yet.",
  "dash.checking": "Checking…",
  "dash.lib_ok": "Reachable",
  "dash.lib_bad": "Unreachable",
  "dash.lib_disabled": "Disabled",
  "dash.lib_updated": "Updated {when}",
  "dash.lib_never": "Not loaded yet",
  "dash.lib_settings": "Settings of {name}",
  "dash.empty_title": "Let’s set up your first sync",
  "dash.start_setup": "Start the setup",
  "dash.empty_text": "Connect a library, plug in a player, choose what goes on it. Every step can be changed later.",
  "dash.add_library": "Add a library",
  "dash.step1": "Add a library", "dash.step1_sub": "Navidrome, Audiobookshelf or a folder",
  "dash.step2": "Plug in a device", "dash.step2_sub": "iPod, Rockbox player, USB drive or FTP",
  "dash.step3": "Pick templates", "dash.step3_sub": "What goes on it: all music, ★4+, new…",
  "dash.step4": "Review the first plan", "dash.step4_sub": "Nothing is written before you apply",
  "dash.setup": "Getting started",
  "dash.setup_count": "{done} of {n} done",
  "dash.setup.library": "Add a library", "dash.setup.device": "Connect and register a device",
  "dash.setup.templates": "Choose what goes on it", "dash.setup.sync": "Run the first sync",
  "dash.setup.doctor": "System check is green (optional)",
});

export const nav = "dashboard";
export const title = () => t("nav.dashboard");

const ACTIVE = ["queued", "running"];
const dev = (d, tab = "") => `#/devices/${encodeURIComponent(d.id)}${tab}`;
const reviewHref = (d) => `#/devices/${encodeURIComponent(d.id)}/plan${d.waiting?.plan_id ? `/${encodeURIComponent(d.waiting.plan_id)}` : ""}`;
const hhmm = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export async function render(ctx) {
  const [devices, reports, libraries, jobs] = await Promise.all([
    get("/devices"), get("/sync-reports"), get("/libraries"), get("/jobs"),
  ]);
  const reg = devices.registered;
  ctx.state.libraries = libraries;
  // the last full sync of every device (reports are newest first): a storage failure suggests gentle write mode
  const last = {};
  for (const r of reports) if (r.kind === "full" && !(r.device_id in last)) last[r.device_id] = r;
  const gentle = await gentleCandidates(reg, last);
  if (!libraries.length && !reg.length && !devices.unknown.length) return emptyDashboard();

  const [updates, checks] = await Promise.all([get("/updates").catch(() => null), systemChecks()]);
  const needs = needRows(reg, devices.unknown, gentle, libraries, last, updates, checks);
  // automatic updates are quiet rows, not jobs with a bar
  const active = jobs.filter((j) => ACTIVE.includes(j.status) && j.kind !== "update");
  const quiet = reg.filter((d) => d.update?.state === "running");
  // the last finished ones stay openable; a successful update is not news
  const recent = jobs.filter((j) => j.finished_at && !(j.kind === "update" && j.status === "succeeded"))
    .sort((a, b) => (a.finished_at < b.finished_at ? 1 : -1)).slice(0, 5);
  const connected = reg.filter((d) => d.connected).length;
  const badLibs = libraries.filter((l) => l.enabled !== false && health.get(l.id) && !health.get(l.id).ok).length;
  const running = active.find((j) => j.status === "running");
  ctx.state.statJob = running?.id || null; // the "Running jobs" card follows this job's progress

  return html`<div class="pagehead"><div><h1>${t("nav.dashboard")}</h1>
      <p data-dash-sub>${t("dash.sub", { connected, needs: needs.length, time: hhmm(new Date()) })}</p></div>
      <div class="actions">${connected > 1 ? html`<button type="button" class="btn" data-action="syncAll">${icon("sync", { size: 16 })}${t("sync_all.button")}</button>` : ""}<a class="btn" href="#/devices">${icon("devices", { size: 16 })}${t("dash.all_devices")}</a></div></div>
    ${phoneDevices(reg.filter((d) => d.connected), active)}
    <div class="dash-stats">
      ${stat("connected", "plug", "ok", t("dash.stat.connected"), connected, t("dash.stat.connected_sub", { n: reg.length }), "#/devices")}
      ${stat("attention", "warn", needs.length ? "wn" : "ok", t("dash.stat.attention"), needs.length,
        needs.length ? t("dash.stat.attention_sub") : t("dash.stat.all_clear"), needs[0]?.href || null)}
      ${stat("jobs", "sync", "ac", t("dash.stat.jobs"), active.length,
        running ? jobStat(running.progress, running.eta_s) : t("dash.stat.idle"),
        active.length ? jobHref(active[0]) : null)}
      ${stat("libraries", "libraries", "in", t("dash.stat.libraries"), libraries.length,
        badLibs ? t("dash.stat.libraries_bad", { n: badLibs }) : t("dash.stat.libraries_ok"), "#/libraries")}
    </div>
    <div class="dash-cols">
      <div class="dash-main">
        ${setupCard(libraries, reg, reports, checks)}
        <section class="panel" aria-labelledby="hNeeds" data-needs>
          <div class="panel-head"><h2 id="hNeeds">${t("dash.needs")}</h2><span class="muted">${needs.length === 1 ? t("dash.needs_count_one") : t("dash.needs_count", { n: needs.length })}</span></div>
          ${needs.length ? needs.map((n) => n.row) : html`<div class="panel-empty">${icon("ccheck", { size: 18 })}${t("dash.nothing_needs")}</div>`}
        </section>
        <section class="panel" aria-labelledby="hActive">
          <div class="panel-head"><h2 id="hActive">${t("dash.jobs")}</h2></div>
          <div id="dashJobs">${active.map((j) => jobRow(j, reg, libraries))}${quiet.map(quietRow)}
          ${active.length || quiet.length ? "" : html`<div class="panel-empty">${t("dash.no_jobs")}</div>`}</div>
        </section>
      </div>
      <div class="dash-side">
        <section class="panel" aria-labelledby="hRecent">
          <div class="panel-head"><h2 id="hRecent">${t("dash.recent_jobs")}</h2></div>
          ${recent.length ? recent.map(recentRow) : html`<div class="panel-empty">${t("dash.no_recent")}</div>`}
        </section>
        <section class="panel" aria-labelledby="hLibs">
          <div class="panel-head"><h2 id="hLibs">${t("dash.libraries")}</h2><a class="btn ghost small" href="#/libraries">${t("dash.all_libraries")}</a></div>
          ${libraries.length ? libraries.map(libRow) : html`<div class="panel-empty">${t("dash.no_libraries")}</div>`}
        </section>
      </div>
    </div>`;
}

function emptyDashboard() {
  const steps = [1, 2, 3, 4];
  return html`<div class="pagehead"><div><h1>${t("nav.dashboard")}</h1><p>${t("dash.sub_empty")}</p></div></div>
    <div class="dash-empty" data-dash-empty>
      <div class="dash-empty-text"><span class="tile">${icon("sync", { size: 24 })}</span><h2>${t("dash.empty_title")}</h2>
        <p>${t("dash.empty_text")}</p>
        <div class="toolbar"><a class="btn primary" href="#/welcome" data-start-wizard>${t("dash.start_setup")}</a><a class="btn" href="#/libraries/new">${t("dash.add_library")}</a></div></div>
      <ol class="dash-steps">${steps.map((n) => html`<li><span class="num">${n}</span><span><b>${t(`dash.step${n}`)}</b><small>${t(`dash.step${n}_sub`)}</small></span></li>`)}</ol>
    </div>`;
}

// the first-run checklist: shown until every required step is done or it is dismissed; the
// dismissal is remembered per browser under a key of the first library and device, so a fresh install shows it again
function setupCard(libraries, reg, reports, checks) {
  const first = reg[0];
  const steps = [
    ["library", libraries.length > 0, "#/libraries/new"],
    ["device", reg.length > 0, "#/devices"],
    ["templates", reg.some((d) => !d.no_libraries), first ? dev(first, "/rules") : "#/devices"],
    ["sync", reports.some((r) => r.kind === "full" && r.status === "succeeded"), first ? dev(first) : "#/devices"],
  ];
  const key = `setup:${libraries[0]?.id ?? ""}:${first?.id ?? ""}`;
  if (steps.every((x) => x[1]) || dismissed().has(key)) return "";
  steps.push(["doctor", !checks.some((c) => c.state === "error"), "#/settings"]);
  const done = steps.filter((x) => x[1]).length;
  return html`<section class="panel" aria-labelledby="hSetup" data-setup>
    <div class="panel-head"><h2 id="hSetup">${t("dash.setup")}</h2><span class="muted">${t("dash.setup_count", { done, n: steps.length })}</span>
      <button type="button" class="btn small ghost" data-action="dismissNeed" data-key="${key}" title="${t("dash.dismiss")}" aria-label="${t("dash.dismiss")}">${icon("x", { size: 14 })}</button></div>
    <ol class="dash-steps setup-steps">${steps.map(([id, ok, href], i) => html`<li data-setup-step="${id}" data-done="${ok ? "true" : "false"}">
      <span class="num">${ok ? icon("check", { size: 14 }) : i + 1}</span><a href="${href}">${t(`dash.setup.${id}`)}</a></li>`)}</ol></section>`;
}

// devices whose last full sync failed on their storage and that still write without gentle mode
async function gentleCandidates(reg, last) {
  const ids = reg.filter((d) => d.connected && last[d.id]?.suggest_gentle && last[d.id].status === "failed").map((d) => d.id);
  const infos = await Promise.all(ids.map((id) => get(`/devices/${encodeURIComponent(id)}`).catch(() => null)));
  return infos.filter((i) => i && !i.config.sync?.gentle_write).map((i) => ({ id: i.config.id, name: i.config.name, job: last[i.config.id].job_id }));
}

// one row per thing that needs the user: a calm sentence and one primary action (a secondary one at most)
// most severe first: errors, then warnings, then information
const SEVERITY = { er: 0, wn: 1, in: 2 };
const DISMISSED = "syncmusic.dismissed";

function dismissed() {
  try { return new Set(JSON.parse(localStorage.getItem(DISMISSED) || "[]")); } catch (e) { return new Set(); }
}

// the system check probes every library: asked once per page load and then every 10 minutes, not on every rerender
let checkCache = { at: 0, items: [] };
async function systemChecks() {
  if (Date.now() - checkCache.at > 600_000) {
    checkCache = { at: Date.now(), items: await get("/diagnostics").catch(() => []) };
  }
  return checkCache.items;
}

function needRows(reg, unknown, gentle, libraries, last = {}, updates = null, checks = []) {
  const rows = [];
  const gone = dismissed();
  // `dismiss`: a key remembered in this browser - the row stays away until its key changes (a newer report, version)
  const add = (key, tone, ico, title, sub, href, action, extra = "", dismiss = null) => {
    if (dismiss && gone.has(dismiss)) return;
    const x = dismiss ? html`<button type="button" class="btn small ghost over" data-action="dismissNeed" data-key="${dismiss}" title="${t("dash.dismiss")}" aria-label="${t("dash.dismiss")}">${icon("x", { size: 14 })}</button>` : "";
    rows.push({ href, tone, row: html`<div class="need linkrow" ${key}>
      <span class="need-ico ${tone}" aria-hidden="true">${ico}</span>
      <div class="meta"><b><a class="stretch" href="${href}">${title}</a></b><small>${sub}</small></div>
      <div class="need-actions">${extra}${action}${x}</div></div>` });
  };
  for (const d of reg.filter((x) => x.waiting && !x.busy && x.connected)) {
    add(html`data-need="review" data-device="${d.id}"`, "wn", icon("clock", { size: 17 }), t("dash.review_title", { name: d.name }),
      [d.waiting.reason, t("dash.review_sub")].filter(Boolean).join(" · "), reviewHref(d),
      html`<a class="btn small over" data-review="${d.id}" href="${reviewHref(d)}">${t("dash.review")}</a>`);
  }
  for (const u of unknown) {
    const href = `#/devices/register?disk=${encodeURIComponent(u.disk)}`;
    // an MTP player: its type's icon and Beta badge, the USB serial instead of the sysfs path
    const probe = u.mtp ? u.probes?.[0]?.type_name : null;
    add(html`data-need="unknown" data-unknown-row="${u.disk}"`, "in", typeIcon(probe || "unknown", "device", 17),
      html`${t("dash.unknown_title", { model: [u.vendor, u.model].filter(Boolean).join(" ") || t("dash.unknown_model") })}${betaBadge(probe)}`,
      u.mtp ? u.serial || "" : u.disk, href,
      html`<a class="btn small over" data-register="${u.disk}" href="${href}">${t("dash.register")}</a>`);
  }
  for (const d of reg.filter((x) => x.error)) {
    add(html`data-need="error" data-device-row="${d.id}"`, "er", icon("xcircle", { size: 17 }), t("dash.error_title", { name: d.name, error: d.error }),
      t("dash.error_sub"), dev(d), html`<a class="btn small over" href="${dev(d)}">${t("dash.open_device")}</a>`,
      html`<a class="btn small ghost over" data-device-error href="#/logs?${new URLSearchParams({ device: d.id })}">${t("dash.details")}</a>`);
  }
  for (const g of gentle) {
    const details = g.job ? html`<a class="btn small ghost over" href="#/devices/${encodeURIComponent(g.id)}/sync/${encodeURIComponent(g.job)}">${t("dash.details")}</a>` : "";
    add(html`data-need="gentle" data-device="${g.id}"`, "er", icon("shield", { size: 17 }), t("dash.gentle_title", { name: g.name }),
      t("dash.gentle_sub"), dev(g), html`<button type="button" class="btn small over" data-action="gentleOn" data-id="${g.id}" data-name="${g.name}">${t("dash.gentle_on")}</button>`, details);
  }
  // storage that may be failing: the device's Storage health card says why and what to do
  for (const d of reg.filter((x) => x.health_warning)) {
    add(html`data-need="health" data-device="${d.id}"`, "er", icon("warn", { size: 17 }), t("dash.health_title", { name: d.name }),
      d.health_warning, dev(d), html`<a class="btn small over" href="${dev(d)}">${t("dash.open_device")}</a>`,
      html`<button type="button" class="btn small ghost over" data-action="healthAck" data-id="${d.id}" data-name="${d.name}">${t("dash.health_ack")}</button>`);
  }
  // listening data that did not reach its library: the device page opens its "Not delivered" list
  for (const d of reg.filter((x) => x.feedback_pending || x.feedback_dropped)) {
    const href = dev(d, "?feedback=1");
    const sub = [d.feedback_pending ? t("dash.fb_pending", { n: d.feedback_pending }) : "",
      d.feedback_dropped ? t("dash.fb_dropped", { n: d.feedback_dropped }) : ""].filter(Boolean).join(" · ");
    add(html`data-need="feedback" data-device="${d.id}"`, "wn", icon("reverse", { size: 17 }), t("dash.fb_title", { name: d.name }),
      sub, href, html`<a class="btn small over" href="${href}">${t("dash.fb_show")}</a>`);
  }
  // nothing to plan: say so instead of "Review plan"; the device's templates decide its libraries (408)
  for (const d of reg.filter((x) => x.no_libraries && x.connected)) {
    add(html`data-need="no-templates" data-no-libraries="${d.id}"`, "wn", icon("templates", { size: 17 }), t("dash.no_templates_title", { name: d.name }),
      t("dash.no_templates_sub"), dev(d, "/rules"), html`<a class="btn small over" href="${dev(d, "/rules")}">${t("dash.choose_templates")}</a>`);
  }
  for (const l of libraries.filter((x) => x.enabled !== false && health.get(x.id) && !health.get(x.id).ok)) {
    const href = `#/libraries/${encodeURIComponent(l.id)}`;
    add(html`data-need="library" data-library="${l.id}"`, "er", icon("xcircle", { size: 17 }), t("dash.lib_bad_title", { name: l.name }),
      health.get(l.id).text, href, html`<a class="btn small over" href="${href}">${t("dash.open_library")}</a>`);
  }
  // the last full sync failed or was cancelled (not the storage case above, which offers gentle mode)
  const gentleIds = new Set(gentle.map((g) => g.id));
  for (const d of reg.filter((x) => !x.busy && !gentleIds.has(x.id) && ["failed", "cancelled"].includes(last[x.id]?.status))) {
    const r = last[d.id];
    const href = r.job_id ? `#/devices/${encodeURIComponent(d.id)}/sync/${encodeURIComponent(r.job_id)}` : dev(d);
    add(html`data-need="sync-${r.status}" data-device="${d.id}"`, r.status === "failed" ? "er" : "wn", icon("xcircle", { size: 17 }),
      t(r.status === "failed" ? "dash.sync_failed_title" : "dash.sync_cancelled_title", { name: d.name }), r.message || t("dash.sync_failed_sub"),
      href, html`<a class="btn small over" href="${href}">${t("dash.details")}</a>`, "", `sync:${r.id}`);
  }
  // items the last full sync left out for lack of space (the device's space map tells what takes it)
  for (const d of reg.filter((x) => last[x.id]?.skipped_for_space?.length)) {
    const r = last[d.id], n = r.skipped_for_space.length;
    add(html`data-need="space" data-device="${d.id}"`, "wn", icon("warn", { size: 17 }),
      t(n === 1 ? "dash.space_title_one" : "dash.space_title", { name: d.name, n }), t("dash.space_sub"), dev(d),
      html`<a class="btn small over" href="${dev(d)}">${t("dash.open_device")}</a>`, "", `space:${r.id}`);
  }
  // a player plugged in that does not share its storage yet
  for (const d of reg.filter((x) => x.no_medium)) {
    add(html`data-need="no-medium" data-device="${d.id}"`, "in", icon("plug", { size: 17 }), t("dash.no_medium_title", { name: d.name }),
      t("dash.no_medium_sub"), dev(d), html`<a class="btn small over" href="${dev(d)}">${t("dash.open_device")}</a>`);
  }
  // system check errors; library connections have their own rows above
  for (const c of checks.filter((x) => x.state === "error" && x.id !== "libraries")) {
    add(html`data-need="check" data-check="${c.id}"`, "er", icon("warn", { size: 17 }), t("dash.check_title", { label: c.label }),
      [c.detail, c.fix].filter(Boolean).join(" · "), "#/settings", html`<a class="btn small over" href="#/settings">${t("dash.open_settings")}</a>`);
  }
  if (updates?.available && updates.latest) {
    add(html`data-need="update"`, "in", icon("download", { size: 17 }), t("dash.update_title", { version: updates.latest }), t("dash.update_sub"), "#/settings",
      html`<a class="btn small over" href="#/settings">${t("dash.open_settings")}</a>`, "", `update:${updates.latest}`);
  }
  return rows.map((r, i) => [r, i]).sort((a, b) => SEVERITY[a[0].tone] - SEVERITY[b[0].tone] || a[1] - b[1]).map(([r]) => r);
}

const jobStat = (progress, eta) => [`${Math.round((progress || 0) * 100)}%`, etaText(eta, true)].filter(Boolean).join(" · ");

function stat(key, ico, tone, label, value, sub, href) {
  const body = html`<span class="stat-label"><span class="stat-ico ${tone}">${icon(ico, { size: 16 })}</span><span>${label}</span>${href ? icon("right", { size: 14 }) : ""}</span>
    <strong>${value}</strong><small>${sub}</small>`;
  return href ? html`<a class="stat statlink" href="${href}" data-stat="${key}">${body}</a>` : html`<div class="stat" data-stat="${key}">${body}</div>`;
}

// every job opens its full view: device syncs their sync status, others the job view
function jobRow(j, reg, libraries) {
  const pct = Math.round((j.progress || 0) * 100);
  const d = reg.find((x) => x.id === j.device_id);
  const l = libraries.find((x) => x.id === j.library_id);
  const ico = d ? typeIcon(d.type, "device", 20) : l ? typeIcon(l.type, "library", 20) : icon("sync", { size: 20 });
  const state = j.cancelling ? ["warn", t("job.cancelling"), "clock"] : j.status === "running" ? ["busy", t("status.running")] : ["neutral", t("status.waiting"), "clock"];
  return html`<div class="jobrow linkrow" data-job="${j.id}" data-kind="${j.kind}">
    <div class="jobrow-top"><span class="jobrow-ico">${ico}</span>
      <div class="meta"><b><a class="stretch" href="${jobHref(j)}" data-job-link>${j.name}</a></b><small>${t("dash.job_sub", { kind: kindText(j.kind), when: hhmm(new Date(j.started_at || j.created_at)) })}</small></div>
      <span data-job-state>${badge(...state)}</span>
      <button type="button" class="btn small over" data-action="cancelJob" data-id="${j.id}" data-name="${j.name}" ${j.cancelling ? "disabled" : ""}>${t(j.cancelling ? "job.cancelling" : "job.cancel")}</button></div>
    <div class="bar thin ${d ? "" : "info"}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${j.name}"><i style="width:${pct}%"></i></div>
    <div class="jobrow-line"><span data-job-msg>${pct}%${j.message ? ` · ${j.message}` : ""}</span>${j.status === "running" ? html`<span data-job-eta>${etaText(j.eta_s)}</span>` : ""}</div><small class="muted" data-job-step>${stepText(j)}</small></div>`;
}

/** Phone only: a card per connected device with its state, the
 * running sync's progress and big buttons - the primary one follows the state (Show progress / Review & sync / Open
 * settings / Sync), Eject beside it. Hidden on wider screens by CSS. */
function phoneDevices(conn, active) {
  if (!conn.length) return "";
  return html`<section class="dash-phone" aria-label="${t("dash.phone_devices")}" data-phone-devices>${conn.map((d) => {
    const [state, text] = deviceState(d);
    const job = active.find((j) => j.device_id === d.id && j.status === "running");
    const pct = Math.round((job?.progress || 0) * 100);
    const id = encodeURIComponent(d.id);
    const primary = job ? [jobHref(job), "right", "dash.phone.progress"]
      : d.error ? [`#/devices/${id}/settings`, "settings", "dash.phone.fix"]
      : [`#/devices/${id}/plan`, "sync", d.waiting ? "dash.phone.review" : "dash.phone.sync"];
    const blocked = !job && (d.setting_clock || d.no_medium || d.ejecting || d.ejected || (d.busy && !job));
    return html`<div class="dash-phone-card" data-phone-device="${d.id}">
      <a class="dash-phone-head" href="#/devices/${id}"><span class="dash-phone-ico">${typeIcon(d.type, "device", 24)}</span>
        <span class="dash-phone-name"><b>${d.name}</b>${badge(state, text)}</span>${icon("right", { size: 16 })}</a>
      ${job ? html`<div data-phone-job="${job.id}"><div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${d.name}"><i style="width:${pct}%"></i></div>
        <div class="jobrow-line"><span data-phone-msg>${pct}%${job.message ? ` · ${job.message}` : ""}</span><span data-phone-eta>${etaText(job.eta_s, true)}</span></div><small class="muted" data-phone-step>${stepText(job)}</small></div>` : ""}
      ${d.error ? html`<p class="muted dash-phone-err">${d.error}</p>` : ""}
      <div class="dash-phone-btns">${blocked ? html`<button type="button" class="btn primary big" disabled>${icon(primary[1], { size: 18 })}${t(primary[2])}</button>`
        : html`<a class="btn primary big" href="${primary[0]}" data-phone-primary>${icon(primary[1], { size: 18 })}${t(primary[2])}</a>`}
        <button type="button" class="btn big" data-action="phoneEject" data-id="${d.id}" data-name="${d.name}" ${job || d.ejecting || d.ejected ? "disabled" : ""}>${icon("eject", { size: 18 })}${t("dash.phone.eject")}</button></div></div>`;
  })}</section>`;
}

function quietRow(d) {
  return html`<a class="quietrow" href="${dev(d)}" data-update-row="${d.id}">${typeIcon(d.type, "device", 18)}<b>${d.name}</b>${updateLine("running", t("dash.updating"))}</a>`;
}

function recentRow(j) {
  // a sync job that succeeded shows its report's status: partial (items failed) or failed (verification)
  const status = resultOf(j.kind, j.status, j.result?.status);
  const state = { succeeded: "ok", partial: "warn", failed: "error", cancelled: "neutral" }[status] || "neutral";
  return html`<div class="recentrow linkrow" data-recent-job="${j.id}" data-status="${status}"><div class="meta"><b><a class="stretch" href="${jobHref(j)}" data-job-link>${j.name}</a></b>
    <small>${kindText(j.kind)} · ${when(j.finished_at)}</small></div>${badge(state, t(`job.status.${status}`))}</div>`;
}

function healthBadge(l) {
  if (l.enabled === false) return badge("neutral", t("dash.lib_disabled"));
  const h = health.get(l.id);
  // one word (the design's badge); the reason is in its title and in "Needs you"
  return h ? (h.ok ? badge("ok", t("dash.lib_ok")) : html`<span title="${h.text}">${badge("error", t("dash.lib_bad"))}</span>`)
    : badge("info", t("dash.checking"), "sync");
}

function libRow(l) {
  const id = encodeURIComponent(l.id);
  // the row opens the library browser, the health badge the library's settings (with its connection check)
  return html`<div class="librow linkrow" data-library-row="${l.id}">${typeIcon(l.type, "library", 18)}
    <div class="meta"><b><a class="stretch" href="#/libraries/${id}/browse">${l.name}</a></b><small>${l.updated_at ? t("dash.lib_updated", { when: when(l.updated_at) }) : t("dash.lib_never")}</small></div>
    <a class="over" data-health-link href="#/libraries/${id}" aria-label="${t("dash.lib_settings", { name: l.name })}"><span data-health="${l.id}">${healthBadge(l)}</span></a></div>`;
}

export function mounted(ctx, root) {
  // library checks run after the page is shown (one per library and page load, shared with the sidebar and the
  // Libraries list); one slow server does not hold the others; a library found unreachable joins "Needs you"
  const libs = ctx.state.libraries || [];
  for (const l of libs) {
    if (l.enabled === false || health.has(l.id)) continue;
    checkLibrary(l.id).then((h) => {
      const el = root.querySelector(`[data-health="${CSS.escape(l.id)}"]`);
      if (el) setHtml(el, healthBadge(l));
      if (!h.ok && root.isConnected) ctx.rerender();
    });
  }
  checkUnknown(libs);
}

export const actions = {
  syncAll() { syncAll(); },
  dismissNeed(el, ctx) {
    const keys = dismissed();
    keys.add(el.dataset.key);
    try { localStorage.setItem(DISMISSED, JSON.stringify([...keys].slice(-200))); } catch (e) { /* private mode */ }
    ctx.rerender();
  },
  async phoneEject(el, ctx) {
    const { id, name } = el.dataset;
    if (!(await confirm({ title: t("dash.phone.eject_title", { name }), message: t("dash.phone.eject_text"), ok: t("dash.phone.eject") }))) return;
    el.disabled = true;
    try {
      await post(`/devices/${encodeURIComponent(id)}/eject`);
      toast(t("dash.phone.ejected", { name }), "ok");
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      ctx.rerender();
    }
  },
  async cancelJob(el, ctx) {
    const { id, name } = el.dataset;
    if (!(await confirmCancel(name))) return;
    el.disabled = true;
    el.textContent = t("job.cancelling");
    try {
      await post(`/jobs/${encodeURIComponent(id)}/cancel`);
    } catch (e) {
      el.disabled = false;
      el.textContent = t("job.cancel");
      toast(errorMessage(e), "error");
    }
  },
  // hide "storage may be failing" until a newer error; Undo takes the acknowledgement back
  async healthAck(el, ctx) {
    const { id, name } = el.dataset;
    el.disabled = true;
    const url = `/devices/${encodeURIComponent(id)}/health/ack`;
    await post(url);
    el.closest("[data-need]")?.remove();
    toast(t("dash.health_acked", { name }), "ok", {
      undo: async () => {
        await post(`${url}?undo=true`);
        ctx.rerender();
      },
    });
  },
  async gentleOn(el, ctx) {
    const { id, name } = el.dataset;
    el.disabled = true;
    // keep the device's other sync settings, like the sync status page
    const set = async (on) => {
      const { config } = await get(`/devices/${encodeURIComponent(id)}`);
      await patch(`/devices/${encodeURIComponent(id)}`, { sync: { ...config.sync, gentle_write: on } });
    };
    await set(true);
    el.closest("[data-need]")?.remove();
    toast(t("dash.gentle_done", { name }), "ok", {
      undo: async () => {
        await set(false);
        toast(t("dash.gentle_undone", { name }));
        ctx.rerender();
      },
    });
  },
};

const RELOAD = ["device.", "sync.", "update.", "job.created", "job.started", "job.finished", "reverse.", "live."];

export function onEvent(ev, ctx, root) {
  if (ev.type === "job.progress") {
    const phone = root.querySelector(`[data-phone-job="${CSS.escape(ev.data.job_id)}"]`);
    if (phone) {
      const p = Math.round((ev.data.progress || 0) * 100);
      phone.querySelector(".bar i").style.width = `${p}%`;
      phone.querySelector(".bar").setAttribute("aria-valuenow", String(p));
      phone.querySelector("[data-phone-msg]").textContent = `${p}%${ev.data.message ? ` · ${ev.data.message}` : ""}`;
      phone.querySelector("[data-phone-eta]").textContent = etaText(ev.data.eta_s, true);
      phone.querySelector("[data-phone-step]").textContent = stepText(ev.data);
    }
    const row = root.querySelector(`[data-job="${CSS.escape(ev.data.job_id)}"]`);
    if (!row) {
      if (!root.querySelector(`[data-update-row="${CSS.escape(ev.data.device_id || "")}"]`)) ctx.rerender();
      return;
    }
    const pct = Math.round((ev.data.progress || 0) * 100);
    row.querySelector(".bar i").style.width = `${pct}%`;
    row.querySelector(".bar").setAttribute("aria-valuenow", String(pct));
    row.querySelector("[data-job-msg]").textContent = `${pct}%${ev.data.message ? ` · ${ev.data.message}` : ""}`;
    const eta = row.querySelector("[data-job-eta]");
    if (eta) eta.textContent = etaText(ev.data.eta_s);
    const step = row.querySelector("[data-job-step]");
    if (step) step.textContent = stepText(ev.data);
    const card = ev.data.job_id === ctx.state.statJob && root.querySelector('[data-stat="jobs"] small');
    if (card) card.textContent = jobStat(ev.data.progress, ev.data.eta_s);
    if (ev.data.cancelling) {
      const btn = row.querySelector("[data-action=cancelJob]");
      btn.disabled = true;
      btn.textContent = t("job.cancelling");
      setHtml(row.querySelector("[data-job-state]"), badge("warn", t("job.cancelling"), "clock"));
    }
  } else if (RELOAD.some((p) => ev.type.startsWith(p))) {
    ctx.rerender();
  }
}
