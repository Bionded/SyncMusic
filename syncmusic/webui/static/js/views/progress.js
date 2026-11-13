// Sync progress and report: one layout for a device's
// full sync and update (the reverse sync runs inside both). Header: type icon, "Sync · <device>", status, "Job #… · started … · took …",
// Open <device>. Running: big percent, time left, bytes written, Cancel (asks first, then "Cancelling after the
// current file…"), the bar, the phases as a checklist, "Now: …" with "file N of M", counters - all moved in place by
// `job.progress`. Finished: the outcome (icon, title, one sentence), count tiles, the failed files with their step,
// the gentle-mode suggestion after a storage failure, the reverse sync, Eject (only once every write and the
// verification are done) or Sync again. A Log card follows the job either way.
import { activityList } from "./activity.js";
import { html, when, bytes } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, post, patch } from "../api.js";
import { badge, toast, confirm, suggestionBox, problemBox } from "../ui.js";
import { etaText, writeSpeed, deviceIcon, isCheck, hibyReboot, ejectedText } from "./common.js";
import { icon } from "../icons.js";
import { feedbackLine } from "./feedback.js";

let libNames = {}; // library id → name for the "waiting for <library>" line

addStrings({
  "crumb.sync": "Sync",
  "crumb.check": "Integrity check",
  "prog.head.check": "Integrity check · {name}",
  "prog.head.check-fix": "Delete broken files · {name}",
  "prog.s.done": "Done",
  "prog.r.check": "Integrity check finished",
  "prog.r.check_failed": "The integrity check stopped",
  "prog.r.check-fix": "Broken files deleted",
  "prog.r.check-fix_failed": "Deleting the broken files failed",
  "prog.sub.check": "Nothing on the device was changed. The device's Overview lists what was found.",
  "prog.sub.check-fix": "{n} broken files deleted; the next sync writes them again.",
  "prog.sub.check-fix_one": "{n} broken file deleted; the next sync writes it again.",
  "prog.c.checked": "Checked",
  "prog.c.missing": "Missing",
  "prog.c.broken": "Broken",
  "prog.c.foreign": "Not from SyncMusic",
  "prog.back_check": "Open Storage health",
  "prog.title": "Sync of {name}",
  "prog.head.sync": "Sync · {name}",
  "prog.head.update": "Update · {name}",
  "prog.head.remove": "Remove · {name}",
  "prog.head.wipe": "Wipe · {name}",
  "prog.head.repair": "Check and repair · {name}",
  "prog.r.repaired": "File system errors corrected",
  "prog.r.repair_clean": "No file system errors found",
  "prog.r.repair_failed": "Check and repair failed",
  "prog.sub.repair": "Automatic syncs run again. Sync to write back anything the repair removed.",
  "prog.repair_output": "Check output",
  "prog.r.wiped": "{name} is wiped",
  "prog.r.wipe_failed": "Wipe failed",
  "prog.sub.wiped": "{n} files removed; Rockbox and its settings are kept. The next sync is a first sync.",
  "prog.sub.wiped_one": "{n} file removed; Rockbox and its settings are kept. The next sync is a first sync.",
  "prog.sub.wiped_android": "{n} files removed from SyncMusic's folder; nothing else on the player was touched. The next sync is a first sync.",
  "prog.sub.wiped_android_one": "{n} file removed from SyncMusic's folder; nothing else on the player was touched. The next sync is a first sync.",
  "prog.sub.wiped_hiby": "{n} files removed; the player's databases and settings are kept. The next sync is a first sync.",
  "prog.sub.wiped_hiby_one": "{n} file removed; the player's databases and settings are kept. The next sync is a first sync.",
  "prog.jobline": "Job #{id} · started {at}",
  "prog.jobid": "Job #{id}",
  "prog.took": " · took {d}",
  "prog.open": "Open device",
  "prog.queued": "Waiting for another job on this device to finish.",
  "prog.s.queued": "Waiting",
  "prog.s.running": "Running",
  "prog.s.succeeded": "Synced",
  "prog.s.partial": "Partial",
  "prog.s.failed": "Failed",
  "prog.s.cancelled": "Cancelled",
  "prog.s.skipped": "Skipped",
  "prog.s.updated": "Updated",
  "prog.s.wiped": "Wiped",
  "prog.r.update_skipped": "Update skipped",
  "prog.progress": "Sync progress",
  "prog.written": "{size} written",
  "prog.written_speed": "{size} written · {speed}/s",
  "prog.preparing": "Preparing",
  "prog.cancel": "Cancel",
  "prog.cancel_title": "Cancel the sync?",
  "prog.cancel_text": "The sync stops after the current file; what was written stays consistent on the device.",
  "prog.cancel_ok": "Cancel the sync",
  "prog.cancelling": "Cancelling after the current file…",
  "prog.phase.reverse": "Reading listening data",
  "prog.phase.copy": "Converting & copying",
  "prog.phase.database": "Writing the database",
  "prog.phase.verify": "Verifying",
  "prog.now": "Now:",
  "prog.file_n": "file {n} of {total}",
  "prog.t.written": "Files written",
  "prog.t.failed": "Failed",
  "prog.t.bytes": "Written",
  "prog.log": "Log",
  "prog.following": "Following",
  "prog.open_logs": "Open in Logs",
  "prog.done": "Finished",
  "prog.report": "Report",
  "prog.verified": "Verified on the device after flushing: {n} items.",
  "prog.verified_one": "Verified on the device after flushing: {n} item",
  "prog.not_verified": "Not verified (the sync did not finish).",
  "prog.r.succeeded": "{name} is synced",
  "prog.r.partial": "Partly synced",
  "prog.r.failed": "Sync failed",
  "prog.r.cancelled": "Cancelled after the current file",
  "prog.r.update": "Update finished",
  "prog.r.update_failed": "Update failed",
  "prog.r.reverse_failed": "Reverse sync failed",
  "prog.r.removed": "Removed from {name}",
  "prog.sub.safe": "Safe to eject.",
  "prog.sub.partial": "{n} files could not be written; everything else on the device is consistent.",
  "prog.sub.partial_one": "{n} file could not be written; everything else on the device is consistent.",
  "prog.sub.postponed": "{n} items were postponed because a library was unreachable; the next sync writes them. Everything else on the device is consistent.",
  "prog.sub.postponed_one": "{n} item was postponed because a library was unreachable; the next sync writes it. Everything else on the device is consistent.",
  "prog.sub.cancelled": "Everything written so far is complete and on the device. Sync again to continue where it stopped.",
  "prog.sub.failed": "The sync stopped. What was written before is complete; sync again to continue.",
  "prog.sub.update": "Listening data brought back, then changes pushed. No files copied.",
  "prog.sub.update_failed": "Nothing on the device was changed. Updates retry every {n} min.",
  "prog.c.added": "Added",
  "prog.c.replaced": "Replaced",
  "prog.c.tags": "Tags updated",
  "prog.c.removed": "Removed",
  "prog.c.verified": "Verified",
  "prog.c.failed": "Failed",
  "prog.c.added_cancel": "Added before cancel",
  "prog.c.playlists": "Playlists",
  "prog.c.ratings": "Ratings",
  "prog.c.positions": "Positions",
  "prog.c.copied": "Files copied",
  "prog.failed_files": "Failed files",
  "prog.step": "Step:",
  "prog.again": "Sync again",
  "prog.reverse": "Reverse sync:",
  "prog.activity": "What this sync did",
  "prog.reverse_none": "Nothing to send back.",
  "prog.reverse_none_yet": "Nothing delivered yet.",
  "prog.reverse_counts": "{plays}, {ratings}, {positions}",
  "prog.reverse_finished": "{n} episodes marked finished.",
  "prog.reverse_finished_one": "{n} episode marked finished.",
  "prog.reverse_pending": "{n} items wait for the library and are retried next time",
  "prog.reverse_pending_one": "{n} item waits for the library and is retried next time",
  "prog.failed_job": "The sync failed",
  "prog.eject": "Eject",
  "prog.eject_title": "Eject {name}?",
  "prog.eject_text": "The device is unmounted and powered off. Unplug it when it says so.",
  "prog.ejecting": "Ejecting {name}…",
  "prog.ejected": "{name} can be unplugged now.",
  "prog.auto_eject": "Ejected after the sync - you can unplug {name}.",
  "prog.eject_hint": "Eject before unplugging: it unmounts the device after every write is on it.",
  "prog.to_device": "Device page",
  "prog.folder": "A folder device: nothing to eject.",
  "prog.server": "A server: nothing to eject.",
  "prog.gentle": "Players with a CF or SD card often drop off USB under many small writes. Gentle write mode writes in batches with pauses. It is slower and stays off until you turn it on.",
  "prog.gentle_on": "Turn on gentle write mode",
  "prog.gentle_done": "Gentle write mode is on for {name}. It applies from the next time the device is mounted.",
  "prog.gentle_undone": "Gentle write mode is off again for {name}.",
});

