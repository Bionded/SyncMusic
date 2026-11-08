// Device page tab Templates (route `rules`): a device
// has templates only (its picks and removals live in its own template "<Device> - own"). The linked templates are
// grouped by library; their order is the priority when space runs out (rank 1 wins; the rules of a template keep
// their order), ↑ ↓ move one inside its group, × removes it (Undo). Add template opens an inline picker under the list. Mirror / Add
// only, the device limit, and a Save / Discard bar (nothing is stored before Save); the live preview beside it.
import { html, bytes, setHtml } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { helpTip } from "../help.js";
import { get, post, patch } from "../api.js";
import { badge, toast, unsavedBar, capacityBar } from "../ui.js";
import { testResult } from "../rulebuilder.js";
import { templatesFor } from "./common.js";
import { icon } from "../icons.js";
import { sortable, moveKey, grip } from "../reorder.js";

addStrings({
  "rules.others_title": "From other installations",
  "rules.others_help": "Templates another SyncMusic installation keeps on this player. Import makes a copy of your own, linked to this device; the original is never changed.",
  "rules.others_from": "from {name}",
  "rules.others_import": "Import",
  "rules.others_import_all": "Import all",
  "rules.others_imported": "Imported",
  "rules.others_link": "Link my copy",
  "rules.others_updated": "Updated on {name}",
  "rules.others_update": "Update my copy",
  "rules.others_needs": "Needs {libraries}, which this installation does not have",
  "rules.others_done": "{n} template(s) imported and linked",
  "rules.others_update_done": "{name} updated from {from}",
  "rules.templates": "Templates on this device",
  "rules.templates_help": "Each template pulls from its library. Higher in the list wins when space runs out.",
  "rules.add": "Add template",
  "rules.none_title": "No templates yet",
  "rules.none_text": "Nothing will be written to this device. Add a template to start.",
  "rules.default": "Default · linked",
  "rules.default_edited": "Default · edited",
  "rules.builtin": "Built-in",
  "rules.own": "Own",
  "rules.own_help": "Picked for this device or removed from it",
  "rules.all_libraries": "All libraries",
  "rules.rank": "Priority {n}",
  "rules.sum": "{items} · {size}",
  "rules.sum_none": "No matches",
  "rules.sum_above": "{items} · all already brought by a template above",
  "rules.sum_wait": "Counting…",
  "rules.cut": "{size} left out by the device limit",
  "rules.preview_plan": "Open the full plan",
  "rules.preview_title": "Live preview",
  "rules.up": "Move {name} up",
  "rules.down": "Move {name} down",
  "rules.remove": "Remove {name}",
  "rules.removed": "{name} removed from {device}. Save to keep it.",
  "rules.added": "{name} added. Save to keep it.",
  "rules.mode": "When something stops matching",
  "rules.mode.mirror": "Mirror",
  "rules.mode.mirror_d": "Remove it from the device. The device always matches the templates.",
  "rules.mode.additive": "Add only",
  "rules.mode.additive_d": "Keep it. Only new matches are added; nothing is removed.",
  "rules.pick_title": "Add template to {name}",
  "rules.pick_search": "Search templates",
  "rules.pick_add": "Add",
  "rules.pick_default": "Default",
  "rules.pick_added": "Added",
  "rules.pick_blocked": "Not for this player",
  "rules.pick_music_only": "This player gets music only. Audiobooks and podcasts are not synced to it.",
  "rules.pick_nothing": "No template matches.",
  "rules.pick_new": "Create new template",
  "rules.pick_done": "Done",
  "rules.saved": "Templates saved for {name}. The next plan uses them.",
  "rules.limit": "Device limits",
  "rules.limit_help": "On top of each template’s own limits. What does not fit is left out and listed in the plan.",
  "rules.limit_gb": "At most (GB)",
  "rules.limit_tracks": "And at most (items)",
  "rules.limit_none": "no limit",
  "rules.fill": "Fill first with",
  "rules.fill.priority": "Template order",
  "rules.fill.rating": "Highest rated",
  "rules.fill.recently_played": "Recently played",
  "rules.fill.play_count": "Most played",
  "rules.fill.recently_added": "Recently added",
  "rules.fill.random": "Random (stable per device)",
  "rules.limit_invalid": "A limit must be a positive number.",
  "rules.preview_help": "Updates while you edit; nothing is saved until you press Save.",
  "rules.preview_loading": "Updating…",
  "rules.preview_matches": "Matches {items} · {size}",
  "rules.preview_fits": "fits",
  "rules.preview_not_checked": "not checked",
  "rules.preview_no_fit": "does not fit",
  "rules.preview_offline": "device not connected",
  "rules.preview_free_after": "Free after the sync: {free} of {capacity}",
  "rules.preview_reserve": "Includes {reserve} reserved for the device database and the safety margin.",
  "rules.preview_space_unknown": "Free space unknown: the server cannot tell it (not checked)",
  "rules.preview_short": "{needed} more than is free",
  "rules.preview_changes": "{add} to add · {remove} to remove · {keep} unchanged",
  "rules.preview_left_out": "{n} left out by the device limit",
  "rules.preview_offline_text": "Sizes are the library's; the device limit and the free space count once the device is connected.",
  "rules.preview_first": "First {n} matches",
  "rules.preview_first_one": "The only match",
  "rules.preview_incomplete": "Preview paused: {error}",
  "rules.preview_error": "No preview: {error}",
});

