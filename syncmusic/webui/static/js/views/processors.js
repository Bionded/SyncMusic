// Device page tab Processors: the ordered chain with enable switches, up/down buttons (the same with
// touch, mouse and screen readers), each step's settings, add, remove, re-suggest (the type's suggested chain).
// Processor templates: the tab applies a template once or links it (the device then follows the
// template); the same chain editor edits the templates themselves (views/proctemplates.js, `chainEditor`). Quality
// profiles above it: Maximum quality / Save space / For a weak player replace the chain, Custom = by hand.
import { html, setHtml } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { helpTip } from "../help.js";
import { get, patch, post } from "../api.js";
import { toast } from "../ui.js";
import { FormError } from "../forms.js";
import { processorForm, newRow } from "../proceditors.js";
import { icon } from "../icons.js";
import { sortable, moveKey, grip } from "../reorder.js";
import { suggestText, variantList } from "./common.js";

addStrings({
  "proc.q_title": "Quality",
  "proc.q_label": "Quality profile",
  "proc.q.max": "Maximum quality",
  "proc.q.max_text": "Files the device plays stay as they are; the rest is converted to the best it plays.",
  "proc.q.space": "Save space",
  "proc.q.space_text": "Lossless files become MP3 V2 (about 190 kbit/s); lossy files the device plays stay.",
  "proc.q.weak": "For a weak player",
  "proc.q.weak_text": "Everything becomes MP3 192 kbit/s at 44.1 kHz, covers at most 300 px.",
  "proc.q.custom": "Custom",
  "proc.q.custom_text": "The steps below as you set them. Editing a step switches to Custom.",
  "proc.q_applied": "{name}: the chain below is replaced.",
  "proc.lint_title": "This chain may not do what you want",
  "proc.pv_title": "Preview on one track",
  "proc.pv_track": "Track",
  "proc.pv_search": "Find any track (Enter)",
  "proc.pv_nothing": "No track matches",
  "proc.pv_run": "Run the chain",
  "proc.pv_running": "Converting with ffmpeg…",
  "proc.pv_none": "No tracks to try: add a template that pulls from a library first.",
  "proc.pv_help": "Runs the steps as they are on screen, saved or not. Nothing is written to the device.",
  "proc.pv_before": "Before",
  "proc.pv_after": "After",
  "proc.pv_download": "Download result",
  "proc.pv_failed": "The chain failed: {error}",
  "proc.help": "Each file goes through the enabled steps from top to bottom before it is written to the device.",
  "proc.enabled": "Enabled",
  "proc.up": "Move {name} up",
  "proc.down": "Move {name} down",
  "proc.remove": "Remove {name}",
  "proc.settings": "Settings",
  "proc.no_settings": "No settings.",
  "proc.none": "No processors: files are copied as they are.",
  "proc.add": "Add",
  "proc.add_type": "Processor to add",
  "proc.resuggest": "Use the suggested chain",
  "proc.resuggested": "Suggested chain restored - save to keep it.",
  "proc.unknown": "Unknown processor {type}",
  "proc.tpl_title": "Processor template",
  "proc.tpl_select": "Template",
  "proc.tpl_link": "Keep linked",
  "proc.tpl_apply": "Apply",
  "proc.tpl_help": "Apply copies the template's steps into this device. Keep linked: later changes of the template change this device too.",
  "proc.tpl_linked": "Linked to {name}: changes of the template change this device. Saving changes below detaches it.",
  "proc.tpl_detach": "Detach",
  "proc.tpl_detached": "Detached from {name}: the steps stay as they are.",
  "proc.tpl_applied": "{name} applied.",
  "proc.tpl_linked_now": "{name} applied and linked.",
  "proc.tpl_saved_detached": "Saved - no longer linked to {name}.",
  "proc.tpl_none": "No template fits this device.",
  "proc.sg_apply": "Apply {name}",
  "proc.sg_apply_draft": "Apply the new chain",
  "proc.var_title": "Generated chains",
  "proc.var_help": "Made for what this device's templates select, with the estimated size on it. Use one to replace the steps below.",
  "proc.var_use": "Use this chain",
  "proc.steps": "Steps",
  "proc.steps_help": "run top to bottom for every file",
  "proc.suggested": "Suggested: {why}",
  "proc.plays": "Files not covered by a rule are copied as they are. Device plays: {formats}.",
  "proc.sum.transcode_none": "No rules: files are copied as they are",
  "proc.sum.any": "Any format",
  "proc.sum.covers_embed": "Embedded",
  "proc.sum.covers_sidecar": "Next to the files",
  "proc.sum.covers_device": "In the iPod's database",
  "proc.sum.covers_remove": "Removed",
  "proc.sum.retag": "Tags from the library",
  "proc.sum.retag_n": "Tags from the library, {n} fields set by you",
  "proc.sum.retag_n_one": "Tags from the library, {n} field set by you",
  "proc.sum.lyrics": "Synced: {synced} · plain: {plain}",
  "proc.why.formats": "this device can't play {formats}.",
  "proc.why.lossy": "lossless files cost this player battery; MP3 plays longer.",
  "proc.why.covers": "players show the cover from the file; big ones slow down the menus.",
  "proc.why.covers_db": "the iPod shows covers from its own database, not from the files.",
  "proc.why.retag": "the device shows the library's tags, not the files' own.",
  "proc.why.lyrics": "the device shows lyrics on its Now Playing screen.",
});