export const nav = "devices";
export const title = (ctx) => (ctx.state.cfg ? t("prog.title", { name: ctx.state.cfg.name }) : t("nav.devices"));
// Devices / <device> / Sync
export const crumbs = (ctx) => (ctx.state.cfg ? [{ label: t("nav.devices"), href: "#/devices" },
  { label: ctx.state.cfg.name, href: `#/devices/${encodeURIComponent(ctx.state.cfg.id)}` }, { label: t(isCheck(ctx.state.job?.kind) ? "crumb.check" : "crumb.sync") }] : []);

const PHASES = ["reverse", "copy", "database", "verify"];

function took(j) {
  if (!j.started_at || !j.finished_at) return "";
  const s = Math.max(0, Math.round((new Date(j.finished_at) - new Date(j.started_at)) / 1000));
  return t("prog.took", { d: s < 60 ? `${s} s` : `${Math.round(s / 60)} min` });
}

const hhmm = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");

export async function render(ctx) {
  const [info, job, libs] = await Promise.all([
    get(`/devices/${encodeURIComponent(ctx.params.id)}`),
    get(`/jobs/${encodeURIComponent(ctx.params.jobId)}`),
    get("/libraries").catch(() => []),
  ]);
  libNames = Object.fromEntries(libs.map((l) => [l.id, l.name]));
  ctx.state.cfg = info.config;
  ctx.state.job = job;
  const cfg = info.config;
  const running = job.status === "running" || job.status === "queued";
  const id = encodeURIComponent(ctx.params.id);
  const report = job.result && job.result.device_id ? job.result : null;
  // a removal (Player contents / orphans) is a sync job whose report has no plan
  const kind = isCheck(job.kind) || job.kind === "wipe" || job.kind === "repair" ? job.kind : report?.kind === "update" || job.kind === "update" ? "update" : report && !report.plan_id && report.kind === "full" ? "remove" : "sync";
  const state = running ? (job.cancelling ? ["warn", t("prog.cancelling")] : job.status === "queued" ? ["neutral", t("prog.s.queued")] : ["busy", t("prog.s.running")])
    : kind === "update" && report?.status === "succeeded" ? ["ok", t("prog.s.updated")]
    : kind === "wipe" && job.status === "succeeded" ? ["ok", t("prog.s.wiped")]
    : kind === "repair" && job.status === "succeeded" ? ["ok", t("prog.s.done")]
    : isCheck(kind) && job.status === "succeeded" ? ["ok", t("prog.s.done")]
    : RESULT_BADGE()[report?.status || job.status] || ["neutral", job.status];
  return html`<div class="job-head">
      <span class="job-ico">${deviceIcon(cfg.type, 26)}</span>
      <div class="job-meta"><h1>${t(`prog.head.${kind}`, { name: cfg.name })}</h1>
        <div class="job-line">${badge(state[0], state[1])}<span>${job.started_at || job.created_at ? t("prog.jobline", { id: job.id, at: hhmm(job.started_at || job.created_at) }) : t("prog.jobid", { id: job.id })}${took(job)}</span></div></div>
      <a class="btn" href="#/devices/${id}">${t("prog.open", { name: cfg.name })}</a></div>
    ${running ? live(job, kind) : finished(job, report, kind, info)}
    <section class="card job-log"><div class="job-log-head"><h2>${t("prog.log")}</h2>
      ${running ? html`<span class="job-follow" data-following><i></i>${t("prog.following")}</span>` : ""}
      <a class="btn small" data-report-log href="#/logs?${new URLSearchParams({ job: job.id, level: "DEBUG" })}">${t("prog.open_logs")}</a></div>
      <pre class="codebox" data-log>${(job.log || []).slice(-30).join("\n")}</pre></section>`;
}

