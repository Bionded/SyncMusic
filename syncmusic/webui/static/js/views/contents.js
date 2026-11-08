// Player contents: everything on the device - also what other tools put there - browsed like a
// library: Tracks / Albums / Artists / Playlists / Audiobooks / Podcasts with counts (kinds the device cannot hold
// are hidden unless something of that kind is on it), searchable, "Only not from SyncMusic", paged. Albums,
// audiobooks and podcasts are a cover grid; artists and playlists
// list their tracks. Names and tags come from the device itself (iPod: its iTunesDB, files: their tags).
// Ticking an album / artist / playlist ticks its items; the selection bar's "Remove from device…" deletes them (a
// sync job) and - ticked by default - excludes them in the device's own template so the next sync does not bring
// them back. ▶ plays the file on the device.
import { html, bytes } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, post } from "../api.js";
import { badge, toast, dialog } from "../ui.js";
import { go } from "../router.js";
import { thumb, openJob } from "./common.js";
import { icon } from "../icons.js";

addStrings({
  "pc.search": "Search title, artist, album",
  "pc.search_video": "Search title, show",
  "pc.search_btn": "Search",
  "pc.count": "Items on the device: {n}",
  "pc.shown": "Shown: {shown} of {n}",
  "pc.empty": "There is nothing on this device yet.",
  "pc.none_found": "Nothing matches the search.",
  "pc.unavailable": "Connect the device to see what is on it.",
  "pc.unknown_artist": "Unknown artist",
  "pc.no_album": "No album",
  "pc.movies": "Movies",
  "pc.season": "Season {n}",
  "pc.tab_empty": "Nothing of this kind is on the device.",
  "pc.select_all": "Select all shown",
  "pc.select_group": "Select {name}",
  "pc.play": "Play {title}",
  "pc.play_failed": "Cannot play {title} in the browser.",
  "pc.manual": "Added by hand",
  "pc.orphan": "Orphan: source gone",
  "pc.other_instance": "Another installation",
  "pc.foreign": "Not from SyncMusic",
  "pc.missing": "File missing",
  "pc.remove": "Remove from device…",
  "pc.none_selected": "Select items to remove first.",
  "pc.selected": "{n} selected",
  "pc.clear": "Clear",
  "pc.managed": "Managed by",
  "pc.managed.all": "Anyone",
  "pc.managed.mine": "This installation",
  "pc.managed.others": "Other installations",
  "pc.managed.foreign": "Not from SyncMusic",
  "pc.read_only": "Managed by another installation: it removes this item",
  "pc.tracks_link": "{n} tracks",
  "pc.tracks_link_one": "{n} track",
  "pc.remove_title": "Remove {n} from {name}?",
  "pc.remove_text": "The files are deleted from {name} right away. Your libraries are not touched.",
  "pc.also_exclude": "Also exclude them from this device’s templates",
  "pc.also_exclude_sub": "Otherwise the next sync puts them back. Adds an exclude rule to this device only.",
  "pc.remove_ok": "Remove {n}",
  "pc.browse_by": "Browse by",
  "pc.view": "View",
  "pc.view_grid": "Grid",
  "pc.view_list": "List",
  "pc.sort": "Sort by",
  "pc.sort.default": "Artist",
  "pc.sort.album": "Album",
  "pc.sort.year": "Year",
  "pc.sort.added": "Recently added",
  "pc.sort.size": "Size",
  "pc.refresh": "Refresh",
  "pc.refresh_hint": "Read the player again (after changing it with another program while it is connected)",
  "pc.tab.tracks": "Tracks",
  "pc.tab.albums": "Albums",
  "pc.tab.artists": "Artists",
  "pc.tab.playlists": "Playlists",
  "pc.tab.audiobooks": "Audiobooks",
  "pc.tab.podcasts": "Podcasts",
  "pc.tab.videos": "Videos",
  "pc.col_title": "Title",
  "pc.col_artist": "Artist",
  "pc.col_album": "Album",
  "pc.col_length": "Length",
  "pc.col_size": "Size",
  "pc.prev": "Previous",
  "pc.next": "Next",
  "pc.page": "Page {page} of {pages}",
  "pc.no_playlists": "There are no playlists on this device.",
  "pc.position": "{at} of {length}",
  "pc.position_title": "Listened up to {at} (on the device, as of the last sync)",
  "pc.library_position": "library: {at}",
  "pc.no_bookmarks": "This device keeps no listening positions: where you stopped is not synced to it or back.",
});

