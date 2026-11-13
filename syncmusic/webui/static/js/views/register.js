// Registration wizard: a detected disk (by USB serial / file system id - it can be formatted) or,
// explicitly, a folder (cannot be formatted); type from the detection, name, template (it decides the libraries,
// ), recommended
// processors, optional format right after (typed-name confirmation on the device page). Rules and processors are a
// real choice: an existing template, a new template for this device (a copy, named after it), or rules /
// processors of this device only. Nothing is preselected: the user picks both.
import { html, bytes, setHtml } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, post } from "../api.js";
import { toast, errorBox } from "../ui.js";
import { schemaForm, FormError } from "../forms.js";
import { icon, typeIcon } from "../icons.js";
import { goFrom } from "../router.js";
import { pickerButton, pickerActions } from "../picker.js";
import { templatesFor, suggestText, variantList, betaBadge } from "./common.js";

addStrings({
  "reg.title": "Register a device",
  "reg.sub": "Pick the connected drive, name it and choose what goes on it.",
  "reg.disks": "Connected drives",
  "reg.no_disks": "No unregistered drive is connected. Plug in the player or USB drive; it appears here by itself.",
  "reg.folder": "A folder instead (advanced)",
  "reg.folder_help": "A folder is written like a drive but cannot be formatted or ejected. Pick a drive above when you can.",
  "reg.folder_path": "Folder path",
  "reg.network": "A server (FTP or SMB share) or an Android player (MTP)",
  "reg.find": "Find servers on the network",
  "reg.finding": "Looking for servers…",
  "reg.found_title": "Servers found - choose one",
  "reg.found_none": "No {kind} server answered. Type its address below.",
  "reg.network_help": "Files are uploaded to a folder on the server; enter its address and login below (empty user: anonymous on FTP, guest on SMB).",
  "reg.type": "Device type",
  "reg.mtp_serial": "USB serial",
  "reg.mtp_serial_help": "The player's identity: it is recognised by it when plugged in.",
  "reg.mtp_storage": "Storage",
  "reg.mtp_storage_loading": "Reading the player's storage…",
  "reg.mtp_storage_line": "{name} - {size}, {free} free",
  "reg.mtp_folder": "Folder on the device",
  "reg.mtp_folder_help": "./ is the storage's top folder. Wipe deletes only the files SyncMusic put there.",
  "reg.detected_change": "Detected: {what}. Change it if this is wrong.",
  "reg.dual_boot": "This iPod runs Rockbox. Most Rockbox iPods also start the Apple firmware (hold Menu while it boots): choose Stock iPod firmware to sync for that one instead - the first sync creates its music database, Rockbox stays.",
  "reg.step_device": "1 · Device",
  "reg.step_what": "2 · What goes on it",
  "reg.step_procs": "3 · Processors",
  "reg.purpose": "What is this disk for?",
  "reg.purpose.player": "A player or car stick",
  "reg.purpose.player_text": "Files in the formats the player plays, with the processing you choose below.",
  "reg.purpose.backup": "A backup",
  "reg.purpose.backup_text": "The library files as they are: every format, no processing, checked after writing. Nothing is deleted unless you switch the rules to Mirror later.",
  "reg.base_folder": "Folder on the disk",
  "reg.base_folder_help": "Everything goes into this folder; empty: the disk's top level. A new folder is created by the first sync.",
  "reg.backup_procs": "A backup keeps the files as they are: no processing. You can add steps later in the device's Processors tab.",
  "reg.none_selected": "None selected",
  "reg.n_selected": "{n} selected",
  "reg.what_help": "Nothing is preselected. Templates can be changed any time under Device → Templates.",
  "reg.no_templates": "No templates yet: add a library first, its default templates appear here.",
  "reg.group_other": "Several or all libraries",
  "reg.music_only": "Rockbox players get music only.",
  "reg.copy": "Make a copy for this device that you can change on its own",
  "reg.copy_help": "The chosen templates' rules in one new template named after the device.",
  "reg.name": "Name",
  "reg.use_template_help": "The device follows the template: when the template changes, the device does too.",
  "reg.proc_template": "Processor template",
  "reg.use_proc_template": "Use a processor template",
  "reg.sg.draft": "{name} (new, saved on Register)",
  "reg.new_proc_template": "A new processor template for this device",
  "reg.new_proc_template_help": "The recommended steps below, saved as a template named after the device.",
  "reg.variant": "A generated chain",
  "reg.variant_help": "Made for what the chosen templates select, with the estimated size on this device.",
  "reg.device_procs": "Only this device's processors",
  "reg.device_procs_help": "No template: the steps checked below.",
  "reg.no_proc_templates": "No processor template for this device type.",
  "reg.no_processors": "None for this device type.",
  "reg.format": "Format the drive first",
  "reg.format_help": "Everything on it is erased (an iPod keeps its firmware). You confirm by typing its name on the next screen.",
  "reg.wipe": "Wipe the player first",
  "reg.wipe_help": "Its music partition is erased and Rockbox is put back. You confirm by typing its name on the next screen.",
  "reg.step_first": "4 · First sync",
  "reg.update_every": "Checks for changes every {n} min while connected (Settings → Sync defaults).",
  "reg.update_none": "Checks for changes when plugged in and when a library changes (Settings → Sync defaults).",
  "reg.cache_with": "Syncs with the processing cache (Settings → Sync); change it later in the device's Settings tab.",
  "reg.cache_without": "Syncs without the processing cache (Settings → Sync); change it later in the device's Settings tab.",
  "reg.first_help": "What happens to the files already on it. The first plan shows this choice again before anything is written.",
  "reg.first.adopt": "Adopt what is on the device",
  "reg.first.adopt_text": "Matches existing files to your libraries and keeps them.",
  "reg.first.keep": "Merge",
  "reg.first.keep_text": "Keeps everything on the device and only adds what the templates ask for. Nothing is removed on the first sync.",
  "reg.first.wipe": "Wipe the music folder",
  "reg.first.wipe_text": "Removes all music, playlists and the database on the first sync, then writes the templates.",
  "reg.cont_help": "SyncMusic synced this player before (home: {home}). Choose how to go on.",
  "reg.cont_help_mine": "SyncMusic synced this player before. Choose how to go on.",
  "reg.cont.continue": "Continue",
  "reg.cont.continue_text": "Keeps what is on it, including other instances' items, and adds your libraries. Auto-sync on connect stays with the home instance.",
  "reg.cont.fresh": "Start fresh",
  "reg.cont.fresh_text": "Wipes the music on the first sync. Only the home instance can do this.",
  "reg.cont.fresh_not_home": "Only the home instance {home} can start it fresh: continue, then make this instance home.",
  "reg.cont.keep": "Keep files",
  "reg.cont.keep_text": "Registers it as a new device; the items already on it are treated as files SyncMusic did not put there.",
  "reg.submit": "Register",
  "reg.step_summary": "5 · Summary",
  "reg.next": "Next",
  "reg.back": "Back",
  "reg.all_steps": "Show all steps on one page",
  "reg.one_step": "Step by step",
  "reg.sum.device": "Device",
  "reg.sum.type": "Type",
  "reg.sum.purpose": "Purpose",
  "reg.sum.folder": "Folder on the disk",
  "reg.sum.what": "What goes on it",
  "reg.sum.procs": "Processing",
  "reg.sum.first": "First sync",
  "reg.sum.none": "—",
  "reg.sum.as_is": "As is (backup)",
  "reg.missing": "Pick a drive or a folder and give the device a name.",
  "reg.choose": "Choose a template…",
  "reg.choose_copy": "Tick the templates to copy first.",
  "reg.choose_procs": "Choose the processors: a template, a new template or only this device's processors.",
  "reg.done": "{name} registered.",
  "reg.gone": "{disk} is not connected any more.",
});