// the outcome badge (strings looked up at render time)
const RESULT_BADGE = () => ({
  succeeded: ["ok", t("prog.s.succeeded")], partial: ["warn", t("prog.s.partial")], failed: ["error", t("prog.s.failed")],
  cancelled: ["neutral", t("prog.s.cancelled")], skipped: ["warn", t("prog.s.skipped")],
});

function phaseList(stats, kind) {
  if (kind !== "sync" && kind !== "update") return "";
  const at = PHASES.indexOf(stats.phase);
  return html`<ol class="job-phases" data-phases>${PHASES.map((p, i) => html`<li data-phase-step="${p}" class="${i < at ? "done" : i === at ? "now" : ""}" ${i === at ? html`aria-current="step"` : ""}>
    ${icon(i < at ? "ccheck" : i === at ? "sync" : "circle", { size: 16, spin: i === at })}${t(`prog.phase.${p}`)}</li>`)}</ol>`;
}

function tallies(stats) {
  return html`<div class="count-tiles" data-tally>
    <div class="count-tile"><b data-t-written>${stats.files_done ?? 0}</b><small>${t("prog.t.written")}</small></div>
    <div class="count-tile"><b data-t-failed>${stats.failed ?? 0}</b><small>${t("prog.t.failed")}</small></div>
    <div class="count-tile"><b data-t-bytes>${bytes(stats.bytes || 0)}</b><small>${t("prog.t.bytes")}</small></div></div>`;
}

