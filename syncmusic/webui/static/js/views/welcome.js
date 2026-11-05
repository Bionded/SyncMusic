// First-run wizard `#/welcome`: six steps with a labelled progress bar -
// Library, Device, Templates, Quality, Review, Done - Back and "Skip setup" on every step. It drives the same API as
// the long screens: POST /libraries (+ check), POST /devices/register (templates only, no preselection),
// PATCH /devices/{id} {quality_profile}, POST /devices/{id}/plan, POST /plans/{id}/apply. The wizard's progress lives
// in the module (`W`) and the step in the URL (`?step=`), so a reload starts again from what exists: a library that
// is already there is offered instead of the form. Registering an unknown drive on connect keeps its own screen
// (`#/devices/register`): the device part of this wizard is the same flow, shorter.
import { html, bytes } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, patch, post } from "../api.js";
import { toast, badge } from "../ui.js";
import { go, goFrom } from "../router.js";
import { FormError } from "../forms.js";
import { settingsForm, sourceActions } from "./libraries.js";
import { checkLibrary } from "../libhealth.js";
import { icon, typeIcon } from "../icons.js";
import { betaBadge } from "./common.js";

addStrings({
  "wiz.title": "First run · step {n} of 6",
  "wiz.crumb": "Setup",
  "wiz.missing": "Fill in the name first.",
  "wiz.skip": "Skip setup",
  "wiz.back": "Back",
  "wiz.step.library": "Library",
  "wiz.unnamed": "USB drive",
  "wiz.step.device": "Device",
  "wiz.step.templates": "Templates",
  "wiz.step.quality": "Quality",
  "wiz.step.review": "Review",
  "wiz.step.done": "Done",
  "wiz.h.library": "Add a library",
  "wiz.l.library": "Where does your music come from? You can add more libraries later from the Libraries menu.",
  "wiz.h.device": "Plug in a device",
  "wiz.l.device": "SyncMusic recognises iPods, Rockbox players and USB drives as soon as they are plugged in. FTP devices are added by address.",
  "wiz.h.templates": "What goes on {name}?",
  "wiz.l.templates": "Templates are saved rules. Nothing is preselected; pick one or more. You can edit them or add your own later.",
  "wiz.h.quality": "How should files be written?",
  "wiz.l.quality": "A quality profile is a chain of processors: convert, covers, tags, lyrics. It is fine-tuned per device later.",
  "wiz.h.review": "Review the first plan",
  "wiz.l.review": "This is what the first sync will do. Nothing has been written yet.",
  "wiz.h.done": "All set",
  "wiz.cta.library": "Save and continue",
  "wiz.cta.library_have": "Continue",
  "wiz.cta.device": "Use this device",
  "wiz.cta.templates": "Continue",
  "wiz.cta.quality": "Build the plan",
  "wiz.cta.review": "Apply and sync",
  "wiz.cta.done": "Go to the dashboard",
  "wiz.have_libraries": "You have {n} libraries. Continue with them, or add another one from the Libraries menu later.",
  "wiz.have_libraries_one": "You have {n} library. Continue with it, or add another one from the Libraries menu later.",
  "wiz.lib_name": "Name",
  "wiz.lib_failed": "{name} is saved but cannot be reached: {error}. Fix its settings in Libraries, or continue anyway.",
  "wiz.lib_ok": "{name} is connected.",
  "wiz.waiting": "Waiting for a device… plug it into one of this computer’s USB ports.",
  "wiz.detected": "Detected",
  "wiz.ftp": "Add an FTP device instead",
  "wiz.dev_name": "Name on SyncMusic",
  "wiz.pick_device": "Choose a device first.",
  "wiz.pick_template": "Pick at least one template.",
  "wiz.picked": "{n} selected",
  "wiz.picked_none": "None selected yet.",
  "wiz.no_templates": "There are no templates yet: add a library first (it brings its default templates).",
  "wiz.recommended": "Suggested",
  "wiz.pick_profile": "Choose a profile.",
  "wiz.c.add": "To add",
  "wiz.c.replace": "To replace",
  "wiz.c.remove": "To remove",
  "wiz.c.write": "To write",
  "wiz.space": "After sync: {used} used of {total} · {free} free · {fit}",
  "wiz.fits": "fits",
  "wiz.no_fit": "does not fit",
  "wiz.no_fit_text": "The selection does not fit on the device. Open the full plan to choose what to leave off, or pick fewer templates.",
  "wiz.full_plan": "Open the full plan",
  "wiz.first": "What is on the device already?",
  "wiz.first.keep": "Keep it and add mine",
  "wiz.first.adopt": "Adopt what matches my library ({n} files)",
  "wiz.first.adopt_one": "Adopt what matches my library ({n} file)",
  "wiz.first.wipe": "Remove it and start fresh",
  "wiz.first_text": "{n} media files are on the device already.",
  "wiz.running": "The first sync is running",
  "wiz.running_text": "You can close this page; the server keeps going and the sidebar shows the progress.",
  "wiz.show_progress": "Show progress",
  "wiz.restart": "The setup lost its place (the page was reloaded); it starts again from what exists.",
});