export const nav = "devices";
export const title = () => t("reg.title");

// a library's default template is named after its library too: several libraries each have an "All music"
function tplLabel(tp, libraries) {
  const lib = tp.default_of && libraries.find((l) => l.id === tp.default_of);
  return lib ? `${tp.name} - ${lib.name}` : tp.name;
}

export async function render(ctx) {
  const [devices, types, libraries, templates, procTemplates, libTypes, procTypes, appSettings] = await Promise.all([
    get("/devices"), get("/device-types"), get("/libraries"), get("/templates"), get("/processor-templates"),
    get("/library-types"), get("/processor-types"), get("/settings").catch(() => null),
  ]);
  ctx.state.procNames = Object.fromEntries(procTypes.map((p) => [p.type, p.display_name])); // step names
  ctx.state.types = types.filter((x) => !x.paired); // Wi-Fi clients come from pairing
  Object.assign(ctx.state, { libraries, templates, libTypes });
  ctx.state.procTemplates = procTemplates;
  ctx.state.unknown = devices.unknown;
  const wanted = ctx.query.get("disk");
  const selected = devices.unknown.find((u) => u.disk === wanted) || (wanted ? null : devices.unknown[0]) || null;
  ctx.state.selected = selected ? selected.disk : "";
  const gone = wanted && !selected ? errorBox({ message: t("reg.gone", { disk: wanted }) }, "") : "";
  return html`<div class="ed-head reg-head"><a class="iconbtn" href="#/devices" aria-label="${t("nav.devices")}" title="${t("nav.devices")}">${icon("left", { size: 18 })}</a>
      <div><h1>${t("reg.title")}</h1><div class="reg-drive ${selected ? "mono" : ""}" data-reg-drive>${driveLine(selected) || t("reg.sub")}</div></div></div>
    ${gone}
    <ol class="reg-steps" data-reg-steps>${STEPS.map((k, i) => html`<li><button type="button" data-reg-goto="${i}">${t(`reg.${k}`)}</button></li>`)}</ol>
    <form class="grid" data-action="register" data-change="regChanged" style="max-width:860px">
      <div class="card" data-step="0"><div class="card-head"><h2>${t("reg.step_device")}</h2></div><div class="card-body">
        <fieldset style="border:0;padding:0;margin:0"><legend class="sr">${t("reg.disks")}</legend>
        ${devices.unknown.length ? devices.unknown.map((u) => diskChoice(u, ctx.state.selected === u.disk)) : html`<div class="callout">${t("reg.no_disks")}</div>`}
        <label class="row" style="cursor:pointer"><input type="radio" name="target" value="" ${ctx.state.selected ? "" : "checked"}>
          <div class="meta"><b>${t("reg.folder")}</b><small>${t("reg.folder_help")}</small></div></label>
        ${networkTypes(ctx).length ? html`<label class="row" style="cursor:pointer"><input type="radio" name="target" value="${NETWORK}">
          <div class="meta"><b>${t("reg.network")}</b><small>${t("reg.network_help")}</small></div></label>` : ""}
        </fieldset>
        <div class="field ${ctx.state.selected ? "hide" : ""}" id="regFolder"><label for="regPath">${t("reg.folder_path")}</label><input id="regPath" placeholder="/media/user/MYDRIVE"></div>
        <div id="regDetails">${details(ctx, selected)}</div>
        <div id="regPurpose">${purposeFields(ctx)}</div>
        <div class="hide" id="regNetwork"></div>
      </div></div>
      <div class="card" data-step="1"><div class="card-head"><h2>${t("reg.step_what")}</h2><span class="muted" data-reg-count>${t("reg.none_selected")}</span></div><div class="card-body">
        <p class="muted" style="margin-top:0">${t("reg.what_help")}</p>
        <div id="regTemplates">${templateChips(ctx)}</div>
        <label class="checkrow reg-copy"><input type="checkbox" id="regCopy"> <span>${t("reg.copy")}<small class="muted" style="display:block">${t("reg.copy_help")}</small></span></label>
      </div></div>
      <div class="card" data-step="2"><div class="card-head"><h2>${t("reg.step_procs")}</h2></div><div class="card-body"><p class="muted" id="regBackupProcs" hidden>${t("reg.backup_procs")}</p><div id="regProcs">${processors(ctx)}</div>
        ${appSettings ? html`<p class="hint" data-reg-cache>${t(appSettings.default_no_cache ? "reg.cache_without" : "reg.cache_with")}</p>
          <p class="hint" data-reg-update>${appSettings.default_update_every_min ? t("reg.update_every", { n: appSettings.default_update_every_min }) : t("reg.update_none")}</p>` : ""}</div></div>
      <div class="card" data-step="3"><div class="card-head"><h2>${t("reg.step_first")}</h2></div><div class="card-body">
        <div id="regContinuity" hidden></div>
        <div id="regFirst"><p class="muted" style="margin-top:0">${t("reg.first_help")}</p>
        <div class="reg-opts" role="radiogroup" aria-label="${t("reg.step_first")}">${FIRST.map((v) => html`<label class="reg-opt"><input type="radio" name="firstSync" value="${v}" ${v === "keep" ? "checked" : ""}>
          <span><b>${t(`reg.first.${v}`)}</b><small>${t(`reg.first.${v}_text`)}</small></span></label>`)}</div></div>
        <div id="regFormatRow" ${formatOffered(ctx) ? "" : "hidden"}>${formatRow(ctx)}</div></div></div>
      <div class="card" data-step="4" data-reg-summary-card><div class="card-head"><h2>${t("reg.step_summary")}</h2></div><div class="card-body"><dl class="reg-summary" id="regSummary"></dl></div></div>
      <div class="reg-actions"><button class="btn ghost reg-all" type="button" data-reg-all>${t("reg.all_steps")}</button><span class="grow"></span><a class="btn" href="#/devices">${t("common.cancel")}</a>
        <button class="btn" type="button" data-reg-back>${t("reg.back")}</button><button class="btn primary" type="button" data-reg-next>${t("reg.next")}</button><button class="btn primary" type="submit">${t("reg.submit")}</button></div>
    </form>`;
}

