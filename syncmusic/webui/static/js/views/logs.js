// Newest first, filters as selects that apply at once (level presets, device, job) plus text,
// an error row with a traceback opens below with Copy and Open job; following live pauses while scrolled down.
import { html, setHtml } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get } from "../api.js";
import { icon } from "../icons.js";
import { toast } from "../ui.js";
import { go } from "../router.js";

addStrings({
  "logs.title": "Logs",
  "logs.sub": "Following new lines · newest first · the last {mb} MB are kept",
  "logs.sub_paused": "Paused while you read · newest first · the last {mb} MB are kept",
  "logs.level": "Level",
  "logs.level.DEBUG": "Everything, with debug",
  "logs.level.INFO": "All levels",
  "logs.level.WARNING": "Warnings and errors",
  "logs.level.ERROR": "Errors only",
  "logs.device": "Device",
  "logs.all_devices": "All devices",
  "logs.job": "Job",
  "logs.all_jobs": "All jobs",
  "logs.step": "Step",
  "logs.server": "server",
  "logs.all_steps": "All steps",
  "logs.step.plan": "plan",
  "logs.step.reverse": "listening data",
  "logs.step.copy": "copy",
  "logs.step.database": "database",
  "logs.step.verify": "verify",
  "logs.search": "Text, track, error…",
  "logs.live": "Follow live",
  "logs.download": "Download",
  "logs.count": "{shown} of {total} matching entries",
  "logs.none": "No log entries match.",
  "logs.copy": "Copy",
  "logs.copied": "Copied.",
  "logs.open_job": "Open job",
  "logs.no_device": "-",
});

const LEVELS = ["INFO", "WARNING", "ERROR", "DEBUG"];
const RANK = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
const SHORT = { DEBUG: "DEBUG", INFO: "INFO", WARNING: "WARN", ERROR: "ERROR", CRITICAL: "CRIT" };
const MAX_ROWS = 1000;
const STEPS = ["plan", "reverse", "copy", "database", "verify"]; // a sync job's phases
const LOG_MB = 30; // 5 MB files, 5 backups (infra/log.py)

export const nav = "settings";
export const title = () => t("logs.title");

function filters(ctx) {
  const q = ctx.query;
  return { level: q.get("level") || "INFO", device: q.get("device") || "", job: q.get("job") || "", step: q.get("step") || "", query: q.get("query") || "" };
}

function params(f) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) if (v) p.set(k, v);
  return p;
}

