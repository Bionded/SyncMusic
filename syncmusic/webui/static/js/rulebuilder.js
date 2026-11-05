// Visual rule builder + YAML/JSON text mode of the template editor (a device has templates only).
// State: {rules, excludes, priority} (a RuleSet). A rule's criteria is edited visually as a tree
// of all/any groups that nest, e.g. (in playlist X and artist Y) or in playlist Z; criteria the builder
// cannot show (an operator with several fields) keep their value and are edited in text mode.
// The DOM is the editing surface: `capture()` reads it back into the state before every structural change and
// the builder is redrawn at once (no debounce: a quick click must never see stale rows).
import { html, setHtml, bytes } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { get, post } from "./api.js";
import { dialog, problemBox } from "./ui.js";
import { icon } from "./icons.js";
import { sortable, moveKey, grip } from "./reorder.js";

addStrings({
  "rb.text": "YAML",
  "rb.rules": "Include",
  "rb.excludes": "Exclude",
  "rb.excludes_help": "Anything an exclude rule matches never goes to the device.",
  "rb.add_rule": "Add a rule",
  "rb.add_exclude": "Add an exclude rule",
  "rb.rule_name": "Rule name",
  "rb.match": "Match",
  "rb.match.all": "all of these",
  "rb.match.any": "any of these",
  "rb.add_cond": "Add a condition",
  "rb.add_group": "Add a group",
  "rb.remove_cond": "Remove this condition",
  "rb.remove_group": "Remove this group",
  "rb.group": "Group",
  "rb.field": "Field",
  "rb.op": "Condition",
  "rb.value": "Value",
  "rb.days": "days",
  "rb.from": "from",
  "rb.to": "to",
  "rb.no_conds": "No conditions: the rule takes everything of its libraries.",
  "rb.per_podcast": "episodes of every podcast",
  "rb.unplayed_only": "not played yet (a played one makes room)",
  "rb.complex": "The builder cannot show these conditions: edit them in YAML mode.",
  "rb.hand_picked": "{n} items picked by hand (sent from a library or removed from the device). They are kept as they are; edit the list in YAML mode.",
  "rb.hand_picked_one": "{n} item picked by hand (sent from a library or removed from the device). It is kept as it is; edit the list in YAML mode.",
  "rb.kind": "Media",
  "rb.kind_any": "any",
  "rb.sort": "Order by",
  "rb.sort_none": "library order",
  "rb.desc": "highest / newest first",
  "rb.reshuffle": "New random order",
  "rb.reshuffle.none": "Never (stays the same)",
  "rb.reshuffle.week": "Every week",
  "rb.reshuffle.month": "Every month",
  "rb.limit": "At most (items)",
  "rb.limits": "Order and limits",
  "rb.at_most": "At most",
  "rb.unit": "Unit",
  "rb.unit.tracks": "tracks",
  "rb.unit.GB": "GB",
  "rb.unit.hours": "hours",
  "rb.limit_duration": "At most (time, e.g. 48h)",
  "rb.as_playlist_on": "Also create a playlist with these items on the device",
  "rb.as_playlist_default": "Playlist",
  "rb.sum_sort": "by {field}",
  "rb.sum_limit": "at most {n} {unit}",
  "rb.sum_limits": "several limits",
  "rb.sum_playlist": "playlist",
  "rb.limit_size": "At most (size, e.g. 10GB)",
  "rb.as_playlist": "Also as a device playlist named",
  "rb.skip_playlists": "Don't put these playlists on the device",
  "rb.enabled": "Enabled",
  "rb.remove_rule": "Remove rule {name}",
  "rb.to_exclude": "Make it an exclude rule",
  "rb.to_include": "Make it an include rule",
  "rb.test": "Test",
  "rb.test_libs": "Test against",
  "rb.test_all": "all libraries",
  "rb.unavailable": "not in these libraries",
  "rb.text_help": "Edit the rules as YAML or JSON; any Navidrome smart playlist (.nsp) also works as one rule.",
  "rb.true": "yes",
  "rb.false": "no",
  "rb.rule": "Rule {n}",
  "rb.untitled": "(untitled rule)",
  "rb.empty_value": "A condition has no value: fill it in or remove it.",
  "rb.preset": "From a preset",
  "rb.presets": "Start from a preset:",
  "rb.items_match": "items that match",
  "rb.conditions": "conditions",
  "rb.insert_cond": "Insert a condition below this one",
  "rb.rule_of": "Rule",
  "rb.preset_title": "Add a rule from a preset",
  "rb.preset_help": "The rule is added to the list below; change it there like any other.",
  "rb.preset_none": "No preset fits these libraries.",
  "rb.preset_add": "Add the rule",
  "rb.pick": "Pick from library",
  "rb.pick_title": "Pick from the library",
  "rb.pick_help": "Tick artists, albums, genres or playlists: the rule takes everything that matches any of them.",
  "rb.pick.artist": "Artists",
  "rb.pick.album": "Albums",
  "rb.pick.genre": "Genres",
  "rb.pick.playlist": "Playlists",
  "rb.pick.audiobook": "Audiobooks",
  "rb.pick.podcast": "Podcasts",
  "rb.pick.episode": "Episodes",
  "rb.pick.author": "Authors",
  "rb.pick.bookseries": "Series",
  "rb.pick_help_abs": "Tick audiobooks, podcasts, episodes, series, authors, genres or playlists (collections): the rule takes everything that matches any of them (a podcast: all its episodes, an author or series: all their books).",
  "rb.pick_library": "Library",
  "rb.pick_library_one": "This template uses only {name}.",
  "rb.pick_as": "View as",
  "rb.pick_as_main": "Main account",
  "rb.pick_only_for": "only for {name}",
  "rb.pick_search": "Search",
  "rb.pick_search_hint": "Type 3 letters or more to search the whole library; the list shows the most frequent first.",
  "rb.pick_none": "Nothing found.",
  "rb.pick_loading": "Loading…",
  "rb.pick_error": "The library could not be read: {error}",
  "rb.pick_add": "Add {n} picks",
  "rb.pick_add_one": "Add {n} pick",
  "rb.pick_add_none": "Add",
  "rb.pick_nothing": "Tick at least one value.",
  "rb.pick_count": "{n} tracks",
  "rb.pick_count_one": "{n} track",
  "op.is": "is", "op.isNot": "is not", "op.contains": "contains", "op.notContains": "does not contain",
  "op.startsWith": "starts with", "op.endsWith": "ends with", "op.gt": "greater than", "op.lt": "less than",
  "op.inTheRange": "between", "op.inTheLast": "in the last", "op.notInTheLast": "not in the last",
  "op.before": "before", "op.after": "after", "op.inPlaylist": "in playlist", "op.notInPlaylist": "not in playlist",
  "op.latestEpisodes": "the latest", "op.in": "is in", "op.notIn": "is not in",
  "rb.chip_add": "Add a value (Enter or comma)",
  "rb.chip_remove": "Remove {value}",
});

const KINDS = ["music", "podcast", "audiobook", "video"];
const SUGGEST = new Set(["string", "list", "playlist"]);
let catalogue = null; // {fields: [{name, type, available}], operators: {type: [op]}}
const catalogues = {}; // per library set: the fields those libraries have
let playlistNames = {}; // playlist id → name, to show an `inPlaylist {id}` condition by name
let seq = 0;

/** The field catalogue for `libraryIds` (default: every library): only the fields those libraries have are
 * offered. Also starts loading their playlist names in the background: the builder renders at once and
 * an `inPlaylist {id}` shows its id until its name arrives. */
export async function loadCatalogue(libraryIds = null) {
  const key = (libraryIds || []).filter(Boolean).join(",");
  if (!catalogues[key]) {
    const c = await get(key ? `/rules/fields?libraries=${encodeURIComponent(key)}` : "/rules/fields");
    c.fields.push({ name: "playlist", type: "playlist", nsp: true, available: true });
    c.byName = Object.fromEntries(c.fields.map((f) => [f.name, f]));
    catalogues[key] = c;
  }
  catalogue = catalogues[key];
  loadPlaylistNames(libraryIds); // playlists change: read on every view, never waited for
  return catalogue;
}