function fileLine(stats) {
  return stats.files_total ? t("prog.file_n", { n: stats.files_done ?? 0, total: stats.files_total }) : "";
}

function live(job, kind) {
  const pct = Math.round((job.progress || 0) * 100);
  const stats = job.stats || {};
  return html`<section class="card job-live" data-live>
      <div class="job-big"><span class="job-pct" data-pct>${pct}%</span>
        <span class="job-left"><span data-eta>${job.status === "running" ? etaText(job.eta_s) : t("prog.queued")}</span>
          <small class="muted" data-bytes>${stats.bytes ? t("prog.written", { size: bytes(stats.bytes) }) : ""}</small></span>
        <button class="btn" data-action="cancel" ${job.cancelling ? "disabled" : ""}>${t(job.cancelling ? "prog.cancelling" : "prog.cancel")}</button></div>
      <div class="bar job-bar ${job.cancelling ? "cancelling" : ""}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${t("prog.progress")}"><i style="width:${pct}%"></i></div>
      ${phaseList(stats, kind)}
      <div class="job-now"><span class="muted">${t("prog.now")}</span> <b data-phase>${job.message || job.name}</b><small class="muted" data-file-n>${fileLine(stats)}</small></div>
      <div class="job-now" data-preparing ${stats.preparing?.length ? "" : "hidden"}><span class="muted">${t("prog.preparing")}</span> <span data-prep-list>${(stats.preparing || []).join(" · ")}</span></div>
      ${kind === "sync" || kind === "update" ? tallies(stats) : ""}
    </section>`;
}

// the count tiles of a finished job by kind
function counts(report, kind, cancelled) {
  if (kind === "check") {
    return [[report.checked ?? 0, "prog.c.checked"], [(report.missing || []).length, "prog.c.missing"], [(report.broken || []).length, "prog.c.broken"],
      [(report.foreign || []).length, "prog.c.foreign"]];
  }
  if (kind === "check-fix") return [[`−${report.removed ?? 0}`, "prog.c.removed"]];
  const c = report.counts || {};
  const fails = (report.failures || []).length;
  if (kind === "update") {
    return [[c.playlists || 0, "prog.c.playlists"], [c.ratings || 0, "prog.c.ratings"], [c.positions || 0, "prog.c.positions"], [c.add || 0, "prog.c.copied"],
      ...(fails ? [[fails, "prog.c.failed"]] : [])];
  }
  if (kind === "remove") return [[c.remove ? `−${c.remove}` : "0", "prog.c.removed"], ...(fails ? [[fails, "prog.c.failed"]] : [])];
  if (cancelled) return [[`+${c.add || 0}`, "prog.c.added_cancel"], ...(fails ? [[fails, "prog.c.failed"]] : [])];
  return [[`+${c.add || 0}`, "prog.c.added"], [c.replace || 0, "prog.c.replaced"], [c.update_meta || 0, "prog.c.tags"],
    [c.remove ? `−${c.remove}` : "0", "prog.c.removed"], ...(c.verified !== undefined ? [[c.verified, "prog.c.verified"]] : []),
    ...(fails ? [[fails, "prog.c.failed"]] : [])];
}