const STEPS = ["library", "device", "templates", "quality", "review", "done"];
const PROFILE_TEXT = {
  max: "Keeps lossless where the player can play it, the best covers and every tag.",
  space: "Smaller files: lossy and lossless both become a compact format. Fits much more.",
  weak: "Easy on old players: MP3, small covers, no embedded lyrics.",
};
// what the wizard has made so far (a reload loses it: the steps start again from what exists)
const W = { libType: null, addLib: false, disk: null, name: "", device: null, picked: new Set(), profile: null, plan: null, job: null };
const fresh = () => Object.assign(W, { libType: null, addLib: false, disk: null, name: "", device: null, picked: new Set(), profile: null, plan: null, job: null });

export const nav = "dashboard";
export const title = () => t("wiz.crumb");

const step = (ctx) => Math.min(6, Math.max(1, Number(ctx.query.get("step")) || 1));
const at = (n) => go(`/welcome?step=${n}`);

export async function render(ctx) {
  const n = step(ctx);
  // a step that needs what an earlier one made, after a reload: back to the first step that is missing
  const need = n >= 3 && !W.disk ? 2 : n >= 4 && !W.device ? 3 : n >= 5 && !W.plan ? 4 : n === 6 && !W.job ? 5 : 0;
  if (need) { toast(t("wiz.restart")); at(n === 3 || need === 2 ? 1 : need); return html``; }
  const key = STEPS[n - 1];
  const body = await BODIES[key](ctx);
  const bar = STEPS.map((s, i) => html`<li class="${i + 1 < n ? "done" : i + 1 === n ? "on" : ""}" ${i + 1 === n ? html`aria-current="step"` : ""}><span class="wiz-seg"></span><span>${t(`wiz.step.${s}`)}</span></li>`);
  const head = key === "templates" ? t("wiz.h.templates", { name: W.name }) : t(`wiz.h.${key}`);
  const cta = key === "library" && ctx.state.libraries?.length && !W.addLib ? t("wiz.cta.library_have") : t(`wiz.cta.${key}`);
  return html`<div class="wiz" data-wizard data-step="${key}">
    <div class="wiz-top"><span class="muted">${t("wiz.title", { n })}</span>${n < 6 ? html`<button type="button" class="btn ghost small" data-action="wizSkip">${t("wiz.skip")}</button>` : ""}</div>
    <ol class="wiz-bar" aria-label="${t("wiz.title", { n })}">${bar}</ol>
    <h1>${head}</h1>${t(`wiz.l.${key}`) && key !== "done" ? html`<p class="wiz-lead">${t(`wiz.l.${key}`)}</p>` : ""}
    <form class="wiz-body" data-action="wizNext" novalidate>${body}
      <div class="wiz-foot">${n > 1 && n < 6 ? html`<button type="button" class="btn" data-action="wizBack">${icon("left", { size: 16 })}${t("wiz.back")}</button>` : html`<span></span>`}
        <button type="submit" class="btn primary" data-wiz-next>${cta}${n < 6 ? icon("right", { size: 16 }) : ""}</button></div></form></div>`;
}

// --- steps ----------------------------------------------------------------------------------------------------------