// a job in the filter: its name and when it started, so two updates of one device differ; two that would
// still read the same (started in the same minute) get the seconds, then the short job id
const jobLabel = (j, precise = 0) => {
  const at = j.started_at || j.created_at;
  const when = { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", ...(precise ? { second: "2-digit" } : {}) };
  const text = at ? `${j.name || j.id} · ${new Date(at).toLocaleString([], when)}` : j.name || j.id;
  return precise > 1 ? `${text} · #${String(j.id).slice(0, 6)}` : text;
};

function jobLabels(list) {
  const labels = new Map(list.map((j) => [j.id, jobLabel(j)]));
  for (const precise of [1, 2]) {
    const seen = new Map();
    for (const l of labels.values()) seen.set(l, (seen.get(l) || 0) + 1);
    for (const j of list) if (seen.get(labels.get(j.id)) > 1) labels.set(j.id, jobLabel(j, precise));
  }
  return labels;
}

/** One line: time, level, "device · source", the message; one with a traceback opens below (Copy, Open job). */
function row(e, names) {
  const time = (e.ts || "").replace(",", ".").split(" ").pop().slice(0, 8);
  // "device · step" (the sync step the job was in); otherwise the logger's last part
  const dev = e.device ? names[e.device] || e.device : t("logs.no_device");
  // uvicorn's own lines come from its logger "uvicorn.error" at INFO: call them the server's
  const src = e.step ? (STEPS.includes(e.step) ? t(`logs.step.${e.step}`) : e.step)
    : (e.logger || "").startsWith("uvicorn") ? t("logs.server") : (e.logger || "").split(".").pop();
  const where = `${dev} · ${src}`;
  const lvl = html`<span class="log-lvl" data-lvl="${e.level}">${SHORT[e.level] || e.level}</span>`;
  const top = html`<span class="mono log-ts" title="${e.ts || ""}">${time}</span>${lvl}<span class="mono log-where" title="${where}"><span class="lw-dev">${dev}</span><span class="lw-src"> · ${src}</span></span><span class="mono log-text">${e.msg}</span>`;
  if (!e.exc) return html`<div class="log-row" data-level="${e.level}"><div class="log-line">${top}</div></div>`;
  return html`<details class="log-row" data-level="${e.level}"><summary class="log-line">${top}</summary>
    <div class="log-exc"><div class="muted mono log-meta">${[e.device && `device: ${names[e.device] || e.device}`, e.job && `job: ${e.job}`, e.logger && `logger: ${e.logger}`].filter(Boolean).join("   ")}</div>
      <pre class="codebox">${e.exc}</pre>
      <div class="toolbar"><button type="button" class="btn small" data-action="copyExc">${t("logs.copy")}</button>
        ${e.job ? html`<a class="btn small" href="#/jobs/${encodeURIComponent(e.job)}">${t("logs.open_job")}</a>` : ""}</div></div></details>`;
}

export async function render(ctx) {
  const f = filters(ctx);
  ctx.state.filters = f;
  const p = params(f);
  p.set("limit", "500");
  const [res, devices, jobs] = await Promise.all([get(`/logs/entries?${p}`), get("/devices"), get("/jobs").catch(() => [])]);
  ctx.state.names = Object.fromEntries(devices.registered.map((d) => [d.id, d.name]));
  ctx.state.shown = res.entries.length;
  const recent = jobs.slice(-50).reverse();
  // the jobs the shown lines name too (older than the job list), and the one filtered on
  for (const id of [f.job, ...res.entries.map((e) => e.job)]) if (id && !recent.some((j) => j.id === id)) recent.push({ id, name: id });
  const labels = jobLabels(recent);
  const sel = (id, label, opts) => html`<label class="sr" for="${id}">${label}</label><select id="${id}" data-change="filter">${opts}</select>`;
  return html`<div class="pagehead logs-head"><div><h1>${t("logs.title")}</h1><div class="sub" data-logs-sub>${t("logs.sub", { mb: LOG_MB })}</div></div>
      <div class="actions"><label class="btn log-live"><input type="checkbox" id="logLive" checked> ${t("logs.live")}</label>
        <a class="btn" id="logDownload" href="/api/v1/logs/download?${params(f)}" download>${icon("download", { size: 16 })}${t("logs.download")}</a></div></div>
    <form class="log-filters" data-action="filter" role="search">
      <label class="searchfield">${icon("search", { size: 16 })}<span class="sr">${t("logs.search")}</span><input id="logQuery" type="search" value="${f.query}" placeholder="${t("logs.search")}"></label>
      ${sel("logLevel", t("logs.level"), LEVELS.map((l) => html`<option value="${l}" ${l === f.level ? "selected" : ""}>${t(`logs.level.${l}`)}</option>`))}
      ${sel("logDevice", t("logs.device"), html`<option value="">${t("logs.all_devices")}</option>${devices.registered.map((d) => html`<option value="${d.id}" ${d.id === f.device ? "selected" : ""}>${d.name}</option>`)}`)}
      ${sel("logJob", t("logs.job"), html`<option value="">${t("logs.all_jobs")}</option>${recent.map((j) => html`<option value="${j.id}" ${j.id === f.job ? "selected" : ""}>${labels.get(j.id)}</option>`)}`)}
      ${sel("logStep", t("logs.step"), html`<option value="">${t("logs.all_steps")}</option>${STEPS.map((x) => html`<option value="${x}" ${x === f.step ? "selected" : ""}>${t(`logs.step.${x}`)}</option>`)}`)}
    </form>
    <div class="log-count muted" id="logCount">${t("logs.count", { shown: res.entries.length, total: res.total })}</div>
    <div class="card log-list" id="logList">${res.entries.length ? [...res.entries].reverse().map((e) => row(e, ctx.state.names)) : html`<div class="empty" data-empty>${t("logs.none")}</div>`}</div>`;
}

export function mounted(ctx, root) {
  // following pauses while the page is scrolled down to read, and resumes at the top
  const sub = root.querySelector("[data-logs-sub]");
  const onScroll = () => {
    if (!sub.isConnected) { window.removeEventListener("scroll", onScroll); return; }
    ctx.state.paused = window.scrollY > 120;
    sub.textContent = t(ctx.state.paused ? "logs.sub_paused" : "logs.sub", { mb: LOG_MB });
  };
  ctx.state.paused = false;
  window.addEventListener("scroll", onScroll, { passive: true });
}

export const actions = {
  filter(el) {
    const form = el.closest("form") || el;
    const f = {
      level: form.querySelector("#logLevel").value,
      device: form.querySelector("#logDevice").value,
      job: form.querySelector("#logJob").value,
      step: form.querySelector("#logStep").value,
      query: form.querySelector("#logQuery").value.trim(),
    };
    go(`/logs?${params(f)}`);
  },
  async copyExc(el) {
    const text = el.closest(".log-exc").querySelector("pre").textContent;
    try { await navigator.clipboard.writeText(text); toast(t("logs.copied"), "ok"); } catch { toast(text.slice(0, 200)); }
  },
};

function wanted(e, f) {
  if ((RANK[e.level] || 0) < (RANK[f.level] || 0)) return false;
  if (f.device && e.device !== f.device) return false;
  if (f.job && e.job !== f.job) return false;
  if (f.step && e.step !== f.step) return false;
  const text = `${e.msg || ""} ${e.logger || ""} ${e.exc || ""}`.toLowerCase();
  return f.query.toLowerCase().split(/\s+/).filter(Boolean).every((w) => text.includes(w));
}

export function onEvent(ev, ctx, root) {
  if (ev.type !== "log.entry" || !root.querySelector("#logLive")?.checked) return;
  if (!wanted(ev.data, ctx.state.filters)) return;
  const list = root.querySelector("#logList");
  if (!list) return;
  list.querySelector("[data-empty]")?.remove();
  const holder = document.createElement("div");
  setHtml(holder, row(ev.data, ctx.state.names || {}));
  const before = list.getBoundingClientRect().height;
  list.prepend(holder.firstElementChild); // newest first
  while (list.children.length > MAX_ROWS) list.lastElementChild.remove();
  // paused (scrolled down): the lines being read stay where they are
  if (ctx.state.paused) window.scrollBy(0, list.getBoundingClientRect().height - before);
  ctx.state.shown += 1;
  root.querySelector("#logCount").textContent = t("logs.count", { shown: list.children.length, total: ctx.state.shown });
}
