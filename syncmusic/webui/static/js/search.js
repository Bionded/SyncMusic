// Global search : the search button in the top bar and Ctrl K / ⌘K open an
// overlay with one field; results come grouped (artists, albums, tracks from the cached libraries, devices, templates,
// jobs). Arrow keys move, Enter opens the highlighted (the first) hit, Esc closes.
// Command palette: before the server's hits come pages and actions (sync, update, eject a device,
// refresh a library, sync all) matched fuzzily in the browser; the ones used last come first, also with no query.
import { html, $, $$, setHtml } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { get, post } from "./api.js";
import { icon } from "./icons.js";
import { go } from "./router.js";
import { toast } from "./ui.js";
import { NAV, syncAll, isRail, setRail } from "./sidebar.js";

addStrings({
  "search.label": "Search",
  "search.placeholder": "Search music, devices, templates, jobs…",
  "search.hint": "Type to search everything.",
  "search.none": "Nothing found for “{q}”.",
  "search.not_loaded": "Not searched yet (open the library once to load it): {names}",
  "search.group.artists": "Artists",
  "search.group.albums": "Albums",
  "search.group.tracks": "Tracks",
  "search.group.videos": "Videos",
  "search.group.books": "Audiobooks",
  "search.group.podcasts": "Podcasts",
  "search.group.devices": "Devices",
  "search.group.templates": "Templates",
  "search.group.jobs": "Jobs",
  "search.group.recent": "Recent",
  "search.group.pages": "Pages",
  "search.group.actions": "Actions",
  "search.group.settings": "Settings",
  "search.hint_palette": "Type to search everything, or pick a page or an action.",
  "search.open": "Open {name}",
  "search.act.sync": "Sync {name}",
  "search.act.sync_sub": "Review the plan, then apply",
  "search.act.update": "Update {name} now",
  "search.act.eject": "Eject {name}",
  "search.act.refresh": "Refresh {name}",
  "search.act.sync_all": "Sync all connected devices",
  "search.done.update": "Update of {name} started.",
  "search.done.eject": "{name} ejected.",
  "search.done.refresh": "Refreshing {name}.",
});

// ---- palette items: {key, group, title, sub, ico, path | run}
const RECENT = "syncmusic.palette";
const MAX_RECENT = 5;
function recentKeys() {
  try { return JSON.parse(localStorage.getItem(RECENT) || "[]"); } catch (e) { return []; }
}
function remember(key) {
  const keys = [key, ...recentKeys().filter((k) => k !== key)].slice(0, 20);
  try { localStorage.setItem(RECENT, JSON.stringify(keys)); } catch (e) { /* private mode */ }
}

async function paletteItems() {
  const [devices, libraries] = await Promise.all([get("/devices").catch(() => ({ registered: [] })), get("/libraries").catch(() => [])]);
  const items = NAV.map((n) => ({ key: `page:${n.id}`, group: "pages", title: t("search.open", { name: t(`nav.${n.id}`) }), ico: n.icon, path: n.path }));
  const act = (key, title, ico, run, sub = "") => items.push({ key, group: "actions", title, sub, ico, run });
  for (const d of devices.registered || []) {
    const id = encodeURIComponent(d.id), name = d.name;
    items.push({ key: `sync:${d.id}`, group: "actions", title: t("search.act.sync", { name }), sub: t("search.act.sync_sub"), ico: "sync", path: `/devices/${id}/plan` });
    if (!d.connected) continue;
    act(`update:${d.id}`, t("search.act.update", { name }), "sync", async () => { await post(`/devices/${id}/update`); toast(t("search.done.update", { name })); });
    act(`eject:${d.id}`, t("search.act.eject", { name }), "eject", async () => { await post(`/devices/${id}/eject`); toast(t("search.done.eject", { name })); });
  }
  for (const l of libraries) {
    act(`refresh:${l.id}`, t("search.act.refresh", { name: l.name }), "sync", async () => { await post(`/libraries/${encodeURIComponent(l.id)}/refresh`); toast(t("search.done.refresh", { name: l.name })); });
  }
  act("sync-all", t("search.act.sync_all"), "sync", syncAll);
  act("sidebar", t(isRail() ? "side.expand" : "side.collapse"), isRail() ? "right" : "left", () => setRail(!isRail()));
  const { settingsIndex } = await import("./views/settings.js");
  for (const x of settingsIndex()) {
    items.push({ key: `setting:${x.key}`, group: "settings", title: x.label, sub: x.sub, ico: "settings", path: `/settings?${new URLSearchParams({ q: x.label })}`, find: `${x.label} ${x.sub}` });
  }
  return items;
}