const FILL = ["priority", "rating", "recently_played", "play_count", "recently_added", "random"];
const GB = 1e9;

const KIND_ICON = { music: "music", podcast: "podcast", audiobook: "book", video: "video" };

export async function rulesTab(ctx) {
  const id = encodeURIComponent(ctx.state.cfg.id);
  const [all, libraries, types, libTypes, others] = await Promise.all([
    get("/templates"), get("/libraries"), get("/device-types"), get("/library-types"),
    // the device's file is read only while it is reachable; offline or unreadable: no card
    get(`/devices/${id}/device-templates`).then((r) => r.templates, () => []),
  ]);
  ctx.state.others = others;
  // a Rockbox player is offered no podcast/audiobook-only template
  const allowed = new Set(templatesFor(ctx.state.cfg.type, all, types, libraries, libTypes).map((x) => x.id));
  const rs = ctx.state.cfg.rule_set;
  Object.assign(ctx.state, { templates: all, allowed, libraries });
  if (!ctx.state.list) ctx.state.list = savedOrder(rs, all);
  ctx.state.list = grouped(ctx, ctx.state.list);
  // wide screens: the preview is a column beside the cards; narrower it is the last card - it never covers one (369.1)
  return html`<div class="rules-layout" data-rules><div class="rules-main"><div class="grid" data-rules-form>
    <section data-tpls><div class="dt-head"><div><h2>${t("rules.templates")}</h2><div class="sub">${t("rules.templates_help")}</div></div>
      <button class="btn primary" data-action="rulesAdd" aria-expanded="false" aria-controls="tplPick">${icon("plus", { size: 16 })}${t("rules.add")}</button></div>
      <div data-tpl-list>${tplList(ctx)}</div>
      <div class="dt-inpick" id="tplPick" data-pick-panel role="region" aria-label="${t("rules.pick_title", { name: ctx.state.cfg.name })}" hidden>
        <div class="dt-pickhead"><b>${t("rules.pick_title", { name: ctx.state.cfg.name })}</b></div>
        <div class="dt-psearch"><label class="sr" for="tplPickQ">${t("rules.pick_search")}</label>
          <input id="tplPickQ" type="search" placeholder="${t("rules.pick_search")}" autocomplete="off" data-nosave></div>
        <div class="dt-plist" data-pick-list></div>
        <div class="dt-pfoot"><a class="btn" href="#/templates/new" data-pick-new>${icon("plus", { size: 15 })}${t("rules.pick_new")}</a>
          <span style="flex:1"></span><button type="button" class="btn" data-pick-done data-nosave>${t("rules.pick_done")}</button></div></div></section>
    <div class="card" data-mode><div class="card-head"><h2>${t("rules.mode")} ${helpTip("rules")}</h2></div><div class="card-body">
      <div class="optcards" role="radiogroup" aria-label="${t("rules.mode")}">${["mirror", "additive"].map((m) => html`<label class="optcard">
        <input type="radio" name="rulesMode" value="${m}" ${rs.mode === m ? "checked" : ""}>
        <span><b>${t(`rules.mode.${m}`)}</b><small>${t(`rules.mode.${m}_d`)}</small></span></label>`)}</div></div></div>
    ${limitCard(rs)}
  </div>${othersCard(others)}</div>
    <div class="card rules-preview" data-preview><div class="card-head"><div><h2>${t("rules.preview_title")}</h2><div class="sub">${t("rules.preview_help")}</div></div>
      <span data-preview-badge></span></div>
      <div class="card-body" data-preview-body aria-live="polite"><span class="muted">${t("rules.preview_loading")}</span></div></div>
  </div>`;
}