const NAMES_TIMEOUT_MS = 10000;

async function loadPlaylistNames(libraryIds) {
  let ids = libraryIds;
  try {
    if (!ids) ids = (await get("/libraries")).map((l) => l.id);
  } catch { return; /* names are cosmetic */ }
  // in parallel, each with a time limit: an unreachable library just keeps showing its playlist ids
  await Promise.all(ids.filter(Boolean).map(async (lid) => {
    let timer;
    try {
      const lists = await Promise.race([
        get(`/libraries/${encodeURIComponent(lid)}/playlists`),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("timeout")), NAMES_TIMEOUT_MS); }),
      ]);
      const old = { ...playlistNames };
      for (const pl of lists) playlistNames[pl.id] = pl.name;
      showPlaylistNames(old);
    } catch { /* unreachable or slow */ } finally { clearTimeout(timer); }
  }));
}

/** Put arrived names into the shown playlist conditions that still show the id (or a name since renamed). */
function showPlaylistNames(old) {
  for (const input of document.querySelectorAll('[data-rb] input[data-c="value"][data-ref]')) {
    let ref;
    try { ref = JSON.parse(input.dataset.ref); } catch { continue; }
    if (ref?.id == null || ref.name != null || playlistNames[ref.id] == null) continue;
    if (input.value === String(ref.id) || input.value === old[ref.id]) input.value = playlistNames[ref.id];
  }
}

// --- model ------------------------------------------------------------------------------------------------------

// A group is {match: "all"|"any", items: [group | cond]}, a cond {field, op, value}. Paths name a group: "" is the
// rule's top group, "1" its second item, "1.0" that group's first item. A playlist cond also keeps the original
// reference (`ref`, e.g. {id} from an .nsp, or {path}): it is saved unchanged while its shown value is not edited.

/** criteria → the top group, or null when the builder cannot show it. */
function toTree(criteria) {
  if (!criteria || !Object.keys(criteria).length) return { match: "all", items: [] };
  const keys = Object.keys(criteria);
  if (keys.length === 1 && (keys[0] === "all" || keys[0] === "any") && Array.isArray(criteria[keys[0]])) {
    return toGroup(keys[0], criteria[keys[0]]);
  }
  return toGroup("all", keys.map((k) => ({ [k]: criteria[k] }))); // {a: .., b: ..} = all of them
}

function toGroup(match, list) {
  const items = [];
  for (const c of list) {
    const ks = Object.keys(c || {});
    if (ks.length !== 1) return null;
    const op = ks[0], body = c[op];
    if (op === "all" || op === "any") {
      if (!Array.isArray(body)) return null;
      const g = toGroup(op, body);
      if (g === null) return null;
      items.push(g);
      continue;
    }
    if (op === "inPlaylist" || op === "notInPlaylist") {
      if (body?.any === true) items.push({ field: "playlist", op, value: "*" });
      else items.push({ field: "playlist", op, value: playlistLabel(body), ref: body });
      continue;
    }
    if (op === "latestEpisodes") { // the pseudo field `latest_episodes`: the episodes per podcast
      if (body?.per_series == null || Object.keys(body).some((k) => k !== "per_series" && k !== "unplayed")) return null;
      items.push({ field: "latest_episodes", op, value: body.per_series, unplayed: !!body.unplayed });
      continue;
    }
    const fk = Object.keys(body || {});
    if (fk.length !== 1) return null;
    // a field or operator the builder does not offer (e.g. the hand-picked `in: {track_id: [ids]}`) is
    // not shown as a row: a row would turn it into a different condition on save
    if (!catalogue?.byName[fk[0]] || !Object.values(catalogue.operators).some((ops) => ops.includes(op))) return null;
    items.push({ field: fk[0], op, value: body[fk[0]] });
  }
  return { match, items };
}

const isGroup = (x) => Array.isArray(x.items);

const ID_FIELDS = new Set(["track_id", "album_id", "artist_id"]);

/** How many ids a rule picks by hand (`in` lists of ids only, e.g. the Manual selection), else null. */
function handPicked(criteria) {
  const conds = criteria?.any ?? criteria?.all;
  if (!Array.isArray(conds) || Object.keys(criteria).length !== 1 || !conds.length) return null;
  let n = 0;
  for (const c of conds) {
    const ks = Object.keys(c?.in || {});
    if (Object.keys(c || {}).length !== 1 || ks.length !== 1 || !ID_FIELDS.has(ks[0]) || !Array.isArray(c.in[ks[0]])) {
      return null;
    }
    n += c.in[ks[0]].length;
  }
  return n;
}

/** A rule the builder cannot show: it is kept unchanged on save. */
function opaqueNote(criteria) {
  const n = handPicked(criteria);
  if (n !== null) return html`<p class="muted" data-hand-picked="${n}">${t("rb.hand_picked", { n })}</p>`;
  return html`<div class="warningbox">${t("rb.complex")}</div>`;
}

/** What the value box shows for a playlist reference: its name (an id is looked up), else the id or path. */
function playlistLabel(ref) {
  if (!ref) return "";
  if (ref.name != null) return String(ref.name);
  if (ref.id != null) return playlistNames[ref.id] ?? String(ref.id);
  return String(ref.path ?? "");
}

function playlistRef(c) {
  if (c.value === "*") return { any: true };
  // unchanged: keep {id} / {path} as it was (an id still shows itself while its name has not arrived)
  if (c.ref && (playlistLabel(c.ref) === c.value || (c.ref.id != null && String(c.ref.id) === c.value))) return c.ref;
  return { name: c.value };
}

export function fromTree(g) {
  const items = g.items.filter((c) => !isGroup(c) || c.items.length); // an emptied inner group is dropped
  if (!items.length) return { all: [] }; // no condition: everything (an empty "any" would match nothing)
  return {
    [g.match]: items.map((c) => {
      if (isGroup(c)) return fromTree(c);
      if (c.field === "playlist") return { [c.op]: playlistRef(c) };
      if (c.field === "latest_episodes") return { latestEpisodes: { per_series: c.value, ...(c.unplayed ? { unplayed: true } : {}) } };
      return { [c.op]: { [c.field]: c.value } };
    }),
  };
}

function groupAt(tree, path) {
  let g = tree;
  for (const i of path ? path.split(".") : []) g = g.items[Number(i)];
  return g;
}

function newRule(prefix) {
  return { id: `${prefix}-${Date.now().toString(36)}${(++seq).toString(36)}`, name: "", criteria: { all: [] } };
}

// --- rendering ----------------------------------------------------------------------------------------------------

/** The builder for state `st` ({rs, mode: "visual"|"text", text, readOnly}); `st.id` names it. */
export function builder(st) {
  st.id = st.id || `rb${++seq}`;
  return html`<div class="rb" data-rb="${st.id}" data-change="rbChanged">${inner(st)}</div>`;
}

// the Rules / YAML switch is the template editor's: it calls the rbMode action
function inner(st) {
  if (st.mode === "text") {
    return html`<p class="muted">${t("rb.text_help")}</p>
      <label class="sr" for="${st.id}-text">${t("rb.text")}</label>
      <textarea class="codebox" id="${st.id}-text" spellcheck="false" ${st.readOnly ? "readonly" : ""}>${st.text || ""}</textarea>`;
  }
  const rs = st.rs;
  const ro = st.readOnly;
  // presets as chips: one with parameters or a pick asks for them first
  const presets = (st.presets || []).filter((x) => x.available);
  return html`${ro || !presets.length ? "" : html`<div class="rb-presets" data-rb-presets><span class="muted">${t("rb.presets")}</span>
      ${presets.map((x) => html`<button type="button" class="chip" data-action="rbPreset" data-preset-chip="${x.id}" title="${x.description}">${x.name}</button>`)}</div>`}
    ${rs.rules.map((r, i) => ruleCard(st, r, "rules", i))}
    ${ro ? "" : html`<button type="button" class="btn small dashed" data-action="rbAddRule" data-list="rules">${icon("plus", { size: 16 })}${t("rb.add_rule")}</button>`}
    ${rs.excludes.map((r, i) => ruleCard(st, r, "excludes", i))}
    ${ro ? "" : html`<button type="button" class="btn small dashed" data-action="rbAddRule" data-list="excludes">${icon("plus", { size: 16 })}${t("rb.add_exclude")}</button>`}
    <p class="muted rb-exc-help">${t("rb.excludes_help")}</p>
`;
}