// fuzzy: every character of the query in order (case-insensitive); a lower score is a better match
function score(q, text) {
  const s = text.toLowerCase();
  const at = s.indexOf(q);
  if (at >= 0) return at;
  let pos = -1, gaps = 0;
  for (const ch of q) {
    const next = s.indexOf(ch, pos + 1);
    if (next < 0) return null;
    gaps += next - pos - 1;
    pos = next;
  }
  return 100 + gaps;
}

function pick(items, q) {
  const recent = recentKeys();
  const rank = (it) => { const i = recent.indexOf(it.key); return i < 0 ? 99 : i; };
  q = q.trim().toLowerCase().replace(/\s+/g, " ");
  if (!q) {
    const top = items.filter((it) => recent.includes(it.key)).sort((a, b) => rank(a) - rank(b)).slice(0, MAX_RECENT);
    return { recent: top, pages: items.filter((it) => it.group === "pages" && !top.includes(it)), actions: [], settings: [] };
  }
  // settings also match their explanation, but only as a plain substring (fuzzy over long text matches anything)
  const match = (it) => { const a = score(q, it.title); return a !== null || !it.find ? a : it.find.toLowerCase().includes(q) ? 50 : null; };
  const hit = items.map((it) => ({ it, s: match(it) })).filter((x) => x.s !== null)
    .sort((a, b) => rank(a.it) - rank(b.it) || a.s - b.s).map((x) => x.it);
  return { recent: [], pages: hit.filter((it) => it.group === "pages"), actions: hit.filter((it) => it.group === "actions").slice(0, 8),
    settings: hit.filter((it) => it.group === "settings").slice(0, 6) };
}

function localResults(found) {
  return ["recent", "pages", "actions", "settings"].filter((g) => found[g].length).map((g) => html`<div class="search-group" role="group" aria-label="${t(`search.group.${g}`)}">
    <div class="search-ghead">${t(`search.group.${g}`)}</div>
    ${found[g].map((it) => {
      const what = html`${icon(it.ico, { size: 16 })}<span class="search-what"><b>${it.title}</b>${it.sub ? html`<small>${it.sub}</small>` : ""}</span>`;
      return it.run ? html`<button type="button" class="search-hit" role="option" data-search-hit data-key="${it.key}">${what}</button>`
        : html`<a class="search-hit" role="option" href="#${it.path}" data-search-hit data-key="${it.key}">${what}</a>`;
    })}</div>`);
}

const GROUPS = ["artists", "albums", "tracks", "videos", "books", "podcasts", "devices", "templates", "jobs"];
// the /search response has one "tracks" list for every media kind; non-music hits get their own group
const ITEM_GROUP = { video: "videos", audiobook: "books", podcast: "podcasts" };
const ITEM_GROUPS = ["tracks", ...Object.values(ITEM_GROUP)];
function regroup(res) {
  const out = { ...res, tracks: [], videos: [], books: [], podcasts: [] };
  for (const h of res.tracks || []) out[ITEM_GROUP[h.kind] || "tracks"].push(h);
  return out;
}
const GROUP_ICON = { artists: "artist", albums: "album", tracks: "music", devices: "device", templates: "filter", jobs: "pulse" };
const KIND_ICON = { audiobook: "book", podcast: "podcast", video: "video" };
const hitIcon = (group, h) => (ITEM_GROUPS.includes(group) && KIND_ICON[h.kind]) || GROUP_ICON[group];
const KIND_TAB = { music: "songs", audiobook: "books", podcast: "podcasts", video: "videos" };

const browse = (h, tab, q) => `/libraries/${encodeURIComponent(h.library_id)}/browse?${new URLSearchParams({ tab, q })}`;

// [title, sub, path] of one hit
function row(group, h) {
  if (group === "artists") return [h.name, h.library, browse(h, h.kind === "music" ? "artists" : KIND_TAB[h.kind], h.name)];
  if (group === "albums") return [h.name, [h.artist, h.library].filter(Boolean).join(" · "), browse(h, h.kind === "music" ? "albums" : KIND_TAB[h.kind], h.name)];
  if (ITEM_GROUPS.includes(group)) return [h.title, [h.artist, h.album, h.library].filter(Boolean).join(" · "), browse(h, KIND_TAB[h.kind] || "songs", h.title)];
  if (group === "devices") return [h.name, "", `/devices/${encodeURIComponent(h.id)}`];
  if (group === "templates") return [h.name, "", `/templates/${encodeURIComponent(h.id)}`];
  return [h.name, h.status, `/jobs/${encodeURIComponent(h.id)}`];
}