// templates other installations stored on the player: import as linked copies, update a copy
function othersCard(others) {
  if (!others.length) return "";
  const importable = others.filter((o) => o.state === "importable");
  const action = (o) => {
    if (o.state === "importable") return html`<button type="button" class="btn small" data-action="othersImport" data-uid="${o.uid}">${t("rules.others_import")}</button>`;
    if (o.state === "unavailable") return "";
    if (o.updated) return html`<button type="button" class="btn small" data-action="othersUpdate" data-id="${o.copy_id}">${t("rules.others_update")}</button>`;
    if (!o.linked) return html`<button type="button" class="btn small" data-action="othersImport" data-uid="${o.uid}">${t("rules.others_link")}</button>`;
    return html`<span class="muted">${t("rules.others_imported")}</span>`;
  };
  const note = (o) => (o.state === "unavailable" ? t("rules.others_needs", { libraries: o.missing_libraries.join(", ") })
    : o.updated ? t("rules.others_updated", { name: o.instance_name }) : "");
  return html`<section class="card" data-others><div class="card-head"><div><h2>${t("rules.others_title")}</h2><div class="sub">${t("rules.others_help")}</div></div>
    ${importable.length > 1 ? html`<button type="button" class="btn" data-action="othersImport" data-uid="${importable.map((o) => o.uid).join("\n")}">${t("rules.others_import_all")}</button>` : ""}</div>
    <div class="card-body"><ul class="others-list">${others.map((o) => html`<li data-other="${o.uid}" data-state="${o.state}">
      <span class="others-name"><span><b>${o.name}</b> <span class="muted">· ${t("rules.others_from", { name: o.instance_name })}</span></span>
        ${note(o) ? html`<small class="${o.updated ? "others-updated" : ""}">${note(o)}</small>` : ""}</span>${action(o)}</li>`)}</ul></div></section>`;
}

// the saved templates in priority order: by the first place of one of their rules in `priority`, else as linked
function savedOrder(rs, all) {
  const place = (id) => {
    const rules = all.find((x) => x.id === id)?.rules || [];
    const i = Math.min(...rules.map((r) => rs.priority.indexOf(r.id)).filter((n) => n >= 0));
    return Number.isFinite(i) ? i : rs.priority.length + rs.template_ids.indexOf(id);
  };
  return rs.template_ids.filter((id) => all.some((x) => x.id === id)).sort((a, b) => place(a) - place(b));
}

// a template's group: its library (several or all libraries form their own group)
function groupKey(tp) {
  return tp.library_ids.join(",");
}

// the list with each library's templates together, groups in the order of their first template
function grouped(ctx, ids) {
  const tpl = (id) => ctx.state.templates.find((x) => x.id === id);
  const keys = [...new Set(ids.map((id) => groupKey(tpl(id))))];
  return keys.flatMap((k) => ids.filter((id) => groupKey(tpl(id)) === k));
}

function libraryOf(ctx, tp) {
  return tp.library_ids.length === 1 ? ctx.state.libraries.find((l) => l.id === tp.library_ids[0]) : null;
}

// what a template is for: the kinds its rules take (`kind`), else its library's main kind
function templateKind(ctx, tp) {
  const kinds = new Set(tp.rules.flatMap((r) => r.kind || []));
  if (kinds.size === 1) return [...kinds][0];
  const lib = libraryOf(ctx, tp);
  return lib?.type === "audiobookshelf" ? "audiobook" : lib?.settings?.default_kind || "music";
}