function ruleCard(st, r, list, i) {
  const f = toTree(r.criteria);
  const key = `${list}:${i}`;
  const ro = st.readOnly;
  const label = r.name || t("rb.untitled");
  const inc = list === "rules";
  // the design: an Include / Exclude card - "items that match all / any of these conditions" in its head,
  // the rule's name, Enabled and the include ⇄ exclude switch in a quieter line below
  const pill = html`<span class="rc-pill ${inc ? "inc" : "exc"}">${icon(inc ? "plus" : "minus", { size: 13, width: 2.4 })}${t(inc ? "rb.rules" : "rb.excludes")}</span>`;
  // include rules are in priority order: drag by the grip or Alt+↑ / Alt+↓; excludes have no order
  const head = inc && !ro && st.rs.rules.length > 1 ? html`${grip(label)}${pill}` : pill;
  return html`<fieldset class="rule-card ${inc ? "inc" : "exc"} ${r.enabled === false ? "off" : ""}" data-rule="${key}" ${ro ? "disabled" : ""}>
    <legend class="sr">${label}</legend>
    ${f === null ? html`<div class="toolbar rb-group-head">${head}</div>${opaqueNote(r.criteria)}` : groupBox(st, key, f, "", ro, head)}
    <div class="rc-meta">
      <label class="sr" for="${st.id}-${key}-name">${t("rb.rule_name")}</label>
      <input class="rb-input rc-name" id="${st.id}-${key}-name" data-k="name" value="${r.name}" placeholder="${t("rb.rule_name")}">
      <label class="checkrow" style="margin:0"><input type="checkbox" data-k="enabled" ${r.enabled !== false ? "checked" : ""}> ${t("rb.enabled")}</label>
      ${ro ? "" : html`<button type="button" class="btn small ghost" data-action="rbFlip" data-rule-key="${key}">${inc ? t("rb.to_exclude") : t("rb.to_include")}</button>
        <button type="button" class="iconbtn" data-action="rbRemoveRule" data-rule-key="${key}" aria-label="${t("rb.remove_rule", { name: label })}" title="${t("rb.remove_rule", { name: label })}">${icon("trash", { size: 16 })}</button>`}
    </div>
    ${limitsBox(st, r, key, inc)}
  </fieldset>`;
}

// "At most" of a rule: one number with its unit; a rule that already has
// several limits shows each of them
const UNITS = { tracks: "limit", GB: "limit_size", hours: "limit_duration" };

function shownLimits(r) {
  const set = Object.entries(UNITS).filter(([, k]) => r[k] != null && r[k] !== "");
  if (set.length > 1) return null;
  const [unit, k] = set[0] || ["GB", "limit_size"];
  const raw = r[k] ?? "";
  const n = unit === "tracks" ? raw : String(raw).replace(/\s*(GB|G|h|H)$/i, "");
  return { unit, n: /^[\d.]*$/.test(String(n)) ? n : raw };
}

/** A rule's "Order and limits" (open when one is set): sort with "highest first", at most N tracks / GB / hours,
 * the media kinds, a device playlist with the rule's result, the opt-out for its "in playlist" playlists. */
function limitsBox(st, r, key, inc) {
  const id = `${st.id}-${key}`;
  const desc = r.order === "desc" || (r.sort || "").startsWith("-");
  const lim = shownLimits(r);
  const kinds = KINDS.map((k) => html`<label class="checkrow" style="margin:0"><input type="checkbox" data-kind="${k}" ${r.kind && r.kind.includes(k) ? "checked" : ""}> ${t(`libs.kind.${k}`)}</label>`);
  const set = [r.sort ? t("rb.sum_sort", { field: (r.sort || "").replace(/^-/, "") }) : "", lim && lim.n !== "" ? t("rb.sum_limit", { n: lim.n, unit: t(`rb.unit.${lim.unit}`) }) : !lim ? t("rb.sum_limits") : "",
    r.kind?.length ? r.kind.map((k) => t(`libs.kind.${k}`)).join(", ") : "", r.as_playlist ? t("rb.sum_playlist") : ""].filter(Boolean);
  return html`<details class="rc-limits" data-rc-limits ${set.length ? "open" : ""}><summary>${t(inc ? "rb.limits" : "rb.kind")}${set.length ? html` <span class="muted">· ${set.join(" · ")}</span>` : ""}</summary>
    <div class="rc-limits-grid">
      ${inc ? html`<div class="field"><label for="${id}-sort">${t("rb.sort")}</label>
        <select id="${id}-sort" data-k="sort"><option value="">${t("rb.sort_none")}</option>
          ${fieldOptions((r.sort || "").replace(/^-/, ""), catalogue.fields.filter((x) => x.type !== "playlist" && x.type !== "episodes"))}
          <option value="random" ${r.sort === "random" ? "selected" : ""}>random</option></select>
        <label class="checkrow"><input type="checkbox" data-k="desc" ${desc ? "checked" : ""}> ${t("rb.desc")}</label>
        <span class="rc-reshuffle" data-reshuffle ${r.sort === "random" ? "" : "hidden"}><label for="${id}-rp">${t("rb.reshuffle")}</label>
          <select id="${id}-rp" data-k="random_period">${["none", "week", "month"].map((p) => html`<option value="${p}" ${(r.random_period || "none") === p ? "selected" : ""}>${t(`rb.reshuffle.${p}`)}</option>`)}</select></span></div>
      ${lim ? html`<div class="field"><label for="${id}-limit-n">${t("rb.at_most")}</label>
        <div class="rc-limit"><input id="${id}-limit-n" data-k="limit_n" inputmode="decimal" value="${lim.n}">
          <label class="sr" for="${id}-limit-u">${t("rb.unit")}</label>
          <select id="${id}-limit-u" data-k="limit_unit">${Object.keys(UNITS).map((u) => html`<option value="${u}" ${u === lim.unit ? "selected" : ""}>${t(`rb.unit.${u}`)}</option>`)}</select></div></div>`
        : html`<div class="field"><label for="${id}-limit">${t("rb.limit")}</label><input id="${id}-limit" data-k="limit" inputmode="numeric" value="${r.limit ?? ""}">
          <label for="${id}-size">${t("rb.limit_size")}</label><input id="${id}-size" data-k="limit_size" value="${r.limit_size ?? ""}">
          <label for="${id}-dur">${t("rb.limit_duration")}</label><input id="${id}-dur" data-k="limit_duration" value="${r.limit_duration ?? ""}"></div>`}` : ""}
      <div class="field"><span class="rc-lab">${t("rb.kind")}</span><div class="toolbar">${kinds}</div></div>
      ${inc ? html`<div class="field full"><label class="checkrow"><input type="checkbox" data-k="as_pl_on" ${r.as_playlist ? "checked" : ""}> ${t("rb.as_playlist_on")}</label>
        <label class="sr" for="${id}-pl">${t("rb.as_playlist")}</label>
        <input id="${id}-pl" data-k="as_playlist" value="${r.as_playlist ?? ""}" placeholder="${r.name || t("rb.as_playlist")}" ${r.as_playlist ? "" : "hidden"}></div>
      <div class="field full"><label class="checkrow"><input type="checkbox" data-k="skip_playlists" ${r.skip_playlists ? "checked" : ""}> ${t("rb.skip_playlists")}</label></div>` : ""}
    </div></details>`;
}