const BODIES = {
  async library(ctx) {
    const [libraries, types] = await Promise.all([get("/libraries"), get("/library-types")]);
    ctx.state.libraries = libraries;
    ctx.state.types = types;
    if (libraries.length && !W.addLib) {
      return html`<p data-wiz-have>${t(libraries.length === 1 ? "wiz.have_libraries_one" : "wiz.have_libraries", { n: libraries.length })}</p>
        <div class="wiz-list">${libraries.map((l) => html`<div class="wiz-opt static">${typeIcon(l.type, "library", 22)}<span><b>${l.name}</b><small>${l.type}</small></span></div>`)}</div>`;
    }
    const type = W.libType || types[0].type;
    const tp = types.find((x) => x.type === type) || types[0];
    W.libType = tp.type;
    ctx.state.form = settingsForm(tp, {}); // the Add library form: Local / SMB / FTP switch, Advanced
    return html`<div class="type-cards" role="radiogroup" aria-label="${t("wiz.step.library")}">${types.map((x) => html`<button type="button" class="type-card" role="radio"
        aria-checked="${x.type === tp.type ? "true" : "false"}" data-action="wizLibType" data-type="${x.type}">${typeIcon(x.type, "library", 22)}<span><b>${x.display_name}</b><small>${x.display_hint || ""}</small></span></button>`)}</div>
      <section class="card" style="margin-top:14px"><div class="card-body">
        <div class="field"><label for="wizLibName">${t("wiz.lib_name")} *</label><input id="wizLibName" required value="${tp.display_name}" autocomplete="off"></div>
        <div style="margin-top:14px">${ctx.state.form.html}</div></div></section>`;
  },

  async device(ctx) {
    const devices = await get("/devices");
    const unknown = devices.unknown || [];
    ctx.state.unknown = unknown;
    if (!unknown.find((u) => u.disk === W.disk)) W.disk = unknown[0]?.disk || null;
    const chosen = unknown.find((u) => u.disk === W.disk);
    if (chosen && !W.name) W.name = diskName(chosen);
    // what the probes found: its type's icon, its label or model as the title, the disk path in the facts
    const opts = unknown.map((u) => {
      const probe = u.probes?.[0];
      const what = u.mtp // an MTP player: no size or device path, its USB serial
        ? [[u.vendor, u.model].filter(Boolean).join(" "), u.serial].filter(Boolean).join(" · ")
        : [[u.vendor, u.model].filter(Boolean).join(" "), probe?.model, bytes(u.size), u.disk].filter(Boolean).join(" · ");
      return html`<label class="wiz-opt"><input type="radio" name="wizDisk" value="${u.disk}" data-change="wizDisk" ${u.disk === W.disk ? "checked" : ""}>
        ${typeIcon(probe?.type_name || "unknown", "device", 22)}<span><b>${diskName(u) || t("wiz.unnamed")}${betaBadge(probe?.type_name)}</b><small>${what}</small></span>
        ${badge("ok", t("wiz.detected"))}</label>`;
    });
    return html`${unknown.length ? html`<div class="wiz-list" role="radiogroup">${opts}</div>
        <div class="field" style="margin-top:14px"><label for="wizDevName">${t("wiz.dev_name")} *</label><input id="wizDevName" required value="${W.name}" autocomplete="off"></div>`
      : html`<div class="wiz-wait" data-wiz-waiting role="status">${icon("sync", { size: 18, cls: "spin" })}${t("wiz.waiting")}</div>`}
      <p style="margin-top:12px"><a href="#/devices/register?target=network">${t("wiz.ftp")}</a></p>`;
  },

  async templates(ctx) {
    const [templates, libraries] = await Promise.all([get("/templates"), get("/libraries")]);
    const names = Object.fromEntries(libraries.map((l) => [l.id, l.name]));
    const usable = templates.filter((x) => !x.id.startsWith("own-"));
    ctx.state.templates = usable;
    if (!usable.length) return html`<div class="callout">${t("wiz.no_templates")}</div>`;
    return html`<div class="wiz-list" data-wiz-templates>${usable.map((x) => html`<label class="wiz-opt"><input type="checkbox" name="wizTpl" value="${x.id}" data-change="wizTpl" ${W.picked.has(x.id) ? "checked" : ""}>
        <span><b>${x.name}</b><small>${[x.description, (x.library_ids || []).map((l) => names[l] || l).join(", ")].filter(Boolean).join(" · ")}</small></span></label>`)}</div>
      <p class="muted" data-wiz-picked style="margin-top:10px">${W.picked.size ? t("wiz.picked", { n: W.picked.size }) : t("wiz.picked_none")}</p>`;
  },

  async quality() {
    const [profiles, types] = await Promise.all([get(`/devices/${encodeURIComponent(W.device.id)}/quality-profiles`), get("/processor-types")]);
    W.profile = W.profile || profiles[0]?.id || null;
    const names = Object.fromEntries(types.map((x) => [x.type, x.display_name]));
    const chain = (p) => p.processors.filter((s) => s.enabled !== false).map((s) => names[s.type] || s.type).join(" · ");
    return html`<div class="wiz-list" role="radiogroup">${profiles.map((p, i) => html`<label class="wiz-opt"><input type="radio" name="wizProfile" value="${p.id}" data-change="wizProfile" ${p.id === W.profile ? "checked" : ""}>
        <span><b>${p.name}${i === 0 ? html` ${badge("ok", t("wiz.recommended"))}` : ""}</b><small>${PROFILE_TEXT[p.id] || ""}</small><small>${chain(p)}</small></span></label>`)}</div>`;
  },

  async review() {
    const p = W.plan;
    const cell = (v, l) => html`<div class="wiz-count"><b>${v}</b><span>${l}</span></div>`;
    const s = p.space || {};
    const usedAfter = s.capacity && s.free_after != null ? s.capacity - s.free_after : null;
    const space = s.unknown || usedAfter == null ? "" : html`<p class="${p.does_not_fit ? "err" : "muted"}" data-wiz-space>${t("wiz.space", {
      used: bytes(usedAfter), total: bytes(s.capacity), free: bytes(Math.max(0, s.free_after)), fit: p.does_not_fit ? t("wiz.no_fit") : t("wiz.fits") })}</p>`;
    // an empty device has nothing to keep, adopt or wipe: no question then (the apply sends "keep")
    const first = p.first_sync && p.first_sync_existing ? html`<fieldset class="wiz-first" data-wiz-first><legend><b>${t("wiz.first")}</b></legend>
        ${p.first_sync_existing ? html`<p class="muted">${t("wiz.first_text", { n: p.first_sync_existing })}</p>` : ""}
        ${["keep", "adopt", "wipe"].map((v) => html`<label class="wiz-opt"><input type="radio" name="wizFirst" value="${v}" ${v === "keep" ? "checked" : ""}>
          <span><b>${t(v === "adopt" && p.first_sync_matches === 1 ? "wiz.first.adopt_one" : `wiz.first.${v}`, { n: p.first_sync_matches || 0 })}</b></span></label>`).filter((_, i) => i !== 1 || p.first_sync_matches)}</fieldset>` : "";
    return html`<div class="wiz-counts" data-wiz-counts>${cell(p.counts.add || 0, t("wiz.c.add"))}${cell(p.counts.replace || 0, t("wiz.c.replace"))}
        ${cell(p.counts.remove || 0, t("wiz.c.remove"))}${cell(bytes(p.write_bytes || 0), t("wiz.c.write"))}</div>
      ${space}${p.does_not_fit ? html`<div class="callout warn" data-wiz-nofit>${t("wiz.no_fit_text")}</div>` : ""}${first}
      <p><a href="#/devices/${encodeURIComponent(W.device.id)}/plan/${encodeURIComponent(p.id)}">${t("wiz.full_plan")}</a></p>`;
  },

  async done() {
    return html`<div class="wiz-done" data-wiz-done><span class="tile ok">${icon("ccheck", { size: 26 })}</span>
      <div><b>${t("wiz.running")}</b><p class="muted">${t("wiz.running_text")}</p>
      <a class="btn" href="#/devices/${encodeURIComponent(W.device.id)}/sync/${encodeURIComponent(W.job)}">${t("wiz.show_progress")}</a></div></div>`;
  },
};