/** The templates as chips grouped by library; a Rockbox player gets no book / podcast-only
 * templates: such a library says so. Nothing is preselected; ticks survive a type change. */
function templateChips(ctx) {
  const { libraries, templates, libTypes, types } = ctx.state;
  const picked = ctx.state.picked || (ctx.state.picked = new Set());
  const usable = templates.filter((tp) => !tp.id.startsWith("own-")); // a device's own picks are not offered
  if (!usable.length) return html`<div class="callout">${t("reg.no_templates")}</div>`;
  const offered = new Set(templatesFor(ctx.state.type, usable, types, libraries, libTypes).map((tp) => tp.id));
  const groups = libraries.map((l) => ({ name: l.name, type: l.type, items: usable.filter((tp) => tp.library_ids.length === 1 && tp.library_ids[0] === l.id) }));
  const rest = usable.filter((tp) => !groups.some((g) => g.items.includes(tp)));
  if (rest.length) groups.push({ name: t("reg.group_other"), type: "library", items: rest });
  for (const id of [...picked]) if (!offered.has(id)) picked.delete(id);
  return html`${groups.filter((g) => g.items.length).map((g) => {
    const shown = g.items.filter((tp) => offered.has(tp.id));
    return html`<div class="reg-group" data-reg-group><div class="reg-group-head">${typeIcon(g.type, "library", 15)}<b>${g.name}</b></div>
      ${shown.length ? html`<div class="reg-chips">${shown.map((tp) => html`<label class="chip"><input type="checkbox" name="tpl" value="${tp.id}" ${picked.has(tp.id) ? "checked" : ""}>${tp.name}</label>`)}</div>` : ""}
      ${shown.length < g.items.length ? html`<p class="muted" data-music-only>${t("reg.music_only")}</p>` : ""}</div>`;
  })}`;
}

function countPicked(form, ctx) {
  const n = ctx.state.picked.size;
  form.querySelector("[data-reg-count]").textContent = n ? t("reg.n_selected", { n }) : t("reg.none_selected");
}

// the first sync, chosen here and preselected in the first plan (keep = "Merge")
const FIRST = ["adopt", "keep", "wipe"];

/** The drive under the title: vendor model · size · /dev path (mono, as drawn). */
function driveLine(u) {
  if (u?.mtp) return [[u.vendor, u.model].filter(Boolean).join(" "), u.serial].filter(Boolean).join(" · "); //
  return u ? [[u.vendor, u.model].filter(Boolean).join(" "), bytes(u.size), u.disk].filter(Boolean).join(" · ") : "";
}

// formatting first: a connected iPod or USB drive; a Rockbox player calls it Wipe; not a folder or FTP
function formatOffered(ctx) {
  return !!ctx.state.selected && ctx.state.selected !== NETWORK && ["ipod", "flashdrive", "rockbox"].includes(ctx.state.type);
}

function formatRow(ctx) {
  const rb = ctx.state.type === "rockbox";
  return html`<label class="checkrow reg-format"><input type="checkbox" id="regFormat"> <span>${t(rb ? "reg.wipe" : "reg.format")}<small class="muted" style="display:block">${t(rb ? "reg.wipe_help" : "reg.format_help")}</small></span></label>`;
}

function diskChoice(u, checked) {
  const probe = u.probes[0];
  const what = [[u.vendor, u.model].filter(Boolean).join(" "), u.mtp ? null : probe?.model, u.mtp ? u.serial : bytes(u.size), u.mtp ? null : u.disk].filter(Boolean).join(" · ");
  return html`<label class="row" style="cursor:pointer"><input type="radio" name="target" value="${u.disk}" ${checked ? "checked" : ""}>
    <span class="tile">${typeIcon(probe?.type_name || "unknown")}</span><div class="meta"><b>${probe?.suggested_name || u.disk}${betaBadge(probe?.type_name)}</b><small>${what}</small></div></label>`;
}