function groupBox(st, key, g, path, ro, pill = null) {
  const id = `${st.id}-${key}-g${path.replaceAll(".", "_")}`;
  const nested = path !== "";
  const kids = g.items.map((c, j) => {
    const p = nested ? `${path}.${j}` : `${j}`;
    return isGroup(c) ? groupBox(st, key, c, p, ro) : condRow(st, key, c, j, path, ro);
  });
  return html`<div class="${nested ? "rb-group nested" : "rb-group"}" data-group="${path}" ${nested ? html`role="group" aria-label="${t("rb.group")}"` : ""}>
    <div class="toolbar rb-group-head">${pill || ""}${g.items.length || nested ? html`<span>${nested ? t("rb.match") : t("rb.items_match")}</span>
      <label class="sr" for="${id}-match">${t("rb.match")}</label>
      <select class="rb-input rb-match" id="${id}-match" data-k="match">${["all", "any"].map((m) => html`<option value="${m}" ${g.match === m ? "selected" : ""}>${t(`rb.match.${m}`)}</option>`)}</select>${nested ? "" : html`<span>${t("rb.conditions")}</span>`}` : ""}
      ${nested && !ro ? html`<button type="button" class="iconbtn" data-action="rbRemoveGroup" data-rule-key="${key}" data-path="${path}" aria-label="${t("rb.remove_group")}">${icon("x", { size: 16 })}</button>` : ""}</div>
    <div class="rb-items">${kids.length ? kids : html`<div class="muted" style="margin:6px 0">${nested ? "" : t("rb.no_conds")}</div>`}</div>
    ${ro ? "" : html`<div class="toolbar rb-group-foot"><button type="button" class="btn small" data-action="rbAddCond" data-rule-key="${key}" data-path="${path}">${icon("plus", { size: 16 })}${t("rb.add_cond")}</button>
      <button type="button" class="btn small" data-action="rbAddGroup" data-rule-key="${key}" data-path="${path}">${icon("plus", { size: 16 })}${t("rb.add_group")}</button>
      ${nested ? "" : html`<button type="button" class="btn small" data-action="rbPick" data-rule-key="${key}">${t("rb.pick")}</button>`}</div>`}
  </div>`;
}

/** The fields to offer: those the libraries have, plus `current` when a rule already uses another one (marked). */
function fieldOptions(current, fields = catalogue.fields) {
  return fields.filter((x) => x.available !== false || x.name === current).map((x) => html`<option value="${x.name}" ${x.name === current ? "selected" : ""}>${x.available === false ? `${x.name} - ${t("rb.unavailable")}` : x.name}</option>`);
}

function condRow(st, key, c, j, path = "", ro = false) {
  const spec = catalogue.byName[c.field] || { name: c.field, type: "string" };
  const ops = catalogue.operators[spec.type] || [c.op];
  const id = `${st.id}-${key}-${path ? `${path.replaceAll(".", "_")}_` : ""}${j}`;
  return html`<div class="cond" data-cond="${j}">
    <label class="sr" for="${id}-f">${t("rb.field")}</label>
    <select class="rb-input" id="${id}-f" data-c="field">${fieldOptions(c.field)}</select>
    <label class="sr" for="${id}-o">${t("rb.op")}</label>
    <select class="rb-input" id="${id}-o" data-c="op">${(ops.includes(c.op) ? ops : [c.op, ...ops]).map((o) => html`<option value="${o}" ${o === c.op ? "selected" : ""}>${t(`op.${o}`)}</option>`)}</select>
    ${valueInput(id, spec, c)}
    ${ro ? "" : html`<span class="cond-btns"><button type="button" class="iconbtn" data-action="rbRemoveCond" data-rule-key="${key}" data-path="${path}" data-j="${j}" aria-label="${t("rb.remove_cond")}" title="${t("rb.remove_cond")}">${icon("minus", { size: 16 })}</button>
      <button type="button" class="iconbtn" data-action="rbInsertCond" data-rule-key="${key}" data-path="${path}" data-j="${j}" aria-label="${t("rb.insert_cond")}" title="${t("rb.insert_cond")}">${icon("plus", { size: 16 })}</button></span>`}
  </div>`;
}

function valueInput(id, spec, c) {
  const lab = html`<label class="sr" for="${id}-v">${t("rb.value")}</label>`;
  if (spec.type === "bool") {
    return html`${lab}<select class="rb-input" id="${id}-v" data-c="value" data-t="bool"><option value="true" ${c.value !== false ? "selected" : ""}>${t("rb.true")}</option><option value="false" ${c.value === false ? "selected" : ""}>${t("rb.false")}</option></select>`;
  }
  if (c.op === "inTheRange") {
    const [a, b] = Array.isArray(c.value) ? c.value : ["", ""];
    const type = spec.type === "date" ? "date" : "text";
    return html`<span>${t("rb.from")}</span><input class="rb-input" type="${type}" id="${id}-v" data-c="range0" data-t="${spec.type}" value="${a ?? ""}" aria-label="${t("rb.from")}">
      <span>${t("rb.to")}</span><input class="rb-input" type="${type}" data-c="range1" data-t="${spec.type}" value="${b ?? ""}" aria-label="${t("rb.to")}">`;
  }
  if (c.op === "latestEpisodes") { // a field switched to this one keeps no text value
    const n = typeof c.value === "number" || /^\d+$/.test(String(c.value ?? "")) ? c.value : "";
    return html`${lab}<input class="rb-input" id="${id}-v" data-c="value" data-t="number" inputmode="numeric" value="${n}" style="width:80px"><span>${t("rb.per_podcast")}</span>
      <label class="checkrow" style="margin:0"><input type="checkbox" data-c="unplayed" ${c.unplayed ? "checked" : ""}> ${t("rb.unplayed_only")}</label>`;
  }
  if (c.op === "inTheLast" || c.op === "notInTheLast") {
    return html`${lab}<input class="rb-input" id="${id}-v" data-c="value" data-t="number" inputmode="numeric" value="${c.value ?? ""}" style="width:80px"><span>${t("rb.days")}</span>`;
  }
  if (c.op === "in" || c.op === "notIn") return chipsInput(id, spec, c);
  if (c.op === "before" || c.op === "after") {
    return html`${lab}<input class="rb-input" type="date" id="${id}-v" data-c="value" data-t="date" value="${c.value ?? ""}">`;
  }
  const suggest = SUGGEST.has(spec.type) || spec.name === "library_id";
  const ref = c.field === "playlist" && c.ref ? JSON.stringify(c.ref) : "";
  return html`${lab}<span class="autocomplete-wrap"><input class="rb-input" id="${id}-v" data-c="value" data-t="${spec.type}" value="${c.value ?? ""}" autocomplete="off"
      ${ref ? html`data-ref="${ref}"` : ""} ${suggest ? html`list="${id}-dl" data-suggest="${spec.name}"` : ""}>${suggest ? html`<datalist id="${id}-dl"></datalist>` : ""}</span>`;
}

// `in` / `notIn`: the values as chips; the hidden input holds them as JSON for readCond
const chipValues = (c) => (Array.isArray(c.value) ? c.value : c.value === "" || c.value == null ? [] : [String(c.value)]);

function chipsHtml(values) {
  return values.map((v, i) => html`<span class="vchip" data-chip="${i}">${v}<button type="button" class="chip-x" data-action="rbChipRemove" data-i="${i}" aria-label="${t("rb.chip_remove", { value: v })}" title="${t("rb.chip_remove", { value: v })}">${icon("x", { size: 13 })}</button></span>`);
}

function chipsInput(id, spec, c) {
  const values = chipValues(c);
  const suggest = SUGGEST.has(spec.type);
  return html`<span class="chips-box" data-chips>
    <input type="hidden" data-c="value" data-t="chips" value="${values.length ? JSON.stringify(values) : ""}">
    <span class="chips-list">${chipsHtml(values)}</span>
    <span class="autocomplete-wrap"><input class="rb-input chip-input" id="${id}-v" data-chip-input autocomplete="off" placeholder="${t("rb.chip_add")}" aria-label="${t("rb.chip_add")}"
      ${suggest ? html`list="${id}-dl" data-suggest="${spec.name}"` : ""}>${suggest ? html`<datalist id="${id}-dl"></datalist>` : ""}</span></span>`;
}