/** The chain editor: `ctx.state.procRows` (+ procTypes) on screen; `opts.suggested` offers "Use the suggested
 * chain", `opts.readOnly` shows it without changes. Save calls `ctx.state.procSave(rows)` (the device tab or a
 * template editor); `opts.noSave` leaves the Save button to the page (a `data-action="procSave"` of its own). */
export async function chainEditor(ctx, opts = {}) {
  if (!ctx.state.procTypes) {
    const types = await get("/processor-types");
    ctx.state.procTypes = Object.fromEntries(types.map((p) => [p.type, p]));
  }
  ctx.state.procOpts = opts;
  return html`<div class="card"><div class="card-body" data-procs>${body(ctx)}</div></div>`;
}

export async function processorsTab(ctx) {
  const { cfg } = ctx.state;
  if (!ctx.state.procRows) ctx.state.procRows = structuredClone(cfg.processors);
  const tpls = (await get("/processor-templates")).filter((x) => !x.device_type || x.device_type === cfg.type);
  ctx.state.procTemplates = tpls;
  ctx.state.procSave = saveDevice;
  ctx.state.procLint = cfg.id; // warnings need the device's capabilities
  const linked = tpls.find((x) => x.id === cfg.processor_template_id);
  // the best template for the device's libraries now: shown with Apply, never switched by itself
  const sg = (ctx.state.procSuggestion = await post("/devices/suggest-processor-template", { device_id: cfg.id }).catch(() => null));
  const sgTpl = sg?.template_id ? tpls.find((x) => x.id === sg.template_id) : null;
  const sgShown = sg?.reason.files && (sg.draft || (sgTpl && sgTpl.id !== cfg.processor_template_id));
  const editor = await chainEditor(ctx, { suggested: ctx.state.type?.suggested_processors || [] });
  // quality profiles: a whole chain in one choice; the chain edited by hand is Custom
  const current = cfg.quality_profile || "custom";
  const quality = html`<section class="card ds-card" style="margin-bottom:14px" data-quality><h2>${t("proc.q_title")}</h2>
    <div class="ds-options proc-q" role="radiogroup" aria-label="${t("proc.q_label")}">${["max", "space", "weak", "custom"].map((q) =>
      html`<label class="option"><input type="radio" name="procQuality" value="${q}" data-change="procQuality" ${q === current ? "checked" : ""} ${q === "custom" && current !== "custom" ? "disabled" : ""}>
        <span><b>${t(`proc.q.${q}`)}</b><small>${t(`proc.q.${q}_text`)}</small></span></label>`)}</div></section>`;
  return html`<div class="proc-layout"><div class="proc-main">${quality}<div class="card" style="margin-bottom:14px" data-proc-template><div class="card-head"><h2>${t("proc.tpl_title")} ${helpTip("processors")}</h2></div><div class="card-body">
      ${sgShown ? html`<div class="callout" style="margin-bottom:10px" data-proc-suggest="${sgTpl ? sgTpl.id : "draft"}">${suggestText(sg.reason)}
        <button class="btn small" style="margin-left:8px" data-action="procApplySuggested">${sgTpl ? t("proc.sg_apply", { name: sgTpl.name }) : t("proc.sg_apply_draft")}</button></div>` : ""}
      ${linked ? html`<div class="callout" style="margin-bottom:10px" data-linked="${linked.id}">${t("proc.tpl_linked", { name: linked.name })}
        <button class="btn small" style="margin-left:8px" data-action="procDetach">${t("proc.tpl_detach")}</button></div>` : ""}
      ${tpls.length ? html`<div class="toolbar">
        <label for="procTpl">${t("proc.tpl_select")}</label>
        <select id="procTpl" class="field" style="padding:9px;border-radius:9px;background:var(--panel2);border:1px solid var(--line)">
          ${tpls.map((x) => html`<option value="${x.id}" ${x.id === cfg.processor_template_id ? "selected" : ""}>${x.name}</option>`)}</select>
        <label class="checkrow" style="margin:0"><input type="checkbox" id="procLink" checked> ${t("proc.tpl_link")}</label>
        <button class="btn" data-action="procApply">${t("proc.tpl_apply")}</button></div>
        <small class="muted">${t("proc.tpl_help")}</small>` : html`<div class="muted">${t("proc.tpl_none")}</div>`}
    </div></div>${variants(sg)}${editor}</div>${await previewAside(ctx)}</div>`;
}

