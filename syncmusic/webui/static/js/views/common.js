// Helpers shared by several screens.
import { t, addStrings } from "../i18n.js";
import { html, bytes } from "../dom.js";
import { badge, toast } from "../ui.js";
import { goFrom } from "../router.js";
import { typeIcon } from "../icons.js";

addStrings({
  "beta.badge": "Beta",
  "beta.android": "Android players over MTP are in beta: unlock the player and allow file transfer, and report problems.",
  "beta.jellyfin": "Jellyfin libraries are in beta: movies and episodes go to the player, nothing is sent back.",
  "hiby.reboot": "Reboot the player after unplugging to see the changes (SonixOS keeps an old view of the card until a reboot).",
  "tips.head": "Tips for this player",
  "tips.more": "Read more",
  "tips.link": "Tips for this player",
  "tips.link_sub": "Recommended habits and player settings, on the Overview tab",
  "tips.hiby.eject": "Eject in SyncMusic before unplugging. After unplugging, let the R1 finish updating its library before you connect it again or switch it off - this protects its library database.",
  "tips.hiby.detect": "Detect changes (on the R1: Settings → Library) can be off when only SyncMusic fills the player: it starts faster and does not rewrite its database. Keep it on if you also copy files by hand. SyncMusic cannot change this setting.",
  "tips.hiby.split": "Split genres, Split artists and Join albums are settings of the player (Settings → Library). Device settings → Player library → Split genres follows the player by default; if you set it to Off or On, choose the same on the R1 - the sync report warns while the two differ.",
  "tips.hiby.fs": "exFAT is the recommended file system for big cards. If the player or the computer reports file system errors, run Repair on this page.",
  "tips.hiby.automount": "Do not let the computer's own automounter open the player while SyncMusic uses it (a file manager window counts too).",
  "var.copy": "Files as they are",
  "var.lossless": "Lossless as {to}",
  "var.fit": "{q} for lossless, only when it doesn't fit",
  "var.lossy": "Lossless → {q}",
  "var.fits": "{needed} · fits in {free}",
  "var.no_fit": "{needed} · does not fit in {free}",
  "var.size": "{needed}",
  "var.converted": " · converts {n} files",
  "var.converted_one": " · converts {n} file",
  "var.best": "Best",
  "dev.state.ejecting": "Ejecting",
  "dev.state.ejected": "Ejected - unplug it",
  "dev.state.no_medium": "Storage not shared",
  "dev.state.clock": "Setting the clock…",
  "dev.state.syncing": "Syncing",
  "dev.state.updating": "Updating",
  "dev.state.checking": "Checking files…",
  "dev.state.error": "Needs attention",
  "dev.state.review": "Waiting for review",
  "dev.state.no_libraries": "No templates",
  "dev.state.connected": "Connected",
  "dev.state.offline": "Not connected",
  "reg.sg.copy": "Suggested: copies as is - all {files} play natively{fit}.",
  "reg.sg.convert": "Suggested: converts {converted} of {files}{fit}{asis}.",
  "reg.sg.unplayable": "Suggested: {unplayable} of {files} would still not play on this device{fit}.",
  "reg.sg.generated": "No template fits; a new one converts {formats}. ",
  "reg.sg.generated_fit": "No template fits; a new one converts the lossless files to save space. ",
  "reg.sg.fits": " and fit - {needed} of {free} free",
  "reg.sg.no_fit": " - {needed} would not fit in {free}",
  "reg.sg.as_is": " (as they are: {as_is})",
  "reg.sg.files": "{n} files",
  "reg.sg.files_one": "{n} file",
});

/** The device type's icon (icons.js). */
export const deviceIcon = (type, size) => typeIcon(type, "device", size);

/** [badge state, text] of a registered device from GET /devices. */
// the integrity check and its Delete broken files are device jobs, but no sync
export const isCheck = (kind) => kind === "check" || kind === "check-fix";