// "Add FTP device" on the Devices list opens this screen with the server choice made
export function mounted(ctx, root) {
  const form = root.querySelector("form") || root;
  if (form.matches?.('form[data-action="register"]')) wizard(root, form, ctx);
  if (form.querySelector?.("#regSuggest")) suggestProcs(form, ctx);
  if (form.querySelector?.("#regContinuity")) peekChanged(form, ctx);
  loadStorages(form);
  const radio = ctx.query.get("target") === "network" && root.querySelector(`input[name="target"][value="${NETWORK}"]`);
  if (radio && !radio.checked) {
    radio.checked = true;
    radio.dispatchEvent(new Event("change", { bubbles: true }));
  }
}

const NETWORK = "network:"; // the "A server" choice of the target radios

// Step by step: one card at a time (Back / Next, Enter = Next), a summary last; nothing is saved before Register.
// "Show all steps on one page" (remembered in this browser) gives the single-page form with every field at once.
const STEPS = ["step_device", "step_what", "step_procs", "step_first", "step_summary"];
const ALL_KEY = "sm.regSteps";

function wizard(root, form, ctx) {
  const cards = [...form.querySelectorAll("[data-step]")];
  const nav = root.querySelector("[data-reg-steps]");
  const back = form.querySelector("[data-reg-back]");
  const next = form.querySelector("[data-reg-next]");
  const submit = form.querySelector('button[type="submit"]');
  const toggle = form.querySelector("[data-reg-all]");
  let step = 0;
  const all = () => localStorage.getItem(ALL_KEY) === "all";
  const show = () => {
    const one = !all();
    form.classList.toggle("reg-wizard", one);
    nav.hidden = !one;
    toggle.textContent = t(one ? "reg.all_steps" : "reg.one_step");
    cards.forEach((c, i) => { c.hidden = one && i !== step; });
    form.querySelector("[data-reg-summary-card]").hidden = !one || step !== cards.length - 1;
    back.hidden = !one || step === 0;
    next.hidden = !one || step === cards.length - 1;
    submit.hidden = one && step !== cards.length - 1;
    nav.querySelectorAll("[data-reg-goto]").forEach((b, i) => {
      b.setAttribute("aria-current", i === step ? "step" : "false");
      b.disabled = i > step;
    });
    if (one && step === cards.length - 1) setHtml(form.querySelector("#regSummary"), summary(form, ctx));
  };
  // the fields of the shown step must be valid before the next one (the browser shows what is wrong)
  const valid = () => {
    if (![...cards[step].querySelectorAll("input, select, textarea")].every((el) => el.closest("[hidden], .hide") || el.reportValidity())) return false;
    const procs = cards[step].querySelector("#regProcs");
    if (procs && !procs.hidden && !procsChosen(form)) { toast(t("reg.choose_procs"), "error"); return false; }
    return true;
  };
  const go = (to) => {
    if (to > step && !valid()) return;
    step = Math.max(0, Math.min(cards.length - 1, to));
    show();
    cards[step].querySelector("input:not([type=hidden]), select, button")?.focus({ preventScroll: true });
    cards[step].scrollIntoView?.({ block: "nearest" });
  };
  next.addEventListener("click", () => go(step + 1));
  back.addEventListener("click", () => go(step - 1));
  nav.addEventListener("click", (e) => {
    const b = e.target.closest("[data-reg-goto]");
    if (b && !b.disabled) go(Number(b.dataset.regGoto));
  });
  toggle.addEventListener("click", () => {
    localStorage.setItem(ALL_KEY, all() ? "steps" : "all");
    show();
  });
  form.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !form.classList.contains("reg-wizard") || step === cards.length - 1) return;
    if (e.target.matches("textarea, button, a")) return;
    e.preventDefault();
    go(step + 1);
  });
  show();
}

/** A processor choice is made (a backup needs none: its card hides the choice). */
function procsChosen(form) {
  const mode = form.querySelector('input[name="procMode"]:checked')?.value;
  return !!mode && !(mode === "template" && !form.querySelector("#regProcTemplate")?.value);
}

/** What Register will create, from the form as it is now. */
function summary(form, ctx) {
  const val = (sel) => form.querySelector(sel)?.value?.trim() || "";
  const checked = (name) => form.querySelector(`input[name="${name}"]:checked`);
  const label = (input) => input?.closest("label")?.querySelector("b")?.textContent || input?.closest("label")?.textContent?.trim() || "";
  const type = ctx.state.types.find((x) => x.type === ctx.state.type);
  const backup = checked("regPurpose")?.value === "backup";
  const tpls = ctx.state.templates.filter((tp) => ctx.state.picked?.has(tp.id)).map((tp) => tp.name);
  const target = ctx.state.selected === NETWORK ? t("reg.network") : ctx.state.selected || val("#regPath");
  const mode = checked("procMode");
  const procs = backup ? t("reg.sum.as_is")
    : mode?.value === "template" ? form.querySelector("#regProcTemplate")?.selectedOptions?.[0]?.textContent || ""
    : [...form.querySelectorAll('input[name="proc"]:checked')].map((x) => x.parentElement.textContent.trim()).join(", ");
  const rows = [
    ["reg.sum.device", [val("#regName"), target].filter(Boolean).join(" · ")],
    ["reg.sum.type", type?.display_name || ctx.state.type],
    checked("regPurpose") ? ["reg.sum.purpose", label(checked("regPurpose"))] : null,
    form.querySelector("#regBaseFolder") ? ["reg.sum.folder", val("#regBaseFolder") || "/"] : null,
    ["reg.sum.what", tpls.join(", ")],
    ["reg.sum.procs", procs],
    ["reg.sum.first", label(checked("continuity") || checked("firstSync"))],
  ].filter(Boolean);
  return html`${rows.map(([k, v]) => html`<dt>${t(k)}</dt><dd>${v || t("reg.sum.none")}</dd>`)}`;
}