const TRACKS_PER_PAGE = 100;
const GROUPS_PER_PAGE = 25;
const COVERS_PER_PAGE = 60;
const VIEW_KEY = "syncmusic.contentsView"; // grid / list per tab, remembered per browser
const GRID_TABS = ["albums", "audiobooks", "podcasts"]; // tabs that can show covers; albums default to the grid
const SORTS = ["default", "album", "year", "added", "size"];

function viewOf(tab) {
  if (!GRID_TABS.includes(tab)) return "list";
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(VIEW_KEY) || "{}"); } catch { /* a broken value: defaults */ }
  return saved[tab] || (tab === "albums" ? "grid" : "list"); // spoken word: each episode shows where it stands
}

// groups in the chosen order; "default" keeps the service's (album artist, album)
function sortGroups(groups, sort) {
  const sum = (g) => g.items.reduce((n, x) => n + (x.size || 0), 0);
  const year = (g) => Math.max(0, ...g.items.map((x) => x.year || 0));
  const added = (g) => g.items.reduce((m, x) => (x.synced_at && x.synced_at > m ? x.synced_at : m), "");
  const by = {
    album: (a, b) => a.name.localeCompare(b.name),
    year: (a, b) => year(b) - year(a) || a.name.localeCompare(b.name),
    added: (a, b) => added(b).localeCompare(added(a)),
    size: (a, b) => sum(b) - sum(a),
  }[sort];
  return by ? [...groups].sort(by) : groups;
}

// tab → which rows it lists and how they are grouped (null = a flat track list)
const TABS = {
  tracks: { kind: "music", group: null },
  albums: { kind: "music", group: "album" },
  artists: { kind: "music", group: "artist" },
  playlists: { kind: null, group: "playlist" },
  audiobooks: { kind: "audiobook", group: "album" },
  podcasts: { kind: "podcast", group: "album" },
  videos: { kind: "video", group: "show" }, // a show by season, movies apart
};

function tabsFor(caps, items) {
  const has = (kind) => items.some((i) => i.kind === kind);
  return Object.keys(TABS).filter((tab) => {
    if (tab === "playlists") return caps.supports_playlists !== false || items.some((i) => Object.keys(i.playlists || {}).length);
    if (tab === "audiobooks") return !!caps.supports_audiobooks || has("audiobook");
    if (tab === "podcasts") return !!caps.supports_podcasts || has("podcast");
    if (tab === "videos") return !!caps.supports_video || has("video");
    return true;
  });
}

const artistOf = (it) => it.album_artist || it.artist || t("pc.unknown_artist");

/** [{id, name, sub, items}] of the tab, in the service's order (kind, album artist, album, disc, track). */
function groupsOf(spec, items) {
  const groups = new Map();
  const add = (id, name, sub, it) => {
    if (!groups.has(id)) groups.set(id, { id, name, sub, items: [] });
    groups.get(id).items.push(it);
  };
  for (const it of items) {
    if (spec.group === "playlist") {
      for (const name of Object.keys(it.playlists || {})) add(`pl|${name}`, name, "", it);
    } else if (spec.group === "show") {
      if (!it.show) add("sh|", t("pc.movies"), "", it);
      else add(`sh|${it.show}|${it.season ?? ""}`, it.show, it.season != null ? t("pc.season", { n: it.season }) : "", it);
    } else if (spec.group === "artist") {
      add(`ar|${artistOf(it)}`, artistOf(it), "", it);
    } else {
      const album = it.album || t("pc.no_album");
      add(`al|${artistOf(it)}|${album}`, album, artistOf(it), it);
    }
  }
  const out = [...groups.values()];
  if (spec.group === "playlist") {
    out.sort((a, b) => a.name.localeCompare(b.name));
    for (const g of out) g.items.sort((a, b) => a.playlists[g.name] - b.playlists[g.name]);
  } else if (spec.group === "artist") {
    out.sort((a, b) => a.name.localeCompare(b.name));
  } else if (spec.group === "show") { // shows A–Z by season, movies last; episodes in order
    const season = (g) => g.items[0].season ?? 0;
    out.sort((a, b) => (a.id === "sh|") - (b.id === "sh|") || a.name.localeCompare(b.name) || season(a) - season(b));
    for (const g of out) g.items.sort((a, b) => (a.episode ?? 0) - (b.episode ?? 0) || a.title.localeCompare(b.title));
  }
  return out;
}

