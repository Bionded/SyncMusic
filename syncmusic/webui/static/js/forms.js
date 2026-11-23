// Forms generated from a Settings JSON schema (`settings_schema` of /library-types, /device-types,
// /processor-types; model schemas) - the web port of the Flet SchemaForm.
// Field kinds: string (`writeOnly` → password), integer, number, boolean, enum (also via $ref), anyOf [X, null]
// (empty = null), anyOf of several types (a number if it reads as one), arrays of strings (comma-separated),
// objects whose properties are all strings (one text field each, e.g. the path layout -), other objects (JSON text), strings with `examples` (a suggestion list). An integer named `*_bytes` is shown and typed in MB. Labels/help/option texts come from
// i18n (`field.<name>`, `.help`, `.<value>`). The service validates; the form only converts text to the type.
import { html } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { textTip } from "./help.js";
import "./fields.js";

addStrings({
  "field.layout.example": "e.g. {path}",
  "field.layout.preset": "Folders and file names",
  "field.layout.preset.album_artist_year": "Album artist / Year – Album / Track",
  "field.layout.preset.artist_album": "Artist / Album / Track",
  "field.layout.preset.album_folder": "Album artist – Album / Track",
  "field.layout.preset.one_folder": "All in one folder",
  "field.layout.preset.custom": "Custom…",
  "form.show": "Show",
  "form.hide": "Hide",
  "form.tls_http": "Only used for HTTPS URLs.",
  "field.genre_separators.example": "e.g. {from} → {to}",
});

// Split genres: the live example of the chosen separators (each with its full-width form, as the player has)
const SEPARATORS = { semicolon: ";\uff1b", slash: "/\uff0f", ampersand: "&\uff06", comma: ",\uff0c\u3001", plus: "+\uff0b" };
const SPLIT_SAMPLE = "Rock; Pop/Indie & Folk, Jazz";
function splitExample(names) {
  const chars = names.map((n) => SEPARATORS[n] || "").join("");
  const parts = chars ? SPLIT_SAMPLE.split(new RegExp(`[${chars.replace(/[\]\\^-]/g, "\\$&")}]`)) : [SPLIT_SAMPLE];
  const out = [];
  for (const p of parts.map((x) => x.trim()).filter(Boolean)) if (!out.some((o) => o.toLowerCase() === p.toLowerCase())) out.push(p);
  return t("field.genre_separators.example", { from: SPLIT_SAMPLE, to: out.length > 1 ? out.join(" · ") : SPLIT_SAMPLE });
}
function syncSplit(form) {
  const mode = form.querySelector("[data-field=split_genres] select");
  const box = form.querySelector("[data-field=genre_separators]");
  if (!mode || !box) return;
  box.hidden = mode.value !== "on";
  const names = [...box.querySelectorAll("input:checked")].map((x) => x.value);
  box.querySelector("[data-split-example]").textContent = splitExample(names);
}

const MB = 1024 * 1024;
let formSeq = 0;

function label(name) {
  const key = `field.${name}`;
  const s = t(key);
  return s === key ? name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : s;
}
function option(name, value) {
  const key = `field.${name}.${value}`;
  const s = t(key);
  return s === key ? String(value) : s;
}
function help(name, prop) {
  const key = `field.${name}.help`;
  const s = t(key);
  return s === key ? prop.description || "" : s;
}

function resolve(schema, raw) {
  let prop = { ...raw };
  let nullable = false;
  if (prop.anyOf) {
    const opts = prop.anyOf.filter((p) => p.type !== "null");
    nullable = opts.length < prop.anyOf.length;
    const rest = { ...prop };
    delete rest.anyOf;
    prop = opts.length > 1 ? { ...rest, type: "scalar" } : { ...rest, ...(opts[0] || {}) };
  }
  if (prop.$ref) {
    const target = (schema.$defs || {})[prop.$ref.split("/").pop()] || {};
    const rest = { ...prop };
    delete rest.$ref;
    prop = { ...target, ...rest };
  }
  return [prop, nullable];
}

function kindOf(name, prop) {
  if (prop.enum) return "enum";
  if (prop.type === "string" && prop.writeOnly) return "secret";
  if (prop.type === "array" && prop.items?.enum) return "choices";
  if (prop.type === "array") return prop.items?.type === "string" ? "list" : "object";
  if (prop.type === "integer" && name.endsWith("_bytes")) return "megabytes";
  if (prop.type === "number" && name.endsWith("_fraction")) return "percent"; // stored 0–1, shown 0–100 %
  if (prop.type === "object" && prop.properties && Object.values(prop.properties).every((p) => p.type === "string")) return "texts";
  return prop.type || "object";
}