// [icon, tone, title, sentence] of a finished job
function outcome(report, kind, cfg, job, ejectable) {
  const status = report?.status || (job.status === "succeeded" ? "succeeded" : "failed");
  const name = cfg.name;
  if (kind === "update") {
    if (status === "skipped") return ["circle", "nu", t("prog.r.update_skipped"), report.message.replace(/^update sync skipped: /, "")];
    return status === "failed" ? ["xcircle", "er", t("prog.r.update_failed"), t("prog.sub.update_failed", { n: cfg.update_every_min ?? 15 })]
      : ["ccheck", "ok", t("prog.r.update"), t("prog.sub.update")];
  }
  if (isCheck(kind)) {
    if (job.status !== "succeeded") return ["xcircle", "er", t(`prog.r.${kind === "check" ? "check" : "check-fix"}_failed`), ""];
    if (kind === "check-fix") return ["ccheck", "ok", t("prog.r.check-fix"), t("prog.sub.check-fix", { n: job.result?.removed ?? 0 })];
    const r = job.result || {};
    const problems = (r.missing || []).length + (r.broken || []).length;
    return [problems ? "warn" : "ccheck", problems ? "wn" : "ok", t("prog.r.check"), `${r.summary || ""}. ${t("prog.sub.check")}`];
  }
  if (kind === "wipe") return status === "succeeded" ? ["ccheck", "ok", t("prog.r.wiped", { name }), t(({ hiby: "prog.sub.wiped_hiby", android: "prog.sub.wiped_android" })[job.result?.type] ?? "prog.sub.wiped", { n: job.result?.removed ?? 0 })] : ["xcircle", "er", t("prog.r.wipe_failed"), ""];
  if (kind === "repair") {
    if (job.status !== "succeeded") return ["xcircle", "er", t("prog.r.repair_failed"), ""];
    return ["ccheck", "ok", t(job.result?.fixed ? "prog.r.repaired" : "prog.r.repair_clean"), t("prog.sub.repair")];
  }
  if (kind === "remove" && status === "succeeded") return ["ccheck", "ok", t("prog.r.removed", { name }), ""];
  const verified = report?.counts?.verified;
  const v = verified !== undefined ? t("prog.verified", { n: verified }) : t("prog.not_verified");
  if (status === "succeeded") return ["ccheck", "ok", t("prog.r.succeeded", { name }), ejectable ? `${v} ${t("prog.sub.safe")}` : v];
  if (status === "partial") {
    const n = (report.failures || []).length || (report.skipped_for_space || []).length;
    // only postponed items (a library outage): "0 files could not be written" would be wrong
    const sub = !n && report.postponed ? t("prog.sub.postponed", { n: report.postponed }) : t("prog.sub.partial", { n });
    return ["warn", "wn", t("prog.r.partial"), `${sub} ${v}`];
  }
  if (status === "cancelled") return ["circle", "nu", t("prog.r.cancelled"), t("prog.sub.cancelled")];
  return ["xcircle", "er", t("prog.r.failed"), t("prog.sub.failed")];
}

