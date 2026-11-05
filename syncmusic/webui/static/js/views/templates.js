// Templates: reusable rule sets linked to devices. List with usage; a dedicated create/edit screen with
// the visual builder (autocomplete from the index), YAML mode and Test (matches + estimated size); built-in
// templates are read-only and can be copied; deleting a template a device uses unlinks it from those devices first
// (a device has templates only).
import { bytes, html } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, post, patch, del } from "../api.js";
import { badge, pageHead, toast, confirm, typedConfirm, emptyState, guardLeave } from "../ui.js";
import { goFrom } from "../router.js";
import { builder, builderActions, capture, current, draft, fromTree, loadCatalogue, pickFromLibrary, redraw, testResult, mountBuilder } from "../rulebuilder.js";
import { icon, typeIcon } from "../icons.js";

addStrings({
  "tpl.sub": "Saved rules that decide what goes on a device. Each library gets defaults; edit, copy or delete them.",
  "tpl.own": "Own",
  "tpl.on_devices": "On {n} devices",
  "tpl.on_devices_one": "On {n} device",
  "tpl.n_templates": "{n} templates",
  "tpl.n_templates_one": "{n} template",
  "tpl.show": "Show",
  "tpl.show_all": "All",
  "tpl.show_defaults": "Defaults",
  "tpl.show_own": "Own",
  "tpl.filter": "Filter templates",
  "tpl.no_match": "No template matches the filter.",
  "tpl.none_shown": "No templates of this kind.",
  "tpl.empty_title": "No templates yet",
  "tpl.empty": "Templates are created for you when you add a library. You can also write your own.",
  "tpl.add_library": "Add a library",
  "tpl.reset": "Reset to default",
  "tpl.reset_title": "Reset {name} to the default?",
  "tpl.reset_text": "Your changes to its rules and name are dropped; the library's default rules come back. Devices that use it get them at their next plan.",
  "tpl.reset_ok": "Reset",
  "tpl.reset_done": "{name} reset to the default rules.",
  "tpl.delete_used_typed": "It is used by {names}. They stop getting its items; on mirror devices the items are removed at the next sync. Type its name to delete it and unlink it.",
  "tpl.new": "New template",
  "tpl.rules": "{n} rules",
  "tpl.rules_one": "{n} rule",
  "tpl.unused": "Not used",
  "tpl.builtin": "Built-in",
  "tpl.default": "Default",
  "tpl.group_other": "Several or all libraries",
  "tpl.group_picks": "Device picks",
  "tpl.picks_badge": "Picks of {device}",
  "tpl.picks_sub": "What you added to or removed from {device} by hand · {rules}",
  "tpl.delete_picks_text": "It holds what you added to or removed from {names} by hand. Delete it and unlink it? Added items leave the device and removed ones come back at its next sync.",
  "tpl.default_edited": "Default · edited",
  "tpl.copy": "Copy",
  "tpl.delete": "Delete…",
  "tpl.name": "Template name",
  "tpl.library": "Libraries",
  "tpl.view_as": "View as",
  "tpl.view_as_main": "Main accounts",
  "tpl.main_account": "main account",
  "tpl.accounts_differ": "Devices using this template sign in as different users ({list}): a playlist or rating one user has may be missing for another.",
  "tpl.library_help": "The libraries this template takes media from: its rules, suggestions and Test use only these. All libraries = every library of the device.",
  "tpl.library_all": "All libraries",
  "tpl.from": "from {name}",
  "tpl.readonly": "A built-in template cannot be changed. Copy it to make your own version.",
  "tpl.save": "Save",
  "tpl.name_placeholder": "Template name",
  "tpl.library_one": "Library",
  "tpl.views": "Edit as",
  "tpl.view_rules": "Rules",
  "tpl.view_pick": "Pick from library",
  "tpl.view_yaml": "YAML",
  "tpl.pick_text": "Tick artists, albums, genres or playlists of the library: they become one include rule of this template.",
  "tpl.pick_open": "Choose from the library…",
  "tpl.picked_rule": "Picked from the library",
  "tpl.picked": "{n} picks added as a rule. Save to keep them.",
  "tpl.picked_one": "{n} pick added as a rule. Save to keep it.",
  "tpl.note_used": "Used by",
  "tpl.note_next_plan": "A change applies at their next plan.",
  "tpl.unsaved": "Unsaved changes",
  "tpl.discard": "Discard",
  "tpl.preview": "Preview",
  "tpl.preview_updating": "Updating…",
  "tpl.preview_loading": "Loading…",
  "tpl.preview_headline": "{items} · {size}",
  "tpl.preview_sub": "In {from} · {inc}, {exc}",
  "tpl.preview_rule": "{items}",
  "tpl.preview_rule_above": "{items}, all already brought by a rule above",
  "tpl.preview_rule_nothing": "matches nothing",
  "tpl.preview_mixed": "A mix of every rule",
  "tpl.preview_download": "Download all matches",
  "tpl.preview_none": "Nothing matches. Loosen a rule or switch “all of these” to “any of these”.",
  "tpl.preview_incomplete": "Finish the rules to see what they select.",
  "tpl.preview_error": "No preview: {error}",
  "tpl.preview_no_library": "No preview: the template's library is not there. Add a library or choose another one.",
  "tpl.saved": "Template saved.",
  "tpl.created": "Template {name} created.",
  "tpl.name_missing": "Give the template a name.",
  "tpl.delete_title": "Delete {name}?",
  "tpl.delete_text": "The template is removed. Devices are not changed.",
  "tpl.delete_used_title": "{name} is in use",
  "tpl.detach_delete": "Unlink and delete",
  "tpl.deleted": "Template {name} deleted.",
  "tpl.after_change": "Devices using it: {names}. Their next plan uses the new rules.",
});