/** The generated chains to choose from, the best one ticked; Use replaces the device's steps. */
function variants(sg) {
  if (!sg?.reason.files || !sg.variants?.length) return "";
  return html`<section class="card" style="margin-bottom:14px" data-proc-variants><div class="card-head"><h2>${t("proc.var_title")}</h2></div>
    <div class="card-body"><small class="muted">${t("proc.var_help")}</small>${variantList(sg, "procVariant")}
    <div class="toolbar" style="margin-top:10px"><button class="btn" data-action="procUseVariant">${t("proc.var_use")}</button></div></div></section>`;
}

// "Preview on one track" (design ProcessorChain): a track picker, Run the chain on what is on screen,
// the before / after table with changed values bold, Download result
async function previewAside(ctx) {
  const tracks = await get(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/chain-preview/tracks`).catch(() => []);
  return html`<aside class="card proc-pv" aria-label="${t("proc.pv_title")}" data-chain-preview>
    <div class="proc-pv-head"><h3>${t("proc.pv_title")}</h3>
      ${tracks.length ? html`<input id="chainSearch" type="search" class="field" data-pv-search data-change="chainSearch" placeholder="${t("proc.pv_search")}" aria-label="${t("proc.pv_search")}" autocomplete="off">
        <select id="chainTrack" class="field" data-change="chainPick" aria-label="${t("proc.pv_track")}">${trackOptions(tracks)}</select>
        <button type="button" class="btn" data-action="chainRun">${icon("play", { size: 14 })}${t("proc.pv_run")}</button>
        <small class="muted">${t("proc.pv_help")}</small>` : html`<p class="muted">${t("proc.pv_none")}</p>`}</div>
    <div data-chain-result></div></aside>`;
}

const trackOptions = (tracks) => tracks.map((x) => html`<option value="${x.library_id}\n${x.item_id}">${x.label}</option>`);

function previewResult(r) {
  return html`<div role="table" class="proc-pv-table" aria-label="${t("proc.pv_title")}">
      <div role="row" class="proc-pv-row head"><span role="columnheader"></span><span role="columnheader">${t("proc.pv_before")}</span><span role="columnheader">${t("proc.pv_after")}</span></div>
      ${r.rows.map((x) => html`<div role="row" class="proc-pv-row" data-pv-row="${x.key}" ${x.changed ? "data-changed" : ""}>
        <span role="rowheader" class="muted">${x.key}</span><span role="cell">${x.before}</span><span role="cell">${x.changed ? html`<b>${x.after}</b>` : x.after}</span></div>`)}</div>
    <a class="btn primary proc-pv-dl" href="${r.download}" download="${r.file}" data-chain-download>${icon("download", { size: 16 })}${t("proc.pv_download")}</a>`;
}

async function saveDevice(ctx, rows) {
  const before = ctx.state.cfg.processor_template_id;
  ctx.state.cfg = await patch(`/devices/${encodeURIComponent(ctx.state.cfg.id)}`, { processors: rows });
  const name = ctx.state.procTemplates?.find((x) => x.id === before)?.name;
  toast(before && !ctx.state.cfg.processor_template_id ? t("proc.tpl_saved_detached", { name }) : t("common.saved"));
  ctx.state.procRows = structuredClone(ctx.state.cfg.processors);
  ctx.rerender();
}

// the card's content from ctx.state; redrawn at once after every change (a later redraw would let a quick
// Save read the rows in their old order)
function body(ctx) {
  const types = Object.values(ctx.state.procTypes);
  const rows = ctx.state.procRows;
  ctx.state.procForms = rows.map((slot, i) => {
    const pt = ctx.state.procTypes[slot.type];
    return pt ? processorForm(slot.type, pt.settings_schema, slot.settings || {}, i) : null;
  });
  const opts = ctx.state.procOpts || {};
  return html`<div data-chain-warnings aria-live="polite">${warnings(ctx.state.procWarnings || [])}</div>
    <h3 class="proc-steps-h">${t("proc.steps")} <span class="muted">${t("proc.steps_help")}</span></h3>
    <fieldset style="border:0;padding:0;margin:0" ${opts.readOnly ? "disabled" : ""}>
    ${rows.length ? rows.map((slot, i) => row(ctx, slot, i, rows.length)) : html`<div class="empty">${t("proc.none")}</div>`}
    ${opts.readOnly ? "" : html`<div class="toolbar" style="margin-top:14px">
      <label class="sr" for="procAdd">${t("proc.add_type")}</label>
      <select id="procAdd" class="field" style="padding:9px;border-radius:9px;background:var(--panel2);border:1px solid var(--line)">
        ${types.map((p) => html`<option value="${p.type}">${p.display_name}</option>`)}</select>
      <button class="btn small" data-action="procAdd">${icon("plus", { size: 16 })}${t("proc.add")}</button>
      ${opts.suggested?.length ? html`<button class="btn small" data-action="procSuggest">${t("proc.resuggest")}</button>` : ""}
      ${opts.noSave ? "" : html`<span style="flex:1"></span>
      <button class="btn primary" data-action="procSave">${opts.saveLabel || t("common.save")}</button>`}
    </div>`}</fieldset>`;
}

/** Keep a processor's settings open after a redraw (its own buttons changed it). */
function openSettings(i) {
  const d = document.querySelector(`[data-procs] [data-proc="${i}"] details`);
  if (d) d.open = true;
}

function redraw(ctx) {
  const root = document.querySelector("[data-procs]");
  if (!root) return;
  dragToReorder(ctx, root);
  // settings that were open stay open (an editor's own buttons redraw the whole chain)
  const open = [...root.querySelectorAll("[data-proc] details[open]")].map((d) => d.closest("[data-proc]").dataset.proc);
  setHtml(root, body(ctx));
  for (const i of open) openSettings(i);
  refreshLint(ctx);
}

// the design: a numbered step card - icon, name, a summary of its settings, Enabled, ↑ ↓, delete, the
// "Suggested: …" reason when the device type's suggested chain has this step, its editor expanding below
const STEP_ICON = { transcode: "bolt", covers: "image", retag: "tag", lyrics: "lyrics" };
const fmtName = (f) => String(f).toUpperCase().replace("OPUS", "Opus").replace("ALAC", "ALAC");

/** One line saying what a step does with its settings. */
function summary(slot) {
  const s = slot.settings || {};
  if (slot.type === "transcode") {
    const rules = s.rules || [];
    if (!rules.length) return t("proc.sum.transcode_none");
    return rules.map((r) => `${(r.from || []).length ? r.from.map(fmtName).join(", ") : t("proc.sum.any")} → ${fmtName(r.to)}`).join(" · ");
  }
  if (slot.type === "covers") {
    const mode = t(`proc.sum.covers_${s.mode || "embed"}`);
    return [mode, s.max_size ? `${s.max_size} px` : "", s.format ? s.format.toUpperCase() : ""].filter(Boolean).join(", ");
  }
  if (slot.type === "retag") {
    const n = Object.keys(s.templates || {}).length;
    return n ? t("proc.sum.retag_n", { n }) : t("proc.sum.retag");
  }
  if (slot.type === "lyrics") return t("proc.sum.lyrics", { synced: s.synced || "lrc", plain: s.plain || "lrc" });
  return "";
}

/** Why the device type suggests this step (from its capabilities), or "". */
function reason(ctx, slot) {
  const type = ctx.state.type;
  if (!type || !(type.suggested_processors || []).some((x) => x.type === slot.type)) return "";
  const caps = type.capabilities || {};
  const formats = caps.formats || [];
  if (slot.type === "transcode") {
    if (caps.prefer_lossy) return t("proc.why.lossy");
    const cant = ["flac", "opus", "ogg", "wma"].filter((f) => !formats.includes(f)).map(fmtName);
    return cant.length ? t("proc.why.formats", { formats: cant.join(", ") }) : "";
  }
  if (slot.type === "covers") return t(caps.needs_device_db ? "proc.why.covers_db" : "proc.why.covers");
  if (slot.type === "retag") return t("proc.why.retag");
  if (slot.type === "lyrics") return t("proc.why.lyrics");
  return "";
}

function row(ctx, slot, i, n) {
  const pt = ctx.state.procTypes[slot.type];
  const name = pt ? pt.display_name : t("proc.unknown", { type: slot.type });
  const form = ctx.state.procForms[i];
  const why = reason(ctx, slot);
  const plays = slot.type === "transcode" && ctx.state.type?.capabilities?.formats?.length
    ? html`<p class="muted proc-plays">${t("proc.plays", { formats: ctx.state.type.capabilities.formats.map(fmtName).join(", ") })}</p>` : "";
  return html`<div class="proc ${slot.enabled ? "" : "off"}" data-proc="${i}"><div class="proc-top">
      ${n > 1 ? grip(name) : ""}<span class="num">${i + 1}</span>
      <span class="proc-ico">${icon(STEP_ICON[slot.type] || "settings", { size: 16 })}</span>
      <span class="proc-what"><span class="name">${name}</span><small class="proc-sum" data-proc-sum>${summary(slot)}</small></span>
      <label class="proc-on" title="${t("proc.enabled")}"><input type="checkbox" data-proc-enabled ${slot.enabled ? "checked" : ""} aria-label="${t("proc.enabled")}: ${name}"><span class="sr">${t("proc.enabled")}</span></label>
      <button class="iconbtn" data-action="procMove" data-i="${i}" data-d="-1" aria-label="${t("proc.up", { name })}" title="${t("proc.up", { name })}" ${i === 0 ? "disabled" : ""}>${icon("up", { size: 16 })}</button>
      <button class="iconbtn" data-action="procMove" data-i="${i}" data-d="1" aria-label="${t("proc.down", { name })}" title="${t("proc.down", { name })}" ${i === n - 1 ? "disabled" : ""}>${icon("down", { size: 16 })}</button>
      <button class="iconbtn" data-action="procRemove" data-i="${i}" aria-label="${t("proc.remove", { name })}" title="${t("proc.remove", { name })}">${icon("trash", { size: 16 })}</button>
    </div>
    ${why ? html`<div class="proc-why" data-proc-why>${icon("info", { size: 14 })}<span>${t("proc.suggested", { why })}</span></div>` : ""}
    <details class="proc-edit"><summary>${t("proc.settings")}</summary><div class="proc-form">${form ? form.html : html`<span class="muted">${t("proc.no_settings")}</span>`}${plays}</div></details>
  </div>`;
}

/** The rows as they are on screen; throws FormError for an invalid settings field. */
function screenRows(ctx, root) {
  return ctx.state.procRows.map((slot, i) => {
    const el = root.querySelector(`[data-proc="${i}"]`);
    const form = ctx.state.procForms[i];
    return {
      type: slot.type,
      enabled: el.querySelector("[data-proc-enabled]").checked,
      settings: form ? { ...(slot.settings || {}), ...form.read(el) } : slot.settings || {},
    };
  });
}

// the warnings box: what makes no sense in the chain, each with a one-click fix (design ProcessorChain)
function warnings(list) {
  if (!list.length) return "";
  return html`<div class="callout warn proc-lint" role="status" data-chain-lint><b>${t("proc.lint_title")}</b>
    <ul>${list.map((w, i) => html`<li data-lint="${w.id}"><span>${w.message}.</span>
      <button type="button" class="btn small" data-action="procFix" data-i="${i}">${w.fix}</button></li>`)}</ul></div>`;
}

// drag a step by its grip (mouse, touch, Alt+↑ / Alt+↓); ↑ ↓ stay as buttons
function dragToReorder(ctx, root) {
  sortable(root, {
    row: "[data-proc]",
    key: (r) => r.dataset.proc,
    move: (a, b) => {
      if (!capture(ctx)) return;
      const rows = ctx.state.procRows;
      const order = moveKey(rows.map((_, i) => String(i)), a, b);
      ctx.state.procRows = order.map((i) => rows[Number(i)]);
      redraw(ctx);
      root.dispatchEvent(new Event("change", { bubbles: true }));
      return String(order.indexOf(a));
    },
  });
}

let lintSeq = 0;

/** Ask the service what makes no sense in the chain on screen (when the tab is shown, after every redraw and
 * change). */
export async function refreshLint(ctx) {
  const root = document.querySelector("[data-procs]");
  dragToReorder(ctx, root);
  if (!root || !ctx.state.procLint) return;
  if (!root.dataset.lintBound) {
    root.dataset.lintBound = "1";
    root.addEventListener("change", () => refreshLint(ctx));
  }
  let rows;
  try { rows = screenRows(ctx, root); } catch { return; } // a half-typed field: keep the last warnings
  const mine = ++lintSeq;
  const list = await post(`/devices/${encodeURIComponent(ctx.state.procLint)}/chain-lint`, { processors: rows }).catch(() => null);
  if (mine !== lintSeq || list === null) return;
  ctx.state.procWarnings = list;
  const box = root.querySelector("[data-chain-warnings]");
  if (box) setHtml(box, warnings(list));
}

/** Read what is on screen into ctx.state.procRows (before any change); false when a settings field is invalid. */
function capture(ctx) {
  const root = document.querySelector("[data-procs]");
  if (!root) return true;
  try {
    ctx.state.procRows = screenRows(ctx, root);
    return true;
  } catch (e) {
    if (e instanceof FormError) { toast(e.message, "error"); return false; }
    throw e;
  }
}

export const processorActions = {
  // the search field refills the track list: any item of the device's libraries, not only the samples;
  // it runs on Enter or when the field is left (a change event)
  async chainSearch(el, ctx) {
    const q = el.value.trim();
    const found = await get(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/chain-preview/tracks?${new URLSearchParams(q ? { q } : {})}`);
    const select = el.closest("[data-chain-preview]").querySelector("#chainTrack");
    if (!select || el.value.trim() !== q) return; // typed on meanwhile
    setHtml(select, found.length ? trackOptions(found) : html`<option value="" disabled selected>${t("proc.pv_nothing")}</option>`);
    processorActions.chainPick(select);
  },
  // another track chosen: the result shown was for the one before
  chainPick(el) {
    setHtml(el.closest("[data-chain-preview]").querySelector("[data-chain-result]"), html``);
  },
  procFix(el, ctx) {
    const w = (ctx.state.procWarnings || [])[Number(el.dataset.i)];
    if (!w) return;
    ctx.state.procRows = structuredClone(w.processors);
    ctx.state.procWarnings = [];
    toast(w.fix, "ok");
    redraw(ctx);
  },
  async chainRun(el, ctx) {
    if (!capture(ctx)) return;
    const box = el.closest("[data-chain-preview]").querySelector("[data-chain-result]");
    const value = document.getElementById("chainTrack").value;
    if (!value) { toast(t("proc.pv_nothing"), "error"); return; }
    const [libraryId, itemId] = value.split("\n");
    el.disabled = true;
    setHtml(box, html`<div class="proc-pv-run" role="status">${icon("sync", { size: 16, spin: true })}${t("proc.pv_running")}</div>`);
    try {
      const r = await post(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/chain-preview`,
        { library_id: libraryId, item_id: itemId, processors: structuredClone(ctx.state.procRows) });
      setHtml(box, previewResult(r));
    } catch (e) {
      setHtml(box, html`<div class="callout err" data-chain-error>${t("proc.pv_failed", { error: e.message })}</div>`);
    } finally { el.disabled = false; }
  },
  async procQuality(el, ctx) {
    if (el.value === "custom") return;
    ctx.state.cfg = await patch(`/devices/${encodeURIComponent(ctx.state.cfg.id)}`, { quality_profile: el.value });
    ctx.state.procRows = null; // the editor shows the new chain
    toast(t("proc.q_applied", { name: t(`proc.q.${el.value}`) }));
    ctx.rerender();
  },
  procMove(el, ctx) {
    if (!capture(ctx)) return;
    const i = Number(el.dataset.i), j = i + Number(el.dataset.d);
    const rows = ctx.state.procRows;
    if (j < 0 || j >= rows.length) return;
    [rows[i], rows[j]] = [rows[j], rows[i]];
    redraw(ctx);
    document.querySelector(`[data-proc="${j}"] [data-action="procMove"][data-d="${el.dataset.d}"]`)?.focus();
  },
  procRemove(el, ctx) {
    if (!capture(ctx)) return;
    ctx.state.procRows.splice(Number(el.dataset.i), 1);
    redraw(ctx);
  },
  procAdd(el, ctx) {
    if (!capture(ctx)) return;
    const type = document.getElementById("procAdd").value;
    ctx.state.procRows.push({ type, enabled: true, settings: {} });
    redraw(ctx);
  },
  // a row of a dedicated editor's list (transcode rules, retag templates)
  procRowAdd(el, ctx) {
    if (!capture(ctx)) return;
    const i = Number(el.dataset.i), list = el.dataset.list;
    const slot = ctx.state.procRows[i];
    const settings = (slot.settings = { ...(slot.settings || {}) });
    const row = newRow(slot.type, list);
    if (Array.isArray(row)) { // a template: the first tag without one
      const used = settings[list] || {};
      const tag = ["title", "artist", "album", "album_artist", "genre", "comment"].find((x) => !(x in used));
      if (!tag) return;
      settings[list] = { ...used, [tag]: "" };
    } else if (row) settings[list] = [...(settings[list] || []), row];
    redraw(ctx);
    openSettings(i);
  },
  procRowRemove(el, ctx) {
    const i = Number(el.dataset.i), list = el.dataset.list, j = Number(el.dataset.j);
    // the row goes even when another row is half-filled: read what is valid, drop this one by position
    const root = document.querySelector(`[data-procs] [data-proc="${i}"]`);
    root?.querySelector(list === "rules" ? `[data-tc-rule="${j}"]` : `[data-rt-row="${j}"]`)?.remove();
    if (!capture(ctx)) return;
    redraw(ctx);
    openSettings(i);
  },
  procRedraw(el, ctx) {
    const i = Number(el.closest("[data-proc]")?.dataset.proc);
    if (!capture(ctx)) return;
    redraw(ctx);
    openSettings(i);
    document.getElementById(el.id)?.focus();
  },
  procSuggest(el, ctx) {
    ctx.state.procRows = structuredClone(ctx.state.procOpts?.suggested || []);
    toast(t("proc.resuggested"));
    redraw(ctx);
  },
  async procSave(el, ctx) {
    if (!capture(ctx)) return;
    el.disabled = true;
    try {
      await ctx.state.procSave(ctx, structuredClone(ctx.state.procRows));
    } finally { el.disabled = false; }
  },
  async procApply(el, ctx) {
    const tpl = ctx.state.procTemplates.find((x) => x.id === document.getElementById("procTpl").value);
    const link = document.getElementById("procLink").checked;
    const body = link ? { processor_template_id: tpl.id } : { processors: tpl.processors, processor_template_id: null };
    ctx.state.cfg = await patch(`/devices/${encodeURIComponent(ctx.state.cfg.id)}`, body);
    ctx.state.procRows = structuredClone(ctx.state.cfg.processors);
    toast(t(link ? "proc.tpl_linked_now" : "proc.tpl_applied", { name: tpl.name }));
    ctx.rerender();
  },
  async procApplySuggested(el, ctx) {
    const sg = ctx.state.procSuggestion;
    const tpl = sg.template_id && ctx.state.procTemplates.find((x) => x.id === sg.template_id);
    // a template is linked like Apply with Keep linked; a generated chain becomes the device's own steps
    const body = tpl ? { processor_template_id: tpl.id } : { processors: sg.draft.processors, processor_template_id: null };
    ctx.state.cfg = await patch(`/devices/${encodeURIComponent(ctx.state.cfg.id)}`, body);
    ctx.state.procRows = structuredClone(ctx.state.cfg.processors);
    toast(tpl ? t("proc.tpl_linked_now", { name: tpl.name }) : t("common.saved"));
    ctx.rerender();
  },
  async procUseVariant(el, ctx) {
    const i = Number(document.querySelector('input[name="procVariant"]:checked')?.value);
    const v = ctx.state.procSuggestion?.variants?.[i];
    if (!v) return;
    ctx.state.cfg = await patch(`/devices/${encodeURIComponent(ctx.state.cfg.id)}`, { processors: v.processors, processor_template_id: null });
    ctx.state.procRows = structuredClone(ctx.state.cfg.processors);
    toast(t("common.saved"));
    ctx.rerender();
  },
  async procDetach(el, ctx) {
    const name = ctx.state.procTemplates.find((x) => x.id === ctx.state.cfg.processor_template_id)?.name;
    ctx.state.cfg = await patch(`/devices/${encodeURIComponent(ctx.state.cfg.id)}`, { processor_template_id: null });
    toast(t("proc.tpl_detached", { name }));
    ctx.rerender();
  },
};