/** A name for an unknown disk: its probe's suggestion (the volume label, the iPod's name), else vendor and model. */
function diskName(u) {
  return u.probes?.[0]?.suggested_name || [u.vendor, u.model].filter(Boolean).join(" ").trim();
}

// --- actions --------------------------------------------------------------------------------------------------------

function slug(name) {
  return name.toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "library";
}

const NEXT = {
  async library(form, ctx) {
    if (ctx.state.libraries.length && !W.addLib) return true;
    let settings;
    try { settings = ctx.state.form.read(form); } catch (e) {
      if (e instanceof FormError) { toast(e.message, "error"); return false; }
      throw e;
    }
    const name = form.querySelector("#wizLibName").value.trim();
    if (!name) { toast(t("wiz.missing"), "error"); return false; }
    const existing = new Set(ctx.state.libraries.map((l) => l.id));
    let id = slug(name), i = 2;
    while (existing.has(id)) id = `${slug(name)}-${i++}`;
    const lib = await post("/libraries", { id, type: W.libType, name, settings });
    const h = await checkLibrary(lib.id);
    toast(h.ok ? t("wiz.lib_ok", { name }) : t("wiz.lib_failed", { name, error: h.text }), h.ok ? "info" : "error");
    if (h.ok) post(`/libraries/${encodeURIComponent(lib.id)}/refresh`).catch(() => null); // the first scan starts now
    return true;
  },
  async device(form) {
    if (!W.disk) { toast(t("wiz.pick_device"), "error"); return false; }
    W.name = form.querySelector("#wizDevName").value.trim();
    if (!W.name) { toast(t("wiz.missing"), "error"); return false; }
    return true;
  },
  async templates() {
    if (!W.picked.size) { toast(t("wiz.pick_template"), "info"); return false; }
    const body = { name: W.name, disk: W.disk, rule_set: { template_ids: [...W.picked] } };
    if (W.device) { // Back from a later step: the device exists, only its templates change
      W.device = { ...W.device, ...(await patch(`/devices/${encodeURIComponent(W.device.id)}`, { rule_set: body.rule_set })) };
    } else {
      W.device = await post("/devices/register", body);
    }
    return true;
  },
  async quality() {
    if (!W.profile) { toast(t("wiz.pick_profile"), "error"); return false; }
    const id = encodeURIComponent(W.device.id);
    await patch(`/devices/${id}`, { quality_profile: W.profile });
    W.plan = await post(`/devices/${id}/plan`, {});
    return true;
  },
  async review(form) {
    const first = form.querySelector('input[name="wizFirst"]:checked')?.value || "keep";
    const job = await post(`/plans/${encodeURIComponent(W.plan.id)}/apply`, { confirm: true, ...(W.plan.first_sync ? { first_sync: first } : {}) });
    W.job = job.id;
    return true;
  },
};