function finished(job, report, kind, info) {
  const cfg = info.config;
  const id = encodeURIComponent(cfg.id);
  const status = report?.status || (job.status === "cancelled" ? "cancelled" : job.status === "succeeded" ? "succeeded" : "failed");
  const ejectable = !cfg.mount_path && !info.network && !report?.auto_eject;
  const [ico, tone, head, sub] = outcome(report, kind, cfg, job, ejectable);
  const again = ["partial", "failed", "cancelled"].includes(status) && kind === "sync";
  const actions = html`${ejectable ? html`<button class="btn" data-action="eject">${icon("eject", { size: 16 })}${t("prog.eject")}</button>` : ""}
    ${again ? html`<a class="btn primary" href="#/devices/${id}/plan" data-sync-again>${t("prog.again")}</a>` : ""}
    ${kind === "check" ? html`<a class="btn primary" href="#/devices/${id}" data-back-check>${t("prog.back_check")}</a>` : ""}`;
  // a check's result is its report (counts of what it found), not a sync report
  const tiles = isCheck(kind) ? (job.status === "succeeded" ? job.result : null) : report;
  const fails = report?.failures || [];
  const rev = report?.reverse;
  const error = report?.status === "failed" ? report.message : job.status === "failed" ? job.error : "";
  return html`<section class="card job-report" data-report data-outcome="${status}">
      <div class="job-report-head"><span class="job-outcome ${tone}">${icon(ico, { size: 24 })}</span>
        <div class="job-report-text"><b>${head}</b>${sub ? html`<span data-verified>${sub}</span>` : ""}</div>
        <div class="job-report-actions">${actions}</div></div>
      ${error ? problemBox({ message: error }) : ""}
      ${kind === "repair" && job.result?.output ? html`<details class="job-repair" data-repair-output><summary>${t("prog.repair_output")}</summary><pre>${job.result.output}</pre></details>` : ""}
      ${tiles || (kind === "wipe" && status === "succeeded") ? html`<div class="count-tiles" data-counts>${(tiles ? counts(tiles, kind, status === "cancelled") : [[`−${job.result?.removed ?? 0}`, "prog.c.removed"]]).map(([v, k]) => html`<div class="count-tile"><b>${v}</b><small>${t(k)}</small></div>`)}</div>` : ""}
      ${fails.length ? html`<div class="job-fails"><h3>${t("prog.failed_files")}</h3>${fails.map((f) => html`<div class="job-fail" data-failure>
        <b>${f.item || f.title || f.ref || ""}</b><span>${f.step ? html`<span class="muted">${t("prog.step")}</span> ${f.step} · ` : ""}${f.error || f.message || ""}</span>
        ${f.path || f.ref ? html`<small class="muted mono">${[f.ref, f.path].filter(Boolean).join(" → ")}</small>` : ""}</div>`)}</div>` : ""}
      ${(report?.warnings || []).map((w) => html`<div class="callout" data-report-warning>${w}</div>`)}
      ${status === "succeeded" || status === "partial" ? (["sync", "update", "wipe", "format"].includes(kind) ? hibyReboot(cfg.type) : "") : ""}
      ${report?.suggest_gentle && !cfg.sync?.gentle_write ? suggestionBox({
        iconName: "shield", message: t("prog.gentle"), attrs: html`data-suggest-gentle`,
        action: html`<button class="btn small" data-action="gentleOn">${t("prog.gentle_on")}</button>`,
      }) : ""}
      ${rev ? html`<div class="job-reverse">${icon("reverse", { size: 16 })}<span><b>${t("prog.reverse")}</b> ${reverseText(rev)}</span></div>` : ""}
      ${report?.activity?.length ? html`<details class="job-activity" data-report-activity open><summary>${t("prog.activity")}</summary>${activityList(report.activity)}</details>` : ""}
      ${report?.auto_eject ? html`<p class="muted job-noeject" data-auto-eject>${t("prog.auto_eject", { name: cfg.name })}</p>`
        : isCheck(kind) ? "" : !ejectable ? html`<p class="muted job-noeject">${t(info.network ? "prog.server" : "prog.folder")}</p>` : html`<p class="muted job-noeject">${t("prog.eject_hint")}</p>`}
    </section>`;
}

function reverseText(r) {
  const plays = r.plays_submitted || 0, ratings = r.ratings_pushed || 0, positions = r.positions_pushed || 0;
  return html`${r.failed ? badge("error", t("prog.r.reverse_failed")) : ""}
    ${plays || ratings || positions ? t("prog.reverse_counts", { plays: t("count.play", { n: plays }), ratings: t("count.rating", { n: ratings }), positions: t("count.position", { n: positions }) }) : r.pending ? t("prog.reverse_none_yet") : t("prog.reverse_none")}
    ${r.finished_pushed ? html` <span class="muted" data-finished>${t("prog.reverse_finished", { n: r.finished_pushed })}</span>` : ""}
    ${r.pending ? html` <span class="muted">${t("prog.reverse_pending", { n: r.pending })}</span>` : ""}
    ${feedbackLine(r, libNames) ? html`<span class="job-fb" data-feedback-line>${feedbackLine(r, libNames)}</span>` : ""}
    ${[...(r.errors || []), ...(r.warnings || [])].length ? html`<ul>${[...(r.errors || []), ...(r.warnings || [])].map((e) => html`<li>${e}</li>`)}</ul>` : ""}`;
}

// turn gentle write mode on or off, keeping the other sync settings
async function setGentle(ctx, on) {
  const { cfg } = ctx.state;
  ctx.state.cfg = await patch(`/devices/${encodeURIComponent(cfg.id)}`, { sync: { ...cfg.sync, gentle_write: on } });
}

