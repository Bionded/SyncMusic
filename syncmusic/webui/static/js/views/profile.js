// Device profile card of Device settings: what the device really handles, each value with its
// source (detected / known model / device setting / your override / default), a per-field override editor (no JSON)
// and Reset. Overrides are `capability_overrides` of the device config.
import { html } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { patch } from "../api.js";
import { badge, toast } from "../ui.js";

addStrings({
  "pf.title": "Device profile",
  "pf.help": "What this device handles and where each value comes from. Processors and the plan use it.",
  "pf.model": "Model: {label}",
  "pf.model_id": "Model: {label} ({model})",
  "pf.offline": "Connect the device to see what it reports itself; until then the defaults and your choices are shown.",
  "pf.music_only": "Music only: this player takes no podcasts or audiobooks.",
  "pf.change": "Change",
  "pf.reset": "Reset",
  "pf.reset_all": "Reset all overrides",
  "pf.reset_done": "Back to the detected values",
  "pf.save": "Save",
  "pf.cancel": "Cancel",
  "pf.saved": "Override saved",
  "pf.src.default": "Default",
  "pf.src.known_model": "Known model",
  "pf.src.detected": "Detected",
  "pf.src.setting": "Device setting",
  "pf.src.override": "Your override",
  "pf.f.formats": "Playable formats",
  "pf.f.max_sample_rate": "Highest sample rate",
  "pf.f.max_bit_depth": "Highest bit depth",
  "pf.f.max_tracks": "Most tracks",
  "pf.f.cover_size": "Cover size",
  "pf.f.supports_playlists": "Playlists",
  "pf.f.supports_music": "Music",
  "pf.f.supports_podcasts": "Podcasts",
  "pf.f.supports_audiobooks": "Audiobooks",
  "pf.f.has_rtc": "Has a clock",
  "pf.f.prefer_lossy": "Suggest converting lossless",
  "pf.v.no_limit": "No limit",
  "pf.v.no_covers": "No covers",
  "pf.v.any": "Any format",
  "pf.v.yes": "Yes",
  "pf.v.no": "No",
  "pf.v.unknown": "Unknown",
  "pf.v.hz": "{khz} kHz",
  "pf.v.bits": "{n} bit",
  "pf.v.px": "{n} px",
  "pf.v.tracks": "{n} tracks",
  "pf.v.tracks_one": "{n} track",
  "pf.setting_hint": "Set in the device settings above; an override here wins.",
  "pf.cover_hint": "Empty = no covers.",
  "pf.tracks_hint": "Empty = no limit.",
});

const ORDER = ["formats", "max_sample_rate", "max_bit_depth", "max_tracks", "cover_size", "supports_playlists",
  "supports_music", "supports_podcasts", "supports_audiobooks", "has_rtc", "prefer_lossy"];
const FORMATS = ["mp3", "aac", "alac", "flac", "ogg", "opus", "wav", "aiff", "wv", "ape", "mpc", "wma"];
const RATES = [44100, 48000, 88200, 96000, 176400, 192000];
const DEPTHS = [16, 24];
const SOURCE_STATE = { detected: "ok", known_model: "ok", setting: "busy", override: "warn", default: "neutral" };

function show(name, v) {
  if (name === "formats") return v && v.length ? [...v].sort().join(", ") : t("pf.v.any");
  if (name === "max_sample_rate") return v ? t("pf.v.hz", { khz: v / 1000 }) : t("pf.v.no_limit");
  if (name === "max_bit_depth") return v ? t("pf.v.bits", { n: v }) : t("pf.v.no_limit");
  if (name === "max_tracks") return v ? t("pf.v.tracks", { n: v }) : t("pf.v.no_limit");
  if (name === "cover_size") return v ? t("pf.v.px", { n: v }) : t("pf.v.no_covers");
  if (v === null || v === undefined) return t("pf.v.unknown");
  return t(v ? "pf.v.yes" : "pf.v.no");
}

function select(id, value, options) {
  return html`<select id="${id}" name="value">${options.map(([v, label]) => html`<option value="${v}" ${String(v) === String(value ?? "") ? "selected" : ""}>${label}</option>`)}</select>`;
}

function editor(name, v) {
  const id = `pf-${name}`;
  if (name === "formats") {
    const all = [...FORMATS, ...(v || []).filter((f) => !FORMATS.includes(f))];
    return html`<div class="chips" role="group" aria-label="${t("pf.f.formats")}">${all.map((f) => html`<label class="chip"><input type="checkbox" name="fmt" value="${f}" ${(v || []).includes(f) ? "checked" : ""}>${f}</label>`)}</div>`;
  }
  if (name === "max_sample_rate") return select(id, v, [["", t("pf.v.no_limit")], ...RATES.map((r) => [r, t("pf.v.hz", { khz: r / 1000 })])]);
  if (name === "max_bit_depth") return select(id, v, [["", t("pf.v.no_limit")], ...DEPTHS.map((d) => [d, t("pf.v.bits", { n: d })])]);
  if (name === "max_tracks" || name === "cover_size") {
    const [min, max, hint] = name === "cover_size" ? [50, 1500, "pf.cover_hint"] : [1, 1000000, "pf.tracks_hint"];
    return html`<input id="${id}" name="value" type="number" min="${min}" max="${max}" step="1" value="${v ?? ""}" aria-label="${t(`pf.f.${name}`)}" style="max-width:160px"> <small class="muted">${t(hint)}</small>`;
  }
  const opts = [["true", t("pf.v.yes")], ["false", t("pf.v.no")]];
  if (name === "has_rtc") opts.push(["", t("pf.v.unknown")]);
  return select(id, v === null || v === undefined ? "" : String(v), opts);
}

