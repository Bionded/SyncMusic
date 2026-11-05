// The Sync behaviour part of the device's Settings tab (merged with Device settings): Automatic updates (on /
// off, how often the libraries are checked, may updates remove), Full sync when connected (Ask me / Automatically /
// Never = on_connect ask / auto / manual), Full sync schedule and safety (nightly time, verify after writing, the
// delete threshold) and the type's other sync settings under Advanced. One Save / Discard bar for all of it.
import { html } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { helpTip } from "../help.js";
import { toast, errorBox } from "../ui.js";
import { schemaForm, FormError } from "../forms.js";
import { windowsEditor, bindWindows, readWindows } from "../windows.js";

addStrings({
  "ds.upd_title": "Automatic updates",
  "ds.upd_text": "Brings plays, ratings and positions back to the libraries, then pushes changed tags, ratings, smart playlists and positions to the device. Never copies new files. Skips quietly when nothing changed and never runs during a full sync.",
  "ds.upd_on": "When connected, keep it updated automatically",
  "ds.every": "Check the libraries for changes every",
  "ds.every_sub": "Also runs right after a library reports a change.",
  "ds.every_label": "Check interval",
  "ds.every.15": "15 min",
  "ds.every.30": "30 min",
  "ds.every.60": "1 h",
  "ds.every.off": "Off",
  "ds.every_min": "{n} min",
  "ds.every_min_one": "{n} min",
  "ds.removals": "Updates may remove items that no longer match",
  "ds.removals_sub": "Off: removals wait for the next full sync and its review.",
  "ds.played": "Updates remove what was played on this device",
  "ds.deleted": "Remove songs deleted from the library",
  "ds.deleted_sub": "Syncs remove items their library no longer has. Kept when a library returns nothing or loses many items at once.",
  "ds.played_sub": "An episode played to the end that an unplayed-only rule no longer selects goes at the next update.",
  "ds.full_title": "Full sync when connected",
  "ds.full_text": "A full sync copies new and replaced files after a plan.",
  "ds.when.ask": "Ask me",
  "ds.when.ask_text": "Build a plan and wait for you to review it.",
  "ds.when.auto": "Automatically",
  "ds.when.auto_text": "Apply the plan unless it hits the delete threshold.",
  "ds.when.manual": "Never",
  "ds.when.manual_text": "Only the automatic update runs. Start a full sync from here when you want.",
  "ds.safety_title": "Full sync schedule and safety",
  "ds.nightly": "Full sync every night while connected",
  "ds.nightly_time": "Nightly sync time",
  "ds.verify": "Verify after writing",
  "ds.verify_sub": "Reads the files back after flushing the device.",
  "ds.verify.all": "Every file",
  "ds.verify.sample": "Sample 10 %",
  "ds.verify.off": "No",
  "ds.eject": "Eject after a sync",
  "ds.eject_sub": "So it can be unplugged right away. Not after a sync that failed or was cancelled.",
  "ds.eject.off": "No",
  "ds.eject.full": "Full syncs",
  "ds.eject.any": "All syncs",
  "ds.threshold": "Ask before removing more than",
  "ds.threshold_sub": "An automatic full sync that removes more of the items on the device waits for you.",
  "ds.threshold_label": "Delete threshold in percent",
  "ds.threshold_unit": "% of the items",
  "ds.threshold_bad": "The delete threshold is a whole number from 0 to 100.",
  "ds.nocache_title": "Conversion cache",
  "ds.nocache_text": "Converted and downloaded files are kept in the cache so the next sync of this or another device reuses them.",
  "ds.nocache": "Sync without the cache",
  "ds.nocache_sub": "For libraries much larger than the cache: each file is held in memory only until it is on the device. Large files go to a temporary file on this computer's disk.",
  "ds.advanced": "Advanced",
  "ds.origin_default": "Default",
  "ds.origin_changed": "Changed for this device · default: {value}",
  "ds.origin_reset": "Reset to default",
  "ds.origin_on": "on",
  "ds.origin_off": "off",
});