function networkTypes(ctx) {
  // real servers first (FTP is the default choice), the Android player (MTP, no server) last
  const net = ctx.state.types.filter((x) => x.network);
  return [...net.filter((x) => x.type !== "android"), ...net.filter((x) => x.type === "android")];
}

/** The settings form of the chosen network type (host, port, login, ...), with an on-demand server search
 * that fills host and port. */
function networkForm(ctx) {
  const tp = ctx.state.types.find((x) => x.type === ctx.state.type);
  ctx.state.netForm = schemaForm(tp.settings_schema, {}, { prefix: "n-" });
  // an Android player (MTP) is no server: no server search, no remote folder picker
  if (tp.type === "android") return ctx.state.netForm.html;
  return html`<div class="lib-discover"><button type="button" class="btn small" data-action="regFind">${icon("search", { size: 15 })}${t("reg.find")}</button>
    <div data-found aria-live="polite"></div></div>${ctx.state.netForm.html}${pickerButton("dev-net")}`;
}

/** The server search goes under the Server (host) field it fills; the folder picker sits in its field
 * (picker.js). */
function placePicker(net) {
  const host = net.querySelector('[id$="-n-host"]')?.closest(".field");
  const find = net.querySelector(".lib-discover");
  if (host && find) host.after(find);
}

const netKind = (type) => (type === "smb" ? "smb" : "ftp");

function offeredTypes(ctx, unknown) {
  if (ctx.state.selected === NETWORK) return networkTypes(ctx).map((x) => [x.type, x.display_name]);
  // a disk or a folder: never a server type; an MTP player found on USB: the MTP types
  const fits = (x) => (unknown?.mtp ? x.usb_mtp : !x.network);
  const names = Object.fromEntries(ctx.state.types.filter(fits).map((x) => [x.type, x.display_name]));
  const fromProbes = (unknown?.probes || []).map((p) => p.type_name).filter((x) => x in names);
  return (fromProbes.length ? fromProbes : Object.keys(names)).map((k) => [k, names[k]]);
}

/** Type option cards: the detected type first and chosen, then the name. */
function details(ctx, unknown) {
  const types = offeredTypes(ctx, unknown);
  if (!types.some(([k]) => k === ctx.state.type)) ctx.state.type = types[0]?.[0] || "";
  const probe = unknown?.probes?.[0];
  const detected = probe && types.find(([k]) => k === probe.type_name);
  return html`${detected ? html`<div class="reg-detected">${icon("check", { size: 15 })}<span>${t("reg.detected_change", { what: probe.model || detected[1] })}</span></div>` : ""}
    ${unknown?.probes?.some((p) => p.type_name === "ipod" && p.details?.rockbox) ? html`<p class="muted" data-dual-boot>${t("reg.dual_boot")}</p>` : ""}
    <fieldset class="reg-types" ${types.length < 2 ? "hidden" : ""}><legend class="sr">${t("reg.type")}</legend>
      ${types.map(([k, n]) => html`<label class="reg-type"><input type="radio" name="regType" value="${k}" ${k === ctx.state.type ? "checked" : ""}>
        ${typeIcon(k, "device", 22)}<span>${n}${betaBadge(k)}</span></label>`)}</fieldset>
    <div class="field"><label for="regName">${t("reg.name")} *</label><input id="regName" required value="${probe?.suggested_name || ""}" autocomplete="off"></div>
    ${unknown?.mtp ? mtpFields(unknown) : ""}`;
}

/** A USB disk or folder registered as a flash drive: what it is for (player / backup) and the folder on it. */
function purposeFields(ctx) {
  if (ctx.state.type !== "flashdrive" || ctx.state.selected === NETWORK) return "";
  return html`<div data-pick-form data-disk="${ctx.state.selected || ""}"><fieldset class="reg-opts reg-purpose"><legend>${t("reg.purpose")}</legend>
    ${["player", "backup"].map((v) => html`<label class="reg-opt"><input type="radio" name="regPurpose" value="${v}" ${v === "player" ? "checked" : ""}>
      <span><b>${t(`reg.purpose.${v}`)}</b><small>${t(`reg.purpose.${v}_text`)}</small></span></label>`)}</fieldset>
    <div class="field"><label for="regBaseFolder">${t("reg.base_folder")}</label><input id="regBaseFolder" value="" autocomplete="off" placeholder="/">
      ${pickerButton("dev-disk")}<p class="hint">${t("reg.base_folder_help")}</p></div></div>`;
}

/** Backup: no processing to choose (the processors card says so); the folder defaults to SyncMusic. */
function purposeChanged(form) {
  const backup = form.querySelector('input[name="regPurpose"]:checked')?.value === "backup";
  form.querySelector("#regBackupProcs").hidden = !backup;
  form.querySelector("#regProcs").hidden = backup;
  const base = form.querySelector("#regBaseFolder");
  if (base && backup && !base.value.trim()) base.value = "SyncMusic";
  if (base && !backup && base.value.trim() === "SyncMusic") base.value = "";
}

/** A found Android player: its serial (the identity, read-only), the storage from the player and
 * the folder on it (picked over MTP, default the storage's root). */