function length(s) {
  if (!s) return "";
  const total = Math.round(s), h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), r = total % 60;
  const ss = String(r).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

// a podcast/audiobook: where it stands, and the library's position when it moved elsewhere since
function lengthCell(it) {
  if (it.position_s == null || it.kind === "music") return length(it.duration_s);
  const at = length(it.position_s) || "0:00";
  const lib = it.library_position_s != null && Math.abs(it.library_position_s - it.position_s) >= 1
    ? html`<br><small class="muted">${t("pc.library_position", { at: length(it.library_position_s) || "0:00" })}</small>` : "";
  return html`<span style="white-space:nowrap" title="${t("pc.position_title", { at })}">${it.duration_s ? t("pc.position", { at, length: length(it.duration_s) }) : at}</span>${lib}`;
}

const marks = (it) => html`${it.manual ? html` <small class="muted">· ${t("pc.manual")}</small>` : ""}${it.orphan ? html` ${badge("warn", t("pc.orphan"))}` : ""}${it.other_instance ? html` ${badge("neutral", t("pc.other_instance"))}` : ""}${it.foreign ? html` ${badge("neutral", t("pc.foreign"))}` : ""}${it.missing ? html` ${badge("error", t("pc.missing"))}` : ""}`;

// the picture in the item's file; a missing file has none
const thumbUrl = (deviceId, it) => (it && !it.missing ? `/api/v1/devices/${encodeURIComponent(deviceId)}/contents/thumb?${new URLSearchParams({ key: it.key })}` : null);
const thumbOf = (deviceId, it) => thumb(thumbUrl(deviceId, it), it?.kind === "video" ? "video" : "music");
// "S01E02" / "E3" of a video episode
const episodeOf = (it) => (it.episode != null ? `${it.season != null ? `S${String(it.season).padStart(2, "0")}` : ""}E${String(it.episode).padStart(2, "0")}` : "");

// an album / audiobook / podcast as a cover in the grid: the cover is the group's checkbox; its
// items' checkboxes ride along hidden so a removal collects them like in the lists; "N tracks" opens them in Tracks
function coverCard(deviceId, g, tab) {
  const foreign = g.items.every((x) => x.foreign);
  const href = `#/devices/${encodeURIComponent(deviceId)}/contents?${new URLSearchParams({ tab: tab === "albums" ? "tracks" : tab, q: g.name })}`;
  const n = g.items.length;
  const src = thumbUrl(deviceId, g.items.find((x) => !x.missing));
  return html`<div class="pc-card" data-pc-group="${g.id}">
    <label class="pc-cover"><input type="checkbox" data-change="pcSelectGroup" value="${g.id}" aria-label="${t("pc.select_group", { name: g.name })}">
      <span class="pc-art">${src ? html`<img src="${src}" alt="" loading="lazy" decoding="async">` : ""}</span>
      ${foreign ? html`<span class="pc-foreign">${icon("info", { size: 12 })}${t("pc.foreign")}</span>` : ""}</label>
    ${g.items.map((it) => html`<input type="checkbox" name="pcItem" value="${it.key}" data-group="${g.id}" hidden ${it.other_instance ? "disabled" : ""}>`)}
    <b class="pc-title" title="${g.name}">${g.name}</b>
    <span class="pc-sub">${g.sub ? html`<span title="${g.sub}">${g.sub}</span> · ` : ""}<a href="${href}">${t(n === 1 ? "pc.tracks_link_one" : "pc.tracks_link", { n })}</a></span></div>`;
}

const MANAGED = ["all", "mine", "others", "foreign"];
const MANAGED_BY = { all: () => true, mine: (i) => !i.foreign && !i.other_instance, others: (i) => i.other_instance, foreign: (i) => i.foreign };