const EVERY = ["15", "30", "60", "off"];
const WHEN = ["ask", "auto", "manual"];
const VERIFY = ["all", "sample", "off"];
const EJECT = ["off", "full", "any"];
// shown in their own cards: gentle write mode is in Device settings, verify above
const NOCACHE = ["no_cache_files", "no_cache_budget_mb", "no_cache_file_max_mb"];
const OWN = ["verify", "eject_after", "gentle_write", "gentle_batch_files", "gentle_pause_s", "gentle_rate_mb_s", "no_cache", ...NOCACHE];

const radios = (name, values, current, label, text) => html`<div class="segr" role="radiogroup" aria-label="${label}">${values.map((v) =>
  html`<label><input type="radio" name="${name}" value="${v}" ${v === current ? "checked" : ""}><span>${text(v)}</span></label>`)}</div>`;

/** The tab; `updateLine` is the device page's automatic update line with Update now. */
export function syncTab(ctx, updateLine) {
  const { cfg, type } = ctx.state;
  if (!type) return errorBox({ message: `unknown device type ${cfg.type}` }, "");
  const advanced = schemaForm(type.sync_schema, cfg.sync, { prefix: "s-", skip: OWN });
  const props = type.sync_schema?.properties || {};
  const nocache = "no_cache" in props
    ? schemaForm(type.sync_schema, cfg.sync, { prefix: "s-", skip: Object.keys(props).filter((k) => !NOCACHE.includes(k)) }) : null;
  ctx.state.syncForms = { advanced, nocache };
  const on = cfg.update_sync;
  const every = cfg.update_every_min === null ? "off" : String(cfg.update_every_min);
  return html`<form class="dev-settings" data-sync-form>
    <section class="card ds-card"><div><h2>${t("ds.upd_title")} ${helpTip("update_full")}</h2><p class="ds-sub">${t("ds.upd_text")}</p></div>
      <label class="ds-check"><input type="checkbox" id="dsUpd" ${on ? "checked" : ""}><span>${t("ds.upd_on")}</span></label>
      <div class="ds-row" data-needs-upd ${on ? "" : "data-off"}><span class="ds-label">${t("ds.every")}<small>${t("ds.every_sub")}</small></span>
        ${radios("dsEvery", EVERY.includes(every) ? EVERY : [...EVERY.slice(0, -1), every, "off"], every, t("ds.every_label"),
          (v) => (EVERY.includes(v) ? t(`ds.every.${v}`) : t("ds.every_min", { n: v })))}</div>
      <label class="ds-check" data-needs-upd ${on ? "" : "data-off"}><input type="checkbox" id="dsRemovals" ${cfg.update_removals ? "checked" : ""}>
        <span class="ds-label">${t("ds.removals")}<small>${t("ds.removals_sub")}</small></span></label>
      <label class="ds-check" data-needs-upd ${on ? "" : "data-off"}><input type="checkbox" id="dsPlayed" ${cfg.update_remove_played !== false ? "checked" : ""}>
        <span class="ds-label">${t("ds.played")}<small>${t("ds.played_sub")}</small></span></label>
      <div class="ds-foot">${updateLine}</div></section>
    <section class="card ds-card"><div><h2>${t("ds.full_title")}</h2><p class="ds-sub">${t("ds.full_text")}</p></div>
      <div class="ds-options" role="radiogroup" aria-label="${t("ds.full_title")}">${WHEN.map((w) =>
        html`<label class="option"><input type="radio" name="dsWhen" value="${w}" ${cfg.on_connect === w ? "checked" : ""}>
          <span><b>${t(`ds.when.${w}`)}</b><small>${t(`ds.when.${w}_text`)}</small></span></label>`)}</div></section>
    <section class="card ds-card"><h2>${t("ds.safety_title")}</h2>
      <div class="ds-row"><label class="ds-check ds-grow"><input type="checkbox" id="dsNightly" ${cfg.sync_at ? "checked" : ""}><span>${t("ds.nightly")}</span></label>
        <input type="time" id="s-sync_at" class="ds-time" value="${cfg.sync_at || "03:00"}" aria-label="${t("ds.nightly_time")}" ${cfg.sync_at ? "" : "disabled"}></div>
      ${windowsEditor("dev", cfg.sync_windows || [])}
      <div class="ds-row"><span class="ds-label">${t("ds.verify")}<small>${t("ds.verify_sub")}</small></span>
        ${radios("dsVerify", VERIFY, cfg.sync.verify || "all", t("ds.verify"), (v) => t(`ds.verify.${v}`))}</div>
      ${cfg.mount_path || (type.network && !type.usb_mtp) ? "" : html`<div class="ds-row"><span class="ds-label">${t("ds.eject")}<small>${t("ds.eject_sub")}</small></span>
        ${radios("dsEject", EJECT, cfg.sync.eject_after || "off", t("ds.eject"), (v) => t(`ds.eject.${v}`))}</div>`}
      <div class="ds-row"><span class="ds-label">${t("ds.threshold")}<small>${t("ds.threshold_sub")}</small></span>
        <span class="ds-num"><input id="dsThreshold" inputmode="numeric" value="${cfg.delete_threshold_pct}" aria-label="${t("ds.threshold_label")}">${t("ds.threshold_unit")}</span></div>
      <label class="ds-check"><input type="checkbox" id="dsDeleted" ${cfg.remove_deleted !== false ? "checked" : ""}>
        <span class="ds-label">${t("ds.deleted")}<small>${t("ds.deleted_sub")}</small></span></label>
      <details class="ds-adv"><summary>${t("ds.advanced")}</summary>${advanced.html}</details></section>
    ${nocache ? html`<section class="card ds-card" data-nocache><div><h2>${t("ds.nocache_title")} ${helpTip("cache")}</h2><p class="ds-sub">${t("ds.nocache_text")}</p></div>
      <label class="ds-check"><input type="checkbox" id="dsNoCache" ${cfg.sync.no_cache ? "checked" : ""}>
        <span class="ds-label">${t("ds.nocache")}<small>${t("ds.nocache_sub")}</small></span></label>
      <div data-nocache-fields ${cfg.sync.no_cache ? "" : "hidden"}>${nocache.html}</div></section>` : ""}
  </form>`;
}