function mtpFields(u) {
  return html`<div id="regMtp" data-pick-form data-disk="${u.disk}">
    <div class="field"><label for="regMtpSerial">${t("reg.mtp_serial")}</label><input id="regMtpSerial" class="mono" value="${u.serial || ""}" readonly aria-readonly="true">
      <small class="muted">${t("reg.mtp_serial_help")}</small></div>
    <div class="field"><label for="regMtpStorage">${t("reg.mtp_storage")}</label><select id="regMtpStorage" disabled><option value="">${t("reg.mtp_storage_loading")}</option></select>
      <p class="hint warn" data-mtp-error hidden></p></div>
    <div class="field"><label for="regMtpFolder">${t("reg.mtp_folder")}</label><input id="regMtpFolder" value="./" autocomplete="off">
      <small class="muted">${t("reg.mtp_folder_help")}</small></div>
    ${pickerButton("dev-mtp")}</div>`;
}

/** The storage list, read from the player; default: an SD card when there is one, else the largest. */
async function loadStorages(form) {
  const box = form.querySelector("#regMtp");
  if (!box) return;
  const select = box.querySelector("#regMtpStorage");
  const note = box.querySelector("[data-mtp-error]");
  let list;
  try { list = await get(`/discovery/mtp-storages?disk=${encodeURIComponent(box.dataset.disk)}`); } catch (e) {
    if (!box.isConnected) return;
    note.textContent = e.message;
    note.hidden = false;
    select.innerHTML = "";
    return;
  }
  if (!box.isConnected) return;
  const best = list.find((x) => /\bsd\b|card|extern/i.test(x.name)) || [...list].sort((a, b) => b.capacity - a.capacity)[0];
  select.innerHTML = String(html`${list.map((x) => html`<option value="${x.name}" ${x === best ? "selected" : ""}>${t("reg.mtp_storage_line", { name: x.name, size: bytes(x.capacity), free: bytes(x.free) })}</option>`)}`);
  select.disabled = list.length < 2;
  note.hidden = true;
}

function modeChoice(name, value, label, help, checked = false) {
  return html`<label class="row" style="cursor:pointer"><input type="radio" name="${name}" value="${value}" ${checked ? "checked" : ""}>
    <div class="meta"><b>${label}</b><small>${help}</small></div></label>`;
}

function processors(ctx) {
  const tp = ctx.state.types.find((x) => x.type === ctx.state.type);
  const slots = tp ? tp.suggested_processors : [];
  ctx.state.slots = slots;
  // templates made for this device type (the built-in one first) or for any type
  const offered = ctx.state.procTemplates.filter((x) => !x.device_type || x.device_type === ctx.state.type);
  const steps = slots.length
    ? html`${slots.map((s, i) => html`<label class="checkrow"><input type="checkbox" name="proc" value="${i}" ${s.enabled ? "checked" : ""}> ${ctx.state.procNames?.[s.type] || s.type}</label>`)}`
    : html`<div class="muted">${t("reg.no_processors")}</div>`;
  // the type's default template is preselected ("Rockbox: copy")
  const preset = offered.find((x) => x.id === tp?.default_processor_template_id)?.id || "";
  const select = offered.length
    ? html`<select id="regProcTemplate"><option value="" ${preset ? "" : "selected"}>${t("reg.choose")}</option>${offered.map((x) => html`<option value="${x.id}" ${x.id === preset ? "selected" : ""}>${x.name}</option>`)}</select>`
    : html`<div class="muted" id="regProcTemplate">${t("reg.no_proc_templates")}</div>`;
  return html`${offered.length ? html`${modeChoice("procMode", "template", t("reg.use_proc_template"), t("reg.use_template_help"), !!preset)}
      <div class="field" style="margin:4px 0 12px 28px;max-width:420px"><label for="regProcTemplate">${t("reg.proc_template")}</label>${select}</div>` : ""}
    <p class="muted reg-suggest" id="regSuggest" data-suggest-reason hidden></p>
    <div id="regVariantMode" hidden>${modeChoice("procMode", "variant", t("reg.variant"), t("reg.variant_help"))}
      <div id="regVariants" style="margin:4px 0 12px 28px;max-width:520px"></div></div>
    ${modeChoice("procMode", "new", t("reg.new_proc_template"), t("reg.new_proc_template_help"))}
    ${modeChoice("procMode", "device", t("reg.device_procs"), t("reg.device_procs_help"))}
    <div style="margin-left:28px">${steps}</div>`;
}

const CONTINUITY = ["continue", "fresh", "keep"];

/** A drive or folder SyncMusic synced before: Continue (default) / Start fresh (home only) / Keep files instead of
 * the first-sync choice. */
async function peekChanged(form, ctx) {
  const box = form.querySelector("#regContinuity");
  const params = new URLSearchParams();
  const path = form.querySelector("#regPath").value.trim();
  if (ctx.state.selected && ctx.state.selected !== NETWORK) params.set("disk", ctx.state.selected);
  else if (!ctx.state.selected && path) params.set("mount_path", path);
  let r = null;
  const my = (ctx.state.peeking = {});
  if ([...params].length) { try { r = await get(`/devices/register/peek?${params}`); } catch { r = null; } }
  if (my !== ctx.state.peeking || !form.isConnected) return;
  ctx.state.peek = r?.synced_before ? r : null;
  box.hidden = !ctx.state.peek;
  form.querySelector("#regFirst").hidden = !!ctx.state.peek;
  if (!ctx.state.peek) { box.innerHTML = ""; return; }
  const home = r.home ? (r.home.name || r.home.id) : "";
  setHtml(box, html`<p class="muted" style="margin-top:0">${home && !r.is_home ? t("reg.cont_help", { home }) : t("reg.cont_help_mine")}</p>
    <div class="reg-opts" role="radiogroup" aria-label="${t("reg.step_first")}">${CONTINUITY.map((v) => {
      const blocked = v === "fresh" && !r.is_home;
      return html`<label class="reg-opt"><input type="radio" name="continuity" value="${v}" ${v === "continue" ? "checked" : ""} ${blocked ? "disabled" : ""}>
        <span><b>${t(`reg.cont.${v}`)}</b><small>${blocked ? t("reg.cont.fresh_not_home", { home }) : t(`reg.cont.${v}_text`)}</small></span></label>`;
    })}</div>`);
}

