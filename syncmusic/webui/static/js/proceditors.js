// Dedicated settings editors per processor type: no raw JSON. Each returns what `schemaForm`
// returns ({html, read(root) → settings}); the fields an editor does not draw itself stay in the generic schema
// form (and a processor type without an editor gets only that form).
// - transcode: the rules as rows "from formats → to, quality, when" with chips and dropdowns; the sample-rate and
//   bit-depth limits as dropdowns (the device's limit / no limit / a value);
// - retag: the tag templates as rows "tag ← template";
// - covers: "progressive JPEG" as automatic / yes / no.
// Row add/remove buttons are `procRowAdd` / `procRowRemove` (views/processors.js), which redraw the chain.
import { html } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { schemaForm, FormError } from "./forms.js";
import { icon } from "./icons.js";

addStrings({
  "pe.rules": "Conversion rules",
  "pe.rules_help": "The first rule that matches a file decides. A file no rule matches is converted only when the device cannot play it.",
  "pe.no_rules": "No rules: files the device cannot play are converted to a format it plays.",
  "pe.rule": "Rule {n}",
  "pe.from": "From",
  "pe.from_any": "No box ticked = any format",
  "pe.to": "To",
  "pe.quality": "Quality",
  "pe.when": "When",
  "pe.above": "Only lossless files and those above the target bitrate",
  "pe.add_rule": "Add a rule",
  "pe.remove_rule": "Remove rule {n}",
  "pe.to.mp3": "MP3", "pe.to.aac": "AAC", "pe.to.alac": "ALAC (lossless)", "pe.to.flac": "FLAC (lossless)",
  "pe.to.opus": "Opus", "pe.to.ogg": "Ogg Vorbis", "pe.to.copy": "Keep as it is",
  "pe.when.always": "Always",
  "pe.when.not_supported": "When the device cannot play it",
  "pe.when.space_needed": "Only when space runs out",
  "pe.q_default": "Default ({kbps} kbit/s)",
  "pe.q_default_none": "Default",
  "pe.q_none": "Lossless: no quality setting",
  "pe.q_kbps": "{kbps} kbit/s",
  "pe.q_vbr_mp3": "VBR V{v} (about {kbps} kbit/s)",
  "pe.q_vbr_ogg": "VBR q{v} (about {kbps} kbit/s)",
  "pe.max_sample_rate": "Maximum sample rate",
  "pe.max_bit_depth": "Maximum bit depth",
  "pe.rate_device": "The device's limit",
  "pe.rate_none": "No limit",
  "pe.rate_hz": "{hz} Hz",
  "pe.depth_bits": "{bits} bit",
  "pe.templates": "Tag templates",
  "pe.templates_help": "Write a tag from other fields, e.g. title ← {track_no:02d} {title}. Fields: {fields}.",
  "pe.no_templates": "No templates: tags are written as the library has them.",
  "pe.tag": "Tag",
  "pe.template": "Template",
  "pe.add_template": "Add a template",
  "pe.remove_template": "Remove the template for {tag}",
  "pe.template_empty": "A tag template is empty: fill it in or remove it.",
  "pe.template_twice": "{tag} has two templates.",
  "pe.progressive": "Progressive JPEG",
  "pe.progressive.auto": "Automatic (the device's choice)",
  "pe.progressive.yes": "Yes",
  "pe.progressive.no": "No",
});

const FORMATS = ["mp3", "aac", "alac", "flac", "ogg", "opus", "wav", "aiff", "ape", "wv", "wma"];
const TARGETS = ["mp3", "aac", "alac", "flac", "opus", "ogg", "copy"];
const WHEN = ["always", "not_supported", "space_needed"];
// as in processors/transcode.py
const DEFAULT_KBPS = { aac: 256, mp3: 320, opus: 160, ogg: 192 };
const STANDARD_KBPS = [64, 80, 96, 112, 128, 160, 192, 224, 256, 320];
const LAME_VBR_KBPS = [245, 225, 190, 175, 165, 130, 115, 100, 85, 65];
const VORBIS_Q_KBPS = [64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 500];
const RATES = [44100, 48000, 88200, 96000, 176400, 192000];
const DEPTHS = [16, 24, 32];
// as in processors/retag.py
const TAG_TARGETS = ["title", "artist", "album", "album_artist", "genre", "comment"];
const TEMPLATE_FIELDS = ["title", "artist", "album_artist", "album", "track_no", "disc_no", "year", "genre", "comment", "series", "author", "chapter_index", "kind"];

/** The settings form of processor `type` (schema from /processor-types). `index` names the chain row. */
export function processorForm(type, schema, settings, index) {
  const prefix = `p${index}-`;
  const editor = EDITORS[type];
  if (!editor) return schemaForm(schema, settings, { prefix });
  const own = editor.fields;
  const rest = schemaForm(schema, settings, { prefix, skip: own });
  const mine = editor.draw(settings, prefix, index);
  return {
    html: editor.after ? html`${rest.html}${mine}` : html`${mine}${rest.html}`,
    read: (root) => ({ ...rest.read(root), ...editor.read(root, prefix, settings) }),
  };
}