export function deviceState(d) {
  if (d.ejecting) return ["busy", t("dev.state.ejecting")];
  if (d.ejected) return ["neutral", t("dev.state.ejected")];
  if (d.no_medium) return ["warn", t("dev.state.no_medium")]; // storage not shared
  if (d.setting_clock) return ["busy", t("dev.state.clock")];
  // an automatic update is quiet: never called a sync
  if (d.busy && d.update?.state === "running") return ["busy", t("dev.state.updating")];
  if (d.busy) return ["busy", t(isCheck(d.busy_kind) ? "dev.state.checking" : "dev.state.syncing")];
  if (d.error) return ["error", t("dev.state.error")];
  if (d.no_libraries) return ["warn", t("dev.state.no_libraries")];
  if (d.waiting && d.connected) return ["warn", t("dev.state.review")];
  if (d.connected) return ["ok", t("dev.state.connected")];
  return ["neutral", t("dev.state.offline")];
}

// The iPod's iTunesDB size against its limit: shown on the device page and in the plan review; the level
// (ok / warn / over) comes from the service, which owns the 63 MiB limit and the warning threshold.
addStrings({
  "db.label": "iTunesDB",
  "db.size": "{size} of {limit}",
  "db.none": "no database yet",
  "db.level.ok": "Within the limit",
  "db.level.warn": "Close to the limit",
  "db.level.over": "Over the limit",
});

const DB_BADGE = { ok: "ok", warn: "warn", over: "error" };

export function mib(n) {
  const v = Number(n) / 1048576;
  return `${Number.isInteger(v) ? v : v.toFixed(1)} MiB`;
}

/** "iTunesDB: 1.2 MiB of 63 MiB" with a worded badge; `size` null = no database yet. */
export function dbSizeLine(size, limit, level) {
  const text = size === null || size === undefined ? t("db.none") : t("db.size", { size: mib(size), limit: mib(limit) });
  return html`<div class="dbsize" data-db-level="${level}" style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:center">
    <span>${t("db.label")}: <span data-db-size>${text}</span></span>${badge(DB_BADGE[level] || "neutral", t(`db.level.${level}`))}</div>`;
}

/** The same as a bar - only in the iPod's Device settings: the level's colour plus its worded badge above. */
export function dbSizeBar(size, limit, level) {
  const pct = size && limit ? Math.min(100, Math.round((size / limit) * 100)) : 0;
  const text = size === null || size === undefined ? t("db.none") : t("db.size", { size: mib(size), limit: mib(limit) });
  return html`<div class="bar dbbar lvl-${level}" style="height:9px;margin-top:6px" role="progressbar" aria-valuemin="0" aria-valuemax="100"
    aria-valuenow="${pct}" aria-valuetext="${text}" aria-label="${t("db.label")}" data-db-bar><i style="width:${pct}%"></i></div>`;
}

// A small square cover next to a row's checkbox: a placeholder note shows until the lazily loaded image
// is there, and stays when there is no cover (204: the image removes itself, see app.js).
// `ico`: the placeholder behind a missing picture - "music" (a note) or "video"
export function thumb(src, ico = "music") {
  return html`<span class="thumb${ico === "video" ? " video" : ""}" aria-hidden="true">${src ? html`<img src="${src}" alt="" loading="lazy" decoding="async">` : ""}</span>`;
}

// time left of a running job: `eta_s` from the service (null = not known yet)
addStrings({
  "eta.estimating": "Estimating the time left…",
  "eta.left": "About {left} left · done at {at}",
  "eta.left_tomorrow": "About {left} left · done tomorrow at {at}",
  "eta.left_day": "About {left} left · done {day}, {at}",
  "eta.soon": "Less than a minute left",
  "eta.min": "{n} min",
  "eta.h": "{h} h {m} min",
  "eta.short": "~{left}",
});

/** "12 min" / "1 h 05 min" */
export function duration(s) {
  const min = Math.round(s / 60);
  return min < 60 ? t("eta.min", { n: min }) : t("eta.h", { h: Math.floor(min / 60), m: min % 60 });
}

/** "About 12 min left · done at 14:05" / "… done tomorrow at 05:27" / "… done Tue 27 Oct, 05:27" /
 * "Less than a minute left" / "Estimating…"; short: "~12 min" or "". */
export function etaText(eta, short = false, now = new Date()) {
  if (eta === null || eta === undefined) return short ? "" : t("eta.estimating");
  if (eta < 60) return short ? t("eta.short", { left: "1 min" }) : t("eta.soon");
  if (short) return t("eta.short", { left: duration(eta) });
  const end = new Date(now.getTime() + eta * 1000);
  const at = end.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(end) - midnight(now)) / 86_400_000);
  if (days === 0) return t("eta.left", { left: duration(eta), at });
  if (days === 1) return t("eta.left_tomorrow", { left: duration(eta), at });
  const day = end.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  return t("eta.left_day", { left: duration(eta), at, day });
}