export const actions = {
  async gentleOn(el, ctx) {
    el.disabled = true;
    const name = ctx.state.cfg.name;
    await setGentle(ctx, true);
    el.closest("[data-suggest-gentle]")?.remove();
    toast(t("prog.gentle_done", { name }), "ok", {
      undo: async () => {
        await setGentle(ctx, false);
        toast(t("prog.gentle_undone", { name }));
        ctx.rerender();
      },
    });
  },
  async cancel(el, ctx) {
    if (!(await confirm({ title: t("prog.cancel_title"), message: t("prog.cancel_text"), ok: t("prog.cancel_ok"), danger: true }))) return;
    el.disabled = true;
    el.textContent = t("prog.cancelling");
    await post(`/jobs/${encodeURIComponent(ctx.params.jobId)}/cancel`);
    toast(t("prog.cancelling"));
  },
  async eject(el, ctx) {
    const { cfg } = ctx.state;
    if (!(await confirm({ title: t("prog.eject_title", { name: cfg.name }), message: t("prog.eject_text"), ok: t("prog.eject") }))) return;
    el.disabled = true;
    toast(t("prog.ejecting", { name: cfg.name }));
    try { await post(`/devices/${encodeURIComponent(cfg.id)}/eject`); } finally { el.disabled = false; }
    toast(ejectedText(cfg, t("prog.ejected", { name: cfg.name }), ctx.state.job?.result?.player_notice));
  },
};

export function onEvent(ev, ctx, root) {
  if (ev.type === "live.reconnected") { ctx.rerender(); return; } // the job may have finished meanwhile
  const jobId = ctx.params.jobId;
  if (ev.data?.job_id !== jobId) return;
  if (ev.type === "job.progress") {
    const box = root.querySelector("[data-live]");
    if (!box) return;
    const pct = Math.round((ev.data.progress || 0) * 100);
    const stats = ev.data.stats || {};
    box.querySelector(".bar i").style.width = `${pct}%`;
    box.querySelector(".bar").setAttribute("aria-valuenow", String(pct));
    box.querySelector("[data-pct]").textContent = `${pct}%`;
    box.querySelector("[data-eta]").textContent = etaText(ev.data.eta_s);
    if (stats.bytes) {
      const speed = writeSpeed(ev.data.job_id, stats.bytes);
      box.querySelector("[data-bytes]").textContent = speed
        ? t("prog.written_speed", { size: bytes(stats.bytes), speed: bytes(speed) }) : t("prog.written", { size: bytes(stats.bytes) });
    }
    box.querySelector("[data-file-n]").textContent = fileLine(stats);
    const prep = box.querySelector("[data-preparing]");
    if (prep && stats.preparing) {
      prep.hidden = !stats.preparing.length;
      prep.querySelector("[data-prep-list]").textContent = stats.preparing.join(" · ");
    }
    const set = (sel, v) => { const el = box.querySelector(sel); if (el) el.textContent = v; };
    set("[data-t-written]", String(stats.files_done ?? 0));
    set("[data-t-failed]", String(stats.failed ?? 0));
    set("[data-t-bytes]", bytes(stats.bytes || 0));
    const at = PHASES.indexOf(stats.phase);
    box.querySelectorAll("[data-phase-step]").forEach((li, i) => {
      const cls = i < at ? "done" : i === at ? "now" : "";
      if (li.className === cls) return;
      li.className = cls;
      if (i === at) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
      li.querySelector(".icon")?.replaceWith(document.createRange().createContextualFragment(
        String(icon(i < at ? "ccheck" : i === at ? "sync" : "circle", { size: 16, spin: i === at }))));
    });
    const cancel = box.querySelector('[data-action="cancel"]');
    if (ev.data.cancelling && cancel) { cancel.disabled = true; cancel.textContent = t("prog.cancelling"); }
    if (ev.data.message) box.querySelector("[data-phase]").textContent = ev.data.message;
    if (ev.data.log_line) {  // the stored line with its time, as the finished view shows it
      const log = root.querySelector("[data-log]");
      log.textContent = [...log.textContent.split("\n").filter(Boolean), ev.data.log_line].slice(-30).join("\n");
      log.scrollTop = log.scrollHeight;
    }
  } else if (ev.type === "job.finished" || ev.type === "job.started") {
    ctx.rerender();
  }
}