function trackRow(deviceId, it, group) {
  const ro = it.other_instance;
  return html`<tr data-key="${it.key}" ${ro ? html`data-read-only` : ""}><td><input type="checkbox" name="pcItem" data-change="pcPick" value="${it.key}" data-group="${group || ""}" aria-label="${it.title}" ${ro ? html`disabled title="${t("pc.read_only")}"` : ""}></td>
    <td class="thumb-cell">${thumbOf(deviceId, it)}</td>
    ${it.kind === "video" ? html`<td>${episodeOf(it) ? html`<span class="muted mono">${episodeOf(it)} </span>` : ""}${it.title}${marks(it)}</td>`
      : html`<td>${it.track_number ? html`<span class="muted mono">${it.track_number}. </span>` : ""}${it.title}${marks(it)}</td><td>${it.artist || ""}</td><td>${it.album || ""}</td>`}
    <td class="mono">${lengthCell(it)}</td><td class="mono pc-size" style="white-space:nowrap">${bytes(it.size)}</td>
    <td>${it.missing ? "" : html`<button type="button" class="btn small" data-action="pcPlay" data-key="${it.key}" data-title="${it.title}" aria-label="${t("pc.play", { title: it.title })}">${icon("play", { size: 14 })}</button>`}</td></tr>`;
}

// a video row has no artist/album: its show is the group, the episode leads the title (phone width)
const head = (video) => html`<thead><tr><th></th><th></th><th>${t("pc.col_title")}</th>${video ? "" : html`<th>${t("pc.col_artist")}</th><th>${t("pc.col_album")}</th>`}<th>${t("pc.col_length")}</th><th class="pc-size">${t("pc.col_size")}</th><th></th></tr></thead>`;