/** A new empty row for the list `list` of a processor's settings (procRowAdd). */
export function newRow(type, list) {
  if (type === "transcode" && list === "rules") return { from: [], to: "mp3", when: "not_supported", quality: { vbr: null, bitrate_kbps: null }, above_target_only: false };
  if (type === "retag" && list === "templates") return ["title", ""];
  return null;
}

// --- transcode ------------------------------------------------------------------------------------------------------

function qualityOptions(to, q) {
  if (to === "alac" || to === "flac" || to === "copy") return [["", t("pe.q_none")]];
  const opts = [["", DEFAULT_KBPS[to] ? t("pe.q_default", { kbps: DEFAULT_KBPS[to] }) : t("pe.q_default_none")]];
  const kbps = [...STANDARD_KBPS];
  if (q?.bitrate_kbps && !kbps.includes(q.bitrate_kbps)) kbps.push(q.bitrate_kbps);
  for (const k of kbps.sort((a, b) => a - b)) opts.push([`k:${k}`, t("pe.q_kbps", { kbps: k })]);
  if (to === "mp3") LAME_VBR_KBPS.forEach((k, v) => opts.push([`v:${v}`, t("pe.q_vbr_mp3", { v, kbps: k })]));
  if (to === "ogg") VORBIS_Q_KBPS.forEach((k, v) => opts.push([`v:${v}`, t("pe.q_vbr_ogg", { v, kbps: k })]));
  if (q?.vbr != null && !opts.some(([x]) => x === `v:${q.vbr}`)) opts.push([`v:${q.vbr}`, `VBR ${q.vbr}`]);
  return opts;
}

const qualityValue = (q) => (q?.vbr != null ? `v:${q.vbr}` : q?.bitrate_kbps ? `k:${q.bitrate_kbps}` : "");

function select(id, options, value, extra = "") {
  return html`<select id="${id}" ${extra}>${options.map(([v, label]) => html`<option value="${v}" ${String(v) === String(value) ? "selected" : ""}>${label}</option>`)}</select>`;
}

function limitSelect(id, label, value, values, unit) {
  const opts = [["device", t("pe.rate_device")], ["", t("pe.rate_none")], ...values.map((v) => [String(v), unit(v)])];
  if (typeof value === "number" && !values.includes(value)) opts.push([String(value), unit(value)]);
  return html`<div class="field"><label for="${id}">${label}</label>${select(id, opts, value === null || value === undefined ? "" : value)}</div>`;
}

function ruleRow(r, i, prefix, index) {
  const id = `${prefix}tr${i}`;
  const from = r.from || [];
  const formats = [...FORMATS, ...from.filter((f) => !FORMATS.includes(f))];
  const n = i + 1;
  return html`<fieldset class="pe-row" data-tc-rule="${i}"><legend class="sr">${t("pe.rule", { n })}</legend>
    <div class="pe-row-head"><b>${t("pe.rule", { n })}</b>
      <button type="button" class="iconbtn" data-action="procRowRemove" data-i="${index}" data-list="rules" data-j="${i}" aria-label="${t("pe.remove_rule", { n })}">${icon("x", { size: 16 })}</button></div>
    <div class="field full"><span class="pe-label" id="${id}-fl">${t("pe.from")}</span>
      <div class="chips" role="group" aria-labelledby="${id}-fl">${formats.map((f, k) => html`<label class="chip"><input type="checkbox" data-tc-from="${f}" data-pos="${from.includes(f) ? from.indexOf(f) : 100 + k}" ${from.includes(f) ? "checked" : ""}> ${f}</label>`)}</div>
      <div class="hint">${t("pe.from_any")}</div></div>
    <div class="form-grid">
      <div class="field"><label for="${id}-to">${t("pe.to")}</label>${select(`${id}-to`, TARGETS.map((x) => [x, t(`pe.to.${x}`)]), r.to, html`data-tc="to" data-change="procRedraw"`)}</div>
      <div class="field"><label for="${id}-q">${t("pe.quality")}</label>${select(`${id}-q`, qualityOptions(r.to, r.quality), qualityValue(r.quality), html`data-tc="quality"`)}</div>
      <div class="field"><label for="${id}-w">${t("pe.when")}</label>${select(`${id}-w`, WHEN.map((x) => [x, t(`pe.when.${x}`)]), r.when || "not_supported", html`data-tc="when" data-change="procRedraw"`)}</div>
      <div class="field full"><label class="checkrow" for="${id}-a"><input type="checkbox" id="${id}-a" data-tc="above" data-change="procRedraw" ${r.above_target_only ? "checked" : ""}> ${t("pe.above")}</label></div>
    </div></fieldset>`;
}