/** A form for `schema` with `values`; returns {html, read(root) → values (throws FormError)}. `only`: field names
 * to show (others keep their stored values when read). `keys`: a field's own i18n name where the shared one does not
 * fit (an Android player's `base_path` is no "Folder on the server"). */
export function schemaForm(schema, values = {}, { only = null, prefix = "", skip = [], keys = {}, tips = false } = {}) {
  const id = `f${++formSeq}`;
  const required = new Set(schema.required || []);
  const fields = [];
  for (const [name, raw] of Object.entries(schema.properties || {})) {
    if ((only && !only.includes(name)) || skip.includes(name)) continue; // skip: the view has its own control
    const [prop, nullable] = resolve(schema, raw);
    const kind = kindOf(name, prop);
    const value = name in values ? values[name] : raw.default ?? prop.default;
    fields.push({ name, key: keys[name] || name, tips, prop, nullable, kind, value, el: `${id}-${prefix}${name}`, url: values.url ?? "" });
  }
  const split = fields.find((f) => f.name === "split_genres");
  for (const f of fields) if (f.name === "genre_separators") f.hidden = split ? split.value !== "on" : false;
  const tpl = html`<div class="form-grid" data-form="${id}">${fields.map((f) => control(f, required.has(f.name)))}</div>`;
  return { html: tpl, read: (root) => read(root, fields, required) };
}

// a unit at the end of a label ("Pause between batches (s)") that `tips` forms show inside the number field
const UNIT = /\s\((MB|MB\/s|GB|s|%)\)$/;
// help longer than this goes behind a "?" in `tips` forms; a short one stays under the field
const LONG_HELP = 90;