/** The format row follows the target and type (offered or not, Format or Wipe); a change unticks it. */
function formatChanged(form, ctx) {
  const row = form.querySelector("#regFormatRow");
  row.hidden = !formatOffered(ctx);
  row.innerHTML = String(formatRow(ctx));
}

/** Ask for the best processor template for what is chosen so far; preselect it unless the user chose already. */
async function suggestProcs(form, ctx) {
  const type = ctx.state.type;
  const network = ctx.state.selected === NETWORK;
  if (!type || network || !form.querySelector("#regSuggest")) return;
  const my = (ctx.state.suggesting = {});
  const body = { type, template_ids: [...ctx.state.picked], name: form.querySelector("#regName")?.value.trim() || "" };
  if (ctx.state.selected) body.disk = ctx.state.selected;
  else if (form.querySelector("#regPath").value.trim()) body.mount_path = form.querySelector("#regPath").value.trim();
  let r;
  try { r = await post("/devices/suggest-processor-template", body); } catch { return; }
  if (my !== ctx.state.suggesting || type !== ctx.state.type || !form.isConnected) return;
  const line = form.querySelector("#regSuggest");
  const select = form.querySelector("select#regProcTemplate");
  if (!line || !select) return;
  line.hidden = !r.reason.files; // nothing to judge yet (no library items)
  // the generated chains to choose from, the best one ticked
  const mode = form.querySelector("#regVariantMode");
  ctx.state.variants = r.reason.files && r.variants?.length ? r.variants : null;
  if (mode) {
    mode.hidden = !ctx.state.variants;
    form.querySelector("#regVariants").innerHTML = ctx.state.variants ? String(variantList(r, "regVariant")) : "";
  }
  if (!r.reason.files) return;
  line.textContent = suggestText(r.reason);
  select.querySelector('option[value="__draft__"]')?.remove();
  ctx.state.draft = r.draft;
  if (r.draft) select.insertAdjacentHTML("beforeend", String(html`<option value="__draft__">${t("reg.sg.draft", { name: r.draft.name })}</option>`));
  if (ctx.state.procTouched) return; // the user's own choice stays
  const id = r.template_id || (r.draft ? "__draft__" : "");
  if (!id) return;
  select.value = id;
  form.querySelector('input[name="procMode"][value="template"]').checked = true;
}