const transcode = {
  fields: ["rules", "max_sample_rate", "max_bit_depth"],
  draw(s, prefix, index) {
    const rules = s.rules || [];
    return html`<div class="field full" data-tc-rules><span class="pe-label">${t("pe.rules")}</span><div class="hint">${t("pe.rules_help")}</div>
        ${rules.length ? rules.map((r, i) => ruleRow(r, i, prefix, index)) : html`<div class="muted" style="margin:6px 0">${t("pe.no_rules")}</div>`}
        <button type="button" class="btn small" data-action="procRowAdd" data-i="${index}" data-list="rules">${icon("plus", { size: 16 })}${t("pe.add_rule")}</button></div>
      <div class="form-grid" style="margin-top:12px">
        ${limitSelect(`${prefix}msr`, t("pe.max_sample_rate"), s.max_sample_rate === undefined ? "device" : s.max_sample_rate, RATES, (hz) => t("pe.rate_hz", { hz }))}
        ${limitSelect(`${prefix}mbd`, t("pe.max_bit_depth"), s.max_bit_depth === undefined ? "device" : s.max_bit_depth, DEPTHS, (bits) => t("pe.depth_bits", { bits }))}
      </div>`;
  },
  read(root, prefix) {
    const rules = [...root.querySelectorAll("[data-tc-rule]")].map((el) => {
      const q = el.querySelector('[data-tc="quality"]').value;
      return {
        // in the stored order, newly ticked ones after them (an unchanged save stores the same list)
        from: [...el.querySelectorAll("[data-tc-from]:checked")].sort((a, b) => a.dataset.pos - b.dataset.pos).map((x) => x.dataset.tcFrom),
        to: el.querySelector('[data-tc="to"]').value,
        when: el.querySelector('[data-tc="when"]').value,
        quality: { vbr: q.startsWith("v:") ? Number(q.slice(2)) : null, bitrate_kbps: q.startsWith("k:") ? Number(q.slice(2)) : null },
        above_target_only: el.querySelector('[data-tc="above"]').checked,
      };
    });
    const limit = (id) => {
      const v = root.querySelector(`#${CSS.escape(id)}`).value;
      return v === "device" ? "device" : v === "" ? null : Number(v);
    };
    return {
      rules,
      max_sample_rate: limit(`${prefix}msr`),
      max_bit_depth: limit(`${prefix}mbd`),
    };
  },
};

// --- retag ------------------------------------------------------------------------------------------------------------

const retag = {
  fields: ["templates"],
  draw(s, prefix, index) {
    const rows = Object.entries(s.templates || {});
    return html`<div class="field full" data-rt-templates><span class="pe-label">${t("pe.templates")}</span>
      <div class="hint">${t("pe.templates_help", { fields: TEMPLATE_FIELDS.map((f) => `{${f}}`).join(" ") })}</div>
      ${rows.length ? rows.map(([tag, tpl], j) => {
        const id = `${prefix}rt${j}`;
        const tags = TAG_TARGETS.includes(tag) ? TAG_TARGETS : [...TAG_TARGETS, tag];
        return html`<div class="pe-tpl" data-rt-row="${j}">
          <label class="sr" for="${id}-tag">${t("pe.tag")}</label>${select(`${id}-tag`, tags.map((x) => [x, x]), tag, html`data-rt="tag"`)}
          <span aria-hidden="true">←</span>
          <label class="sr" for="${id}-tpl">${t("pe.template")}</label><input id="${id}-tpl" data-rt="tpl" value="${tpl}" placeholder="{track_no:02d} {title}" autocomplete="off" spellcheck="false">
          <button type="button" class="iconbtn" data-action="procRowRemove" data-i="${index}" data-list="templates" data-j="${j}" aria-label="${t("pe.remove_template", { tag })}">${icon("x", { size: 16 })}</button></div>`;
      }) : html`<div class="muted" style="margin:6px 0">${t("pe.no_templates")}</div>`}
      <button type="button" class="btn small" data-action="procRowAdd" data-i="${index}" data-list="templates">${icon("plus", { size: 16 })}${t("pe.add_template")}</button></div>`;
  },
  read(root) {
    const templates = {};
    for (const el of root.querySelectorAll("[data-rt-row]")) {
      const tag = el.querySelector('[data-rt="tag"]').value;
      const input = el.querySelector('[data-rt="tpl"]');
      input.removeAttribute("aria-invalid");
      const tpl = input.value.trim();
      if (!tpl || tag in templates) {
        input.setAttribute("aria-invalid", "true");
        input.focus();
        throw new FormError(tpl ? t("pe.template_twice", { tag }) : t("pe.template_empty"));
      }
      templates[tag] = tpl;
    }
    return { templates };
  },
};

// --- covers -----------------------------------------------------------------------------------------------------------

const covers = {
  fields: ["progressive"],
  after: true, // below the mode and size it belongs to
  draw(s, prefix) {
    const v = s.progressive === true ? "yes" : s.progressive === false ? "no" : "auto";
    return html`<div class="form-grid" style="margin-top:12px"><div class="field"><label for="${prefix}prog">${t("pe.progressive")}</label>
      ${select(`${prefix}prog`, ["auto", "yes", "no"].map((x) => [x, t(`pe.progressive.${x}`)]), v)}</div></div>`;
  },
  read(root, prefix) {
    const v = root.querySelector(`#${CSS.escape(`${prefix}prog`)}`).value;
    return { progressive: v === "yes" ? true : v === "no" ? false : null };
  },
};

const EDITORS = { transcode, retag, covers };
