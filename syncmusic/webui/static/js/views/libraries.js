// Libraries: list with connection health, add (Subsonic-compatible, folder, Audiobookshelf) with a
// connection test, edit (secrets stay masked), refresh the index, remove (refused while a device uses it), browse /
// search / filter the index and add selected items, albums or artists to a device (its "Manual selection" rule).
import { html, bytes, setHtml } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, post, patch, del } from "../api.js";
import { badge, pageHead, toast, typedConfirm, confirm, dialog, errorBox, errorMessage } from "../ui.js";
import { schemaForm, FormError } from "../forms.js";
import { go, goFrom } from "../router.js";
import { librariesChanged } from "../sidebar.js";
import { thumb, etaText, betaBadge } from "./common.js";
import { icon, typeIcon, typeLabel } from "../icons.js";
import { health, checkLibrary, forgetHealth } from "../libhealth.js";
import { pickerButton, pickerActions } from "../picker.js";

addStrings({
  "libs.accounts": "Accounts",
  "libs.view_as": "View as",
  "libs.accounts_sub": "Other users of this library (a family member's profile). Devices sign in as one of them in their settings; plays, ratings and positions go to that user.",
  "libs.account_main": "Main account",
  "libs.account_main_sub": "The connection above",
  "libs.account_add": "Add account",
  "libs.account_edit": "Edit",
  "libs.account_remove": "Remove",
  "libs.account_name": "Display name",
  "libs.account_name_empty": "The account needs a display name.",
  "libs.account_title_add": "Add an account to {name}",
  "libs.account_title_edit": "Account “{name}”",
  "libs.account_test": "Test",
  "libs.account_ok": "Signed in",
  "libs.account_saved": "Account “{name}” saved",
  "libs.account_removed": "Account “{name}” removed",
  "libs.account_unused": "No device uses it",
  "libs.account_used": "Used by {names}",
  "libs.account_in_use": "{names} sign in as this account: pick another account in their settings first.",
  "libs.account_remove_title": "Remove account “{name}”?",
  "libs.account_remove_text": "Its credentials are deleted here; nothing changes in the library itself.",
  "libs.source": "Source",
  "libs.source.local": "Local path",
  "libs.source.smb": "SMB share",
  "libs.source.ftp": "FTP server",
  "libs.address": "Address",
  "libs.address_ph.smb": "smb://nas.local/music",
  "libs.address_ph.ftp": "ftp://10.0.0.20:21/music",
  "libs.address_help.smb": "Like smb://host/share/sub-folder: a Windows share or Samba (a NAS).",
  "libs.address_help.ftp": "Like ftp://host:21/folder; ftps:// for an encrypted connection. Passive mode is used.",
  "libs.address_bad.smb": "The address must look like smb://host/share/folder.",
  "libs.address_bad.ftp": "The address must look like ftp://host/folder (or ftps://).",
  "libs.guest": "Guest access (no user name)",
  "libs.anonymous": "Anonymous login",
  "libs.find": "Find servers on the network",
  "libs.finding": "Looking for servers…",
  "libs.found_none": "No {kind} server answered. Type its address below.",
  "libs.found_title": "Servers found - choose one",
  "libs.shares": "List shares",
  "libs.shares_loading": "Asking the server for its shares…",
  "libs.shares_title": "Shares on {host} - choose one",
  "libs.shares_none": "The server offers no shares to this login.",
  "libs.shares_need_host": "Choose a server or type its address first.",
  "libs.read_only": "Read only: SyncMusic never changes anything on the server. Tags are read from the start and end of each file; a whole file is fetched only when it goes to a device.",
  "libs.sub": "Where your music, books and podcasts come from.",
  "libs.empty_title": "No libraries yet",
  "libs.refresh_all": "Refresh all",
  "libs.refreshing_all": "Refreshing {n} libraries…",
  "libs.refreshing_all_one": "Refreshing {n} library…",
  "libs.scanning": "Scanning",
  "libs.scan_left": "{left} left",
  "libs.browse_cached": "Browse shows what was loaded last.",
  "libs.unreachable": "Unreachable",
  "count.book": "{n} books",
  "count.book_one": "{n} book",
  "count.episode": "{n} episodes",
  "count.episode_one": "{n} episode",
  "count.video": "{n} videos",
  "count.video_one": "{n} video",
  "libs.add": "Add a library",
  "libs.none": "Add your Subsonic server (Navidrome, Gonic, Airsonic, …), Audiobookshelf, or a folder with music, audiobooks or podcasts. Default templates are created for each.",
  "libs.browse": "Browse",
  "libs.edit": "Settings",
  "libs.refresh": "Refresh",
  "libs.settings": "Settings",
  "libs.refreshing": "Refreshing {name}…",
  "libs.refreshing_btn": "Refreshing…",
  "libs.updated": "Updated {ago}",
  "libs.not_loaded": "Not loaded yet",
  "libs.ago_now": "just now",
  "libs.ago_min": "{n} min ago",
  "libs.ago_h": "{n} h ago",
  "libs.changed": "{name} has new data.",
  "libs.show_new": "Show it",
  "libs.refreshed": "{name}: {tracks}, {playlists} indexed.",
  "libs.check": "Test connection",
  "libs.ok": "Connected",
  "libs.checking": "Checking…",
  "libs.disabled": "Disabled",
  "libs.add_title": "Add a library",
  "libs.edit_title": "{name} - settings",
  "libs.type": "Type",
  "libs.name": "Name",
  "libs.enabled": "Enabled",
  "libs.save_add": "Save and scan",
  "libs.added": "{name} added and reachable.",
  "libs.added_bad": "{name} added, but the connection test failed: {error}",
  "libs.saved": "Saved.",
  "libs.remove": "Remove library…",
  "libs.remove_title": "Remove {name}?",
  "libs.remove_text": "SyncMusic forgets this library and its default templates. Nothing in the library itself is changed; files already on devices stay until their next sync.",
  "libs.remove_users": "Devices that use its templates: {names}.",
  "libs.back": "Back",
  "libs.step_type": "1 · Type",
  "libs.step_connection": "2 · Connection",
  "libs.type_hint.folder": "A folder on this computer, an SMB share or an FTP server",
  "libs.type_hint.audiobookshelf": "Audiobooks and podcasts",
  "libs.type_hint.jellyfin": "Movies and episodes, to the player only",
  "libs.type_hint.subsonic": "Navidrome, Gonic, Airsonic, …",
  "libs.connecting": "Connecting…",
  "libs.check_ok": "Connected.",
  "libs.delete_title": "Remove this library",
  "libs.delete_text": "Its default templates go with it. Files already on devices stay until the next sync.",
  "libs.saved_scanning": "Library saved · first scan running",
  "libs.saved_sub": "{name} is reachable. The scan runs in the background; you can browse it when it is done.",
  "libs.saved_bad": "Library saved, but it is not reachable",
  "libs.defaults_created": "Default templates created",
  "libs.defaults_note": "They are not added to any device. Pick them per device under Device → Rules.",
  "libs.browse_it": "Browse the library",
  "libs.back_to_list": "Back to libraries",
  "libs.removed": "{name} removed.",
  "libs.remove_edited": "You edited default templates of this library: {names}. Keep them as templates of every library, or delete them too?",
  "libs.remove_keep": "Remove, keep the templates",
  "libs.remove_delete": "Remove with the templates",
  "libs.search": "Search title, artist, album",
  "libs.search_btn": "Search",
  "libs.select_all": "Select all on this page",
  "libs.prev": "Previous",
  "libs.next": "Next",
  "libs.page": "Page {page} of {pages}",
  "libs.no_devices": "No devices registered yet.",
  "libs.no_templates": "No template draws from this library yet.",
  "libs.try_again": "Try again",
  "libs.search_in": "Search {tab}…",
  "libs.sort": "Sort",
  "libs.sort.artist": "Artist A–Z",
  "libs.sort.added": "Recently added",
  "libs.sort.rating": "Rating",
  "libs.sort.year": "Year",
  "libs.sort.show": "Show and episode",
  "libs.sort.title": "Title A–Z",
  "libs.view": "View",
  "libs.view_grid": "Grid",
  "libs.view_list": "List",
  "libs.no_match": "Nothing matches “{q}” in {tab}.",
  "libs.tab_empty": "Nothing here yet.",
  "libs.clear": "Clear",
  "libs.add_to": "Add to device…",
  "libs.selected": "{n} selected",
  "libs.on_device": "On {name}",
  "libs.on_device_partly": "{n} of {of} on {name}",
  "libs.music_only": "Rockbox players get music only",
  "libs.used_by": "Used by {n} devices",
  "libs.used_by_one": "Used by {n} device",
  "libs.add_to_title": "Add {n} items to…",
  "libs.add_to_title_one": "Add {n} item to…",
  "libs.add_text": "Adds an include rule for them. The next sync plan shows what it changes.",
  "libs.add_devices": "A device (its own picks)",
  "libs.add_templates": "A template (every device using it)",
  "libs.add_rule": "Add rule",
  "libs.pick_target": "Pick a device or a template.",
  "libs.rule_added": "Rule added to {name}. {n} will be in the next plan.",
  "libs.rule_added_tpl": "Rule added to {name}. {n} will be in the next plan of its {d}.",
  "libs.rule_added_unused": "Rule added to {name}. No device uses this template yet.",
  "libs.rule_removed": "Rule removed.",
  "count.file": "{n} files",
  "count.file_one": "{n} file",
  "libs.tab.songs": "Songs",
  "libs.tab.albums": "Albums",
  "libs.tab.artists": "Artists",
  "libs.tab.playlists": "Playlists",
  "libs.tab.books": "Books",
  "libs.tab.podcasts": "Podcasts",
  "libs.tab.videos": "Videos",
  "libs.smart": "smart",
  "libs.advanced": "Advanced",
});