export const actions = {
  ...sourceActions,
  async wizNext(form, ctx) {
    const n = step(ctx);
    if (n === 6) { fresh(); go("/"); return; }
    const btn = form.querySelector("[data-wiz-next]");
    btn.disabled = true;
    let ok = false;
    try { ok = await NEXT[STEPS[n - 1]](form, ctx); } finally { btn.disabled = false; }
    if (ok) goFrom(ctx, `/welcome?step=${n + 1}`);
  },
  wizBack(el, ctx) { at(Math.max(1, step(ctx) - 1)); },
  wizSkip() { go("/"); },
  wizLibType(el) {
    W.libType = el.dataset.type;
    at(1);
  },
  wizDisk(el, ctx) {
    W.disk = el.value;
    const u = ctx.state.unknown.find((x) => x.disk === el.value);
    const input = document.getElementById("wizDevName");
    if (input && u && diskName(u)) input.value = diskName(u);
  },
  wizTpl(el) {
    if (el.checked) W.picked.add(el.value); else W.picked.delete(el.value);
    const out = document.querySelector("[data-wiz-picked]");
    if (out) out.textContent = W.picked.size ? t("wiz.picked", { n: W.picked.size }) : t("wiz.picked_none");
  },
  wizProfile(el) { W.profile = el.value; },
};

// a drive plugged in while the Device step waits shows up without a reload (only then: a typed name must stay)
export function onEvent(ev) {
  if (ev.type.startsWith("device.") && document.querySelector("[data-wiz-waiting]")) at(2);
}