export const nav = "templates";
export const title = (ctx) => ctx.state.tpl?.name || t("nav.templates");

export async function render(ctx) {
  if (ctx.path === "/templates") return renderList(ctx);
  return renderEdit(ctx);
}

/** One row: name + badge, what it draws from and its rules, usage, Reset / Copy / Delete. */
function tplRow(x, ctx, libName) {
  const used = ctx.state.usage[x.id];
  const own = x.id.startsWith("own-");
  const dev = own ? used[0] : null;
  const device = own ? (dev ? dev.name : x.id.slice(4)) : "";
  const rules = t("tpl.rules", { n: x.rules.length + x.excludes.length });
  const summary = own ? t("tpl.picks_sub", { device, rules })
    : `${x.library_ids.length ? t("tpl.from", { name: x.library_ids.map((id) => libName[id] || id).join(", ") }) : t("tpl.library_all")} · ${rules}`;
  // a device's own template holds its picks: a badge to the device, no Copy
  const tag = own ? (dev ? html`<a href="#/devices/${encodeURIComponent(dev.id)}" class="tpl-badge-link">${badge("neutral", t("tpl.picks_badge", { device }))}</a>` : "")
    : x.builtin ? badge("neutral", t("tpl.builtin")) : x.default_of ? badge(x.edited ? "warn" : "neutral", t(x.edited ? "tpl.default_edited" : "tpl.default"), "templates")
    : badge("info", t("tpl.own"), "edit");
  const btn = (name, label, cls = "") => html`class="iconbtn ${cls}" data-id="${x.id}" data-name="${x.name}" aria-label="${label}" title="${label}">${icon(name, { size: 16 })}`;
  return html`<div class="row tpl-row ${x.id === ctx.query.get("saved") ? "highlight" : ""}" data-tpl="${x.id}" data-kind="${own ? "picks" : x.default_of ? "default" : "own"}" data-name="${x.name.toLowerCase()}" ${own ? html`data-own` : ""}>
      <div class="tpl-main"><span class="tpl-name"><a href="#/templates/${encodeURIComponent(x.id)}">${x.name}</a>${tag}</span><small>${summary}</small></div>
      <span class="tpl-used" title="${used.map((d) => d.name).join(", ")}" data-used>${used.length ? t("tpl.on_devices", { n: used.length }) : t("tpl.unused")}</span>
      <div class="tpl-actions">${x.default_of && x.edited ? html`<button type="button" data-action="reset" ${btn("undo", t("tpl.reset"))}</button>` : ""}
        ${own ? "" : html`<button type="button" data-action="copy" ${btn("copy", t("tpl.copy"))}</button>`}
        ${x.builtin ? "" : html`<button type="button" data-action="remove" ${btn("trash", t("tpl.delete").replace("…", ""), "danger")}</button>`}</div>
    </div>`;
}

const TPL_SHOW = ["all", "defaults", "own"];