/** The templates a device of `type` may link: a music-only one (Rockbox) gets none that draws only
 * from libraries without music (Audiobookshelf), nor one whose every rule takes only other kinds (a folder's "All
 * podcasts"). `types`: /device-types, `libTypes`: /library-types. */
export function templatesFor(type, templates, types, libraries, libTypes) {
  if (!types.find((x) => x.type === type)?.music_only) return templates;
  const kinds = Object.fromEntries(libTypes.map((x) => [x.type, x.capabilities.kinds]));
  const music = Object.fromEntries(libraries.map((l) => [l.id, (kinds[l.type] || ["music"]).includes("music")]));
  const musicRules = (tp) => !tp.rules.length || tp.rules.some((r) => !r.kind?.length || r.kind.includes("music"));
  return templates.filter((tp) => (!tp.library_ids.length || tp.library_ids.some((id) => music[id] !== false)) && musicRules(tp));
}

const CODEC = { aac: "AAC", mp3: "MP3", opus: "Opus", ogg: "Vorbis", alac: "ALAC", flac: "FLAC" };

/** A generated chain's name: "Lossless → MP3 320k", "MP3 V0", "Vorbis q6". */
export function variantLabel(v) {
  const name = CODEC[v.to] || (v.to || "").toUpperCase();
  const q = v.quality || {};
  const level = q.bitrate_kbps ? ` ${q.bitrate_kbps}k` : q.vbr === undefined ? "" : v.to === "mp3" ? ` V${q.vbr}` : ` q${q.vbr}`;
  return t(`var.${v.key}`, { to: name, q: name + level });
}

/** The generated chains of a suggestion as radios `name` (value = index), each with its estimated size vs free
 * space; `selected` (default: the best) is checked. */
export function variantList(sg, name, selected = sg.best_variant) {
  return html`<div class="ds-options var-list" role="radiogroup" data-variants>${sg.variants.map((v, i) => {
    const size = sg.free === null || sg.free === undefined ? t("var.size", { needed: bytes(v.needed) })
      : t(v.fits ? "var.fits" : "var.no_fit", { needed: bytes(v.needed), free: bytes(sg.free) });
    const conv = v.converted ? t("var.converted", { n: v.converted }) : "";
    return html`<label class="option" data-variant="${i}"><input type="radio" name="${name}" value="${i}" ${i === selected ? "checked" : ""}>
      <span><b>${variantLabel(v)}${i === sg.best_variant ? html` ${badge("ok", t("var.best"))}` : ""}</b><small>${size}${conv}</small></span></label>`;
  })}</div>`;
}

/** The reason line of a suggestion (Register and the Processors tab). */
export function suggestText(r) {
  const fit = r.free === null || r.free === undefined ? ""
    : t(r.fits ? "reg.sg.fits" : "reg.sg.no_fit", { needed: bytes(r.needed), free: bytes(r.free) });
  const asis = r.as_is ? t("reg.sg.as_is", { as_is: bytes(r.as_is) }) : "";
  const gen = !r.generated ? "" : r.formats?.length ? t("reg.sg.generated", { formats: r.formats.join(", ") }) : t("reg.sg.generated_fit");
  return gen + t(`reg.sg.${r.key}`, { ...r, files: t("reg.sg.files", { n: r.files }), fit, asis });
}

// HiBy: SonixOS keeps its databases open over USB and shows a stale card until it is rebooted
export const hibyReboot = (type) => (type === "hiby" ? html`<div class="callout" data-hiby-reboot>${t("hiby.reboot")}</div>` : "");

// Tips per device type: short advice shown on the device page; DEVICE_TIPS lists each type's i18n keys
// (`tips.<type>.<key>`) and the docs section they come from. Add a type here to give it a card.
const DEVICE_TIPS = {
  hiby: { keys: ["eject", "detect", "split", "fs", "automount"], docs: "04-devices.md#tips-for-the-hiby-r1" },
};
const TIPS_CLOSED = "deviceTipsClosed";
export const hasTips = (type) => type in DEVICE_TIPS;
export const tipsDocs = (type) => DEVICE_TIPS[type]?.docs || "";