/** "a, b, "c, d"" → ["a", "b", "c, d"]: commas split, a quoted value may contain one. */
export function splitChips(text) {
  const out = [];
  for (const m of text.matchAll(/\s*(?:"([^"]*)"|([^,]+))\s*(?:,|$)/g)) {
    const v = (m[1] ?? m[2] ?? "").trim();
    if (v) out.push(v);
  }
  return out;
}

function setChips(box, values) {
  const hidden = box.querySelector('[data-c="value"]');
  hidden.value = values.length ? JSON.stringify(values) : "";
  setHtml(box.querySelector(".chips-list"), html`${chipsHtml(values)}`);
  hidden.dispatchEvent(new Event("input", { bubbles: true })); // the live preview and the unsaved bar follow it
}

const boxValues = (box) => { const v = box.querySelector('[data-c="value"]').value; return v ? JSON.parse(v) : []; };

/** Turn what was typed into chips: on Enter, on a comma outside quotes, on leaving the box (a picked suggestion). */
function addTyped(input, force = false) {
  const text = input.value;
  const quotes = (text.match(/"/g) || []).length;
  if (!force && !(text.includes(",") && quotes % 2 === 0)) return false;
  const box = input.closest("[data-chips]");
  const values = boxValues(box);
  for (const v of splitChips(text)) if (!values.some((x) => x.toLowerCase() === v.toLowerCase())) values.push(v);
  input.value = "";
  setChips(box, values);
  return true;
}

document.addEventListener("keydown", (e) => {
  const input = e.target.closest?.("[data-chip-input]");
  if (!input) return;
  if (e.key === "Enter") { e.preventDefault(); addTyped(input, true); }
  else if (e.key === "Backspace" && !input.value) { // the last chip goes back into the input
    const box = input.closest("[data-chips]");
    const values = boxValues(box);
    if (values.length) { input.value = values.pop(); setChips(box, values); e.preventDefault(); }
  }
});
document.addEventListener("input", (e) => {
  const input = e.target.closest?.("[data-chip-input]");
  if (!input) return;
  if (input.value.includes(",")) addTyped(input);
  else if (e.inputType === "insertReplacementText" || e.inputType === undefined) addTyped(input, true); // a datalist pick
});
document.addEventListener("focusout", (e) => {
  const input = e.target.closest?.("[data-chip-input]");
  if (input && input.value.trim()) addTyped(input, true);
});

// --- reading the DOM back -------------------------------------------------------------------------------------------

function typed(t_, raw) {
  if (t_ === "number") { const v = Number(raw); return raw === "" || Number.isNaN(v) ? raw : v; }
  if (t_ === "bool") return raw === "true";
  if (t_ === "chips") return raw ? JSON.parse(raw) : [];
  return raw;
}

/** Read the visual builder into st.rs (text mode: nothing to read here, see sync()). */
export function capture(st) {
  const root = document.querySelector(`[data-rb="${st.id}"]`);
  if (!root || st.mode === "text") return;
  for (const list of ["rules", "excludes"]) {
    st.rs[list] = st.rs[list].map((r, i) => {
      const el = root.querySelector(`[data-rule="${list}:${i}"]`);
      if (!el) return r;
      const v = (k) => el.querySelector(`[data-k="${k}"]`);
      const out = { ...r, name: v("name").value.trim(), enabled: v("enabled").checked };
      const kinds = [...el.querySelectorAll("[data-kind]:checked")].map((x) => x.dataset.kind);
      out.kind = kinds.length ? kinds : null;
      if (v("sort")) { // an include rule's order and limits (an exclude rule has none)
        const sort = v("sort").value;
        out.sort = sort || null;
        out.order = v("desc").checked && sort ? "desc" : "asc";
        out.random_period = sort === "random" ? v("random_period").value : "none";
        if (v("limit_n")) { // one "At most" with its unit
          const n = v("limit_n").value.trim(), unit = v("limit_unit").value;
          for (const k of Object.values(UNITS)) out[k] = null;
          if (n !== "") out[UNITS[unit]] = unit === "tracks" ? Number(n) : unit === "GB" ? `${n}GB` : `${n}h`;
        } else {
          const limit = v("limit").value.trim();
          out.limit = limit === "" ? null : Number(limit);
          out.limit_size = v("limit_size").value.trim() || null;
          out.limit_duration = v("limit_duration").value.trim() || null;
        }
        // "Also create a playlist": its name, or the rule's (or "Playlist") when left empty
        out.as_playlist = v("as_pl_on").checked ? (v("as_playlist").value.trim() || out.name || t("rb.as_playlist_default")) : null;
      }
      if (v("skip_playlists")) out.skip_playlists = v("skip_playlists").checked;
      const top = el.querySelector('[data-group=""]');
      if (top) out.criteria = fromTree(readGroup(top));
      return out;
    });
  }
}

function readGroup(el) {
  // an empty top group has no match choice: no condition takes everything either way
  const match = el.querySelector(':scope > .rb-group-head [data-k="match"]')?.value || "all";
  const items = [...el.querySelector(":scope > .rb-items").children]
    .filter((x) => x.matches("[data-group], [data-cond]"))
    .map((x) => (x.matches("[data-group]") ? readGroup(x) : readCond(x)));
  return { match, items };
}

function readCond(row) {
  const field = row.querySelector('[data-c="field"]').value;
  const op = row.querySelector('[data-c="op"]').value;
  const r0 = row.querySelector('[data-c="range0"]');
  if (r0) {
    const r1 = row.querySelector('[data-c="range1"]');
    return { field, op, value: [typed(r0.dataset.t, r0.value.trim()), typed(r1.dataset.t, r1.value.trim())] };
  }
  const x = row.querySelector('[data-c="value"]');
  const out = { field, op, value: typed(x.dataset.t, x.value.trim()) };
  if (field === "playlist" && x.dataset.ref) out.ref = JSON.parse(x.dataset.ref);
  const unplayed = row.querySelector('[data-c="unplayed"]');
  if (unplayed) out.unplayed = unplayed.checked;
  return out;
}

/** The rule set as the service will get it: from the text (validated by the service) or the builder. */
export async function current(st) {
  if (st.mode === "text") {
    const res = await post("/rules/validate", { text: document.getElementById(`${st.id}-text`).value });
    if (!res.ok) throw new Error(res.errors.join("; "));
    st.rs = { ...st.rs, rules: res.rule_set.rules, excludes: res.rule_set.excludes, priority: res.rule_set.priority };
  } else {
    capture(st);
    checkValues(st);
  }
  return st.rs;
}

/** The rule set being edited for the live preview: like `current()`, but never focuses or marks an input
 * and never throws - {rs} or {error} (an empty condition, invalid text). */
export async function draft(st) {
  if (st.mode === "text") {
    const el = document.getElementById(`${st.id}-text`);
    if (!el) return { error: "" };
    const res = await post("/rules/validate", { text: el.value });
    if (!res.ok) return { error: res.errors.join("; ") };
    return { rs: { ...st.rs, rules: res.rule_set.rules, excludes: res.rule_set.excludes, priority: res.rule_set.priority } };
  }
  capture(st);
  const root = document.querySelector(`[data-rb="${st.id}"]`);
  const inputs = root ? [...root.querySelectorAll('.cond [data-c="value"], .cond [data-c^="range"]')] : [];
  if (inputs.some((x) => x.dataset.t !== "bool" && !x.value.trim())) return { error: t("rb.empty_value") };
  return { rs: st.rs };
}

/** Refuse a condition without a value (usually an untouched new row): `artist contains ""` would match every item
 *. The empty inputs are marked and the first one gets the focus. */
function checkValues(st) {
  const root = document.querySelector(`[data-rb="${st.id}"]`);
  if (!root) return;
  const inputs = [...root.querySelectorAll('.cond [data-c="value"], .cond [data-c^="range"]')];
  for (const x of inputs) x.removeAttribute("aria-invalid");
  const empty = inputs.filter((x) => x.dataset.t !== "bool" && !x.value.trim());
  if (!empty.length) return;
  for (const x of empty) x.setAttribute("aria-invalid", "true");
  (empty[0].closest("[data-chips]")?.querySelector("[data-chip-input]") || empty[0]).focus();
  throw new Error(t("rb.empty_value"));
}

export function redraw(st) {
  const root = document.querySelector(`[data-rb="${st.id}"]`);
  if (root) setHtml(root, inner(st));
  mountBuilder(st);
}

/** Bind what the builder's root needs once it is on the page (drag to reorder the include rules). */
export function mountBuilder(st) {
  const root = document.querySelector(`[data-rb="${st.id}"]`);
  sortable(root, {
    row: '[data-rule^="rules:"]',
    key: (r) => r.dataset.rule,
    move: (a, b) => {
      capture(st);
      const rules = st.rs.rules, i = (k) => Number(k.split(":")[1]);
      const order = moveKey(rules.map((_, n) => n), i(a), i(b));
      st.rs.rules = order.map((n) => rules[n]);
      redraw(st);
      root.dispatchEvent(new Event("change", { bubbles: true })); // unsaved and the preview follow
      return `rules:${order.indexOf(i(a))}`;
    },
  });
}

/** Test result panel (device or template test). */
export function testResult(res) {
  return html`<div class="callout"><b>${t("rules.result", { items: t("count.item", { n: res.items }), size: bytes(res.bytes) })}</b>
    ${res.warnings.map((w) => html`<br>${icon("warn", { size: 14 })} ${w}`)}
    ${res.playlists.length ? html`<br>${t("rules.playlists", { names: res.playlists.join(", ") })}` : ""}
    ${res.per_rule.length ? html`<div style="margin-top:8px">${t("rules.per_rule")}:<ul>${res.per_rule.map((p) => html`<li>${p.name || p.rule_id}: ${p.matches_nothing ? t("rules.nothing") : html`${p.items} · ${bytes(p.bytes)}`}${p.examples?.length ? html` <span class="muted">- ${p.examples.slice(0, 3).map((e) => e.title).join(", ")}</span>` : ""}</li>`)}</ul></div>` : ""}
    ${res.sample.length ? html`<details><summary>${t("rules.sample")}</summary><ul>${res.sample.map((s) => html`<li>${s.title}${s.artist ? ` - ${s.artist}` : ""} <span class="muted">(${s.kind})</span></li>`)}</ul></details>` : ""}
  </div>`;
}

addStrings({
  "rules.result": "{items} · about {size}",
  "rules.per_rule": "Per rule",
  "rules.sample": "A mix of every rule",
  "rules.nothing": "matches nothing",
  "rules.playlists": "Playlists on the device: {names}",
});

// --- actions (merged into the view's actions; `stOf(ctx)` gives the builder state) -------------------------------------

export function builderActions(stOf) {
  const find = (key, st) => { const [list, i] = key.split(":"); return [list, Number(i), st.rs[list]]; };
  // change the group at `path` of a rule's condition tree and store the criteria; returns that group
  const edit = (st, key, path, fn) => {
    const [, i, arr] = find(key, st);
    const tree = toTree(arr[i].criteria);
    const g = groupAt(tree, path);
    fn(g);
    arr[i].criteria = fromTree(tree);
    return g;
  };
  return {
    rbChipRemove(el) {
      const box = el.closest("[data-chips]");
      const values = boxValues(box);
      values.splice(Number(el.dataset.i), 1);
      setChips(box, values);
      box.querySelector("[data-chip-input]")?.focus();
    },
    async rbMode(el, ctx) {
      const st = stOf(ctx);
      const m = el.dataset.m;
      if (m === st.mode || (m === "visual" && st.mode !== "text")) return;
      if (m === "text") {
        capture(st);
        const { rules, excludes, priority } = st.rs;
        st.text = (await post("/rules/text", { rule_set: { rules, excludes, priority } })).text;
        st.mode = "text";
      } else {
        await current(st); // throws (toast) when the text is invalid: stays in text mode
        st.mode = "visual";
      }
      redraw(st);
    },
    rbAddRule(el, ctx) {
      const st = stOf(ctx); capture(st);
      st.rs[el.dataset.list].push(newRule(el.dataset.list === "rules" ? "rule" : "exclude"));
      redraw(st);
      const n = st.rs[el.dataset.list].length - 1;
      document.querySelector(`[data-rb="${st.id}"] [data-rule="${el.dataset.list}:${n}"] [data-k="name"]`)?.focus();
    },
    rbRemoveRule(el, ctx) {
      const st = stOf(ctx); capture(st);
      const [, i, arr] = find(el.dataset.ruleKey, st);
      arr.splice(i, 1);
      redraw(st);
    },
    rbFlip(el, ctx) {
      const st = stOf(ctx); capture(st);
      const [list, i, arr] = find(el.dataset.ruleKey, st);
      const [r] = arr.splice(i, 1);
      st.rs[list === "rules" ? "excludes" : "rules"].push(r);
      redraw(st);
    },
    rbAddCond(el, ctx) {
      const st = stOf(ctx); capture(st);
      const path = el.dataset.path || "";
      const g = edit(st, el.dataset.ruleKey, path, (grp) => grp.items.push({ field: "artist", op: "contains", value: "" }));
      redraw(st);
      const box = document.querySelector(`[data-rb="${st.id}"] [data-rule="${el.dataset.ruleKey}"] [data-group="${path}"] > .rb-items`);
      box?.querySelector(`:scope > [data-cond="${g.items.length - 1}"] [data-c="value"]`)?.focus();
    },
    rbAddGroup(el, ctx) {
      const st = stOf(ctx); capture(st);
      // a new group starts with one condition and the opposite match: "any of" inside "all of" and vice versa
      edit(st, el.dataset.ruleKey, el.dataset.path || "", (grp) => grp.items.push({
        match: grp.match === "all" ? "any" : "all", items: [{ field: "artist", op: "contains", value: "" }],
      }));
      redraw(st);
    },
    rbRemoveGroup(el, ctx) {
      const st = stOf(ctx); capture(st);
      const parts = el.dataset.path.split(".");
      const j = Number(parts.pop());
      edit(st, el.dataset.ruleKey, parts.join("."), (grp) => grp.items.splice(j, 1));
      redraw(st);
    },
    rbInsertCond(el, ctx) {
      // "+" on a row: a new condition on the same field right below it
      const st = stOf(ctx); capture(st);
      const path = el.dataset.path || "", j = Number(el.dataset.j);
      edit(st, el.dataset.ruleKey, path, (grp) => {
        const field = grp.items[j]?.field || "artist";
        const spec = catalogue.byName[field] || { type: "string" };
        grp.items.splice(j + 1, 0, { field, op: (catalogue.operators[spec.type] || ["contains"])[0], value: "" });
      });
      redraw(st);
      const box = document.querySelector(`[data-rb="${st.id}"] [data-rule="${el.dataset.ruleKey}"] [data-group="${path}"] > .rb-items`);
      box?.querySelector(`:scope > [data-cond="${j + 1}"] [data-c="value"]`)?.focus();
    },
    rbRemoveCond(el, ctx) {
      const st = stOf(ctx); capture(st);
      edit(st, el.dataset.ruleKey, el.dataset.path || "", (grp) => grp.items.splice(Number(el.dataset.j), 1));
      redraw(st);
    },
    rbChanged(el, ctx, e) {
      // a field change resets the operator/value inputs to the field's type
      const st = stOf(ctx);
      if (e.target.dataset.k === "as_pl_on") { // the playlist's name field comes with the tick
        const name = e.target.closest(".field").querySelector('[data-k="as_playlist"]');
        name.hidden = !e.target.checked;
        if (e.target.checked) name.focus();
        return;
      }
      if (e.target.dataset.k === "sort") { // "New random order" belongs to a random sort
        e.target.closest(".field").querySelector("[data-reshuffle]").hidden = e.target.value !== "random";
        return;
      }
      if (e.target.dataset.c === "field") {
        const row = e.target.closest("[data-cond]");
        const spec = catalogue.byName[e.target.value];
        const op = row.querySelector('[data-c="op"]'), first = (catalogue.operators[spec.type] || ["is"])[0];
        // the select still lists the old type's operators: add the new type's first one so it can be chosen
        if (![...op.options].some((o) => o.value === first)) op.add(new Option(first, first));
        op.value = first;
        capture(st);
        redraw(st);
      } else if (e.target.dataset.c === "op") {
        capture(st);
        redraw(st);
      }
    },
    async rbPick(el, ctx) {
      const st = stOf(ctx); capture(st);
      const box = el.closest("[data-libraries]");
      const conds = await pickFromLibrary(box?.dataset.libraries || "", box?.dataset.viewAs || "");
      if (!conds?.length) return;
      // into an empty rule (or one that matches "any"): the picks are its conditions; else one "any of" group
      edit(st, el.dataset.ruleKey, "", (grp) => {
        if (!grp.items.length) grp.match = "any";
        if (grp.match === "any") grp.items.push(...conds);
        else grp.items.push({ match: "any", items: conds });
      });
      redraw(st);
    },
    async rbPreset(el, ctx) {
      const st = stOf(ctx); capture(st);
      const libs = el.closest("[data-libraries]")?.dataset.libraries || "";
      const presets = (await get(`/rule-presets${libs ? `?${new URLSearchParams({ libraries: libs })}` : ""}`)).filter((x) => x.available);
      // a chip: that preset at once, unless it has parameters to set or items to pick (the dialog for it alone)
      const one = presets.find((x) => x.id === el.dataset.presetChip);
      const chosen = one && !one.params.length && !one.pick ? { preset: one, params: {} } : await choosePreset(one ? [one] : presets);
      if (!chosen) return;
      const rule = await post(`/rule-presets/${encodeURIComponent(chosen.preset.id)}/rule`, { params: chosen.params });
      if (chosen.preset.pick) { // "These artists or albums": the rule is what is picked
        const conds = await pickFromLibrary(libs);
        if (!conds?.length) return;
        rule.criteria = fromTree({ match: "any", items: conds });
      }
      st.rs.rules.push(rule);
      redraw(st);
      document.querySelector(`[data-rb="${st.id}"] [data-rule="rules:${st.rs.rules.length - 1}"] [data-k="name"]`)?.focus();
    },
  };
}

// --- rule presets -----------------------------------------------------------------------------------

/** A dialog listing the presets (those the libraries can serve) with their parameters. Resolves with
 * {preset, params} or null. */
async function choosePreset(presets) {
  let chosen = null;
  const row = (p, i) => html`<label class="preset-row" data-preset="${p.id}">
      <input type="radio" name="preset" value="${i}" ${i === 0 ? "checked" : ""}>
      <span><b>${p.name}</b><small class="muted">${p.description}</small>
        ${p.params.length ? html`<span class="preset-params">${p.params.map((x) => html`<span class="preset-param"><span>${x.label}</span>
          <input class="rb-input" type="number" inputmode="numeric" data-param="${x.name}" value="${x.default}" min="${x.min}" max="${x.max}" step="1" aria-label="${p.name}: ${x.label}"></span>`)}</span>` : ""}</span></label>`;
  const result = await dialog({
    title: t("rb.preset_title"),
    message: presets.length ? t("rb.preset_help") : t("rb.preset_none"),
    body: presets.length ? html`<div class="preset-list" role="radiogroup" aria-label="${t("rb.preset_title")}">${presets.map(row)}</div>` : "",
    buttons: [{ label: t("common.cancel"), value: null }, ...(presets.length ? [{ label: t("rb.preset_add"), value: "add", kind: "primary" }] : [])],
    initialFocus: 'input[name="preset"]:checked',
    onButton: (v, back) => {
      if (v !== "add") return true;
      const radio = back.querySelector('input[name="preset"]:checked');
      if (!radio) return false;
      const box = radio.closest("[data-preset]");
      const params = {};
      for (const input of box.querySelectorAll("[data-param]")) {
        const n = Number(input.value);
        const ok = input.value.trim() !== "" && Number.isInteger(n) && n >= Number(input.min) && n <= Number(input.max);
        input.toggleAttribute("aria-invalid", !ok);
        if (!ok) { input.focus(); return false; }
        params[input.dataset.param] = n;
      }
      chosen = { preset: presets[Number(radio.value)], params };
      return true;
    },
  });
  return result === "add" ? chosen : null;
}

// --- pick from the library --------------------------------------------------------------------------------

// the picker's tabs per library type: `field` is the rule field a tick becomes, `kind` narrows the values
const PICK_TABS = {
  music: [
    { id: "artist", field: "artist" }, { id: "album", field: "album" },
    { id: "genre", field: "genre" }, { id: "playlist", field: "playlist" },
  ],
  audiobookshelf: [
    { id: "audiobook", field: "series", kind: "audiobook" }, { id: "podcast", field: "series", kind: "podcast" },
    { id: "episode", field: "title", kind: "podcast" }, { id: "bookseries", field: "bookseries" }, { id: "author", field: "author" },
    { id: "genre", field: "genre" }, { id: "playlist", field: "playlist" },
  ],
};
const tabsFor = (lib) => PICK_TABS[lib?.type] || PICK_TABS.music;
const PICK_LIMIT = 200;
const pickLibrary = new Map(); // libs → the library last browsed in the picker

/** A dialog with a searchable checklist per field (artists, albums, genres, playlists of `libs`, comma-separated ids;
 * "" = all). Resolves with the builder conditions of the ticked values (`is` / `in playlist`), or null. */
export async function pickFromLibrary(libs, viewAs = "") {
  const ALL_TABS = [...new Map(Object.values(PICK_TABS).flat().map((x) => [x.id, x])).values()];
  const picked = Object.fromEntries(ALL_TABS.map((x) => [x.id, new Set()]));
  // one library is browsed at a time: the template's ones (all registered ones for "all libraries")
  const all = await get("/libraries").catch(() => []);
  const wanted = libs ? libs.split(",").filter(Boolean) : null;
  const choices = wanted ? wanted.map((id) => all.find((l) => l.id === id) || { id, name: id }) : all;
  const last = pickLibrary.get(libs);
  // "View as" one of the library's accounts: `viewAs` = "lid:account" preselects one
  const [asLib, asAcc] = viewAs.split(":");
  const first = choices.some((l) => l.id === asLib) ? asLib : choices.some((l) => l.id === last) ? last : choices[0]?.id || "";
  const state = { field: "", query: "", seq: 0, library: first, account: first === asLib ? asAcc || "" : "" };
  const accountsOf = (lid) => Object.entries(choices.find((l) => l.id === lid)?.accounts || {});
  const asHtml = () => (accountsOf(state.library).length ? html`<label for="pickAs">${t("rb.pick_as")}</label>
    <select id="pickAs" data-pick-as><option value="">${t("rb.pick_as_main")}</option>
      ${accountsOf(state.library).map(([aid, acc]) => html`<option value="${aid}" ${aid === state.account ? "selected" : ""}>${acc.name}</option>`)}</select>` : "");
  const tabs = () => tabsFor(choices.find((l) => l.id === state.library));
  state.field = tabs()[0].id;
  const tabsHtml = () => tabs().map((x) => html`<button type="button" role="tab" data-pick-field="${x.id}" aria-selected="${x.id === state.field}">${t(`rb.pick.${x.id}`)} <span data-pick-n="${x.id}"></span></button>`);
  const one = choices.length === 1;
  const abs = choices.find((l) => l.id === state.library)?.type === "audiobookshelf";
  const promise = dialog({
    title: t("rb.pick_title"), message: t(abs ? "rb.pick_help_abs" : "rb.pick_help"),
    body: html`<div data-picker>
      ${choices.length ? html`<div class="field pick-library"><label for="pickLibrary">${t("rb.pick_library")}</label>
        <select id="pickLibrary" data-pick-library ${one ? "disabled" : ""}>${choices.map((l) => html`<option value="${l.id}" ${l.id === state.library ? "selected" : ""}>${l.name || l.id}</option>`)}</select>
        ${one ? html`<div class="muted" style="font-size:12px;margin-top:4px">${t("rb.pick_library_one", { name: choices[0].name || choices[0].id })}</div>` : ""}</div>
      <div class="field pick-as" data-pick-as-box>${asHtml()}</div>` : ""}
      <div class="switcher" role="tablist" data-pick-tabs>${tabsHtml()}</div>
      <div class="field"><label for="pickSearch">${t("rb.pick_search")}</label><input id="pickSearch" type="search" autocomplete="off" spellcheck="false">
        <div class="muted" style="font-size:12px;margin-top:4px">${t("rb.pick_search_hint")}</div></div>
      <div class="pick-list" data-pick-list aria-live="polite"></div></div>`,
    buttons: [{ label: t("common.cancel"), value: null }, { label: t("rb.pick_add_none"), value: "add", kind: "primary" }],
    initialFocus: "#pickSearch",
    onButton: (v) => {
      if (v !== "add") return true;
      if (ALL_TABS.some((x) => picked[x.id].size)) return true;
      document.querySelector("[data-picker] [data-pick-list]")?.setAttribute("aria-invalid", "true");
      return false;
    },
  });
  const root = document.querySelector("#overlay .modal-back:last-child");
  const list = root.querySelector("[data-pick-list]");
  const addBtn = root.querySelector('.modalfoot [data-v="1"]');
  let rows = [];
  const counts = () => {
    const n = ALL_TABS.reduce((a, x) => a + picked[x.id].size, 0);
    // nothing ticked: "Add", looking disabled (a click marks the list)
    addBtn.textContent = n ? t("rb.pick_add", { n }) : t("rb.pick_add_none");
    addBtn.dataset.ready = n ? "true" : "false";
    for (const x of tabs()) root.querySelector(`[data-pick-n="${x.id}"]`).textContent = picked[x.id].size ? `(${picked[x.id].size})` : "";
  };
  const show = () => {
    const q = state.query.trim().toLocaleLowerCase();
    // 1-2 letters: filter what is loaded (the service searches from 3); ticked values stay on top
    const shown = rows.filter((r) => !q || r.value.toLocaleLowerCase().includes(q) || (r.label || "").toLocaleLowerCase().includes(q));
    const sel = [...picked[state.field]].filter((v) => !shown.some((r) => r.value === v)).map((value) => ({ value }));
    const all = [...sel, ...shown];
    setHtml(list, all.length ? html`${all.map((r, i) => html`<label class="checkrow pick-row"><input type="checkbox" data-pick-value="${i}" ${picked[state.field].has(r.value) ? "checked" : ""}>
        <span class="pick-name">${r.value}</span>${r.label ? html`<small class="muted">${r.label}</small>` : ""}
        ${r.count != null ? html`<small class="muted">${t("rb.pick_count", { n: r.count })}</small>` : ""}</label>`)}` : html`<div class="muted">${t("rb.pick_none")}</div>`);
    list.removeAttribute("aria-invalid");
    list._values = all.map((r) => r.value);
  };
  const load = async () => {
    const mine = ++state.seq;
    const q = state.query.trim();
    setHtml(list, html`<div class="muted">${t("rb.pick_loading")}</div>`);
    const tab = tabs().find((x) => x.id === state.field);
    const params = new URLSearchParams({ field: tab.field, limit: String(PICK_LIMIT) });
    if (tab.kind) params.set("kind", tab.kind);
    if (q.length >= 3) params.set("q", q);
    if (state.library) params.set("libraries", state.library);
    else if (libs) params.set("libraries", libs);
    try {
      const res = await get(`/rules/values?${params}${state.account ? `&${new URLSearchParams({ accounts: `${state.library}:${state.account}` })}` : ""}`);
      if (state.account && tab.field === "playlist") { // a playlist the main account does not have is marked
        const main = new Set((await get(`/rules/values?${params}`)).map((r) => r.value));
        const name = accountsOf(state.library).find(([aid]) => aid === state.account)?.[1].name || state.account;
        for (const r of res) if (!main.has(r.value)) r.label = t("rb.pick_only_for", { name });
      }
      if (mine !== state.seq) return;
      rows = res;
      show();
    } catch (e) {
      if (mine === state.seq) setHtml(list, problemBox({ message: t("rb.pick_error", { error: e.message || String(e) }) }));
    }
  };
  let timer = null;
  root.addEventListener("input", (e) => {
    if (e.target.id !== "pickSearch") return;
    const before = state.query.trim().length >= 3;
    state.query = e.target.value;
    clearTimeout(timer);
    // the service is asked again only when the search reaches or leaves 3 letters or changes beyond them
    if (state.query.trim().length >= 3 || before) timer = setTimeout(load, 200);
    else show();
  });
  root.addEventListener("change", (e) => {
    if (e.target.id === "pickLibrary") {
      state.library = e.target.value;
      state.account = "";
      setHtml(root.querySelector("[data-pick-as-box]"), html`${asHtml()}`);
      pickLibrary.set(libs, state.library);
      // another type of library has other tabs: the first one, ticks of the others stay counted
      if (!tabs().some((x) => x.id === state.field)) state.field = tabs()[0].id;
      setHtml(root.querySelector("[data-pick-tabs]"), html`${tabsHtml()}`);
      counts();
      rows = [];
      load();
      return;
    }
    if (e.target.id === "pickAs") { state.account = e.target.value; rows = []; load(); return; }
    const i = e.target.dataset?.pickValue;
    if (i === undefined) return;
    const v = list._values[Number(i)];
    if (e.target.checked) picked[state.field].add(v); else picked[state.field].delete(v);
    counts();
  });
  root.addEventListener("click", (e) => {
    const tab = e.target.closest("[data-pick-field]");
    if (!tab || tab.dataset.pickField === state.field) return;
    state.field = tab.dataset.pickField;
    for (const b of root.querySelectorAll("[data-pick-field]")) b.setAttribute("aria-selected", String(b === tab));
    rows = [];
    load();
  });
  counts();
  load();
  if ((await promise) !== "add") return null;
  return ALL_TABS.flatMap((x) => [...picked[x.id]].map((v) => (x.field === "playlist"
    ? { field: "playlist", op: "inPlaylist", value: v, ref: { name: v } }
    : { field: x.field, op: "is", value: v })));
}

// autocomplete: fill the field's datalist from the index while typing (debounced), after more than 2 characters,
// from the libraries of the nearest [data-libraries] (the template's library, the device's libraries)
let timer = null;
let suggestSeq = 0; // only the latest query fills the datalist: an older, slower answer is dropped
document.addEventListener("input", (e) => {
  const input = e.target.closest?.("[data-suggest]");
  if (!input) return;
  clearTimeout(timer);
  const mine = ++suggestSeq;
  if (input.value.trim().length < 3) {
    const dl = document.getElementById(input.getAttribute("list"));
    if (dl) setHtml(dl, "");
    return;
  }
  timer = setTimeout(async () => {
    const libs = input.closest("[data-libraries]")?.dataset.libraries || "";
    const viewAs = input.closest("[data-libraries]")?.dataset.viewAs || ""; // "lid:account"
    try {
      const params = new URLSearchParams({ field: input.dataset.suggest, q: input.value, limit: "20" });
      if (libs) params.set("libraries", libs);
      if (viewAs) params.set("accounts", viewAs);
      const values = await get(`/rules/values?${params}`);
      if (mine !== suggestSeq) return;
      const dl = document.getElementById(input.getAttribute("list"));
      if (dl) setHtml(dl, html`${values.map((v) => html`<option value="${v.value}">${v.label || ""} (${v.count})</option>`)}`);
    } catch { /* suggestions are optional */ }
  }, 200);
});