async function renderList(ctx) {
  const [tpls, libraries] = await Promise.all([get("/templates"), get("/libraries")]);
  const libName = Object.fromEntries(libraries.map((l) => [l.id, l.name]));
  const usage = await Promise.all(tpls.map((x) => get(`/templates/${encodeURIComponent(x.id)}/usage`)));
  ctx.state.usage = Object.fromEntries(tpls.map((x, i) => [x.id, usage[i]]));
  const show = TPL_SHOW.includes(ctx.query.get("show")) ? ctx.query.get("show") : "all";
  const own = (x) => x.id.startsWith("own-");
  const shown = tpls.filter((x) => show === "all" || (show === "defaults" ? !!x.default_of : !x.default_of && !x.builtin));
  // grouped by library: a template of one library under it, the others (several / all) and a device's
  // own picks last
  const groups = libraries.map((l) => ({ id: l.id, name: l.name, type: l.type, items: shown.filter((x) => !own(x) && x.library_ids.length === 1 && x.library_ids[0] === l.id) }));
  const rest = shown.filter((x) => !own(x) && !groups.some((g) => g.items.includes(x)));
  if (rest.length) groups.push({ id: "", name: t("tpl.group_other"), type: "library", items: rest });
  groups.push({ id: "picks", name: t("tpl.group_picks"), type: "device", items: shown.filter(own) });
  const head = pageHead(t("nav.templates"), t("tpl.sub"), html`<a class="btn primary" href="#/templates/new">${icon("plus", { size: 16, width: 2.2 })}${t("tpl.new")}</a>`);
  if (!tpls.length) {
    return html`${head}${emptyState(t("tpl.empty"), { title: t("tpl.empty_title"), action: html`<a class="btn" href="#/libraries/new">${t("tpl.add_library")}</a>` })}`;
  }
  const seg = html`<div class="seg" role="radiogroup" aria-label="${t("tpl.show")}">${TPL_SHOW.map((k) => html`<a role="radio" class="seg-link" aria-checked="${k === show}"
    href="#/templates${k === "all" ? "" : `?show=${k}`}" data-tpl-show="${k}">${t(`tpl.show_${k}`)}</a>`)}</div>`;
  return html`${head}
    <div class="tpl-tools"><label class="tpl-search">${icon("search", { size: 16 })}<input type="search" placeholder="${t("tpl.filter")}" aria-label="${t("tpl.filter")}" data-tpl-filter></label>${seg}</div>
    ${groups.filter((g) => g.items.length).map((g) => html`<section class="tpl-group" data-tpl-group="${g.id}">
      <div class="tpl-group-head">${typeIcon(g.type, g.id === "picks" ? "device" : "library", 16)}<h2>${g.name}</h2><span data-group-count>${t("tpl.n_templates", { n: g.items.length })}</span></div>
      <div class="card tpl-list">${g.items.map((x) => tplRow(x, ctx, libName))}</div></section>`)}
    ${shown.length ? "" : html`<p class="muted">${t("tpl.none_shown")}</p>`}
    <p class="muted" data-tpl-nomatch hidden>${t("tpl.no_match")}</p>`;
}

/** The editor's badge: Built-in, Default (· edited), a device's picks or Own. */
function editBadge(tpl, usage) {
  if (!tpl.id) return "";
  if (tpl.id.startsWith("own-")) return badge("neutral", t("tpl.picks_badge", { device: usage[0]?.name || tpl.id.slice(4) }));
  if (tpl.builtin) return badge("neutral", t("tpl.builtin"));
  if (tpl.default_of) return badge(tpl.edited ? "warn" : "neutral", t(tpl.edited ? "tpl.default_edited" : "tpl.default"), "templates");
  return badge("info", t("tpl.own"), "edit");
}

// the editor's views of one template: the builder, picking from the library, YAML
const VIEWS = ["rules", "pick", "yaml"];