function tplBadge(ctx, tp) {
  if (tp.default_of) return badge(tp.edited ? "warn" : "neutral", t(tp.edited ? "rules.default_edited" : "rules.default"), "templates");
  if (tp.builtin) return badge("neutral", t("rules.builtin"));
  return badge("info", t("rules.own"), "edit");
}

function tplList(ctx) {
  const ids = ctx.state.list;
  if (!ids.length) return html`<div class="dt-empty" data-tpl-empty>${icon("templates", { size: 26 })}<b>${t("rules.none_title")}</b>
    <span>${t("rules.none_text")}</span><button class="btn" data-action="rulesAdd">${t("rules.add")}</button></div>`;
  const tpl = (id) => ctx.state.templates.find((x) => x.id === id);
  const keys = [...new Set(ids.map((id) => groupKey(tpl(id))))];
  let rank = 0;
  return keys.map((k) => {
    const members = ids.filter((id) => groupKey(tpl(id)) === k);
    const lib = libraryOf(ctx, tpl(members[0]));
    const name = lib ? lib.name : k ? k.split(",").map((id) => ctx.state.libraries.find((l) => l.id === id)?.name || id).join(", ") : t("rules.all_libraries");
    return html`<section class="dt-group" data-group="${k}"><div class="dt-ghead">${icon(lib?.type || "templates", { size: 15 })}<span>${name}</span>
      <small data-group-size="${k}"></small></div>
      ${members.map((id, i) => {
        const tp = tpl(id), kind = templateKind(ctx, tp);
        rank += 1;
        return html`<div class="dt-row" data-tpl-row="${id}">
          <span class="dt-rank" title="${t("rules.rank", { n: rank })}">${grip(tp.name, html`${icon("grip", { size: 18, width: 2.6 })}<b>${rank}</b>`)}</span>
          <span class="dt-kind" style="color:var(--k-${{ music: "mu", podcast: "po", audiobook: "ab", video: "vi" }[kind] || "ot"})">${icon(KIND_ICON[kind] || "music", { size: 19 })}</span>
          <div class="dt-main"><div class="dt-name"><a href="#/templates/${encodeURIComponent(id)}">${tp.name}</a>${tplBadge(ctx, tp)}</div>
            ${id === `own-${ctx.state.cfg.id}` ? html`<small class="dt-sum">${t("rules.own_help")}</small>` : ""}
            <small class="dt-sum" data-tpl-sum="${id}">${t("rules.sum_wait")}</small>
            <small class="dt-cut" data-tpl-cut="${id}" hidden></small></div>
          <div class="dt-btns">
            <button type="button" class="iconbtn" data-action="rulesMove" data-id="${id}" data-d="-1" aria-label="${t("rules.up", { name: tp.name })}" ${i === 0 ? "disabled" : ""}>${icon("up", { size: 16 })}</button>
            <button type="button" class="iconbtn" data-action="rulesMove" data-id="${id}" data-d="1" aria-label="${t("rules.down", { name: tp.name })}" ${i === members.length - 1 ? "disabled" : ""}>${icon("down", { size: 16 })}</button>
            <button type="button" class="iconbtn dt-x" data-action="rulesRemove" data-id="${id}" aria-label="${t("rules.remove", { name: tp.name })}">${icon("x", { size: 16 })}</button></div>
        </div>`;
      })}</section>`;
  });
}