function results(q, res, items = []) {
  const found = pick(items, q);
  const local = localResults(found);
  if (!q.trim()) return html`<p class="muted search-empty">${t(items.length ? "search.hint_palette" : "search.hint")}</p>${local}`;
  if (!res.tracks) return local; // the server's answer is still on its way
  res = regroup(res);
  const groups = GROUPS.filter((g) => res[g].length);
  const missing = res.not_loaded.length ? html`<p class="muted search-note">${t("search.not_loaded", { names: res.not_loaded.map((l) => l.name).join(", ") })}</p>` : "";
  if (!groups.length && !local.length) return html`<p class="muted search-empty">${t("search.none", { q: q.trim() })}</p>${missing}`;
  return html`${local}${groups.map((g) => html`<div class="search-group" role="group" aria-label="${t(`search.group.${g}`)}">
    <div class="search-ghead">${t(`search.group.${g}`)}</div>
    ${res[g].map((h) => {
      const [title, sub, path] = row(g, h);
      return html`<a class="search-hit" role="option" href="#${path}" data-search-hit>${icon(hitIcon(g, h), { size: 16 })}<span class="search-what"><b>${title}</b>${sub ? html`<small>${sub}</small>` : ""}</span></a>`;
    })}</div>`)}${missing}`;
}

let open = null; // {close}

export function openSearch() {
  if (open) return;
  const back = document.createElement("div");
  back.className = "modal-back search-back";
  setHtml(back, html`<div class="modal search-panel" role="dialog" aria-modal="true" aria-label="${t("search.label")}">
    <label class="search-field">${icon("search")}<input type="search" id="searchInput" autocomplete="off" spellcheck="false"
      placeholder="${t("search.placeholder")}" aria-label="${t("search.label")}" aria-controls="searchResults"><kbd>Esc</kbd></label>
    <div class="search-results" id="searchResults" role="listbox">${results("", {})}</div></div>`);
  $("#overlay").append(back);
  const input = $("#searchInput", back);
  const list = $("#searchResults", back);
  const previous = document.activeElement;
  let active = 0;
  let timer = null;
  let asked = 0;
  let items = [];
  let last = {}; // the server's last answer, kept while the palette items arrive
  const hits = () => $$("[data-search-hit]", list);
  const mark = () => hits().forEach((a, i) => a.classList.toggle("active", i === active));
  const close = () => {
    back.remove();
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("hashchange", close);
    clearTimeout(timer);
    open = null;
    if (previous?.focus && previous.isConnected) previous.focus();
  };
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
    const n = hits().length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!n) return;
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      mark();
      hits()[active].scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter" && n && document.activeElement === input) {
      e.preventDefault();
      choose(hits()[active]);
    }
  };
  // a palette item is remembered for "Recent"; an action runs after the overlay closed, a link navigates
  const choose = (el) => {
    const key = el.dataset.key;
    if (key) remember(key);
    const it = key && items.find((x) => x.key === key);
    close();
    if (it?.run) Promise.resolve(it.run()).catch((err) => toast(err.message || String(err), "error"));
    else go(el.getAttribute("href").slice(1));
  };
  const draw = (q) => { setHtml(list, results(q, last, items)); active = 0; mark(); };
  const ask = async () => {
    const q = input.value;
    const my = ++asked;
    last = {};
    if (!q.trim()) { draw(q); return; }
    draw(q); // pages and actions at once
    try {
      const res = await get(`/search?${new URLSearchParams({ q })}`);
      if (my !== asked || !back.isConnected) return;
      last = res;
      draw(q);
    } catch (err) {
      if (my === asked && back.isConnected) setHtml(list, html`<p class="muted search-empty">${err.message || String(err)}</p>`);
    }
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(ask, 200); });
  back.addEventListener("click", (e) => {
    const hit = e.target.closest("[data-search-hit]");
    if (hit) { e.preventDefault(); choose(hit); } else if (e.target === back) close();
  });
  paletteItems().then((x) => { items = x; if (back.isConnected) draw(input.value); }).catch(() => null);
  document.addEventListener("keydown", onKey, true);
  window.addEventListener("hashchange", close);
  open = { close };
  input.focus();
}

export function initSearch() {
  const btn = $("#searchBtn");
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  setHtml(btn, html`${icon("search")}<span class="search-btn-text">${t("search.label")}</span><kbd class="search-kbd">${mac ? "⌘K" : "Ctrl K"}</kbd>`);
  btn.setAttribute("aria-label", t("search.label"));
  btn.setAttribute("aria-haspopup", "dialog");
  btn.addEventListener("click", openSearch);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") { e.preventDefault(); openSearch(); }
  });
}