async function renderEdit(ctx) {
  const isNew = ctx.path === "/templates/new";
  // Copy opens an unsaved draft here: nothing exists until Save, which names the new template
  const from = isNew ? ctx.query.get("copy") : null;
  const blank = { id: "", name: "", library_ids: [], rules: [], excludes: [], builtin: false };
  const [tpl, libraries] = await Promise.all([
    isNew ? (from ? ctx.state.draft || post(`/templates/${encodeURIComponent(from)}/copy`, {}).then((d) => ({ ...blank, ...d, id: "" })) : Promise.resolve(blank))
      : get(`/templates/${encodeURIComponent(ctx.params.id)}`),
    get("/libraries"),
  ]);
  if (from) ctx.state.draft = tpl;
  ctx.state.tpl = tpl;
  // the library it draws from; [] = all. One library per template; a template that still
  // draws from several keeps them until another choice is made. A library deleted since stays listed.
  if (ctx.state.libs === undefined) ctx.state.libs = [...tpl.library_ids];
  const libs = ctx.state.libs;
  const known = new Set(libraries.map((l) => l.id));
  ctx.state.libNames = Object.fromEntries(libraries.map((l) => [l.id, l.name]));
  const name = (id) => ctx.state.libNames[id] || id;
  await loadCatalogue(libs.length ? libs : null);
  if (!ctx.state.rb) {
    ctx.state.rb = { rs: { rules: structuredClone(tpl.rules), excludes: structuredClone(tpl.excludes), priority: [] }, readOnly: tpl.builtin };
    if (isNew && !tpl.rules.length) ctx.state.rb.rs.rules.push({ id: `rule-${Date.now().toString(36)}`, name: "", criteria: { all: [] } });
  }
  const st = ctx.state.rb;
  st.presets = await presetsFor(libs);
  const view = st.mode === "text" ? "yaml" : ctx.state.view === "pick" ? "pick" : "rules";
  const usage = isNew ? [] : await get(`/templates/${encodeURIComponent(tpl.id)}/usage`);
  ctx.state.usageNames = usage.map((d) => d.name).join(", ");
  const ro = tpl.builtin;
  const dirty = !!ctx.state.dirty;
  // "View as" a library account: the preview, Test, suggestions and Pick read that user's library
  const pairs = libraries.filter((l) => !libs.length || libs.includes(l.id))
    .flatMap((l) => Object.entries(l.accounts || {}).map(([aid, acc]) => ({ value: `${l.id}:${aid}`, label: `${l.name} - ${acc.name}` })));
  if (ctx.state.viewAs === undefined) ctx.state.viewAs = ctx.query.get("as") || "";
  if (!pairs.some((p) => p.value === ctx.state.viewAs)) ctx.state.viewAs = "";
  const viewAs = pairs.length ? html`<label class="ed-as"><span>${t("tpl.view_as")}</span><select class="lb-sort" data-change="tplViewAs" data-tpl-as>
      <option value="">${t("tpl.view_as_main")}</option>${pairs.map((p) => html`<option value="${p.value}" ${p.value === ctx.state.viewAs ? "selected" : ""}>${p.label}</option>`)}</select></label>` : "";
  const differ = await accountsDiffer(usage, libs, libraries);
  const several = libs.length > 1;
  const libSelect = html`<select id="tplLib" data-change="libPick" ${ro ? "disabled" : ""}>
      <option value="" ${libs.length ? "" : "selected"}>${t("tpl.library_all")}</option>
      ${several ? html`<option value="${libs.join(",")}" selected>${libs.map(name).join(", ")}</option>` : ""}
      ${[...libraries, ...libs.filter((id) => !known.has(id)).map((id) => ({ id, name: id }))].map((l) => html`<option value="${l.id}" ${!several && libs[0] === l.id ? "selected" : ""}>${l.name}</option>`)}</select>`;
  const devices = usage.map((d, i) => html`${i ? ", " : ""}<a href="#/devices/${encodeURIComponent(d.id)}">${d.name}</a>`);
  return html`<div class="ed-head">
      <a class="iconbtn" href="#/templates" aria-label="${t("nav.templates")}" title="${t("nav.templates")}" data-tpl-back>${icon("left", { size: 18 })}</a>
      <h1 class="ed-title"><label class="sr" for="tplName">${t("tpl.name")}</label>
        <input id="tplName" class="ed-name" value="${ctx.state.name ?? tpl.name}" placeholder="${t("tpl.name_placeholder")}" ${ro ? "readonly" : ""} autocomplete="off"></h1>
      <span class="ed-badge" data-tpl-badge>${editBadge(tpl, usage)}</span></div>
    ${usage.length ? html`<p class="ed-note" data-tpl-note>${icon("devices", { size: 15 })}<span>${t("tpl.note_used")} ${devices}. ${t("tpl.note_next_plan")}</span></p>` : ""}
    ${differ ? html`<p class="ed-note" data-tpl-accounts-differ>${icon("info", { size: 15 })}<span>${t("tpl.accounts_differ", { list: differ })}</span></p>` : ""}
    ${ro ? html`<div class="callout" style="margin-bottom:14px">${t("tpl.readonly")}
      <button class="btn small primary" style="margin-left:8px" data-action="copy" data-id="${tpl.id}">${t("tpl.copy")}</button></div>` : ""}
    <div class="ed-layout"><div class="ed-body" data-libraries="${libs.join(",")}" data-view-as="${ctx.state.viewAs}">
      <div class="ed-lib"><label for="tplLib">${t("tpl.library_one")}</label>${libSelect}</div>
      <small class="muted ed-lib-help">${t("tpl.library_help")}</small>
      <div class="seg ed-views" role="tablist" aria-label="${t("tpl.views")}">${VIEWS.map((v) => html`<button type="button" role="tab" aria-selected="${v === view}" data-action="tplView" data-view="${v}">${t(`tpl.view_${v}`)}</button>`)}</div>
      ${view === "pick" ? html`<div class="card ed-pick" data-tpl-pick><div class="card-body">
        <p style="margin-top:0">${t("tpl.pick_text")}</p>
        ${ro ? "" : html`<button type="button" class="btn" data-action="tplPick">${icon("plus", { size: 16 })}${t("tpl.pick_open")}</button>`}</div></div>` : ""}
      <div ${view === "pick" ? "hidden" : ""}>${builder(st)}</div>
      <div class="ed-actions ${dirty ? "dirty" : ""}" data-tpl-actions>
        ${!isNew && !ro ? html`<button type="button" class="btn ghost danger" data-action="remove" data-id="${tpl.id}" data-name="${tpl.name}">${t("tpl.delete").replace("…", "")}</button>` : ""}
        <span class="ed-dirty" data-dirty-text ${dirty ? "" : "hidden"}>${t("tpl.unsaved")}</span>
        <span class="spacer"></span>
        <button type="button" class="btn" data-action="discard" data-dirty ${dirty ? "" : "hidden"}>${t("tpl.discard")}</button>
        <button type="button" class="btn" data-action="test">${t("rb.test")}</button>
        ${ro ? "" : html`<button type="button" class="btn primary" data-action="save">${t("tpl.save")}</button>`}</div>
      <div id="tplResult" aria-live="polite"></div>
    </div>
    <aside class="card ed-preview" data-tpl-preview aria-live="polite"><div class="card-body">
      <div class="ed-preview-head"><h2>${t("tpl.preview")}</h2><span class="muted" data-preview-busy hidden>${t("tpl.preview_updating")}</span></div>
      ${viewAs}
      <div data-preview-body><span class="muted">${t("tpl.preview_loading")}</span></div></div></aside></div>`;
}