// the counts of the newest preview on the rows: a template's items are those its rules selected first
function showCounts(ctx, p) {
  const root = document.querySelector("[data-rules] [data-tpl-list]");
  if (!root) return;
  const byRule = Object.fromEntries((p.per_rule || []).map((r) => [r.rule_id, r]));
  const lostBy = Object.fromEntries((p.left_out_per_rule || []).map((r) => [r.rule_id, r]));
  const groups = {};
  for (const id of ctx.state.list) {
    const tp = ctx.state.templates.find((x) => x.id === id);
    const hits = tp.rules.map((r) => byRule[r.id]).filter(Boolean);
    const items = hits.reduce((a, r) => a + r.items, 0), size = hits.reduce((a, r) => a + r.bytes, 0);
    const el = root.querySelector(`[data-tpl-sum="${CSS.escape(id)}"]`);
    // everything it matches is brought by a template above: say so, not "No matches"
    const matched = hits.reduce((a, r) => a + (r.matched || 0), 0);
    if (el) {
      el.textContent = items ? t("rules.sum", { items: t("count.item", { n: items }), size: bytes(size) })
        : matched ? t("rules.sum_above", { items: t("count.item", { n: matched }) }) : t("rules.sum_none");
    }
    const lost = tp.rules.map((r) => lostBy[r.id]).filter(Boolean).reduce((a, r) => a + r.bytes, 0);
    const cut = root.querySelector(`[data-tpl-cut="${CSS.escape(id)}"]`);
    if (cut) { cut.hidden = !lost; cut.textContent = lost ? t("rules.cut", { size: bytes(lost) }) : ""; }
    groups[groupKey(tp)] = (groups[groupKey(tp)] || 0) + size;
  }
  for (const [k, size] of Object.entries(groups)) {
    const el = root.querySelector(`[data-group-size="${CSS.escape(k)}"]`);
    if (el) el.textContent = bytes(size);
  }
}

function redrawList(ctx) {
  setHtml(document.querySelector("[data-rules] [data-tpl-list]"), tplList(ctx));
  if (ctx.state.lastPreview) showCounts(ctx, ctx.state.lastPreview);
  // an edit outside the form's inputs: the Save bar and the preview follow
  document.querySelector("[data-rules-form]")?.dispatchEvent(new Event("change", { bubbles: true }));
}

// a limit for the whole device with a fill order
function limitCard(rs) {
  const fill = rs.fill_order[0] || "priority";
  const gb = rs.limit_bytes ? +(rs.limit_bytes / GB).toFixed(3) : "";
  return html`<div class="card" data-limit><div class="card-head"><div><h2>${t("rules.limit")}</h2><div class="sub">${t("rules.limit_help")}</div></div></div>
    <div class="card-body"><div class="row" style="gap:12px;flex-wrap:wrap;align-items:flex-end">
      <div class="field" style="width:160px"><label for="limitGb">${t("rules.limit_gb")}</label>
        <input id="limitGb" type="number" min="0" step="any" inputmode="decimal" value="${gb}" placeholder="${t("rules.limit_none")}"></div>
      <div class="field" style="width:160px"><label for="limitTracks">${t("rules.limit_tracks")}</label>
        <input id="limitTracks" type="number" min="0" step="1" inputmode="numeric" value="${rs.limit_tracks ?? ""}" placeholder="${t("rules.limit_none")}"></div>
      <div class="field" style="min-width:200px;flex:1;max-width:280px"><label for="fillOrder">${t("rules.fill")}</label>
        <select id="fillOrder">${FILL.map((k) => html`<option value="${k}" ${k === fill ? "selected" : ""}>${t(`rules.fill.${k}`)}</option>`)}</select></div>
    </div></div></div>`;
}

function limitValue(id, scale) {
  const raw = document.getElementById(id).value.trim();
  if (!raw) return null;
  const n = Math.round(Number(raw) * scale);
  if (!Number.isFinite(n) || n < 0) throw new Error(t("rules.limit_invalid"));
  return n || null; // 0 = no limit
}

// the enabled include rules of the templates in their order: the rule priority when space runs out
function priority(ctx) {
  return ctx.state.list.flatMap((id) => ctx.state.templates.find((x) => x.id === id)?.rules.filter((r) => r.enabled !== false).map((r) => r.id) || []);
}

function ruleSet(ctx) {
  return {
    template_ids: [...ctx.state.list],
    mode: document.querySelector('[data-rules] input[name="rulesMode"]:checked')?.value || "mirror",
    priority: priority(ctx),
    limit_bytes: limitValue("limitGb", GB),
    limit_tracks: limitValue("limitTracks", 1),
    fill_order: document.getElementById("fillOrder").value === "priority" ? [] : [document.getElementById("fillOrder").value],
  };
}

// --- live preview ----------------------------------------------------------------------------------------

const PREVIEW_DELAY_MS = 500;
let previewAbort = null;