export const nav = "libraries";
export const title = (ctx) => ctx.state.lib?.name || t("nav.libraries");

const PAGE = 50;
const ADVANCED = new Set(["subfolder_kinds", "genre_separators"]);
// a folder's location has its own controls: the Local / SMB / FTP switch, `folderSource`
const FOLDER_SOURCE = new Set(["source", "root", "host", "port", "username", "password", "remote_path", "tls"]);
const advanced = (type, p) => ADVANCED.has(p);
const ownControl = (type, p) => type === "folder" && FOLDER_SOURCE.has(p);

export async function render(ctx) {
  const { id, mode } = ctx.params;
  if (ctx.path === "/libraries/new") return renderEdit(ctx, null);
  if (!id) return renderList(ctx);
  const lib = await get(`/libraries/${encodeURIComponent(id)}`);
  ctx.state.lib = lib;
  return mode === "browse" ? renderBrowse(ctx, lib) : renderEdit(ctx, lib);
}

// --- list -------------------------------------------------------------------------------------------------------

// the list: one card per library - type, health in one
// word (an unreachable library's reason inline, Browse still shows its last snapshot), what it holds per
// kind (from the loaded snapshot), a running refresh's progress, its address, "Updated …", Refresh, Settings, Browse
async function renderList(ctx) {
  const [libs, jobs] = await Promise.all([get("/libraries"), get("/jobs")]);
  ctx.state.libs = libs;
  const scans = new Map(jobs.filter((j) => j.kind === "refresh" && j.library_id && ["queued", "running"].includes(j.status)).map((j) => [j.library_id, j]));
  watchScans(ctx, [...scans.values()]);
  const add = html`<a class="btn primary" href="#/libraries/new">${icon("plus", { size: 16 })}${t("libs.add")}</a>`;
  if (!libs.length) {
    return html`${pageHead(t("nav.libraries"), t("libs.sub"), add)}
      <div class="lib-empty" data-libs-empty><span class="tile">${icon("libraries", { size: 26 })}</span><b>${t("libs.empty_title")}</b>
        <p>${t("libs.none")}</p>${add}</div>`;
  }
  return html`${pageHead(t("nav.libraries"), t("libs.sub"), html`<button class="btn" data-action="refreshAll">${icon("sync", { size: 16 })}${t("libs.refresh_all")}</button>${add}`)}
    <div class="lib-grid">${libs.map((l) => libCard(l, scans.get(l.id)))}</div>`;
}

// a scan's job.finished can arrive while the list is still loading (then the card keeps a disabled Refresh until
// the next event): while scans run, look at them every few seconds and redraw once one has ended
let scanTimer = null;
function watchScans(ctx, scans) {
  clearTimeout(scanTimer);
  if (!scans.length) return;
  scanTimer = setTimeout(async () => {
    if (ctx.path !== "/libraries") return;
    const now = await Promise.all(scans.map((j) => get(`/jobs/${encodeURIComponent(j.id)}`).catch(() => j)));
    if (ctx.path !== "/libraries") return;
    if (now.some((j) => !["queued", "running"].includes(j.status))) ctx.rerender();
    else watchScans(ctx, scans);
  }, 3000);
}

const COUNTS = [["tracks", "music", "count.track"], ["books", "book", "count.book"], ["episodes", "podcast", "count.episode"], ["videos", "video", "count.video"], ["playlists", "playlist", "count.playlist"]];

function libCard(l, scan) {
  const id = encodeURIComponent(l.id);
  const h = health.get(l.id);
  const counts = COUNTS.filter(([k]) => l.counts?.[k]);
  const pct = scan ? Math.round((scan.progress || 0) * 100) : 0;
  return html`<article class="lib-card" data-library="${l.id}">
    <div class="lib-top"><span class="lib-ico">${typeIcon(l.type, "library", 22)}</span>
      <div class="lib-name"><a href="#/libraries/${id}/browse">${l.name}</a><small>${typeLabel(l.type, "library")}${betaBadge(l.type)}${l.type === "folder" ? ` · ${t(`libs.kind.${l.settings.default_kind || "music"}`)}` : ""}</small></div>
      <span data-health="${l.id}">${healthBadge(l)}</span></div>
    ${counts.length ? html`<div class="lib-counts">${counts.map(([k, ico, key]) => html`<span class="chip" data-count="${k}">${icon(ico, { size: 14 })}${t(key, { n: l.counts[k] })}</span>`)}</div>` : ""}
    <div data-health-reason="${l.id}">${healthReason(l, h)}</div>
    ${scan ? html`<div class="lib-scan" data-scan="${scan.id}"><div class="bar thin info" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${t("libs.scanning")}"><i style="width:${pct}%"></i></div>
      <small data-scan-text>${scanText(scan.progress, scan.eta_s)}</small></div>` : ""}
    <div class="lib-addr mono" title="${l.settings.url || l.settings.root || ""}">${l.settings.url || l.settings.root || ""}</div>
    <div class="lib-foot"><small class="muted" data-updated="${l.id}">${updatedText(l.updated_at)}</small>
      ${refreshButton(l, { scanning: !!scan, iconOnly: true })}
      <a class="btn" href="#/libraries/${id}">${t("libs.edit")}</a>
      <a class="btn primary-soft" href="#/libraries/${id}/browse">${t("libs.browse")}</a></div>
  </article>`;
}

const scanText = (progress, eta) => [t("libs.scanning"), `${Math.round((progress || 0) * 100)}%`, etaText(eta, true) && t("libs.scan_left", { left: etaText(eta, true) })].filter(Boolean).join(" · ");

// an unreachable library says why, on the card (the badge has one word)
function healthReason(l, h) {
  if (l.enabled === false || !h || h.ok) return "";
  return html`<div class="lib-problem">${icon("xcircle", { size: 16 })}<span>${h.text}${l.updated_at ? ` ${t("libs.browse_cached")}` : ""}</span></div>`;
}

// browsing cache: when the library last confirmed what is shown, and a Refresh that reloads it now
function updatedText(iso) {
  if (!iso) return t("libs.not_loaded");
  const min = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
  const ago = min < 1 ? t("libs.ago_now") : min < 60 ? t("libs.ago_min", { n: min }) : t("libs.ago_h", { n: Math.floor(min / 60) });
  return t("libs.updated", { ago });
}

function refreshButton(l, { scanning = false, iconOnly = false } = {}) {
  const busy = scanning || [...ctxJobs.values()].includes(l.name);
  if (iconOnly) {
    // the list's card: an icon button; it spins while the library reloads
    return html`<button type="button" class="btn iconbtn" data-action="refresh" data-icon-only data-id="${l.id}" data-name="${l.name}" aria-label="${t(busy ? "libs.refreshing_btn" : "libs.refresh")}" title="${t(busy ? "libs.refreshing_btn" : "libs.refresh")}" ${busy ? "disabled" : ""}>${icon("sync", { size: 16, spin: busy })}</button>`;
  }
  return html`<button class="btn" data-action="refresh" data-id="${l.id}" data-name="${l.name}" ${busy ? "disabled" : ""}>${icon("sync", { size: 16, spin: busy })}<span>${busy ? t("libs.refreshing_btn") : t("libs.refresh")}</span></button>`;
}

// where Cancel / Save of the settings form return to: the browser it was opened from, else the list
const back = (ctx, lib) => (lib && ctx.query.get("back") === "browse" ? `#/libraries/${encodeURIComponent(lib.id)}/browse` : "#/libraries");

function healthBadge(l) {
  if (l.enabled === false) return badge("neutral", t("libs.disabled"));
  const h = health.get(l.id);
  // one word; the reason is on the card (healthReason) and in the title
  return h ? (h.ok ? badge("ok", t("libs.ok")) : html`<span title="${h.text}">${badge("error", t("libs.unreachable"))}</span>`)
    : badge("info", t("libs.checking"), "sync");
}