const rb = builderActions((ctx) => ctx.state.rb);

// the "View as" choice as the rule test's `accounts` ({library id: account id})
function asAccounts(ctx) {
  const [lid, aid] = (ctx.state.viewAs || "").split(":");
  return lid && aid ? { [lid]: aid } : {};
}

// "My R1 - Anna, Stick - main account" when the devices using the template read its libraries as different users
async function accountsDiffer(usage, libs, libraries) {
  if (usage.length < 2) return "";
  const ids = new Set(usage.map((d) => d.id));
  const devices = (await get("/devices").catch(() => ({ registered: [] }))).registered.filter((d) => ids.has(d.id));
  const lids = libs.length ? libs : libraries.map((l) => l.id);
  const who = (d) => lids.map((lid) => libraries.find((l) => l.id === lid)?.accounts?.[d.library_accounts?.[lid]]?.name || "").join("/");
  if (new Set(devices.map(who)).size < 2) return "";
  return devices.map((d) => `${d.name} - ${who(d).replace(/^\/*$/, "") || t("tpl.main_account")}`).join(", ");
}

export const actions = {
  async tplMatchesCsv(el, ctx) { // every match, with its rules
    const d = await draft(ctx.state.rb);
    if (d.error !== undefined) return;
    const libs = ctx.state.libs;
    const res = await fetch("/api/v1/rules/test/csv", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rule_set: { rules: d.rs.rules, excludes: d.rs.excludes }, library_ids: libs.length ? libs : null, accounts: asAccounts(ctx) }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).message || res.statusText);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await res.blob());
    a.download = `${(document.getElementById("tplName")?.value || "template").trim() || "template"}-matches.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  },
  ...rb,
  tplViewAs(el, ctx) {
    ctx.state.viewAs = el.value;
    document.querySelector("[data-libraries]").dataset.viewAs = el.value;
    updatePreview(ctx);
  },
  async libPick(el, ctx) {
    ctx.state.libs = el.value ? el.value.split(",") : [];
    document.querySelector("[data-libraries]").dataset.libraries = ctx.state.libs.join(",");
    markDirty(ctx);
    // the preset chips follow the library (what it can serve)
    const st = ctx.state.rb;
    if (st.mode !== "text") capture(st);
    st.presets = await presetsFor(ctx.state.libs);
    redraw(st);
  },
  async tplView(el, ctx) {
    const st = ctx.state.rb, v = el.dataset.view;
    // Rules ⇄ YAML go through the builder's own switch (the text is validated before it leaves YAML)
    if ((v === "yaml") !== (st.mode === "text")) await rb.rbMode({ dataset: { m: v === "yaml" ? "text" : "visual" } }, ctx);
    if (v === "pick") capture(st);
    ctx.state.view = v;
    for (const b of document.querySelectorAll('[data-action="tplView"]')) b.setAttribute("aria-selected", String(b.dataset.view === v));
    const pick = document.querySelector("[data-tpl-pick]");
    if ((v === "pick") !== !!pick) rerenderKeepingName(ctx);
  },
  async tplPick(el, ctx) {
    // ticked artists / albums / genres / playlists become one include rule "Picked from the library"
    const conds = await pickFromLibrary(ctx.state.libs.join(","), ctx.state.viewAs || "");
    if (!conds?.length) return;
    ctx.state.rb.rs.rules.push({ id: `pick-${Date.now().toString(36)}`, name: t("tpl.picked_rule"), criteria: fromTree({ match: "any", items: conds }) });
    ctx.state.view = "rules";
    markDirty(ctx);
    rerenderKeepingName(ctx);
    toast(t("tpl.picked", { n: conds.length }), "ok");
  },
  async discard(el, ctx) {
    ctx.state.rb = undefined;
    ctx.state.libs = undefined;
    ctx.state.view = undefined;
    ctx.state.name = undefined;
    ctx.state.dirty = false;
    ctx.rerender();
  },
  async test(el, ctx) {
    const rs = await current(ctx.state.rb);
    el.disabled = true;
    try {
      const res = await post("/rules/test", { rule_set: { rules: rs.rules, excludes: rs.excludes }, library_ids: ctx.state.libs.length ? ctx.state.libs : null, accounts: asAccounts(ctx) });
      document.getElementById("tplResult").innerHTML = String(testResult(res));
    } finally { el.disabled = false; }
  },
  async save(el, ctx) {
    const name = document.getElementById("tplName").value.trim();
    if (!name) { toast(t("tpl.name_missing"), "error"); return; }
    const rs = await current(ctx.state.rb);
    const body = { name, library_ids: ctx.state.libs, rules: rs.rules, excludes: rs.excludes };
    // saved: back to the list with the template highlighted; an error keeps the editor open
    // being saved: leaving meanwhile or the way back to the list asks nothing; a failed save is unsaved again
    const wasDirty = ctx.state.dirty;
    ctx.state.dirty = false;
    let tpl;
    try {
      if (ctx.path === "/templates/new") {
        tpl = await post("/templates", body);
        toast(t("tpl.created", { name: tpl.name }));
      } else {
        tpl = await patch(`/templates/${encodeURIComponent(ctx.state.tpl.id)}`, body);
        toast(ctx.state.usageNames ? `${t("tpl.saved")} ${t("tpl.after_change", { names: ctx.state.usageNames })}` : t("tpl.saved"));
      }
    } catch (e) {
      if (wasDirty) markDirty(ctx);
      throw e;
    }
    goFrom(ctx, `/templates?${new URLSearchParams({ saved: tpl.id })}`);
  },
  copy(el, ctx) {
    goFrom(ctx, `/templates/new?${new URLSearchParams({ copy: el.dataset.id })}`);
  },
  async reset(el, ctx) {
    const id = el.dataset.id, name = el.dataset.name;
    if (!(await confirm({ title: t("tpl.reset_title", { name }), message: t("tpl.reset_text"), ok: t("tpl.reset_ok") }))) return;
    const tpl = await post(`/templates/${encodeURIComponent(id)}/reset`, {});
    toast(t("tpl.reset_done", { name: tpl.name }), "ok");
    goFrom(ctx, `/templates?${new URLSearchParams({ saved: tpl.id })}`);
  },
  async remove(el, ctx) {
    const id = el.dataset.id, name = el.dataset.name;
    const usage = await get(`/templates/${encodeURIComponent(id)}/usage`);
    if (usage.length) {
      // in use: its name typed, then unlinked from those devices and deleted
      const names = usage.map((d) => d.name).join(", ");
      const ok = await typedConfirm({
        title: t("tpl.delete_used_title", { name }),
        message: t(id.startsWith("own-") ? "tpl.delete_picks_text" : "tpl.delete_used_typed", { names }),
        name, ok: t("tpl.detach_delete"),
      });
      if (!ok) return;
      await del(`/templates/${encodeURIComponent(id)}?detach=true`);
    } else {
      if (!(await confirm({ title: t("tpl.delete_title", { name }), message: t("tpl.delete_text"), ok: t("tpl.delete").replace("…", ""), danger: true }))) return;
      await del(`/templates/${encodeURIComponent(id)}`);
    }
    toast(t("tpl.deleted", { name }));
    ctx.state.dirty = false;
    goFrom(ctx, "/templates");
  },
};

// the Preview column: what the edited rules select, a while after an edit
const PREVIEW_DELAY_MS = 600;
let previewSeq = 0;
let previewAbort = null; // the request in flight: a newer edit cancels it

async function updatePreview(ctx) {
  const box = document.querySelector("[data-tpl-preview]");
  if (!box || !ctx.state.rb) return; // gone, or discarded and being drawn again
  const mine = ++previewSeq;
  const body = box.querySelector("[data-preview-body]"), busy = box.querySelector("[data-preview-busy]");
  const show = (tpl) => { if (mine === previewSeq && box.isConnected) body.innerHTML = String(tpl); };
  const d = await draft(ctx.state.rb);
  if (d.error !== undefined) { show(html`<p class="muted" data-preview-incomplete>${d.error || t("tpl.preview_incomplete")}</p>`); return; }
  const libs = ctx.state.libs;
  // nothing to test against (no library yet, or one deleted since): no request that can only be refused
  const names = ctx.state.libNames;
  if (!Object.keys(names).length || libs.some((id) => !(id in names))) { show(html`<p class="muted" data-preview-nolib>${t("tpl.preview_no_library")}</p>`); return; }
  busy.hidden = false;
  try {
    previewAbort?.abort();
    previewAbort = new AbortController();
    const res = await post("/rules/test", { rule_set: { rules: d.rs.rules, excludes: d.rs.excludes }, library_ids: libs.length ? libs : null, accounts: asAccounts(ctx) }, previewAbort.signal);
    const from = libs.length ? libs.map((id) => ctx.state.libNames[id] || id).join(", ") : t("tpl.library_all");
    const on = (list) => d.rs[list].filter((r) => r.enabled !== false).length;
    show(html`<div class="ed-preview-sum"><b data-preview-headline>${t("tpl.preview_headline", { items: t("count.item", { n: res.items }), size: bytes(res.bytes) })}</b>
        <span class="muted">${t("tpl.preview_sub", { from, inc: t("count.include", { n: on("rules") }), exc: t("count.exclude", { n: on("excludes") }) })}</span></div>
      ${res.warnings.length ? html`<div class="muted ed-preview-warn">${res.warnings.slice(0, 3).map((w) => html`<div>${icon("warn", { size: 14 })} ${w}</div>`)}</div>` : ""}
      ${res.items ? html`${res.per_rule.length > 1 || res.per_rule.some((r) => r.matches_nothing) ? res.per_rule.map(ruleBlock) : ""}
        ${res.per_rule.length > 1 ? html`<div class="ed-preview-rule"><b>${t("tpl.preview_mixed")}</b></div>` : ""}
        <ul class="ed-preview-list" data-preview-sample>${res.sample.slice(0, 20).map(itemLi)}</ul>
        <button type="button" class="btn small ed-preview-dl" data-action="tplMatchesCsv">${icon("download", { size: 15 })}${t("tpl.preview_download")}</button>`
        : html`${res.per_rule.length > 1 ? res.per_rule.map(ruleBlock) : ""}<p class="muted" data-preview-empty>${t("tpl.preview_none")}</p>`}`);
  } catch (e) {
    if (e.name === "AbortError") return;
    show(html`<p class="muted">${t("tpl.preview_error", { error: e.message || String(e) })}</p>`);
  } finally {
    if (mine === previewSeq) busy.hidden = true;
  }
}

const itemLi = (x) => html`<li><b>${x.title}</b>${x.artist || x.album ? html`<span class="muted">${[x.artist, x.album].filter(Boolean).join(" - ")}</span>` : ""}</li>`;

// one rule of the preview: its count and a few examples, or "matches nothing"
function ruleBlock(r) {
  const head = r.matches_nothing ? html`<span data-rule-nothing>${badge("warn", t("tpl.preview_rule_nothing"))}</span>`
    : html`<span class="muted">${t(r.items ? "tpl.preview_rule" : "tpl.preview_rule_above", { items: t("count.item", { n: r.items || r.matched }) })}</span>`;
  return html`<div class="ed-preview-rule" data-preview-rule="${r.rule_id}"><b>${r.name}</b> ${head}</div>
    ${r.examples?.length ? html`<ul class="ed-preview-list short">${r.examples.slice(0, 3).map(itemLi)}</ul>` : ""}`;
}

/** The rule presets with `available` for these libraries ([] = all). */
function presetsFor(libs) {
  return get(`/rule-presets${libs.length ? `?${new URLSearchParams({ libraries: libs.join(",") })}` : ""}`).catch(() => []);
}

/** Draw the editor again (the Pick view, a pick added) without losing a typed name. */
function rerenderKeepingName(ctx) {
  ctx.state.name = document.getElementById("tplName")?.value;
  ctx.rerender();
}

/** Unsaved changes: the action bar says so and offers Discard. */
function markDirty(ctx) {
  // leaving the editor asks first, like every screen with a save bar
  // (the editor is drawn again after a pick: look for its bar when asked, not now)
  if (!ctx.state.dirty) guardLeave(() => !!ctx.state.dirty && !!document.querySelector("[data-tpl-actions]"), () => { ctx.state.dirty = false; });
  ctx.state.dirty = true;
  for (const el of document.querySelectorAll("[data-dirty], [data-dirty-text]")) el.hidden = false;
  document.querySelector("[data-tpl-actions]")?.classList.add("dirty");
}

export function mounted(ctx, root) {
  root.querySelector(".row.highlight")?.scrollIntoView({ block: "center" });
  // the editor: any edit (typing, a select, a builder button) marks it unsaved
  const body = root.querySelector(".ed-body");
  if (body && !ctx.state.tpl?.builtin) {
    const dirty = () => markDirty(ctx);
    for (const el of [body, root.querySelector("#tplName")]) {
      el?.addEventListener("input", dirty);
      el?.addEventListener("change", dirty);
    }
    body.addEventListener("click", (e) => {
      const a = e.target.closest("[data-action]")?.dataset.action || "";
      if (a.startsWith("rb") && a !== "rbMode") dirty();
    });
  }
  if (body && ctx.state.rb && !ctx.state.tpl?.builtin) mountBuilder(ctx.state.rb);
  if (body) {
    let timer = null;
    const later = () => { clearTimeout(timer); timer = setTimeout(() => updatePreview(ctx), PREVIEW_DELAY_MS); };
    for (const type of ["input", "change", "click"]) body.addEventListener(type, later);
    updatePreview(ctx);
  }
  // the list's filter: hides rows (and emptied groups) by name as you type, no redraw
  const filter = root.querySelector("[data-tpl-filter]");
  filter?.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    let any = false;
    for (const g of root.querySelectorAll("[data-tpl-group]")) {
      let n = 0;
      for (const row of g.querySelectorAll("[data-tpl]")) {
        row.hidden = !!q && !row.dataset.name.includes(q);
        if (!row.hidden) n += 1;
      }
      g.hidden = n === 0;
      any ||= n > 0;
    }
    root.querySelector("[data-tpl-nomatch]").hidden = any || !q;
  });
}

export function onEvent() {} // an editor never redraws under the user