/** Called when the Rules tab is on screen: any edit (typing, a choice, a builder button) updates the preview after
 * a short pause; only the newest answer is shown. */
export function rulesMounted(ctx, root) {
  const box = root.querySelector("[data-rules]");
  if (!box) return;
  unsavedBar(box.querySelector("[data-rules-form]"), {
    save: () => save(ctx),
    discard: () => { ctx.state.list = null; ctx.rerender(); },
  });
  let timer = null;
  const later = (e) => {
    if (e?.target?.closest?.("[data-preview]")) return; // reading the preview is not an edit
    clearTimeout(timer);
    timer = setTimeout(() => updatePreview(ctx, box), PREVIEW_DELAY_MS);
  };
  for (const type of ["input", "change", "click"]) box.addEventListener(type, later);
  dragToReorder(ctx, box.querySelector("[data-tpl-list]"));
  wirePicker(ctx, box.querySelector("[data-pick-panel]"));
  updatePreview(ctx, box);
}

// drag a row by its grip onto another of its library group (also by touch and Alt+↑ / Alt+↓)
function dragToReorder(ctx, list) {
  sortable(list, {
    row: "[data-tpl-row]",
    key: (r) => r.dataset.tplRow,
    group: (r) => r.closest("[data-group]")?.dataset.group,
    move: (a, b) => { moveKey(ctx.state.list, a, b); redrawList(ctx); },
  });
}

let previewSeq = 0;