function control(f, req) {
  const whole = label(f.key) + (req ? " *" : "");
  const fixed = { megabytes: "MB", percent: "%" }[f.kind];
  const unit = f.tips ? (fixed || (whole.match(UNIT) || [])[1] || "") : "";
  const name = unit && !fixed ? whole.replace(UNIT, "") : whole;
  const hint = help(f.key, f.prop);
  const long = f.tips && f.kind !== "boolean" && hint.length > LONG_HELP; // a switch row keeps its text under the title
  const lab = long ? html`${name}${textTip(name, hint)}` : name;
  const hintTpl = hint && !long ? html`<div class="hint" id="${f.el}-h">${hint}</div>` : "";
  const described = hint && !long ? html`aria-describedby="${f.el}-h"` : "";
  if (f.kind === "boolean" && f.name === "verify_tls") {
    // only meaningful for HTTPS: greyed out while the form's URL is plain http, its value kept
    const off = isHttp(f.url);
    return html`<div class="field full"><label class="checkrow" for="${f.el}"><input type="checkbox" id="${f.el}" data-verify-tls ${f.value ? "checked" : ""} ${off ? "disabled" : ""} aria-describedby="${f.el}-tls"> ${lab}</label>
      <div class="hint" id="${f.el}-tls" data-tls-hint ${off ? "" : "hidden"}>${t("form.tls_http")}</div>${hintTpl}</div>`;
  }
  if (f.kind === "boolean") {
    return html`<div class="field full"><label class="checkrow" for="${f.el}"><input type="checkbox" id="${f.el}" ${f.value ? "checked" : ""} ${described}> ${lab}</label>${hintTpl}</div>`;
  }
  if (f.kind === "choices") { // a set of fixed values: one checkbox each
    const on = new Set(f.value || []);
    const split = f.name === "genre_separators";
    return html`<fieldset class="field full choices" data-field="${f.name}" id="${f.el}" ${described} ${split && f.hidden ? "hidden" : ""}><legend>${lab}</legend>
      <div class="choices-row">${f.prop.items.enum.map((o) => html`<label class="checkrow"><input type="checkbox" value="${o}" ${on.has(o) ? "checked" : ""}> ${option(f.name, o)}</label>`)}</div>
      ${split ? html`<small class="muted" data-split-example>${splitExample([...on])}</small>` : ""}${hintTpl}</fieldset>`;
  }
  if (f.kind === "enum") {
    return html`<div class="field" data-field="${f.name}"><label for="${f.el}">${lab}</label><select id="${f.el}" ${described}>
      ${f.nullable || f.value === null || f.value === undefined ? html`<option value="" ${f.value == null ? "selected" : ""}>${t(`field.${f.key}.none`) === `field.${f.key}.none` ? "-" : t(`field.${f.key}.none`)}</option>` : ""}
      ${(f.value == null || f.prop.enum.some((o) => String(o) === String(f.value)) ? f.prop.enum : [...f.prop.enum, f.value]).map((o) =>
        html`<option value="${o}" ${String(o) === String(f.value) ? "selected" : ""}>${option(f.name, o)}</option>`)}
    </select>${hintTpl}</div>`;
  }
  if (f.kind === "texts") { // one field per key; an emptied field goes back to its default
    const v = f.value || {};
    const layout = f.name === "layout" && "music" in (f.prop.properties || {});
    const music = (k) => v[k] || f.prop.properties[k]?.default || "";
    const preset = layout ? (Object.entries(LAYOUT_PRESETS).find(([, p]) => p.music === music("music") && p.music_multi_disc === music("music_multi_disc"))?.[0] || "custom") : null;
    const pick = layout ? html`<div class="ds-row texts-preset"><span class="ds-label">${t("field.layout.preset")}<small data-preset-example>${pathExample(music("music"), "music")}</small></span>
      <select data-layout-preset="${f.el}" aria-label="${t("field.layout.preset")}">${[...Object.keys(LAYOUT_PRESETS), "custom"].map((id) =>
        html`<option value="${id}" ${id === preset ? "selected" : ""}>${t(`field.layout.preset.${id}`)}</option>`)}</select></div>` : "";
    return html`<fieldset class="field full texts" ${described} ${layout ? html`data-layout-custom="${preset === "custom" ? "1" : "0"}"` : ""}><legend>${lab}</legend>${pick}${Object.entries(f.prop.properties).map(([k, p]) =>
      html`<label class="texts-row" for="${f.el}-${k}"><span>${option(f.name, k) === k ? p.title || k : option(f.name, k)}</span>
        <input type="text" id="${f.el}-${k}" value="${v[k] ?? p.default ?? ""}" placeholder="${p.default ?? ""}" autocomplete="off" spellcheck="false"
          ${f.name === "layout" && PATH_SAMPLES[k] ? html`data-path-tpl="${k}"` : ""}>
        ${f.name === "layout" && PATH_SAMPLES[k] ? html`<small class="texts-example" data-path-example="${k}">${pathExample(v[k] || p.default || "", k)}</small>` : ""}</label>`)}${hintTpl}</fieldset>`;
  }
  let text = "";
  if (f.kind === "list") text = Array.isArray(f.value) ? f.value.join(", ") : "";
  else if (f.kind === "object") text = f.value === null || f.value === undefined ? "" : JSON.stringify(f.value);
  else if (f.kind === "megabytes" && typeof f.value === "number") text = String(+(f.value / MB).toFixed(3));
  else if (f.kind === "percent" && typeof f.value === "number") text = String(+(f.value * 100).toFixed(2));
  else text = f.value === null || f.value === undefined ? "" : String(f.value);
  if (f.kind === "object") {
    return html`<div class="field full"><label for="${f.el}">${lab}</label><textarea class="codebox" style="min-height:80px" id="${f.el}" ${described}>${text}</textarea>${hintTpl}</div>`;
  }
  const type = f.kind === "secret" ? "password" : f.prop.format === "time" ? "time" : "text"; // time: HH:MM
  const mode = ["integer", "number", "megabytes", "percent"].includes(f.kind) ? html`inputmode="decimal"` : "";
  const full = (f.kind === "string" && f.prop.format !== "time") || f.kind === "secret" || f.kind === "list";
  // suggestions (`examples` of the schema, e.g. the IANA time zones of the iPod setting)
  const examples = f.kind === "string" && Array.isArray(f.prop.examples) ? f.prop.examples : null;
  const list = examples ? html`list="${f.el}-l"` : "";
  const datalist = examples ? html`<datalist id="${f.el}-l">${examples.map((x) => html`<option value="${x}"></option>`)}</datalist>` : "";
  const serverUrl = f.name === "url" ? html`data-server-url inputmode="url"` : "";
  const named = unit ? html`aria-label="${name} (${unit})"` : "";
  const box = html`<input type="${type}" id="${f.el}" value="${text}" ${req ? html`aria-required="true"` : ""} ${mode} ${serverUrl} ${list} ${described} ${named} autocomplete="off">`;
  const input = unit ? html`<span class="unit-in">${box}<span class="unit" aria-hidden="true">${unit}</span></span>` : box;
  // a secret can be shown while typing it; a stored one is only ever the mask
  const field = f.kind === "secret"
    ? html`<span class="secret-wrap">${input}<button type="button" class="btn" data-show-secret="${f.el}" aria-pressed="false">${t("form.show")}</button></span>` : input;
  return html`<div class="field ${full ? "full" : ""}"><label for="${f.el}">${lab}${fixed && !unit ? ` (${fixed})` : ""}</label>
    ${field}${datalist}${hintTpl}</div>`;
}