/** The collapsible "Tips for this player" card of a device type (empty for a type without tips); it stays
 * collapsed once the user closes it (per type, in this browser). */
export function deviceTips(type, docsBase) {
  const tips = DEVICE_TIPS[type];
  if (!tips) return "";
  let closed = [];
  try { closed = JSON.parse(localStorage.getItem(TIPS_CLOSED) || "[]"); } catch { /* a broken value: open */ }
  return html`<details class="card dev-card dev-tips" id="deviceTips" data-device-tips="${type}" ${closed.includes(type) ? "" : "open"}>
    <summary><h2>${t("tips.head")}</h2></summary>
    <ul>${tips.keys.map((k) => html`<li data-tip="${k}">${t(`tips.${type}.${k}`)}</li>`)}</ul>
    <a href="${docsBase}${tips.docs}" target="_blank" rel="noopener">${t("tips.more")}</a></details>`;
}

document.addEventListener("toggle", (e) => {
  const el = e.target;
  if (!(el instanceof HTMLElement) || !el.dataset?.deviceTips) return;
  let closed = [];
  try { closed = JSON.parse(localStorage.getItem(TIPS_CLOSED) || "[]"); } catch { /* start over */ }
  const type = el.dataset.deviceTips;
  closed = closed.filter((x) => x !== type);
  if (!el.open) closed.push(type);
  localStorage.setItem(TIPS_CLOSED, JSON.stringify(closed));
}, true);

// the "can be unplugged" toast; a HiBy's adds the reboot hint once per browser session
// device and library types still in beta (HiBy SonixOS left beta): a badge next to the type
// wherever it is shown
const BETA_TYPES = new Set(["android", "jellyfin"]);

export function betaBadge(type) {
  return BETA_TYPES.has(type) ? html` <span class="tag beta" data-beta title="${t(`beta.${type}`)}">${t("beta.badge")}</span>` : "";
}

export function ejectedText(cfg, text) {
  if (cfg.type !== "hiby" || sessionStorage.getItem("hibyRebootHint")) return text;
  sessionStorage.setItem("hibyRebootHint", "1");
  return `${text} ${t("hiby.reboot")}`;
}

// A button that starts a device job: disabled while the request runs, then the job view opens; the button
// comes back only when the request fails. `start` returns the job; `message` is toasted once it started.
export async function openJob(el, ctx, deviceId, start, message = "") {
  el.disabled = true;
  try {
    const job = await start();
    if (message) toast(message);
    goFrom(ctx, `/devices/${encodeURIComponent(deviceId)}/sync/${encodeURIComponent(job.id)}`);
  } catch (e) {
    el.disabled = false;
    throw e;
  }
}

// rolling write speed of a sync: bytes/s over the last ~15 s of progress events, null until two
// samples are far enough apart
const speedSamples = new Map();
export function writeSpeed(jobId, written, now = Date.now()) {
  if (!jobId || !written) return null;
  const list = speedSamples.get(jobId) || [];
  if (!list.length || list[list.length - 1][1] !== written) list.push([now, written]);
  while (list.length > 2 && now - list[0][0] > 15_000) list.shift();
  speedSamples.set(jobId, list);
  if (speedSamples.size > 20) speedSamples.delete(speedSamples.keys().next().value);
  const [t0, b0] = list[0];
  return now - t0 >= 2000 ? ((written - b0) * 1000) / (now - t0) : null;
}

addStrings({
  "side.step.reverse": "Reading listening data",
  "side.step.copy": "Copying {n}/{total}",
  "side.step.database": "Writing the database",
  "side.step.verify": "Verifying",
});

// a running sync's step for the device cards: "Copying 120/400 · 18 MB/s"; "" outside these steps
export function stepText(job) {
  const st = job?.stats;
  if (!st?.phase || !["reverse", "copy", "database", "verify"].includes(st.phase)) return "";
  const speed = st.phase === "copy" ? writeSpeed(job.job_id || job.id, st.bytes) : null;
  return [t(`side.step.${st.phase}`, { n: st.files_done ?? 0, total: st.files_total ?? "?" }), speed ? `${bytes(speed)}/s` : ""]
    .filter(Boolean).join(" · ");
}