async function updatePreview(ctx, box) {
  if (!box.isConnected) return;
  const mine = ++previewSeq;
  const body = box.querySelector("[data-preview-body]"), badgeEl = box.querySelector("[data-preview-badge]");
  const show = (tpl, b = "") => {
    if (mine !== previewSeq || !box.isConnected) return;
    const open = body.querySelector("details[data-preview-first]")?.open; // an open list stays open across updates
    body.innerHTML = String(tpl);
    if (open) body.querySelector("details[data-preview-first]")?.setAttribute("open", "");
    badgeEl.innerHTML = String(b);
  };
  let rs;
  try {
    rs = ruleSet(ctx);
  } catch (e) {
    show(html`<span class="muted">${t("rules.preview_incomplete", { error: e.message || String(e) })}</span>`);
    return;
  }
  body.setAttribute("aria-busy", "true");
  if (mine === previewSeq) badgeEl.innerHTML = String(badge("neutral", t("rules.preview_loading"), "sync"));
  try {
    previewAbort?.abort(); // a newer edit cancels the request in flight
    previewAbort = new AbortController();
    const p = await post(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/rules/preview`, { rule_set: rs }, previewAbort.signal);
    ctx.state.lastPreview = p;
    showCounts(ctx, p);
    show(previewBody(p, ctx.state.cfg.id), !p.connected ? badge("neutral", t("rules.preview_offline"))
      : p.space_unknown ? badge("neutral", t("rules.preview_not_checked"))
      : badge(p.fits ? "ok" : "error", t(p.fits ? "rules.preview_fits" : "rules.preview_no_fit")));
  } catch (e) {
    if (e.name === "AbortError") return;
    show(html`<div class="muted">${t("rules.preview_error", { error: e.message || String(e) })}</div>`);
  } finally {
    if (mine === previewSeq) body.removeAttribute("aria-busy");
  }
}

function previewBody(p, deviceId) {
  const c = p.counts || {};
  // the device after the sync: the selection by kind, the rest of what is on it as Other, free hatched
  const known = p.connected && !p.space_unknown && p.capacity;
  const kinds = Object.values(p.by_kind || {}).reduce((a, n) => a + n, 0);
  const bar = known ? capacityBar({ capacity: p.capacity, parts: { ...p.by_kind, other: Math.max(0, p.capacity - Math.max(0, p.free_after) - kinds) } }) : "";
  return html`<div data-preview-matches><b>${t("rules.preview_matches", { items: t("count.item", { n: p.items }), size: bytes(p.bytes) })}</b></div>
    ${p.connected ? html`<div data-preview-space>${p.space_unknown ? t("rules.preview_space_unknown") : p.fits
      ? t("rules.preview_free_after", { free: bytes(p.free_after), capacity: bytes(p.capacity) })
      : t("rules.preview_short", { needed: bytes(p.needed - p.free) })}</div>
      ${bar ? html`<div class="preview-bar" data-preview-bar>${bar}</div>` : ""}
      ${p.reserve && !p.space_unknown ? html`<div class="muted" style="font-size:12px" data-preview-reserve>${t("rules.preview_reserve", { reserve: bytes(p.reserve) })}</div>` : ""}
      <div class="muted">${t("rules.preview_changes", { add: c.add || 0, remove: c.remove || 0, keep: (c.keep || 0) + (c.update_meta || 0) })}</div>
      ${p.left_out ? html`<div data-preview-left-out>${t("rules.preview_left_out", { n: p.left_out })}</div>` : ""}`
    : html`<div class="muted">${t("rules.preview_offline_text")}</div>`}
    ${p.warnings.length ? html`<div class="muted" style="margin-top:6px">${p.warnings.slice(0, 3).map((w) => html`${icon("warn", { size: 14 })} ${w}<br>`)}</div>` : ""}
    ${p.sample.length ? html`<details data-preview-first style="margin-top:6px"><summary>${t("rules.preview_first", { n: p.sample.length })}</summary>
      <ul class="preview-list">${p.sample.map((s) => html`<li>${s.title}${s.artist ? ` - ${s.artist}` : ""}</li>`)}</ul></details>` : ""}
    ${p.items ? html`<a class="btn preview-plan" href="#/devices/${encodeURIComponent(deviceId)}/plan" data-preview-plan>${t("rules.preview_plan")}</a>` : ""}`;
}

async function save(ctx) {
  const rs = ruleSet(ctx);
  ctx.state.cfg = await patch(`/devices/${encodeURIComponent(ctx.state.cfg.id)}`, { rule_set: rs });
  toast(t("rules.saved", { name: ctx.state.cfg.name }), "ok");
  // the header's "No templates" badge and Sync button follow the saved templates
  ctx.state.libs = (await get(`/devices/${encodeURIComponent(ctx.state.cfg.id)}`)).libraries;
  if (ctx.state.live) ctx.state.live.no_libraries = !ctx.state.libs.length;
  ctx.redrawHead?.();
}

// the inline picker: every template by library; added ones and those the player cannot take are greyed
function showPicker(ctx, panel, open) {
  panel.hidden = !open;
  document.querySelectorAll('[data-rules] [data-action="rulesAdd"][aria-controls]').forEach((b) => b.setAttribute("aria-expanded", String(open)));
  if (!open) return;
  const q = panel.querySelector("#tplPickQ");
  setHtml(panel.querySelector("[data-pick-list]"), pickerBody(ctx, q.value));
  panel.scrollIntoView({ block: "nearest" });
  q.focus();
}

function wirePicker(ctx, panel) {
  const q = panel.querySelector("#tplPickQ"), list = panel.querySelector("[data-pick-list]");
  q.addEventListener("input", () => setHtml(list, pickerBody(ctx, q.value)));
  panel.querySelector("[data-pick-done]").addEventListener("click", () => {
    showPicker(ctx, panel, false);
    document.querySelector('[data-rules] [data-action="rulesAdd"][aria-controls]')?.focus();
  });
  list.addEventListener("click", (e) => {
    const b = e.target.closest("[data-pick]");
    if (!b || b.disabled) return;
    ctx.state.list = grouped(ctx, [...ctx.state.list, b.dataset.pick]);
    redrawList(ctx);
    setHtml(list, pickerBody(ctx, q.value));
    toast(t("rules.added", { name: ctx.state.templates.find((x) => x.id === b.dataset.pick)?.name }), "ok");
    list.querySelector("[data-pick]:not(:disabled)")?.focus();
  });
}

function pickerBody(ctx, q = "") {
  const query = q.trim().toLowerCase();
  const added = new Set(ctx.state.list);
  const libs = [...ctx.state.libraries.map((l) => ({ key: l.id, name: l.name, type: l.type })), { key: "", name: t("rules.all_libraries"), type: "templates" }];
  const own = `own-${ctx.state.cfg.id}`; // another device's picks are not offered
  const match = ctx.state.templates.filter((x) => (!x.id.startsWith("own-") || x.id === own) && (!query || x.name.toLowerCase().includes(query)));
  const groups = libs.map((g) => ({ ...g, items: match.filter((x) => groupKey(x) === g.key) }))
    .concat([{ key: "*", name: "", type: "templates", items: match.filter((x) => x.library_ids.length > 1) }])
    .filter((g) => g.items.length);
  if (!groups.length) return html`<div class="muted dt-pick-none">${t("rules.pick_nothing")}</div>`;
  return groups.map((g) => {
    const blocked = g.items.some((x) => !ctx.state.allowed.has(x.id));
    return html`<div class="dt-pgroup" data-pick-group="${g.key}"><div class="dt-pghead">${icon(g.type, { size: 13 })}${g.name}</div>
      ${blocked ? html`<div class="dt-pnote">${icon("info", { size: 14 })}${t("rules.pick_music_only")}</div>` : ""}
      ${g.items.map((x) => {
        const no = !ctx.state.allowed.has(x.id), on = added.has(x.id), kind = templateKind(ctx, x);
        return html`<button type="button" class="dt-pitem" data-pick="${x.id}" ${no || on ? "disabled" : ""}>
          <span style="color:var(--k-${{ music: "mu", podcast: "po", audiobook: "ab", video: "vi" }[kind] || "ot"})">${icon(KIND_ICON[kind] || "music", { size: 18 })}</span>
          <span class="dt-pname"><b>${x.name}</b><small>${x.default_of ? t(x.edited ? "rules.default_edited" : "rules.pick_default") : t("rules.own")}</small></span>
          <span class="dt-pcta">${no ? t("rules.pick_blocked") : on ? t("rules.pick_added") : t("rules.pick_add")}</span></button>`;
      })}</div>`;
  });
}

export const rulesActions = {
  async othersImport(el, ctx) {
    el.disabled = true;
    const id = encodeURIComponent(ctx.state.cfg.id);
    const { linked } = await post(`/devices/${id}/device-templates/import`, { uids: el.dataset.uid.split("\n") });
    ctx.state.cfg = (await get(`/devices/${id}`)).config;
    // keep unsaved edits of the list: the new ones are added at the end
    ctx.state.list = [...(ctx.state.list || []), ...linked.filter((x) => !(ctx.state.list || []).includes(x))];
    toast(t("rules.others_done", { n: linked.length }));
    ctx.rerender();
  },
  async othersUpdate(el, ctx) {
    el.disabled = true;
    const tpl = await post(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/device-templates/update/${encodeURIComponent(el.dataset.id)}`);
    toast(t("rules.others_update_done", { name: tpl.name, from: tpl.origin_name }));
    ctx.rerender();
  },
  rulesMove(el, ctx) {
    const list = ctx.state.list, id = el.dataset.id, i = list.indexOf(id), j = i + Number(el.dataset.d);
    if (i < 0 || j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    redrawList(ctx);
    const again = document.querySelector(`[data-rules] [data-tpl-row="${CSS.escape(id)}"] [data-d="${el.dataset.d}"]`);
    (again && !again.disabled ? again : document.querySelector(`[data-rules] [data-tpl-row="${CSS.escape(id)}"] [data-action="rulesMove"]:not(:disabled)`))?.focus();
  },
  rulesRemove(el, ctx) {
    const before = [...ctx.state.list], id = el.dataset.id;
    const name = ctx.state.templates.find((x) => x.id === id)?.name || id;
    ctx.state.list = before.filter((x) => x !== id);
    redrawList(ctx);
    toast(t("rules.removed", { name, device: ctx.state.cfg.name }), "info", {
      undo: () => { if (!document.querySelector("[data-rules]")) return; ctx.state.list = before; redrawList(ctx); },
    });
  },
  rulesAdd(el, ctx) {
    // an inline panel under the list, not a dialog: the list, the preview and the Save bar stay usable while picking
    const panel = document.querySelector("[data-rules] [data-pick-panel]");
    if (!panel) return;
    showPicker(ctx, panel, panel.hidden);
  },
};
export { testResult };