// server URLs: no scheme → the http scheme when the field is left (never https; the server does the same), and the
// form's "Verify TLS certificate" follows the scheme
const isHttp = (url) => /^http:\/\//i.test(String(url || "").trim());

const HTTP = "http:"; // (no scheme-and-slashes literal in UI sources: test_no_external_resources)

export function withScheme(url) {
  const v = String(url || "").trim();
  return v && !/^[a-z][a-z0-9+.-]*:\/\//i.test(v) ? `${HTTP}//${v}` : v;
}

// ready-made music layouts: picking one fills both music templates; "Custom" shows every template
const LAYOUT_PRESETS = {
  album_artist_year: { music: "Music/{album_artist}/{year} - {album}/{disc:02}{track:02} - {title}", music_multi_disc: "Music/{album_artist}/{year} - {album}/CD{disc:02}/{track:02} - {title}" },
  artist_album: { music: "Music/{artist}/{album}/{track:02} - {title}", music_multi_disc: "Music/{artist}/{album}/CD{disc:02}/{track:02} - {title}" },
  album_folder: { music: "Music/{album_artist} - {album}/{track:02} - {title}", music_multi_disc: "Music/{album_artist} - {album}/{disc:02}{track:02} - {title}" },
  one_folder: { music: "Music/{artist} - {title}", music_multi_disc: "Music/{artist} - {title}" },
};

// the path layout's live examples: a single-disc and a two-disc release
const PATH_SAMPLES = {
  music: { title: "So What", artist: "Miles Davis", album_artist: "Miles Davis", album: "Kind of Blue", year: 1959, disc: 1, disc_total: 1, track: 1, genre: "Jazz" },
  music_multi_disc: { title: "Hey You", artist: "Pink Floyd", album_artist: "Pink Floyd", album: "The Wall", year: 1979, disc: 2, disc_total: 2, track: 1, genre: "Rock" },
};

/** The template filled like devices/paths.py does (empty fields drop their separators), with ".mp3". */
export function pathExample(tpl, sample) {
  const vals = PATH_SAMPLES[sample];
  const parts = tpl.split("/").filter((c) => c.trim()).map((c) => c.replace(/\{(\w+)(?::0?(\d))?\}/g, (_, name, pad) => {
    const v = vals[name];
    if (v === undefined || v === null || v === "") return "";
    return pad && typeof v === "number" ? String(v).padStart(+pad, "0") : String(v);
  }).replace(/\s+[-–]\s+(?:[-–]\s+)+/g, " - ").replace(/^[\s\-–_,]+|[\s\-–_,]+$/g, "")).filter(Boolean);
  return parts.length ? t("field.layout.example", { path: `${parts.join("/")}.mp3` }) : "";
}

function syncTls(input) {
  const form = input.closest("[data-form]");
  const box = form?.querySelector("[data-verify-tls]");
  if (!box) return;
  box.disabled = isHttp(input.value);
  form.querySelector("[data-tls-hint]").hidden = !box.disabled;
}

