// A job's full view: every job of Active jobs / Recent jobs opens one - device syncs and reverse syncs the
// sync status (progress.js), every other job (a library refresh, ...) this view: type, device / library, state, times,
// duration, progress with the time left, current step, error, result, the live log, and Cancel while it runs.
import { html, when } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, post } from "../api.js";
import { badge, toast, confirm } from "../ui.js";
import { etaText } from "./common.js";
import { icon } from "../icons.js";

addStrings({
  "job.title": "Job",
  "job.kind.sync": "Device sync",
  "job.kind.update": "Update sync",
  "job.kind.refresh": "Library refresh",
  "job.kind.other": "Job ({kind})",
  "job.type": "Type",
  "job.device": "Device",
  "job.library": "Library",
  "job.removed": "(removed)",
  "job.state": "State",
  "job.created": "Queued at",
  "job.started": "Started",
  "job.finished": "Finished",
  "job.duration": "Duration",
  "job.progress": "Progress",
  "job.step": "Current step",
  "job.error": "Error",
  "job.result": "Result",
  "job.log": "Log",
  "job.no_log": "Nothing logged.",
  "job.cancel": "Cancel",
  "job.cancel_title": "Stop {name}?",
  "job.cancel_text": "It stops at the next safe point.",
  "job.cancel_ok": "Stop",
  "job.cancel_keep": "Keep running",
  "job.cancelling": "Cancelling…",
  "job.back": "Dashboard",
  "job.status.queued": "Waiting",
  "job.status.running": "Running",
  "job.status.succeeded": "Done",
  "job.status.failed": "Failed",
  "job.status.partial": "Partial",
  "job.status.cancelled": "Cancelled",
});

const STATE = { queued: "neutral", running: "busy", succeeded: "ok", failed: "error", cancelled: "neutral" };
// an update and a wipe open the sync view too
const DEVICE_JOBS = ["sync", "update", "wipe", "check", "check-fix"];

/** Where a job opens: a device's sync or reverse sync its sync status, anything else the job view. */
export function jobHref(j) {
  return j.device_id && DEVICE_JOBS.includes(j.kind)
    ? `#/devices/${encodeURIComponent(j.device_id)}/sync/${encodeURIComponent(j.id)}`
    : `#/jobs/${encodeURIComponent(j.id)}`;
}

export const kindText = (kind) => (["sync", "update", "refresh"].includes(kind) ? t(`job.kind.${kind}`) : t("job.kind.other", { kind }));

export const nav = "dashboard";
export const title = (ctx) => ctx.state.job?.name || t("job.title");