export async function renderContents(ctx) {
  const { cfg, live, type } = ctx.state;
  const idle = !!live?.connected && !live.busy && !live.ejecting && !live.ejected && !live.setting_clock && !live.no_medium;
  if (!idle) return html`<div class="card"><div class="card-body muted">${t("pc.unavailable")}</div></div>`;
  const items = await get(`/devices/${encodeURIComponent(cfg.id)}/contents`);
  const tabs = tabsFor(type?.capabilities || {}, items);
  const q = (ctx.query.get("q") || "").trim();
  // who manages an item: this installation, another one (read-only here) or nobody (not from SyncMusic)
  const managed = MANAGED.includes(ctx.query.get("managed")) ? ctx.query.get("managed") : ctx.query.get("foreign") === "1" ? "foreign" : "all";
  const onlyForeign = managed !== "all";
  const needle = q.toLocaleLowerCase();
  const pool = items.filter(MANAGED_BY[managed]);
  const ofKind = (sp) => pool.filter((i) => (sp.kind ? i.kind === sp.kind : true));
  // videos count as items: a show is not one "album"
  const countOf = (x) => (TABS[x].group && x !== "videos" ? groupsOf(TABS[x], ofKind(TABS[x])).length : ofKind(TABS[x]).length);
  // no tab asked for: the first one that lists anything (a video-only device opened on an empty Tracks)
  const tab = tabs.includes(ctx.query.get("tab")) ? ctx.query.get("tab") : tabs.find((x) => countOf(x) > 0) || tabs[0];
  const spec = TABS[tab];
  const ofTab = ofKind(spec);
  const covers = viewOf(tab) === "grid";
  const sort = SORTS.includes(ctx.query.get("sort")) ? ctx.query.get("sort") : "default";
  const sortable = spec.group && !["playlist", "show"].includes(spec.group);
  const shown = needle ? ofTab.filter((i) => [i.title, i.artist, i.album_artist, i.album, i.show].some((x) => (x || "").toLocaleLowerCase().includes(needle))) : ofTab;
  const base = `#/devices/${encodeURIComponent(cfg.id)}/contents`;
  const link = (params) => `${base}?${new URLSearchParams({ ...params, ...(onlyForeign ? { managed } : {}), ...(sort !== "default" && params.tab === tab ? { sort } : {}) })}`;
  let body, total, page;
  const perPage = covers ? COVERS_PER_PAGE : spec.group ? GROUPS_PER_PAGE : TRACKS_PER_PAGE;
  if (!spec.group) {
    total = shown.length;
    page = Math.min(Number(ctx.query.get("page") || 0), Math.max(0, Math.ceil(total / perPage) - 1));
    const rows = shown.slice(page * perPage, (page + 1) * perPage);
    body = rows.length ? html`<div class="card"><div class="table-wrap"><table class="table${spec.kind === "video" ? " pc-video" : ""}">${head(spec.kind === "video")}<tbody>${rows.map((it) => trackRow(cfg.id, it, ""))}</tbody></table></div></div>` : null;
  } else {
    const groups = sortable ? sortGroups(groupsOf(spec, shown), sort) : groupsOf(spec, shown);
    total = groups.length;
    page = Math.min(Number(ctx.query.get("page") || 0), Math.max(0, Math.ceil(total / perPage) - 1));
    const part = groups.slice(page * perPage, (page + 1) * perPage);
    body = part.length && covers ? html`<div class="pc-grid">${part.map((g) => coverCard(cfg.id, g, tab))}</div>`
      : part.length ? part.map((g) => html`<div class="card" style="margin-bottom:10px" data-pc-group="${g.id}"><div class="card-head">
        <label class="checkrow" style="margin:0"><input type="checkbox" data-change="pcSelectGroup" value="${g.id}" aria-label="${t("pc.select_group", { name: g.name })}">
          ${thumbOf(cfg.id, g.items.find((x) => !x.missing))}<span><b>${g.name}</b>${g.sub ? html` <span class="muted">- ${g.sub}</span>` : ""}</span></label>
        <span class="muted">${t("count.item", { n: g.items.length })}</span></div>
      <div class="table-wrap"><table class="table${spec.kind === "video" ? " pc-video" : ""}">${head(spec.kind === "video")}<tbody>${g.items.map((it) => trackRow(cfg.id, it, g.id))}</tbody></table></div></div>`) : null;
  }
  const pages = Math.max(1, Math.ceil(total / perPage));
  const searchKey = spec.kind === "video" ? "pc.search_video" : "pc.search";
  const params = (p) => ({ tab, ...(q ? { q } : {}), page: String(p) });
  const empty = !items.length ? t("pc.empty") : needle || onlyForeign ? t("pc.none_found") : tab === "playlists" ? t("pc.no_playlists") : t("pc.tab_empty");
  return html`<div class="tabs" role="tablist" aria-label="${t("pc.browse_by")}" style="margin-bottom:12px">${tabs.map((x) => html`<a class="tab" role="tab" href="${link({ tab: x, ...(q ? { q } : {}) })}" aria-selected="${x === tab}" data-pc-tab="${x}">${t(`pc.tab.${x}`)} <span class="pc-n">${countOf(x)}</span></a>`)}</div>
    <form class="toolbar" data-action="pcSearch" style="margin-bottom:12px">
      <label class="sr" for="pcQ">${t(searchKey)}</label>
      <input id="pcQ" type="search" value="${q}" placeholder="${t(searchKey)}" style="flex:1;min-width:200px;padding:9px;border-radius:9px;border:1px solid var(--line);background:var(--panel2)">
      <button class="btn" type="submit">${t("pc.search_btn")}</button>
      <label class="pc-sort"><span class="muted">${t("pc.managed")}</span>
        <select class="lb-sort" data-change="pcManaged" aria-label="${t("pc.managed")}">${MANAGED.map((x) => html`<option value="${x}" ${x === managed ? "selected" : ""}>${t(`pc.managed.${x}`)}</option>`)}</select></label></form>
    <div class="toolbar" style="margin-bottom:10px;justify-content:space-between">
      <span class="muted" data-pc-count>${needle || onlyForeign ? t("pc.shown", { shown: shown.length, n: items.length }) : t("pc.count", { n: items.length })}</span>
      <span class="toolbar" style="margin:0;gap:10px">${sortable ? html`<label class="pc-sort"><span class="muted">${t("pc.sort")}</span>
        <select class="lb-sort" data-change="pcSort" aria-label="${t("pc.sort")}">${SORTS.map((x) => html`<option value="${x}" ${x === sort ? "selected" : ""}>${t(`pc.sort.${x}`)}</option>`)}</select></label>` : ""}
      ${GRID_TABS.includes(tab) ? html`<span class="seg" role="group" aria-label="${t("pc.view")}">
        <button type="button" class="seg-btn" data-action="pcView" data-view="grid" aria-pressed="${covers}" aria-label="${t("pc.view_grid")}" title="${t("pc.view_grid")}">${icon("grid", { size: 16 })}</button>
        <button type="button" class="seg-btn" data-action="pcView" data-view="list" aria-pressed="${!covers}" aria-label="${t("pc.view_list")}" title="${t("pc.view_list")}">${icon("list", { size: 16 })}</button></span>` : ""}
      <button type="button" class="btn small ghost" data-action="pcRefresh" title="${t("pc.refresh_hint")}">${icon("sync", { size: 14 })}${t("pc.refresh")}</button>
      <label class="checkrow" style="margin:0"><input type="checkbox" data-change="pcSelectAll"> ${t("pc.select_all")}</label></span></div>
    ${(spec.kind === "audiobook" || spec.kind === "podcast") && !type?.capabilities?.supports_positions_read ? html`<p class="muted" data-pc-no-bookmarks style="margin:0 0 10px">${t("pc.no_bookmarks")}</p>` : ""}
    <audio id="pcAudio" controls preload="none" style="width:100%;margin-bottom:10px" hidden></audio>
    ${body || html`<div class="empty">${empty}</div>`}
    ${pages > 1 ? html`<div class="toolbar" style="margin-top:10px">
      <a class="btn small" ${page > 0 ? html`href="${link(params(page - 1))}"` : html`aria-disabled="true"`}>${icon("left", { size: 16 })}${t("pc.prev")}</a>
      <span class="muted">${t("pc.page", { page: page + 1, pages })}</span>
      <a class="btn small" ${page + 1 < pages ? html`href="${link(params(page + 1))}"` : html`aria-disabled="true"`}>${t("pc.next")}${icon("right", { size: 16 })}</a></div>` : ""}
    <div class="selbar" data-pc-selbar hidden><span style="flex:1;font-weight:650" data-pc-selected></span>
      <button type="button" class="btn ghost" data-action="pcClear">${t("pc.clear")}</button>
      <button type="button" class="btn danger" data-action="pcRemove">${t("pc.remove")}</button></div>`;
}