function readValue(name, form) {
  if (name === "formats") return [...form.querySelectorAll('input[name="fmt"]:checked')].map((x) => x.value);
  const raw = form.querySelector('[name="value"]').value.trim();
  if (["max_sample_rate", "max_bit_depth", "max_tracks", "cover_size"].includes(name)) return raw === "" ? null : Number(raw);
  return raw === "" ? null : raw === "true";
}

function row(name, f) {
  const src = f.source;
  return html`<div class="row pf-row" data-field="${name}" data-source="${src}">
    <div class="meta"><b>${t(`pf.f.${name}`)}</b><small>${show(name, f.value)}</small></div>
    ${badge(SOURCE_STATE[src] || "neutral", t(`pf.src.${src}`))}
    ${src === "override"
      ? html`<button class="btn small" type="button" data-action="profileReset" data-field="${name}">${t("pf.reset")}</button>`
      : html`<button class="btn small ghost" type="button" data-action="profileEdit" data-field="${name}">${t("pf.change")}</button>`}
  </div>
  <form class="pf-edit" data-action="profileSave" data-field="${name}" hidden>
    ${src === "setting" ? html`<p class="muted">${t("pf.setting_hint")}</p>` : ""}
    ${editor(name, f.value)}
    <div class="pf-buttons"><button class="btn primary small" type="submit">${t("pf.save")}</button>
      <button class="btn small ghost" type="button" data-action="profileCancel" data-field="${name}">${t("pf.cancel")}</button></div>
  </form>`;
}

export function profileCard(profile) {
  if (!profile) return "";
  const names = ORDER.filter((n) => n in profile.fields);
  const overrides = names.some((n) => profile.fields[n].source === "override");
  const model = profile.label
    ? t(profile.model && profile.known ? "pf.model_id" : "pf.model", { label: profile.label, model: profile.model })
    : profile.model ? t("pf.model", { label: profile.model }) : "";
  return html`<div class="card" style="margin-top:15px" id="profileCard" data-connected="${profile.connected ? "1" : "0"}">
    <div class="card-head"><div><h2>${t("pf.title")}</h2><div class="sub">${t("pf.help")}</div></div></div>
    <div class="card-body">
      ${model ? html`<p id="profileModel">${model}</p>` : ""}
      ${profile.connected ? "" : html`<p class="muted">${t("pf.offline")}</p>`}
      ${profile.music_only ? html`<p class="muted">${t("pf.music_only")}</p>` : ""}
      ${names.map((n) => row(n, profile.fields[n]))}
      ${overrides ? html`<div style="margin-top:12px"><button class="btn" type="button" data-action="profileResetAll">${t("pf.reset_all")}</button></div>` : ""}
    </div></div>`;
}

async function saveOverrides(ctx, overrides) {
  const cfg = ctx.state.cfg;
  ctx.state.cfg = await patch(`/devices/${encodeURIComponent(cfg.id)}`, { capability_overrides: overrides });
}

function toggle(el, open) {
  const card = el.closest("#profileCard");
  const name = el.dataset.field;
  card.querySelector(`form.pf-edit[data-field="${name}"]`).hidden = !open;
  card.querySelector(`.pf-row[data-field="${name}"] [data-action="profileEdit"]`)?.toggleAttribute("hidden", open);
  if (open) card.querySelector(`form.pf-edit[data-field="${name}"] input, form.pf-edit[data-field="${name}"] select`)?.focus();
}

export const profileActions = {
  profileEdit(el) { toggle(el, true); },
  profileCancel(el) { toggle(el, false); },
  async profileSave(el, ctx) {
    const name = el.dataset.field;
    const value = readValue(name, el);
    await saveOverrides(ctx, { ...(ctx.state.cfg.capability_overrides || {}), [name]: value });
    toast(t("pf.saved"));
    ctx.rerender();
  },
  async profileReset(el, ctx) {
    const { [el.dataset.field]: _, ...rest } = ctx.state.cfg.capability_overrides || {};
    await saveOverrides(ctx, rest);
    toast(t("pf.reset_done"));
    ctx.rerender();
  },
  async profileResetAll(el, ctx) {
    // only the profile's fields; other overrides (e.g. time sync turned off by the device) stay
    const keep = Object.fromEntries(Object.entries(ctx.state.cfg.capability_overrides || {}).filter(([k]) => !ORDER.includes(k)));
    await saveOverrides(ctx, keep);
    toast(t("pf.reset_done"));
    ctx.rerender();
  },
};