export function mounted(ctx, root) {
  if (ctx.path.endsWith("/browse")) {
    // the browser's health badge (and the problem box of an unreachable library) once its check is in
    const lib = ctx.state.lib;
    if (lib && lib.enabled !== false && !health.has(lib.id)) {
      checkLibrary(lib.id).then((h) => {
        if (!root.querySelector(`[data-health="${CSS.escape(lib.id)}"]`)) return;
        if (!h.ok && !root.querySelector('input[name="sel"]:checked')) ctx.rerender();
        else root.querySelector(`[data-health="${CSS.escape(lib.id)}"]`).innerHTML = String(healthBadge(lib));
      });
    }
    selectionChanged(root);
    // the open tab in sight on a phone, where the tab row scrolls (five tabs of a folder library)
    root.querySelector('.tabs [aria-selected="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
    return;
  }
  if (ctx.params.id || ctx.path === "/libraries/new") return;
  for (const l of ctx.state.libs || []) {
    if (l.enabled === false || health.has(l.id)) continue;
    checkLibrary(l.id).then((h) => {
      const el = root.querySelector(`[data-health="${CSS.escape(l.id)}"]`);
      if (el) el.innerHTML = String(healthBadge(l));
      const why = root.querySelector(`[data-health-reason="${CSS.escape(l.id)}"]`);
      if (why) why.innerHTML = String(healthReason(l, h));
    });
  }
}

// --- add / edit ---------------------------------------------------------------------------------------------------

async function renderEdit(ctx, lib) {
  const types = await get("/library-types");
  ctx.state.types = types;
  const type = lib ? lib.type : ctx.query.get("type") || types[0].type;
  const tp = types.find((x) => x.type === type) || types[0];
  const form = settingsForm(tp, lib ? lib.settings : {});
  ctx.state.form = form;
  ctx.state.type = tp.type;
  // 1 · Type as option cards (fixed once the library exists), 2 · Connection
  // (the schema form underneath, secrets with Show), Test connection with a spinner and a one-line result
  const shown = lib ? [tp] : types;
  return html`<div class="pagehead"><div class="edit-title"><a class="btn iconbtn" href="${back(ctx, lib)}" aria-label="${t("libs.back")}">${icon("left", { size: 18 })}</a>
      <h1>${lib ? t("libs.edit_title", { name: lib.name }) : t("libs.add_title")}</h1></div></div>
    <form class="lib-form" data-action="${lib ? "save" : "add"}">
      <section><h2 class="step-head">${t("libs.step_type")}</h2>
        <div class="type-cards" role="radiogroup" aria-label="${t("libs.type")}">${shown.map((x) => html`<button type="button" class="type-card" role="radio"
          aria-checked="${x.type === tp.type ? "true" : "false"}" data-lib-type="${x.type}" ${lib ? "disabled" : html`data-action="pickType"`}>
          ${typeIcon(x.type, "library", 22)}<span><b>${x.display_name}${betaBadge(x.type)}</b><small>${x.display_hint || t(`libs.type_hint.${x.type}`)}</small></span></button>`)}</div></section>
      <section class="card lib-conn"><div class="card-body">
        <h2 class="step-head">${t("libs.step_connection")}</h2>
        <div class="field"><label for="libName">${t("libs.name")} *</label><input id="libName" required value="${lib ? lib.name : tp.display_name}" autocomplete="off"></div>
        <div style="margin-top:14px">${form.html}</div>
        ${lib ? html`<label class="checkrow"><input type="checkbox" id="libEnabled" ${lib.enabled ? "checked" : ""}> ${t("libs.enabled")}</label>` : ""}
        ${lib ? html`<div class="test-row"><button class="btn" type="button" data-action="checkOne">${icon("pulse", { size: 16 })}${t("libs.check")}</button>
          <span id="libCheck" class="test-result" aria-live="polite"></span></div>` : ""}
      </div></section>
      <div class="form-actions">
        ${lib ? html`<button class="btn" type="button" data-action="refresh" data-id="${lib.id}" data-name="${lib.name}">${t("libs.refresh")}</button>` : ""}
        <a class="btn ghost" href="${back(ctx, lib)}">${t("common.cancel")}</a>
        <button class="btn primary" type="submit">${lib ? t("common.save") : t("libs.save_add")}</button></div>
    </form>
    ${lib && accountFields(tp).length ? await accountsCard(lib, tp) : ""}
    ${lib ? html`<section class="danger-row"><div><b>${t("libs.delete_title")}</b><small>${t("libs.delete_text")}</small></div>
      <button class="btn danger" data-action="remove">${t("libs.remove")}</button></section>` : ""}`;
}

// --- accounts ----------------------------------------------------------------------------------

const accountFields = (tp) => tp.account_fields || [];

// the devices signed in as each account: account id → device names
async function accountUsers(libraryId) {
  const out = {};
  for (const d of (await get("/devices")).registered) {
    const aid = d.library_accounts?.[libraryId];
    if (aid) (out[aid] ||= []).push(d.name);
  }
  return out;
}

// what identifies an account in the list: its user name, else nothing (a token)
const accountUser = (acc) => acc.settings?.username || "";

async function accountsCard(lib, tp) {
  const users = await accountUsers(lib.id);
  const rows = Object.entries(lib.accounts || {});
  const row = (aid, acc) => {
    const used = users[aid] || [];
    const who = accountUser(acc);
    return html`<li class="acct-row" data-lib-account="${aid}"><div class="acct-text"><b>${acc.name}</b>
      <small>${who ? html`<span class="mono">${who}</span> · ` : ""}${used.length ? t("libs.account_used", { names: used.join(", ") }) : t("libs.account_unused")}</small></div>
      <div class="acct-actions"><button type="button" class="btn" data-action="editAccount" data-account="${aid}">${t("libs.account_edit")}</button>
      <button type="button" class="btn ghost" data-action="removeAccount" data-account="${aid}">${t("libs.account_remove")}</button></div></li>`;
  };
  return html`<section class="card lib-accounts" id="libAccounts"><div class="card-body">
    <h2 class="step-head">${t("libs.accounts")}</h2><p class="muted">${t("libs.accounts_sub")}</p>
    <ul class="acct-list"><li class="acct-row" data-lib-account=""><div class="acct-text"><b>${t("libs.account_main")}</b>
      <small>${accountUser(lib) ? html`<span class="mono">${accountUser(lib)}</span> · ` : ""}${t("libs.account_main_sub")}</small></div></li>
      ${rows.map(([aid, acc]) => row(aid, acc))}</ul>
    <div><button type="button" class="btn" data-action="addAccount">${icon("plus", { size: 16 })}${t("libs.account_add")}</button></div>
  </div></section>`;
}

/** Add or edit one account in a dialog: display name + the type's account fields, Test, Save (stored at once). */
export async function accountDialog(lib, tp, aid = null) {
  const acc = aid ? lib.accounts[aid] : { name: "", settings: {} };
  const form = schemaForm(tp.settings_schema, acc.settings, { prefix: "acct-", only: accountFields(tp) });
  const body = html`<div class="field"><label for="acctName">${t("libs.account_name")} *</label><input id="acctName" value="${acc.name}" autocomplete="off"></div>
    ${form.html}<div class="test-row"><button type="button" class="btn" data-acct-test>${icon("pulse", { size: 16 })}${t("libs.account_test")}</button>
    <span class="test-result" data-acct-result aria-live="polite"></span></div>`;
  const read = (box) => {
    const name = box.querySelector("#acctName").value.trim();
    if (!name) { toast(t("libs.account_name_empty"), "error"); return null; }
    try { return { name, settings: form.read(box) }; } catch (e) {
      if (e instanceof FormError) { toast(e.message, "error"); return null; }
      throw e;
    }
  };
  let saved = null;
  const done = dialog({
    title: aid ? t("libs.account_title_edit", { name: acc.name }) : t("libs.account_title_add", { name: lib.name }),
    body, initialFocus: "#acctName",
    buttons: [{ label: t("common.cancel"), value: null }, { label: t("common.save"), value: "save", kind: "primary" }],
    onButton: async (value, box) => {
      if (value !== "save") return true;
      const values = read(box);
      if (!values) return false;
      let id = aid;
      if (!id) {
        const base = slug(values.name) || "account";
        id = base;
        for (let n = 2; lib.accounts?.[id]; n++) id = `${base}-${n}`;
      }
      try {
        saved = await patch(`/libraries/${encodeURIComponent(lib.id)}`, { accounts: { ...(lib.accounts || {}), [id]: values } });
      } catch (e) { toast(errorMessage(e), "error"); return false; }
      toast(t("libs.account_saved", { name: values.name }));
      saved = { lib: saved, id };
      return true;
    },
  });
  // Test: the typed values signed in as this account (a masked secret is the account's stored one), nothing saved
  const box = document.querySelector("#overlay .modal-back:last-child");
  box?.querySelector("[data-acct-test]")?.addEventListener("click", async (e) => {
    const values = read(box);
    if (!values) return;
    const out = box.querySelector("[data-acct-result]");
    e.currentTarget.disabled = true;
    let h;
    try {
      const r = await post(`/libraries/${encodeURIComponent(lib.id)}/check`, { account: aid, settings: values.settings });
      h = { ok: !!r.ok, text: r.message || t("status.failed") };
    } catch (err) { h = { ok: false, text: errorMessage(err) }; }
    box.querySelector("[data-acct-test]").disabled = false;
    setHtml(out, html`<span class="test-${h.ok ? "ok" : "fail"}" data-state="${h.ok ? "ok" : "error"}">${icon(h.ok ? "ccheck" : "xcircle", { size: 16 })}${h.ok ? t("libs.account_ok") : h.text}</span>`);
  });
  await done;
  return saved;
}

/** The settings of a library type as a form {html, read}: a folder's Local / SMB / FTP switch first, rarely needed
 * settings under "Advanced" (a folder's per-subfolder media types, genre separators). Also the first-run
 * wizard's; its controls need `sourceActions` among the view's actions. */
export function settingsForm(tp, values) {
  const props = Object.keys(tp.settings_schema.properties || {});
  const adv = props.filter((p) => advanced(tp.type, p));
  const main = schemaForm(tp.settings_schema, values, { only: props.filter((p) => !advanced(tp.type, p) && !ownControl(tp.type, p)) });
  const more = adv.length ? schemaForm(tp.settings_schema, values, { only: adv, prefix: "adv-" }) : null;
  const where = tp.type === "folder" ? folderSource(tp.settings_schema, values) : null;
  return {
    html: html`${where ? where.html : ""}${main.html}${more ? html`<details style="margin-top:12px" data-advanced><summary>${t("libs.advanced")}</summary><div style="margin-top:10px">${more.html}</div></details>` : ""}`,
    read: (root) => ({ ...(where ? where.read(root) : {}), ...main.read(root), ...(more ? more.read(root) : {}) }),
  };
}

// after "Save and scan": the first scan runs, the library's default templates are listed as links - they
// are not added to any device
function savedPanel(lib, h, templates) {
  const id = encodeURIComponent(lib.id);
  return html`<div class="pagehead"><div class="edit-title"><a class="btn iconbtn" href="#/libraries" aria-label="${t("libs.back")}">${icon("left", { size: 18 })}</a>
      <h1>${t("libs.add_title")}</h1></div></div>
    <section class="saved-panel" data-lib-saved="${lib.id}">
      <div class="saved-head"><span class="saved-ico ${h.ok ? "ok" : "er"}">${icon(h.ok ? "ccheck" : "warn", { size: 22 })}</span>
        <div><b>${h.ok ? t("libs.saved_scanning") : t("libs.saved_bad")}</b><small>${h.ok ? t("libs.saved_sub", { name: lib.name }) : h.text}</small></div></div>
      ${templates.length ? html`<b>${t("libs.defaults_created")}</b>
        <div class="saved-tpls">${templates.map((x) => html`<a class="btn small" data-created-template="${x.id}" href="#/templates/${encodeURIComponent(x.id)}">${icon("templates", { size: 15 })}${x.name}</a>`)}</div>
        <small class="muted">${t("libs.defaults_note")}</small>` : ""}
      <div class="toolbar"><a class="btn primary" href="#/libraries/${id}/browse">${t("libs.browse_it")}</a><a class="btn" href="#/libraries">${t("libs.back_to_list")}</a></div>
    </section>`;
}

function slug(name) {
  return name.toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "library";
}

// where a folder library is: Local path / SMB share / FTP server; a server
// is one address (smb://host/share/folder, ftp://host:21/folder, ftps:// = encrypted) plus guest / anonymous or a login
const SOURCES = ["local", "smb", "ftp"];

function folderAddress(v) {
  if (!v.source || v.source === "local") return "";
  const scheme = v.source === "ftp" && v.tls ? "ftps" : v.source;
  const port = v.port ? `:${v.port}` : "";
  return `${scheme}://${v.host || ""}${port}${v.remote_path && v.remote_path !== "/" ? v.remote_path : "/"}`;
}

function folderSource(schema, values) {
  const src = values.source || "local";
  // a local folder needs its path (named and marked by the form); the schema allows none for servers
  const rootSchema = { ...schema, required: [...(schema.required || []), "root"], properties: { ...schema.properties, root: { type: "string", format: "path" } } };
  const local = schemaForm(rootSchema, values, { only: ["root"], prefix: "src-" });
  const login = schemaForm(schema, values, { only: ["username", "password"], prefix: "src-" });
  const guest = src !== "local" && !values.username;
  const kind = src === "ftp" ? "ftp" : "smb";
  const view = html`<div class="field" data-folder-source style="margin-bottom:14px"><span class="pe-label" id="libSrcLabel">${t("libs.source")}</span>
      <div class="segr" role="radiogroup" aria-labelledby="libSrcLabel">${SOURCES.map((x) => html`<label><input type="radio" name="libSource" value="${x}" data-change="libSource" ${x === src ? "checked" : ""}><span>${t(`libs.source.${x}`)}</span></label>`)}</div></div>
    <div data-src="local" style="margin-bottom:14px" ${src === "local" ? "" : "hidden"}>${local.html}${pickerButton("lib-local")}</div>
    <div data-src="remote" ${src === "local" ? "hidden" : ""}>
      <div class="lib-discover"><button type="button" class="btn small" data-action="libFind">${icon("search", { size: 15 })}${t("libs.find")}</button>
        <div data-found aria-live="polite"></div></div>
      <div class="field"><label for="libAddress">${t("libs.address")} *</label>
        <input id="libAddress" class="mono" value="${folderAddress(values)}" placeholder="${t(`libs.address_ph.${kind}`)}" autocomplete="off" spellcheck="false" aria-describedby="libAddressHelp">
        <div class="hint" id="libAddressHelp" data-address-help>${t(`libs.address_help.${kind}`)}</div></div>
      <label class="checkrow"><input type="checkbox" id="libGuest" data-change="libGuest" ${guest ? "checked" : ""}> <span data-guest-label>${t(kind === "ftp" ? "libs.anonymous" : "libs.guest")}</span></label>
      <div data-login ${guest ? "hidden" : ""}>${login.html}</div>
      <div class="lib-discover" data-shares-box ${kind === "smb" ? "" : "hidden"}><button type="button" class="btn small" data-action="libShares">${t("libs.shares")}</button>
        <div data-shares aria-live="polite"></div></div>
      ${pickerButton("lib-remote")}
      <p class="muted" style="font-size:12.5px;margin:10px 0 14px">${t("libs.read_only")}</p></div>`;
  return {
    html: view,
    read(root) {
      const source = root.querySelector('input[name="libSource"]:checked')?.value || "local";
      if (source === "local") return { source, ...local.read(root), host: "", username: "", password: "" };
      const input = root.querySelector("#libAddress");
      let url = null;
      try { url = new URL(input.value.trim()); } catch { url = null; }
      const schemes = source === "ftp" ? ["ftp:", "ftps:"] : ["smb:"];
      if (!url || !schemes.includes(url.protocol) || !url.hostname) {
        input.setAttribute("aria-invalid", "true");
        input.focus();
        throw new FormError(t(`libs.address_bad.${source}`));
      }
      input.removeAttribute("aria-invalid");
      const guestOn = root.querySelector("#libGuest").checked;
      return {
        source, host: url.hostname, port: url.port ? Number(url.port) : null, remote_path: decodeURIComponent(url.pathname || "/") || "/",
        tls: url.protocol === "ftps:", root: null,
        ...(guestOn ? { username: "", password: "" } : login.read(root)),
      };
    },
  };
}

function readSettings(ctx, form) {
  try {
    return ctx.state.form.read(form);
  } catch (e) {
    if (e instanceof FormError) { toast(e.message, "error"); return null; }
    throw e;
  }
}

// --- browse ----------------------------------------------------------------

// tabs by source type: what a tab lists and how its rows are added; `grid`: artwork cards possible
const TABS = {
  songs: { kind: "music" },
  albums: { kind: "music", group: "album", grid: true },
  artists: { kind: "music", group: "artist", grid: true },
  playlists: { playlists: true, grid: true },
  books: { kind: "audiobook", group: "album", grid: true },
  podcasts: { kind: "podcast" },
  videos: { kind: "video" },
};
const SORTS = ["artist", "added", "rating", "year"];
const VIDEO_SORTS = ["show", "title", "added", "year"]; // a video tab has no artists
const VIEW_KEY = "syncmusic.browseView"; // grid / list, remembered per browser

/** The tabs of a library type; a folder library shows the ones it holds anything for (`counts`): its
 * default kind and subfolders of other kinds (`subfolder_kinds`) decide, and it has no empty Playlists tab. */
function tabsFor(lib, counts) {
  if (lib.type === "audiobookshelf") return ["books", "podcasts"];
  if (lib.type === "folder") {
    const k = lib.settings.default_kind || "music";
    const base = k === "audiobook" ? ["books"] : k === "podcast" ? ["podcasts"] : k === "video" ? ["videos"] : ["songs", "albums", "artists"];
    const held = Object.keys(TABS).filter((x) => counts?.[x] > 0);
    return held.length ? held : base;
  }
  return ["songs", "albums", "artists", "playlists"];
}

async function renderBrowse(ctx, lib) {
  // "View as" one of the library's accounts: every list, count and cover is read as that user
  const asId = lib.accounts?.[ctx.query.get("as")] ? ctx.query.get("as") : "";
  browseAs = asId ? { account: asId } : {};
  let head = null;
  if (lib.type === "folder") {  // its tabs depend on what it holds
    try { head = await get(`/libraries/${encodeURIComponent(lib.id)}/items?${new URLSearchParams({ page_size: "1", ...browseAs })}`); } catch { head = null; }
  }
  const tabs = tabsFor(lib, head?.tab_counts);
  const tab = tabs.includes(ctx.query.get("tab")) ? ctx.query.get("tab") : tabs[0];
  const spec = TABS[tab];
  const q = ctx.query.get("q") || "";
  const sorts = spec.kind === "video" ? VIDEO_SORTS : SORTS;
  const sort = sorts.includes(ctx.query.get("sort")) ? ctx.query.get("sort") : sorts[0];
  const page = Number(ctx.query.get("page") || 0);
  const lid = encodeURIComponent(lib.id);
  let res = null, error = null, counts = null;
  try {
    if (spec.playlists) {
      const [all, head] = await Promise.all([
        get(`/libraries/${lid}/playlists?${new URLSearchParams({ query: q, ...browseAs })}`),
        get(`/libraries/${lid}/items?${new URLSearchParams({ page_size: "1", ...browseAs })}`),
      ]);
      res = { total: all.length, items: all.slice(page * PAGE, (page + 1) * PAGE), updated_at: head.updated_at };
      counts = head.tab_counts;
    } else {
      const params = new URLSearchParams({ query: q, page: String(page), page_size: String(PAGE), kind: spec.kind, sort, ...browseAs });
      if (spec.group) params.set("group", spec.group);
      res = await get(`/libraries/${lid}/items?${params}`);
      counts = res.tab_counts;
    }
  } catch (e) { error = e; }
  const view = spec.grid && localStorage.getItem(VIEW_KEY) !== "list" ? "grid" : "list";
  ctx.state.browse = { tab, spec, rows: res ? res.items : [], lib };
  const pages = res ? Math.max(1, Math.ceil(res.total / PAGE)) : 1;
  const base = `#/libraries/${lid}/browse`;
  const link = (p, tb = tab) => `${base}?${new URLSearchParams({ tab: tb, q: tb === tab ? q : "", sort, page: String(p), ...(asId ? { as: asId } : {}) })}`;
  const viewAs = Object.keys(lib.accounts || {}).length ? await viewAsSelect(lib, asId) : "";
  const h = health.get(lib.id);
  // header: ← Libraries, the type icon, name, health + "Updated …", Refresh and Settings
  return html`<a class="backlink" href="#/libraries">${icon("left", { size: 16 })}${t("nav.libraries")}</a>
    <div class="pagehead lb-head"><span class="lib-ico">${typeIcon(lib.type, "library", 24)}</span>
      <div class="lb-title"><h1>${lib.name}</h1>
        <div class="lb-meta" data-browse-updated><span data-health="${lib.id}">${healthBadge(lib)}</span><span data-updated="${lib.id}">${updatedText(res?.updated_at || lib.updated_at)}</span></div></div>
      <div class="actions">${refreshButton(lib)}<a class="btn" data-lib-settings href="#/libraries/${lid}?back=browse">${icon("settings", { size: 16 })}${t("libs.settings")}</a></div></div>
    ${lib.enabled !== false && h && !h.ok ? html`<div class="lib-problem lb-problem" data-browse-problem>${icon("xcircle", { size: 17 })}<span>${h.text}${res?.updated_at ? ` ${t("libs.browse_cached")}` : ""}</span>
      <button class="btn small" data-action="refresh" data-id="${lib.id}" data-name="${lib.name}">${t("libs.try_again")}</button></div>` : ""}
    <div class="tabs" role="tablist">${tabs.map((x) => html`<a class="tab" role="tab" href="${link(0, x)}" aria-selected="${x === tab}" data-tab="${x}">${t(`libs.tab.${x}`)}${counts && counts[x] !== undefined ? html` <span class="tab-n">${counts[x].toLocaleString()}</span>` : ""}</a>`)}</div>
    <div class="lb-tools">${viewAs}
      <form class="lb-search" data-action="search" role="search">
        <label class="searchfield">${icon("search", { size: 16 })}<span class="sr">${t("libs.search")}</span>
          <input id="q" type="search" value="${q}" placeholder="${t("libs.search_in", { tab: t(`libs.tab.${tab}`).toLowerCase() })}"></label>
        <button class="btn" type="submit">${t("libs.search_btn")}</button></form>
      ${spec.playlists ? "" : html`<label class="sr" for="sort">${t("libs.sort")}</label><select id="sort" class="lb-sort" data-change="sort">${sorts.map((x) => html`<option value="${x}" ${x === sort ? "selected" : ""}>${t(`libs.sort.${x}`)}</option>`)}</select>`}
      ${spec.grid ? html`<div class="seg" role="group" aria-label="${t("libs.view")}">
        <button type="button" class="seg-btn" data-action="setView" data-view="grid" aria-pressed="${view === "grid"}" aria-label="${t("libs.view_grid")}" title="${t("libs.view_grid")}">${icon("grid", { size: 16 })}</button>
        <button type="button" class="seg-btn" data-action="setView" data-view="list" aria-pressed="${view === "list"}" aria-label="${t("libs.view_list")}" title="${t("libs.view_list")}">${icon("list", { size: 16 })}</button></div>` : ""}
    </div>
    ${error ? errorBox(error) : !res.items.length ? html`<div class="lb-empty" data-browse-empty>${q ? t("libs.no_match", { q, tab: t(`libs.tab.${tab}`).toLowerCase() }) : t("libs.tab_empty")}</div>` : html`
    <div class="lb-count"><b>${t("count.item", { n: res.total })}</b>
      <label class="checkrow"><input type="checkbox" data-change="selectAll"> ${t("libs.select_all")}</label></div>
    ${view === "grid" ? grid(spec, res.items, lid) : list(spec, res.items, lid)}
    ${pages > 1 ? html`<div class="lb-pager">
      <a class="btn small" ${page > 0 ? html`href="${link(page - 1)}"` : html`aria-disabled="true"`}>${t("libs.prev")}</a>
      <span class="muted">${t("libs.page", { page: page + 1, pages })}</span>
      <a class="btn small" ${page + 1 < pages ? html`href="${link(page + 1)}"` : html`aria-disabled="true"`}>${t("libs.next")}</a></div>` : ""}`}
    <div class="selbar" data-selbar hidden><span class="selbar-n" data-sel-count></span>
      <button type="button" class="btn ghost" data-action="clearSel">${t("libs.clear")}</button>
      <button type="button" class="btn primary" data-action="openAdd">${icon("plus", { size: 16, width: 2.2 })}${t("libs.add_to")}</button></div>`;
}

const coverUrl = (lid, row, large = false) => (row.cover_item ? `/api/v1/libraries/${lid}/thumb?${new URLSearchParams({ item: row.cover_item, ...(large ? { large: "true" } : {}), ...browseAs })}` : null);

let browseAs = {}; // the browser's "View as" account as query parameters ({} = the main account)

// "View as": the main account or one of the library's accounts, each with the devices that sign in as it
async function viewAsSelect(lib, current) {
  const users = await accountUsers(lib.id).catch(() => ({}));
  const label = (aid, acc) => `${acc.name}${users[aid]?.length ? ` - ${users[aid].join(", ")}` : ""}`;
  return html`<label class="lb-as"><span>${t("libs.view_as")}</span><select class="lb-sort" data-change="viewAs" data-view-as>
    <option value="" ${current ? "" : "selected"}>${t("libs.account_main")}</option>
    ${Object.entries(lib.accounts).map(([aid, acc]) => html`<option value="${aid}" ${aid === current ? "selected" : ""}>${label(aid, acc)}</option>`)}</select></label>`;
}

// a row's or card's lines: title, a second line, and the meta on the right
function lines(spec, r) {
  if (spec.playlists) return { title: r.name, sub: `${t("count.track", { n: r.items })}${r.smart ? ` · ${t("libs.smart")}` : ""}`, meta: bytes(r.size_bytes), ico: "playlist" };
  if (spec.group === "artist") return { title: r.name, sub: `${t("count.track", { n: r.items })} · ${bytes(r.size_bytes)}`, meta: bytes(r.size_bytes), ico: "artist" };
  if (spec.group) {
    const by = [r.artist, r.year].filter(Boolean).join(" · ");
    return { title: r.name, sub: by || t("count.track", { n: r.items }), meta: `${t(spec.kind === "audiobook" ? "count.file" : "count.track", { n: r.items })} · ${bytes(r.size_bytes)}`, ico: spec.kind === "audiobook" ? "book" : "album" };
  }
  const ep = r.episode != null ? `${r.season != null ? `S${String(r.season).padStart(2, "0")}` : ""}E${String(r.episode).padStart(2, "0")}` : "";
  const who = spec.kind === "video" ? r.series || r.artist || "" : r.artist || r.series || "";
  const where = spec.kind === "podcast" ? "" : spec.kind === "video" ? ep : r.album || "";
  const fmt = [r.format ? r.format.toUpperCase() : "", duration(r.duration_s)].filter(Boolean).join(" · ");
  return { title: r.title, sub: [who, where].filter(Boolean).join(" - "), meta: fmt || bytes(r.size_bytes), ico: spec.kind === "podcast" ? "podcast" : spec.kind === "audiobook" ? "book" : spec.kind === "video" ? "video" : "music" };
}

// the name in its own span so a long one ends in "…"; a group partly on a device: "Name · 7/8"
const onChips = (r) => [
  ...(r.on_devices || []).map((d) => html`<span class="ondev" title="${t("libs.on_device", { name: d })}">${icon("devices", { size: 12 })}<span class="ondev-name">${d}</span></span>`),
  ...(r.on_devices_partly || []).map((d) => html`<span class="ondev partly" data-partly title="${t("libs.on_device_partly", { name: d.name, n: d.items, of: r.items })}">${icon("devices", { size: 12 })}<span class="ondev-name">${d.name}</span><span class="ondev-n">${d.items}/${r.items}</span></span>`),
];

function grid(spec, rows, lid) {
  return html`<div class="lb-grid" data-browse-grid>${rows.map((r, i) => {
    const l = lines(spec, r);
    const src = coverUrl(lid, r, true);
    return html`<div class="bcard" data-row="${i}">
      <label class="bcard-art">${src ? html`<img src="${src}" alt="" loading="lazy" decoding="async">` : ""}<span class="bcard-ph" aria-hidden="true">${icon(l.ico, { size: 34 })}</span>
        <input type="checkbox" name="sel" value="${i}" aria-label="${l.title}" data-change="pick"><span class="bcard-box" aria-hidden="true">${icon("check", { size: 15, width: 2.8 })}</span></label>
      <div class="bcard-text"><span class="bcard-title" title="${l.title}">${l.title}</span><span class="bcard-sub" title="${l.sub}">${l.sub}</span></div>
      ${(r.on_devices?.length || r.on_devices_partly?.length) ? html`<div class="ondevs">${onChips(r)}</div>` : ""}</div>`;
  })}</div>`;
}

function list(spec, rows, lid) {
  return html`<div class="lb-list" data-browse-list>${rows.map((r, i) => {
    const l = lines(spec, r);
    return html`<label class="lrow" data-row="${i}"><input type="checkbox" name="sel" value="${i}" aria-label="${l.title}" data-change="pick">
      ${thumb(coverUrl(lid, r), l.ico)}
      <span class="lrow-text"><span class="lrow-title" title="${l.title}">${l.title}</span><span class="lrow-sub" title="${l.sub}">${l.sub}</span>
        ${(r.on_devices?.length || r.on_devices_partly?.length) ? html`<span class="ondevs lrow-on">${onChips(r)}</span>` : ""}</span>
      <span class="lrow-meta">${l.meta}</span></label>`;
  })}</div>`;
}

/** The selected rows as manual-selection ids plus the exact files they stand for (albums, artists, books and
 * playlists expanded from the index). */
async function expandSelection(ctx) {
  const { spec, rows } = ctx.state.browse;
  const lid = encodeURIComponent(ctx.params.id);
  const picked = [...document.querySelectorAll('input[name="sel"]:checked')].map((x) => rows[Number(x.value)]);
  const ids = { track_id: new Set(), album_id: new Set(), artist_id: new Set() };
  const files = new Map();
  const tracksOf = async (params) => (await get(`/libraries/${lid}/items?${new URLSearchParams({ ...params, page_size: "500", ...browseAs })}`)).items;
  for (const r of picked) {
    let tracks;
    if (spec.playlists) tracks = await tracksOf({ playlist_id: r.id });
    else if (!spec.group) tracks = [r];
    else if (r.field === "album_id") tracks = await tracksOf({ album_id: r.id });
    else if (r.field === "artist_id") tracks = await tracksOf({ artist_id: r.id });
    else tracks = await tracksOf({ series: r.id.replace(/^series:/, "") });
    if (spec.group && r.field !== "track_id") ids[r.field].add(r.id); // the album / artist keeps syncing as a whole
    else tracks.forEach((x) => ids.track_id.add(x.item_id));
    tracks.forEach((x) => files.set(x.item_id, x));
  }
  return { ids, files: [...files.values()], picked: picked.length };
}

// the sticky bar follows the ticked boxes
function selectionChanged(root = document) {
  const n = root.querySelectorAll('input[name="sel"]:checked').length;
  const bar = root.querySelector("[data-selbar]");
  if (!bar) return;
  bar.hidden = !n;
  bar.querySelector("[data-sel-count]").textContent = t("libs.selected", { n });
  const all = root.querySelector('[data-change="selectAll"]');
  const boxes = root.querySelectorAll('input[name="sel"]').length;
  if (all) { all.checked = n > 0 && n === boxes; all.indeterminate = n > 0 && n < boxes; }
}

/** "Add N items to…": a device (its own template) or a template of this library; the include rule is added at once
 * and the toast offers Undo (which takes out only what was new). Rockbox players get music only. */
async function addDialog(ctx) {
  const { lib } = ctx.state.browse;
  const [devs, templates, types, libTypes] = await Promise.all([get("/devices"), get("/templates"), get("/device-types"), get("/library-types")]);
  const kinds = libTypes.find((x) => x.type === lib.type)?.capabilities.kinds || ["music"];
  const music = lib.type === "folder" ? (lib.settings.default_kind || "music") === "music" : kinds.includes("music");
  const musicOnly = new Set(types.filter((x) => x.music_only).map((x) => x.type));
  const used = (id) => devs.registered.filter((d) => (d.template_ids || []).includes(id)).length;
  const targets = devs.registered.map((d) => ({ kind: "device", id: d.id, name: d.name, icon: d.type, sub: musicOnly.has(d.type) && !music ? t("libs.music_only") : typeLabel(d.type), off: musicOnly.has(d.type) && !music }));
  const tpls = templates.filter((x) => !x.id.startsWith("own-") && !x.builtin && (!x.library_ids.length || x.library_ids.includes(lib.id)))
    .map((x) => ({ kind: "template", id: x.id, name: x.name, icon: "templates", sub: t("libs.used_by", { n: used(x.id) }), off: false }));
  const first = targets.find((x) => !x.off) || tpls[0];
  const n = document.querySelectorAll('input[name="sel"]:checked').length;
  const option = (x) => html`<label class="target${x.off ? " off" : ""}" data-target="${x.kind}:${x.id}"><input type="radio" name="addTarget" value="${x.kind}:${x.id}" ${x === first ? "checked" : ""} ${x.off ? "disabled" : ""}>
    <span class="target-ico">${x.kind === "device" ? typeIcon(x.icon, "device", 18) : icon("templates", { size: 18 })}</span>
    <span class="target-text"><b>${x.name}</b><small>${x.sub}</small></span></label>`;
  const body = html`<p class="muted" style="margin:0 0 12px">${t("libs.add_text")}</p>
    <div class="target-group">${t("libs.add_devices")}</div>
    ${targets.length ? targets.map(option) : html`<p class="muted">${t("libs.no_devices")}</p>`}
    <div class="target-group">${t("libs.add_templates")}</div>
    ${tpls.length ? tpls.map(option) : html`<p class="muted">${t("libs.no_templates")}</p>`}`;
  let picked = null;
  const ok = await dialog({
    title: t("libs.add_to_title", { n }), body,
    buttons: [{ label: t("common.cancel"), value: false }, { label: t("libs.add_rule"), value: true, kind: "primary" }],
    onButton: (value, root) => {
      if (!value) return true;
      picked = root.querySelector('input[name="addTarget"]:checked')?.value || null;
      if (!picked) { toast(t("libs.pick_target"), "error"); return false; }
      return true;
    },
  });
  if (!ok || !picked) return;
  const [kind, id] = [picked.slice(0, picked.indexOf(":")), picked.slice(picked.indexOf(":") + 1)];
  const target = [...targets, ...tpls].find((x) => x.kind === kind && x.id === id);
  const { ids, files } = await expandSelection(ctx);
  const path = `/${kind === "device" ? "devices" : "templates"}/${encodeURIComponent(id)}/manual-selection`;
  const added = [];
  for (const field of ["album_id", "artist_id", "track_id"]) {
    if (!ids[field].size) continue;
    const r = await post(path, { library_id: lib.id, ids: [...ids[field]], field });
    if (r.added?.length) added.push([field, r.added]);
  }
  clearSelection();
  // a template promises a plan only to the devices that use it
  const users = kind === "template" ? used(id) : 1;
  const text = !users ? t("libs.rule_added_unused", { name: target.name })
    : kind === "template" ? t("libs.rule_added_tpl", { name: target.name, n: t("count.file", { n: files.length }), d: t("count.device", { n: users }) })
    : t("libs.rule_added", { name: target.name, n: t("count.file", { n: files.length }) });
  toast(text, "ok", {
    undo: added.length ? async () => {
      for (const [field, list] of added) await post(path, { library_id: lib.id, ids: list, field, remove: true });
      toast(t("libs.rule_removed"));
    } : null,
  });
}

function clearSelection() {
  document.querySelectorAll('input[name="sel"]').forEach((x) => { x.checked = false; });
  selectionChanged();
}

function duration(s) {
  if (!s) return "";
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return `${m}:${String(r).padStart(2, "0")}`;
}

// --- actions --------------------------------------------------------------------------------------------------------

// the folder source switch and the guest login of `settingsForm`
export const sourceActions = {
  ...pickerActions,
  libSource(el) {
    const form = el.closest("form");
    const remote = el.value !== "local";
    form.querySelector('[data-src="local"]').hidden = remote;
    form.querySelector('[data-src="remote"]').hidden = !remote;
    if (!remote) return;
    const kind = el.value === "ftp" ? "ftp" : "smb";
    const address = form.querySelector("#libAddress");
    address.placeholder = t(`libs.address_ph.${kind}`);
    if (address.value && !address.value.startsWith(kind)) address.value = "";
    form.querySelector("[data-address-help]").textContent = t(`libs.address_help.${kind}`);
    form.querySelector("[data-guest-label]").textContent = t(kind === "ftp" ? "libs.anonymous" : "libs.guest");
    form.querySelector("[data-shares-box]").hidden = kind !== "smb";
    setHtml(form.querySelector("[data-found]"), "");
    setHtml(form.querySelector("[data-shares]"), "");
  },
  libGuest(el) {
    el.closest("form").querySelector("[data-login]").hidden = el.checked;
  },
  // network discovery, only when asked: servers of the chosen kind, then an SMB server's shares
  async libFind(el) {
    const form = el.closest("form");
    const kind = sourceKind(form);
    const box = form.querySelector("[data-found]");
    el.disabled = true;
    setHtml(box, html`<p class="hint" role="status">${t("libs.finding")}</p>`);
    try {
      const servers = await post("/discovery/servers", { kinds: [kind] });
      setHtml(box, servers.length ? html`<p class="hint">${t("libs.found_title")}</p><div class="found-list">${servers.map((s) =>
        html`<button type="button" class="found-row" data-action="libPickServer" data-host="${s.host}" data-port="${s.port}">
          ${icon(kind === "smb" ? "smb" : "ftpsrc", { size: 16 })}<b>${s.name}</b><small class="mono">${s.host === s.address ? s.address : `${s.host} · ${s.address}`}${s.port !== DEFAULT_PORT[kind] ? `:${s.port}` : ""}</small></button>`)}</div>`
        : html`<p class="hint">${t("libs.found_none", { kind: kind.toUpperCase() })}</p>`);
    } catch (e) {
      setHtml(box, errorBox(e, ""));
    } finally {
      el.disabled = false;
    }
  },
  libPickServer(el) {
    const form = el.closest("form");
    const kind = sourceKind(form);
    const port = Number(el.dataset.port) !== DEFAULT_PORT[kind] ? `:${el.dataset.port}` : "";
    const address = form.querySelector("#libAddress");
    address.value = `${kind}://${el.dataset.host}${port}/`;
    form.querySelectorAll("[data-found] .found-row").forEach((b) => b.setAttribute("aria-pressed", String(b === el)));
    address.focus();
  },
  async libShares(el) {
    const form = el.closest("form");
    const box = form.querySelector("[data-shares]");
    let url = null;
    try { url = new URL(form.querySelector("#libAddress").value.trim()); } catch { url = null; }
    if (!url || !url.hostname) { setHtml(box, html`<p class="hint warn">${t("libs.shares_need_host")}</p>`); return; }
    const guestOn = form.querySelector("#libGuest").checked;
    const login = guestOn ? {} : {
      username: form.querySelector('[data-login] [id$="-src-username"]')?.value || "",
      password: form.querySelector('[data-login] [id$="-src-password"]')?.value || "",
    };
    el.disabled = true;
    setHtml(box, html`<p class="hint" role="status">${t("libs.shares_loading")}</p>`);
    try {
      const shares = await post("/discovery/shares", { host: url.hostname, port: url.port ? Number(url.port) : 445, ...login });
      setHtml(box, shares.length ? html`<p class="hint">${t("libs.shares_title", { host: url.hostname })}</p><div class="found-list">${shares.map((s) =>
        html`<button type="button" class="found-row" data-action="libPickShare" data-share="${s.name}">${icon("folder", { size: 16 })}<b>${s.name}</b>${s.comment ? html`<small>${s.comment}</small>` : ""}</button>`)}</div>`
        : html`<p class="hint">${t("libs.shares_none")}</p>`);
    } catch (e) {
      setHtml(box, errorBox(e, ""));
    } finally {
      el.disabled = false;
    }
  },
  libPickShare(el) {
    const form = el.closest("form");
    const address = form.querySelector("#libAddress");
    const url = new URL(address.value.trim());
    address.value = `smb://${url.host}/${encodeURIComponent(el.dataset.share)}/`;
    form.querySelectorAll("[data-shares] .found-row").forEach((b) => b.setAttribute("aria-pressed", String(b === el)));
  },
};

const DEFAULT_PORT = { smb: 445, ftp: 21 };
const sourceKind = (form) => (form.querySelector('input[name="libSource"]:checked')?.value === "ftp" ? "ftp" : "smb");

export const actions = {
  ...sourceActions,
  async addAccount(el, ctx) {
    const tp = ctx.state.types.find((x) => x.type === ctx.state.lib.type);
    const r = await accountDialog(ctx.state.lib, tp);
    if (r) { ctx.state.lib = r.lib; ctx.rerender(); }
  },
  async editAccount(el, ctx) {
    const tp = ctx.state.types.find((x) => x.type === ctx.state.lib.type);
    const r = await accountDialog(ctx.state.lib, tp, el.dataset.account);
    if (r) { ctx.state.lib = r.lib; ctx.rerender(); }
  },
  async removeAccount(el, ctx) {
    const lib = ctx.state.lib, aid = el.dataset.account, acc = lib.accounts[aid];
    const users = (await accountUsers(lib.id))[aid] || [];
    if (users.length) { toast(t("libs.account_in_use", { names: users.join(", ") }), "error"); return; }
    if (!(await confirm({ title: t("libs.account_remove_title", { name: acc.name }), message: t("libs.account_remove_text"), ok: t("libs.account_remove"), danger: true }))) return;
    const { [aid]: _gone, ...rest } = lib.accounts;
    ctx.state.lib = await patch(`/libraries/${encodeURIComponent(lib.id)}`, { accounts: rest });
    toast(t("libs.account_removed", { name: acc.name }));
    ctx.rerender();
  },
  async add(form, ctx) {
    const settings = readSettings(ctx, form);
    if (!settings) return;
    const name = form.querySelector("#libName").value.trim();
    const existing = new Set((await get("/libraries")).map((l) => l.id));
    let id = slug(name), n = 2;
    while (existing.has(id)) id = `${slug(name)}-${n++}`;
    form.querySelector('button[type="submit"]').disabled = true;
    let lib;
    try {
      lib = await post("/libraries", { id, type: ctx.state.type, name, settings });
    } finally { form.querySelector('button[type="submit"]').disabled = false; }
    librariesChanged();
    const h = await checkLibrary(lib.id);
    if (!h.ok) {
      toast(t("libs.added_bad", { name, error: h.text }), "error");
      goFrom(ctx, `/libraries/${encodeURIComponent(lib.id)}`);
      return;
    }
    toast(t("libs.added", { name }));
    // "Save and scan": the first refresh starts now; the page shows what was created
    const job = await post(`/libraries/${encodeURIComponent(lib.id)}/refresh`).catch(() => null);
    if (job) track(job, name, ctx, false);
    const templates = (await get("/templates")).filter((x) => x.default_of === lib.id);
    setHtml(document.getElementById("content"), savedPanel(lib, h, templates));
  },
  async save(form, ctx) {
    const settings = readSettings(ctx, form);
    if (!settings) return;
    const lib = ctx.state.lib;
    ctx.state.lib = await patch(`/libraries/${encodeURIComponent(lib.id)}`, {
      name: form.querySelector("#libName").value.trim(), enabled: form.querySelector("#libEnabled").checked, settings,
    });
    forgetHealth(lib.id);
    librariesChanged();
    toast(t("libs.saved"));
    if (ctx.query.get("back") === "browse") goFrom(ctx, back(ctx, lib).slice(1)); // opened from the browser
  },
  async checkOne(el, ctx) {
    // the values in the form, saved or not; the list's status stays that of the saved settings
    const settings = readSettings(ctx, el.closest("form"));
    if (!settings) return;
    const out = document.getElementById("libCheck");
    el.disabled = true;
    setHtml(out, html`<span class="test-wait">${icon("sync", { size: 15, spin: true })}${t("libs.connecting")}</span>`);
    let h;
    try {
      const r = await post(`/libraries/${encodeURIComponent(ctx.state.lib.id)}/check`, { settings });
      h = { ok: !!r.ok, text: r.message || t("status.failed") };
    } catch (e) {
      h = { ok: false, text: errorMessage(e) };
    } finally { el.disabled = false; }
    setHtml(out, html`<span class="test-${h.ok ? "ok" : "fail"}" role="${h.ok ? "status" : "alert"}">${icon(h.ok ? "ccheck" : "xcircle", { size: 16 })}${h.ok ? t("libs.check_ok") : h.text}</span>`);
  },
  pickType(el) {
    go(`/libraries/new?type=${encodeURIComponent(el.dataset.libType)}`);
  },
  async refresh(el, ctx) {
    // reloads the library now (a job); the button says so until it is done, then the page shows the new data
    el.disabled = true;
    if (!("iconOnly" in el.dataset)) el.querySelector("span").textContent = t("libs.refreshing_btn");
    const name = el.dataset.name;
    toast(t("libs.refreshing", { name }));
    let job;
    try {
      job = await post(`/libraries/${encodeURIComponent(el.dataset.id)}/refresh`);
    } catch (e) {
      el.disabled = false;
      if (!("iconOnly" in el.dataset)) el.querySelector("span").textContent = t("libs.refresh");
      throw e;
    }
    await track(job, name, ctx);
  },
  async refreshAll(el, ctx) {
    // every enabled library reloads now (one job each); the cards show their progress
    el.disabled = true;
    const libs = (ctx.state.libs || []).filter((l) => l.enabled !== false);
    const started = await Promise.allSettled(libs.map((l) => post(`/libraries/${encodeURIComponent(l.id)}/refresh`).then((job) => track(job, l.name, ctx, false))));
    const failed = started.filter((r) => r.status === "rejected");
    toast(t("libs.refreshing_all", { n: libs.length - failed.length }));
    if (failed.length) toast(errorMessage(failed[0].reason), "error");
    ctx.rerender();
  },
  async remove(el, ctx) {
    const lib = ctx.state.lib;
    // its untouched default templates go with it; edited ones: the user chooses
    const edited = (await get("/templates")).filter((x) => x.default_of === lib.id && x.edited);
    let query = "";
    if (edited.length) {
      const choice = await dialog({
        title: t("libs.remove_title", { name: lib.name }),
        message: `${t("libs.remove_text")} ${t("libs.remove_edited", { names: edited.map((x) => `“${x.name}”`).join(", ") })}`,
        buttons: [{ label: t("common.cancel"), value: null }, { label: t("libs.remove_keep"), value: "keep" }, { label: t("libs.remove_delete"), value: "delete", kind: "danger" }],
      });
      if (!choice) return;
      query = `?edited_defaults=${choice}`;
    }
    // the devices whose templates draw from it are named; the exact name must be typed
    const users = await devicesUsing(lib.id);
    const body = users.length ? html`<p data-remove-users>${t("libs.remove_users", { names: users.join(", ") })}</p>` : "";
    if (!(await typedConfirm({ title: t("libs.remove_title", { name: lib.name }), message: t("libs.remove_text"), name: lib.name, ok: t("libs.remove").replace("…", ""), body }))) return;
    await del(`/libraries/${encodeURIComponent(lib.id)}${query}`);
    librariesChanged();
    toast(t("libs.removed", { name: lib.name }));
    goFrom(ctx, "/libraries");
  },
  search(form, ctx) {
    const q = form.querySelector("#q").value.trim();
    browseTo(ctx, { q, page: "0" });
  },
  sort(el, ctx) {
    browseTo(ctx, { sort: el.value, page: "0" });
  },
  viewAs(el, ctx) {
    browseTo(ctx, { as: el.value, page: "0" });
  },
  setView(el, ctx) {
    localStorage.setItem(VIEW_KEY, el.dataset.view);
    ctx.rerender();
  },
  selectAll(el) {
    document.querySelectorAll('input[name="sel"]').forEach((x) => { x.checked = el.checked; });
    selectionChanged();
  },
  pick() {
    selectionChanged();
  },
  clearSel() {
    clearSelection();
  },
  async openAdd(el, ctx) {
    el.disabled = true;
    try { await addDialog(ctx); } finally { el.disabled = false; }
  },
};

// the browser with some of its query changed (tab, search, sort, page)
function browseTo(ctx, changes) {
  const params = new URLSearchParams(ctx.query);
  params.set("tab", ctx.state.browse.tab);
  for (const [k, v] of Object.entries(changes)) params.set(k, v);
  go(`/libraries/${encodeURIComponent(ctx.params.id)}/browse?${params}`);
}

// names of the devices whose templates draw from the library (the device list derives them)
async function devicesUsing(libraryId) {
  return (await get("/devices")).registered.filter((d) => (d.libraries || []).includes(libraryId)).map((d) => d.name);
}

// refresh jobs started here → a toast when they finish (job.finished carries no result: fetch it)
const ctxJobs = new Map();

// remember a started refresh; its job.finished may have arrived before the POST returned (a small library on a
// busy host), so look once: a job already done is reported now instead of keeping its Refresh disabled for good
async function track(job, name, ctx, redraw = true) {
  ctxJobs.set(job.id, name);
  const now = await get(`/jobs/${encodeURIComponent(job.id)}`).catch(() => job);
  if (!["queued", "running"].includes(now.status)) {
    report(now);
    if (redraw && !editing(ctx)) ctx.rerender();
  }
}

function report(j) {
  if (!ctxJobs.has(j.id)) return; // reported once, by whichever comes first
  const name = ctxJobs.get(j.id);
  ctxJobs.delete(j.id);
  if (j.status === "succeeded") toast(t("libs.refreshed", { name, tracks: t("count.track", { n: j.result.tracks }), playlists: t("count.playlist", { n: j.result.playlists }) }));
  else toast(`${name}: ${j.error || j.status}`, "error");
}

export function onEvent(ev, ctx, root) {
  if (ev.type === "job.finished" && ctxJobs.has(ev.data.job_id)) {
    get(`/jobs/${encodeURIComponent(ev.data.job_id)}`).then((j) => { report(j); if (!editing(ctx)) ctx.rerender(); });
  } else if (ctx.path === "/libraries") {
    // the list follows refresh jobs: a card gets its scan bar when one starts and loses it when it ends
    const scan = ev.type === "job.progress" && root.querySelector(`[data-scan="${CSS.escape(ev.data.job_id)}"]`);
    if (scan) {
      const pct = Math.round((ev.data.progress || 0) * 100);
      scan.querySelector(".bar i").style.width = `${pct}%`;
      scan.querySelector(".bar").setAttribute("aria-valuenow", String(pct));
      scan.querySelector("[data-scan-text]").textContent = scanText(ev.data.progress, ev.data.eta_s);
    } else if (["job.created", "job.finished"].includes(ev.type) && ev.data.kind === "refresh") ctx.rerender();
  }
  // a background reload brought new data: the list / an untouched browser redraws, a browser with a
  // selection offers it instead of wiping the selection
  if (ev.type === "library.updated" && !editing(ctx)) {
    const id = ev.data.library_id;
    const browsing = ctx.path.endsWith("/browse") && ctx.params.id === id;
    if (ctx.path === "/libraries") ctx.rerender();
    else if (browsing && !root.querySelector('input[name="sel"]:checked')) ctx.rerender();
    else if (browsing && !root.querySelector("[data-new-data]")) {
      const name = ctx.state.lib?.name || id;
      root.querySelector(".lb-head")?.insertAdjacentHTML("afterend", String(html`<div class="callout" data-new-data>${t("libs.changed", { name })} <button class="btn small" data-action="retry">${t("libs.show_new")}</button></div>`));
    }
  }
}

// the add / edit forms are never redrawn under the user
const editing = (ctx) => ctx.path === "/libraries/new" || (ctx.params.id && !ctx.path.endsWith("/browse"));