// the selection bar: how many items are ticked (an item in two playlists counts once)
function selectedKeys() {
  return [...new Set([...document.querySelectorAll('input[name="pcItem"]:checked')].map((x) => x.value))];
}

function updateSelbar() {
  const bar = document.querySelector("[data-pc-selbar]");
  if (!bar) return;
  const n = selectedKeys().length;
  bar.hidden = !n;
  bar.querySelector("[data-pc-selected]").textContent = t("pc.selected", { n });
}

// shift-click ticks the range from the last ticked box of the same kind
// (a checkbox's change event has no shiftKey: the click before it tells)
let lastBox = null, shiftClick = false;
document.addEventListener("click", (e) => { shiftClick = e.shiftKey; }, true);
function pickRange(el) {
  const sel = el.dataset.change === "pcSelectGroup" ? 'input[data-change="pcSelectGroup"]' : 'input[name="pcItem"][data-change="pcPick"]:not(:disabled)';
  const boxes = [...document.querySelectorAll(sel)];
  if (shiftClick && lastBox && lastBox !== el && boxes.includes(lastBox)) {
    const [a, b] = [boxes.indexOf(lastBox), boxes.indexOf(el)].sort((x, y) => x - y);
    for (const x of boxes.slice(a, b + 1)) {
      x.checked = el.checked;
      if (x.dataset.change === "pcSelectGroup") document.querySelectorAll(`input[name="pcItem"][data-group="${CSS.escape(x.value)}"]:not(:disabled)`).forEach((y) => { y.checked = el.checked; });
    }
  }
  lastBox = el;
}