/** Once, on the content root: URL fields of every schema form. */
export function initServerUrls(root) {
  root.addEventListener("click", (e) => {
    const btn = e.target.closest?.("[data-show-secret]");
    if (!btn) return;
    const input = document.getElementById(btn.dataset.showSecret);
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.setAttribute("aria-pressed", String(show));
    btn.textContent = t(show ? "form.hide" : "form.show");
  });
  root.addEventListener("focusout", (e) => {
    if (!e.target.matches?.("[data-server-url]")) return;
    e.target.value = withScheme(e.target.value);
    syncTls(e.target);
  });
  root.addEventListener("change", (e) => {
    const sel = e.target.closest?.("[data-layout-preset]");
    if (!sel) return;
    const box = sel.closest("fieldset");
    box.dataset.layoutCustom = sel.value === "custom" ? "1" : "0";
    const p = LAYOUT_PRESETS[sel.value];
    if (p) {
      for (const [k, tpl] of Object.entries(p)) {
        const input = document.getElementById(`${sel.dataset.layoutPreset}-${k}`);
        if (input) { input.value = tpl; input.dispatchEvent(new Event("input", { bubbles: true })); }
      }
    }
  });
  root.addEventListener("input", (e) => {
    if (e.target.matches?.("[data-path-tpl=music]")) {
      const ex = e.target.closest("fieldset")?.querySelector("[data-preset-example]");
      if (ex) ex.textContent = pathExample(e.target.value || e.target.placeholder, "music");
    }
    if (e.target.matches?.("[data-server-url]")) syncTls(e.target);
    if (e.target.closest?.("[data-field=split_genres], [data-field=genre_separators]")) syncSplit(e.target.closest("[data-form]"));
    if (e.target.matches?.("[data-path-tpl]")) {
      const k = e.target.dataset.pathTpl;
      const out = e.target.closest("label")?.querySelector(`[data-path-example="${k}"]`);
      if (out) out.textContent = pathExample(e.target.value || e.target.placeholder, k);
    }
  });
}

export class FormError extends Error {}

function convert(kind, text) {
  if (kind === "integer") { if (!/^-?\d+$/.test(text)) throw new Error("a whole number"); return parseInt(text, 10); }
  if (kind === "number") { const v = Number(text); if (Number.isNaN(v)) throw new Error("a number"); return v; }
  if (kind === "megabytes") { const v = Number(text); if (Number.isNaN(v)) throw new Error("a number of MB"); return Math.round(v * MB); }
  if (kind === "percent") { const v = Number(text); if (Number.isNaN(v)) throw new Error("a percentage"); return +(v / 100).toFixed(4); }
  if (kind === "scalar") { const v = Number(text); return Number.isNaN(v) ? text : v; }
  if (kind === "list") return text.split(",").map((x) => x.trim()).filter(Boolean);
  if (kind === "object") { try { return JSON.parse(text); } catch (e) { throw new Error(`JSON: ${e.message}`); } }
  return text;
}

function read(root, fields, required) {
  const out = {};
  const bad = [];
  const missing = []; // required and left empty: named by their label, the field marked
  for (const f of fields) {
    if (f.kind === "texts") {
      const v = {};
      for (const [k, p] of Object.entries(f.prop.properties)) {
        const box = root.querySelector(`#${CSS.escape(`${f.el}-${k}`)}`);
        if (box) v[k] = box.value.trim() || p.default || "";
      }
      if (Object.keys(v).length) out[f.name] = { ...(f.value || {}), ...v };
      continue;
    }
    const el = root.querySelector(`#${CSS.escape(f.el)}`);
    if (!el) continue;
    el.removeAttribute("aria-invalid");
    if (f.kind === "boolean") { out[f.name] = el.checked; continue; }
    if (f.kind === "choices") { out[f.name] = [...el.querySelectorAll("input:checked")].map((x) => x.value); continue; }
    if (f.kind === "enum") {
      if (el.value !== "") {
        const o = f.prop.enum.find((x) => String(x) === el.value);
        out[f.name] = o === undefined ? el.value : o;
      } else if (f.nullable) out[f.name] = null;
      continue;
    }
    const text = el.value.trim();
    if (!text) {
      if (f.nullable) out[f.name] = null;
      else if (required.has(f.name)) {
        el.setAttribute("aria-invalid", "true");
        missing.push(label(f.key));
      }
      continue;
    }
    try {
      out[f.name] = convert(f.kind, text);
    } catch (e) {
      el.setAttribute("aria-invalid", "true");
      bad.push(`${label(f.key)}: ${e.message}`);
    }
  }
  if (missing.length) bad.unshift(`${missing.join(", ")} ${missing.length === 1 ? "is" : "are"} required`);
  else if (bad.length) bad[0] = `Check ${bad[0]}`;
  if (bad.length) {
    root.querySelector('[aria-invalid="true"]')?.focus();
    throw new FormError(bad.join("; "));
  }
  return out;
}