export const actions = {
  ...pickerActions,
  async regFind(el, ctx) {
    const kind = netKind(ctx.state.type);
    const box = el.closest(".lib-discover").querySelector("[data-found]");
    el.disabled = true;
    setHtml(box, html`<p class="hint" role="status">${t("reg.finding")}</p>`);
    try {
      const servers = await post("/discovery/servers", { kinds: [kind] });
      setHtml(box, servers.length ? html`<p class="hint">${t("reg.found_title")}</p><div class="found-list">${servers.map((s) =>
        html`<button type="button" class="found-row" data-action="regPickServer" data-host="${s.host}" data-port="${s.port}">
          ${icon(kind === "smb" ? "smb" : "ftpsrc", { size: 16 })}<b>${s.name}</b><small class="mono">${s.host === s.address ? s.address : `${s.host} · ${s.address}`}:${s.port}</small></button>`)}</div>`
        : html`<p class="hint">${t("reg.found_none", { kind: kind.toUpperCase() })}</p>`);
    } catch (e) {
      setHtml(box, errorBox(e, ""));
    } finally {
      el.disabled = false;
    }
  },
  regPickServer(el) {
    const net = el.closest("#regNetwork");
    const host = net.querySelector('[id$="-n-host"]');
    const port = net.querySelector('[id$="-n-port"]');
    if (host) host.value = el.dataset.host;
    if (port) port.value = el.dataset.port;
    net.querySelectorAll("[data-found] .found-row").forEach((b) => b.setAttribute("aria-pressed", String(b === el)));
    host?.focus();
  },
  regChanged(el, ctx, e) {
    const form = el;
    if (e.target.name === "target") {
      ctx.state.selected = e.target.value;
      const network = ctx.state.selected === NETWORK;
      const unknown = ctx.state.unknown.find((u) => u.disk === ctx.state.selected) || null;
      form.querySelector("#regFolder").classList.toggle("hide", !!ctx.state.selected);

      const typed = form.querySelector("#regName")?.value;
      ctx.state.type = "";
      form.querySelector("#regDetails").innerHTML = String(details(ctx, unknown));
      form.querySelector("#regPurpose").innerHTML = String(purposeFields(ctx));
      purposeChanged(form);
      if (typed && !unknown) form.querySelector("#regName").value = typed;
      form.querySelector("#regProcs").innerHTML = String(processors(ctx));
      form.querySelector("#regTemplates").innerHTML = String(templateChips(ctx));
      countPicked(form, ctx);
      formatChanged(form, ctx);
      const drive = document.querySelector("[data-reg-drive]");
      drive.textContent = driveLine(unknown) || t("reg.sub");
      drive.classList.toggle("mono", !!unknown);
      const net = form.querySelector("#regNetwork");
      net.classList.toggle("hide", !network);
      net.innerHTML = network ? String(networkForm(ctx)) : "";
      placePicker(net);
      loadStorages(form);
      ctx.state.procTouched = false;
      suggestProcs(form, ctx);
      peekChanged(form, ctx);
    } else if (e.target.id === "regPath") {
      suggestProcs(form, ctx);
      peekChanged(form, ctx);
    } else if (e.target.id === "regFormat") {
      e.target.closest(".reg-format").classList.toggle("danger", e.target.checked);
    } else if (e.target.name === "tpl") {
      if (e.target.checked) ctx.state.picked.add(e.target.value); else ctx.state.picked.delete(e.target.value);
      countPicked(form, ctx);
      suggestProcs(form, ctx);
    } else if (e.target.id === "regProcTemplate") {
      // picking a template from the list means using it, unless "a new template" (a copy of it) is chosen
      ctx.state.procTouched = true;
      const current = form.querySelector('input[name="procMode"]:checked')?.value;
      if (e.target.value && current !== "new") form.querySelector('input[name="procMode"][value="template"]').checked = true;
    } else if (e.target.name === "regPurpose") {
      purposeChanged(form);
    } else if (e.target.name === "regType") {
      ctx.state.type = e.target.value;
      form.querySelector("#regPurpose").innerHTML = String(purposeFields(ctx));
      purposeChanged(form);
      form.querySelector("#regProcs").innerHTML = String(processors(ctx));
      form.querySelector("#regTemplates").innerHTML = String(templateChips(ctx));
      countPicked(form, ctx);
      formatChanged(form, ctx);
      if (ctx.state.selected === NETWORK) {
        form.querySelector("#regNetwork").innerHTML = String(networkForm(ctx));
        placePicker(form.querySelector("#regNetwork"));
      }
      ctx.state.procTouched = false;
      suggestProcs(form, ctx);
    } else if (e.target.name === "procMode") {
      ctx.state.procTouched = true;
    } else if (e.target.name === "regVariant") { // ticking a generated chain means using it
      ctx.state.procTouched = true;
      form.querySelector('input[name="procMode"][value="variant"]').checked = true;
    }
  },
  async register(form, ctx) {
    const name = form.querySelector("#regName").value.trim();
    const network = ctx.state.selected === NETWORK;
    const disk = network ? "" : ctx.state.selected;
    const path = form.querySelector("#regPath").value.trim();
    if (!name || (!network && !disk && !path)) { toast(t("reg.missing"), "error"); return; }
    let settings = null;
    if (network) {
      try { settings = ctx.state.netForm.read(form.querySelector("#regNetwork")); } catch (e) {
        if (e instanceof FormError) { toast(e.message, "error"); return; }
        throw e;
      }
    }
    const mtp = form.querySelector("#regMtp");
    if (mtp) { // a found Android player: its serial comes from udev
      settings = { base_path: form.querySelector("#regMtpFolder").value.trim() || "./" };
      const storage = form.querySelector("#regMtpStorage").value;
      if (storage) settings.storage = storage;
    }
    const type = ctx.state.type;
    const picked = [...ctx.state.picked];
    const copy = form.querySelector("#regCopy").checked;
    if (copy && !picked.length) { toast(t("reg.choose_copy"), "error"); return; }
    const purpose = form.querySelector('input[name="regPurpose"]:checked')?.value;
    if (purpose) { // a flash drive: what it is for and the folder on it
      const base = form.querySelector("#regBaseFolder").value.trim().replace(/^\/+|\/+$/g, "");
      settings = { ...(settings || {}), purpose, base_folder: base };
    }
    const backup = purpose === "backup"; // the server sets a backup's processing (none)
    const procMode = form.querySelector('input[name="procMode"]:checked')?.value;
    // no preselection: the processors are a choice; no template ticked = none yet
    if (!backup && !procsChosen(form)) {
      toast(t("reg.choose_procs"), "error"); return;
    }
    const chosen = new Set([...form.querySelectorAll('input[name="proc"]:checked')].map((x) => Number(x.value)));
    let slots = ctx.state.slots.map((s, i) => ({ ...s, enabled: chosen.has(i) }));
    if (procMode === "variant") { // the chosen generated chain becomes the device's own steps
      const v = ctx.state.variants?.[Number(form.querySelector('input[name="regVariant"]:checked')?.value)];
      if (!v) { toast(t("reg.choose_procs"), "error"); return; }
      slots = v.processors;
    }
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    let cfg;
    try {
      // a new template is created only now, on Register (copies are stored on save)
      let templateIds = picked;
      if (copy) { // the ticked templates' rules in one new template named after the device
        const drafts = await Promise.all(picked.map((id) => post(`/templates/${encodeURIComponent(id)}/copy`, { name })));
        const libs = drafts.some((d) => !d.library_ids.length) ? [] : [...new Set(drafts.flatMap((d) => d.library_ids))];
        const merged = { name, library_ids: libs, rules: drafts.flatMap((d) => d.rules), excludes: drafts.flatMap((d) => d.excludes) };
        templateIds = [(await post("/templates", merged)).id];
      }
      let procTemplate = !backup && procMode === "template" ? form.querySelector("#regProcTemplate").value : null;
      if (procTemplate === "__draft__") { // the generated chain is stored only now
        procTemplate = (await post("/processor-templates", ctx.state.draft)).id;
      }
      if (!backup && procMode === "new") {
        procTemplate = (await post("/processor-templates", { name, device_type: type, processors: slots })).id;
      }
      cfg = await post("/devices/register", {
        name,
        type,
        rule_set: { template_ids: templateIds },
        ...(backup ? {} : procTemplate ? { processor_template_id: procTemplate } : { processors: slots }),
        ...(network ? { settings } : disk ? { disk, ...(settings ? { settings } : {}) } : { mount_path: path, ...(settings ? { settings } : {}) }),
        ...(ctx.state.peek
          ? { continuity: form.querySelector('input[name="continuity"]:checked')?.value || "continue" }
          : { first_sync_choice: form.querySelector('input[name="firstSync"]:checked')?.value || null }),
      });
    } finally { submit.disabled = false; }
    toast(t("reg.done", { name: cfg.name }));
    const format = disk && formatOffered(ctx) && form.querySelector("#regFormat")?.checked;
    // Format / Wipe live in Device settings: open it there with the dialog
    goFrom(ctx, `/devices/${encodeURIComponent(cfg.id)}/${format ? "settings?format=1" : "overview"}`);
  },
};