export const contentsActions = {
  pcView(el, ctx) {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(VIEW_KEY) || "{}"); } catch { /* start over */ }
    const params = new URLSearchParams(ctx.query);
    const tab = document.querySelector('.tab[aria-selected="true"][data-pc-tab]')?.dataset.pcTab || params.get("tab");
    localStorage.setItem(VIEW_KEY, JSON.stringify({ ...saved, [tab]: el.dataset.view }));
    ctx.rerender();
  },
  pcSort(el, ctx) {
    const params = new URLSearchParams(ctx.query);
    params.delete("page");
    if (!params.get("tab")) params.set("tab", document.querySelector('.tab[aria-selected="true"][data-pc-tab]')?.dataset.pcTab || "albums");
    if (el.value === "default") params.delete("sort"); else params.set("sort", el.value);
    go(`/devices/${encodeURIComponent(ctx.params.id)}/contents?${params}`);
  },
  async pcRefresh(el, ctx) { // read the device again: the list is cached until its database changes
    el.disabled = true;
    await get(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/contents?refresh=true`);
    ctx.rerender();
  },
  pcSearch(form, ctx) {
    const q = form.querySelector("#pcQ").value.trim();
    const tab = ctx.query.get("tab");
    const managed = ctx.query.get("managed");
    const params = new URLSearchParams({ ...(tab ? { tab } : {}), ...(q ? { q } : {}), ...(managed ? { managed } : {}) });
    go(`/devices/${encodeURIComponent(ctx.params.id)}/contents${String(params) ? `?${params}` : ""}`);
  },
  pcManaged(el, ctx) {
    const params = new URLSearchParams(ctx.query);
    params.delete("page");
    params.delete("foreign");
    if (el.value !== "all") params.set("managed", el.value); else params.delete("managed");
    go(`/devices/${encodeURIComponent(ctx.params.id)}/contents?${params}`);
  },
  pcSelectGroup(el) {
    pickRange(el);
    document.querySelectorAll(`input[name="pcItem"][data-group="${CSS.escape(el.value)}"]:not(:disabled)`).forEach((x) => { x.checked = el.checked; });
    updateSelbar();
  },
  pcSelectAll(el) {
    document.querySelectorAll('input[name="pcItem"]:not(:disabled), input[data-change="pcSelectGroup"]').forEach((x) => { x.checked = el.checked; });
    updateSelbar();
  },
  pcPick(el) { pickRange(el); updateSelbar(); },
  pcClear() {
    document.querySelectorAll('input[name="pcItem"], input[data-change="pcSelectGroup"], input[data-change="pcSelectAll"]').forEach((x) => { x.checked = false; });
    updateSelbar();
  },
  pcPlay(el, ctx) {
    const audio = document.getElementById("pcAudio");
    audio.hidden = false;
    // a file the browser cannot play must say so, not do nothing; ALAC etc. arrive as MP3
    const title = el.dataset.title || el.dataset.key;
    audio.onerror = () => toast(t("pc.play_failed", { title }), "error");
    audio.src = `/api/v1/devices/${encodeURIComponent(ctx.params.id)}/contents/file?${new URLSearchParams({ key: el.dataset.key })}`;
    audio.play().catch(() => {}); // autoplay may be refused: the controls are there
  },
  async pcRemove(el, ctx) {
    const { cfg } = ctx.state;
    const keys = selectedKeys();
    if (!keys.length) { toast(t("pc.none_selected"), "error"); return; }
    const n = keys.length;
    let exclude = true;
    const ok = await dialog({
      title: t("pc.remove_title", { n, name: cfg.name }), message: t("pc.remove_text", { name: cfg.name }), tone: "danger",
      body: html`<label class="pc-exclude"><input type="checkbox" id="pcExclude" checked>
        <span><b>${t("pc.also_exclude")}</b><small>${t("pc.also_exclude_sub")}</small></span></label>`,
      buttons: [{ label: t("common.cancel"), value: false }, { label: t("pc.remove_ok", { n }), value: true, kind: "danger solid" }],
      onButton: (value, root) => { if (value) exclude = root.querySelector("#pcExclude").checked; },
    });
    if (ok !== true) return;
    await openJob(el, ctx, cfg.id, () => post(`/devices/${encodeURIComponent(cfg.id)}/contents/remove`, { keys, exclude }));
  },
};