/** The dependent controls follow their switch (the Save / Discard bar is the merged Settings tab's). */
export function syncMounted(form, defaults) {
  if (defaults) origins(form, defaults);
  bindWindows(form);
  form.addEventListener("click", (e) => {
    const b = e.target.closest("[data-reset-default]");
    if (!b) return;
    const [id, , key] = ORIGIN.find(([x]) => x === b.dataset.resetDefault);
    const def = defaultValue(defaults, key);
    const el = control(form, id, def);
    if (!el) return; // e.g. a default interval this device's choices do not offer
    el.checked = typeof def === "boolean" ? def : true;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
  form.addEventListener("change", (e) => {
    if (defaults) origins(form, defaults);
    if (e.target.id === "dsUpd") {
      for (const el of form.querySelectorAll("[data-needs-upd]")) el.toggleAttribute("data-off", !e.target.checked);
    }
    if (e.target.id === "dsNoCache") form.querySelector("[data-nocache-fields]").hidden = !e.target.checked;
    if (e.target.id === "dsNightly") form.querySelector("#s-sync_at").disabled = !e.target.checked;
  });
}

// where a setting's value comes from: the app's default for new devices (Settings → Sync) or
// changed for this device, with Reset to default; [control id or radio name, kind, app setting]
const ORIGIN = [["dsUpd", "check", "default_update_sync"], ["dsEvery", "radio", "default_update_every_min"],
  ["dsWhen", "radio", "default_on_connect"], ["dsVerify", "radio", "default_verify"], ["dsEject", "radio", "default_eject_after"],
  ["dsNoCache", "check", "default_no_cache"]];

function defaultValue(defaults, key) {
  const v = defaults[key];
  if (key === "default_update_every_min") return v == null ? "off" : String(v);
  if (key === "default_eject_after") return v || "off";
  return v;
}

/** The input showing `value` (a radio) or the checkbox. */
function control(form, id, value) {
  return typeof value === "boolean" ? form.querySelector(`#${id}`) : form.querySelector(`input[name="${id}"][value="${value}"]`);
}

/** A radio option's visible name (an option card's bold title, not its description). */
function optionText(form, name, value) {
  const n = form.querySelector(`input[name="${name}"][value="${value}"]`)?.nextElementSibling;
  return (n?.querySelector("b") || n)?.textContent;
}

function origins(form, defaults) {
  for (const [id, kind, key] of ORIGIN) {
    const first = kind === "check" ? form.querySelector(`#${id}`) : form.querySelector(`input[name="${id}"]`);
    if (!first) continue; // not this type (no cache, eject)
    const def = defaultValue(defaults, key);
    const now = kind === "check" ? first.checked : form.querySelector(`input[name="${id}"]:checked`)?.value;
    const same = now === def;
    let chip = form.querySelector(`[data-origin="${id}"]`);
    if (!chip) {
      chip = document.createElement("span");
      chip.className = "ds-origin";
      chip.dataset.origin = id;
      const host = kind === "check" ? first.closest("label") : first.closest(".ds-row")?.querySelector(".ds-label") || first.closest(".card").querySelector("h2").parentElement;
      host.after(chip);
    }
    chip.dataset.state = same ? "default" : "changed";
    const shown = typeof def === "boolean" ? t(def ? "ds.origin_on" : "ds.origin_off") : (optionText(form, id, def) ?? def).replace(/\s+/g, " ").trim();
    chip.replaceChildren(document.createTextNode(same ? t("ds.origin_default") : t("ds.origin_changed", { value: shown })));
    if (!same) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn small ghost";
      b.dataset.resetDefault = id;
      b.textContent = t("ds.origin_reset");
      chip.append(b);
    }
  }
}

/** The form's device changes (a PATCH body), or null after a toast saying what is wrong. */
export function readSync(ctx, form) {
  const { cfg, syncForms: forms } = ctx.state;
  const value = (name) => form.querySelector(`input[name="${name}"]:checked`)?.value;
  const pct = Number(form.querySelector("#dsThreshold").value.trim());
  if (!Number.isInteger(pct) || pct < 0 || pct > 100) { toast(t("ds.threshold_bad"), "error"); return null; }
  let adv;
  try {
    adv = { ...forms.advanced.read(form), ...(forms.nocache ? forms.nocache.read(form) : {}) };
  } catch (e) {
    if (e instanceof FormError) { toast(e.message, "error"); return null; }
    throw e;
  }
  const every = value("dsEvery");
  return {
    update_sync: form.querySelector("#dsUpd").checked,
    update_every_min: every === "off" ? null : Number(every),
    update_removals: form.querySelector("#dsRemovals").checked,
    update_remove_played: form.querySelector("#dsPlayed").checked,
    remove_deleted: form.querySelector("#dsDeleted").checked,
    on_connect: value("dsWhen"),
    sync_at: form.querySelector("#dsNightly").checked ? form.querySelector("#s-sync_at").value || "03:00" : null,
    sync_windows: readWindows(form, "dev"),
    delete_threshold_pct: pct,
    sync: { ...cfg.sync, ...adv, verify: value("dsVerify"),
      ...(value("dsEject") ? { eject_after: value("dsEject") } : {}),
      ...(forms.nocache ? { no_cache: form.querySelector("#dsNoCache").checked } : {}) },
  };
}