function duration(j) {
  if (!j.started_at) return "-";
  const s = Math.max(0, Math.round(((j.finished_at ? new Date(j.finished_at) : new Date()) - new Date(j.started_at)) / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

function result(r) {
  if (r === null || r === undefined) return "-";
  if (typeof r !== "object") return String(r);
  // long unbroken values (32-character ids) wrap instead of widening the page at 390 px
  return html`<ul style="margin:0;padding-left:18px;overflow-wrap:anywhere">${Object.entries(r).filter(([, v]) => v !== null && v !== "" && typeof v !== "object").map(([k, v]) => html`<li>${k}: <b>${v}</b></li>`)}</ul>`;
}

export async function render(ctx) {
  const j = await get(`/jobs/${encodeURIComponent(ctx.params.id)}`);
  ctx.state.job = j;
  // names from the lists: a device or library removed since is simply missing there, no 404 in the console
  const [device, library] = await Promise.all([
    j.device_id ? get("/devices").then((d) => d.registered.find((x) => x.id === j.device_id) || null).catch(() => null) : null,
    j.library_id ? get("/libraries").then((l) => l.find((x) => x.id === j.library_id) || null).catch(() => null) : null,
  ]);
  const running = j.status === "running" || j.status === "queued";
  const pct = Math.round((j.progress || 0) * 100);
  const row = (label, value, attr = "") => html`<tr ${attr}><th scope="row" style="text-align:left;padding-right:14px">${label}</th><td>${value}</td></tr>`;
  // the v2 job header: icon, name, state and "Kind · started …"
  const started = j.started_at || j.created_at;
  return html`<div class="job-head"><span class="job-ico">${icon(j.library_id ? "libraries" : j.device_id ? "devices" : "sync", { size: 26 })}</span>
      <div class="job-meta"><h1>${j.name}</h1><div class="job-line">${badge(STATE[j.status] || "neutral", t(`job.status.${j.status}`))}<span>${kindText(j.kind)}${started
        ? ` · ${new Date(started).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}</span></div></div>
      <a class="btn" href="#/">${icon("left", { size: 16 })}${t("job.back")}</a></div>
    <div class="grid two"><div class="card"><div class="card-body" data-job-view="${j.id}">
      <table class="table"><tbody>
        ${row(t("job.type"), kindText(j.kind))}
        ${j.device_id ? row(t("job.device"), device ? html`<a href="#/devices/${encodeURIComponent(j.device_id)}">${device.name}</a>` : html`${j.device_id} <span class="muted">${t("job.removed")}</span>`) : ""}
        ${j.library_id ? row(t("job.library"), library ? html`<a href="#/libraries/${encodeURIComponent(j.library_id)}/browse">${library.name}</a>` : html`${j.library_id} <span class="muted">${t("job.removed")}</span>`) : ""}
        ${row(t("job.state"), badge(STATE[j.status] || "neutral", t(`job.status.${j.status}`)), html`data-job-state`)}
        ${row(t("job.created"), when(j.created_at))}
        ${row(t("job.started"), when(j.started_at))}
        ${j.finished_at ? row(t("job.finished"), when(j.finished_at)) : ""}
        ${row(t("job.duration"), duration(j), html`data-job-duration`)}
        ${row(t("job.progress"), html`<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${t("job.progress")}"><i style="width:${pct}%"></i></div>
          <span class="muted" data-job-pct>${pct} %</span>${j.status === "running" ? html` · <span class="muted" data-job-eta>${etaText(j.eta_s)}</span>` : ""}`)}
        ${row(t("job.step"), html`<span data-job-step>${j.message || "-"}</span>`)}
        ${j.error ? row(t("job.error"), html`<span style="overflow-wrap:anywhere">${j.error}</span>`) : ""}
        ${j.finished_at ? row(t("job.result"), result(j.result)) : ""}
      </tbody></table>
      ${running ? html`<div style="margin-top:12px"><button class="btn danger" data-action="jobCancel" ${j.cancelling ? "disabled" : ""}>${t(j.cancelling ? "job.cancelling" : "job.cancel")}</button></div>` : ""}
    </div></div>
    <div class="card"><div class="card-head"><h2>${t("job.log")}</h2></div><div class="card-body">
      <pre class="codebox" style="min-height:120px;max-height:360px;overflow:auto;white-space:pre-wrap" data-job-log>${(j.log || []).slice(-200).join("\n") || t("job.no_log")}</pre></div></div></div>`;
}

/** The confirmation before a job is cancelled: its two buttons must not both read "Cancel". */
export function confirmCancel(name) {
  return confirm({ title: t("job.cancel_title", { name }), message: t("job.cancel_text"), ok: t("job.cancel_ok"), cancel: t("job.cancel_keep"), danger: true });
}

export const actions = {
  async jobCancel(el, ctx) {
    const j = ctx.state.job;
    if (!(await confirmCancel(j.name))) return;
    el.disabled = true;
    el.textContent = t("job.cancelling");
    await post(`/jobs/${encodeURIComponent(j.id)}/cancel`);
    toast(t("job.cancelling"));
  },
};

export function onEvent(ev, ctx, root) {
  if (ev.type === "live.reconnected") { ctx.rerender(); return; }
  if (ev.data?.job_id !== ctx.params.id) return;
  if (ev.type === "job.progress") {
    const pct = Math.round((ev.data.progress || 0) * 100);
    const bar = root.querySelector("[data-job-view] .bar");
    if (!bar) return;
    bar.querySelector("i").style.width = `${pct}%`;
    bar.setAttribute("aria-valuenow", String(pct));
    root.querySelector("[data-job-pct]").textContent = `${pct} %`;
    const eta = root.querySelector("[data-job-eta]");
    if (eta) eta.textContent = etaText(ev.data.eta_s);
    const cancel = root.querySelector('[data-action="jobCancel"]');
    if (ev.data.cancelling && cancel) { cancel.disabled = true; cancel.textContent = t("job.cancelling"); }
    if (ev.data.message) root.querySelector("[data-job-step]").textContent = ev.data.message;
    if (ev.data.log_line) {
      const log = root.querySelector("[data-job-log]");
      const lines = log.textContent === t("job.no_log") ? [] : log.textContent.split("\n").filter(Boolean);
      log.textContent = [...lines, ev.data.log_line].slice(-200).join("\n");
      log.scrollTop = log.scrollHeight;
    }
  } else if (ev.type === "job.started" || ev.type === "job.finished") {
    ctx.rerender();
  }
}
