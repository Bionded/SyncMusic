// Plan review: what a sync will do before anything is written. Count cards (To add,
// To replace, Update tags, To remove, Keep, Left out) double as filters of the list below, where every item has
// its artwork, a reason chip and its size; Space now / after as kind bars; warning boxes; a sticky bottom bar with
// Cancel and "Apply · write N". A removal is never applied by one click: the bar's "Yes, remove N files" box has to
// be ticked first. A first sync needs keep / adopt / wipe (wipe asks again); Force plans again with priority
// trimming (one confirmation) and applying a forced plan asks a second time. Cancel and Edit templates change
// nothing.
import { html, bytes, when, setHtml } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { helpTip } from "../help.js";
import { get, post, patch, del } from "../api.js";
import { badge, toast, confirm, errorBox, problemBox } from "../ui.js";
import { icon } from "../icons.js";
import { goFrom } from "../router.js";
import { dbSizeLine, mib, deviceIcon, thumb, suggestText, openJob } from "./common.js";

addStrings({
  "plan.group_n": "{n} items",
  "plan.group_n_one": "{n} item",
  "plan.group_reasons": "{n} reasons",
  "plan.group_reasons_one": "{n} reason",
  "plan.pls": "Playlists",
  "plan.pls.create": "Create “{name}” ({n} songs)",
  "plan.pls.create_one": "Create “{name}” ({n} song)",
  "plan.pls.update": "Update “{name}” ({n} songs)",
  "plan.pls.update_one": "Update “{name}” ({n} song)",
  "plan.pls.remove": "Remove “{name}” from the device",
  "plan.pls.same": "{n} playlists stay as they are.",
  "plan.pls.same_one": "{n} playlist stays as it is.",
  "plan.pls.from_rule": "made by a rule",
  "plan.pls.from_library": "a library playlist",
  "plan.pls.from_smart": "a smart playlist of the library",
  "crumb.plan": "Sync plan",
  "plan.title": "Sync plan · {name}",
  "plan.sub": "Built {time} from {n} templates · nothing has been written yet",
  "plan.sub_one": "Built {time} from {n} template · nothing has been written yet",
  "plan.sub_plain": "Nothing is written until you apply.",
  "plan.planning": "Planning the sync of {name}… a big library takes a moment.",
  "plan.progress_title": "Planning the sync of {name}",
  "plan.progress_hint": "A big library takes a moment. You can leave this page: planning goes on, and coming back shows it.",
  "plan.step.libraries": "Reading the libraries",
  "plan.step.device": "Reading the device",
  "plan.step.compare": "Applying the templates and comparing",
  "plan.step.checks": "Checking space and limits",
  "plan.step_of": "Step {n} of {total}",
  "plan.library_items": "{name}: {n} items so far",
  "plan.library_items_one": "{name}: {n} item so far",
  "plan.items": "{n} items in the libraries",
  "plan.items_one": "{n} item in the libraries",
  "plan.elapsed": "Elapsed {time}",
  "plan.cancel_planning": "Cancel",
  "plan.cancelled": "Planning cancelled.",
  "plan.failed": "Planning failed",
  "plan.edit_rules": "Edit templates",
  "plan.again": "Plan again",
  "plan.gone": "This plan is outdated (the device or its settings changed). Plan again.",
  // the export's group names
  "plan.add": "added",
  "plan.replace": "updated files",
  "plan.update_meta": "updated tags",
  "plan.remove": "removed",
  "plan.keep": "unchanged",
  "plan.left": "left out",
  // count cards and list tabs
  "plan.c.add": "To add",
  "plan.c.replace": "To replace",
  "plan.c.update_meta": "Update tags",
  "plan.c.remove": "To remove",
  "plan.c.keep": "Keep",
  "plan.c.left": "Left out",
  // the Metadata tab
  "plan.c.meta": "Metadata",
  "plan.note.meta": "Tags, ratings, play counts and resume positions this sync changes, on the device and in the libraries. Files are not involved.",
  "plan.meta.dir_all": "Both directions",
  "plan.fl.library_label": "Library",
  "plan.fl.library_all": "All libraries",
  "plan.fl.kind_label": "Kind",
  "plan.fl.kind_all": "All kinds",
  "plan.fl.rule_label": "Rule",
  "plan.fl.rule_all": "All rules",
  "plan.meta.to_device": "Library → device",
  "plan.meta.to_library": "Device → library",
  "plan.meta.field_all": "Every change",
  "plan.meta.dir_label": "Direction",
  "plan.meta.field_label": "Change",
  "plan.meta.conflicts": "{n} conflicts",
  "plan.meta.conflicts_one": "{n} conflict",
  "plan.meta.unknown": "changed (earlier values not recorded)",
  "plan.meta.none": "Nothing but files changes.",
  "plan.f.metadata": "Tags",
  "plan.f.title": "Title",
  "plan.f.artist": "Artist",
  "plan.f.album_artist": "Album artist",
  "plan.f.album": "Album",
  "plan.f.track_no": "Track",
  "plan.f.disc_no": "Disc",
  "plan.f.year": "Year",
  "plan.f.genres": "Genres",
  "plan.f.compilation": "Compilation",
  "plan.f.comment": "Comment",
  "plan.f.series": "Series",
  "plan.f.author": "Author",
  "plan.f.chapter_index": "Chapter",
  "plan.f.published": "Published",
  "plan.f.kind": "Kind",
  "plan.f.rating": "Rating",
  "plan.f.play_count": "Play count",
  "plan.f.played": "Played",
  "plan.f.position": "Resume position",
  "plan.v.played": "played",
  "plan.v.unplayed": "unplayed",
  "plan.cs.update_meta": "no file copy",
  "plan.cs.keep": "unchanged",
  "plan.cs.keep_others": "incl. {n} of other installations ({size})",
  "plan.cs.left": "by limits & space",
  "plan.note.add": "New matches for the templates, in template priority order.",
  "plan.note.replace": "The library has a changed or better copy than the device, or the file moves to a new path layout.",
  "plan.note.update_meta": "Only tags change; files are not copied again.",
  "plan.note.remove": "On the device, but no template wants them any more.",
  "plan.note.keep": "Already on the device and still matching.",
  "plan.note.left": "Would match, but did not fit. Raise the limit or move the template up to include them.",
  "plan.filter": "Filter",
  "plan.filter_label": "Filter items",
  "plan.showing": "Showing {shown} of {n}. Download the list for every item.",
  "plan.sample": "Showing {shown} of {n}: a sample spread over artists, albums and reasons.",
  "plan.loading_items": "Loading the items…",
  "plan.items_failed": "Could not load the items.",
  "plan.nothing_here": "Nothing here.",
  "plan.no_match": "No item matches “{q}”.",
  "plan.why.keep": "Still matches",
  "plan.why.limit": "Left out by the device limit",
  "plan.why.space": "No space after higher-priority templates",
  "plan.space": "Space",
  "plan.now": "Now",
  "plan.after": "After",
  "plan.now_text": "{used} used · {free} free",
  "plan.after_text": "{used} used · {free} free · {fit}",
  "plan.fits": "fits",
  "plan.no_fit": "does not fit",
  "plan.not_checked": "Not checked",
  "plan.needed_text": "Needs {needed}",
  "plan.space_unknown": "The server cannot tell its free space, so it is not checked (set “Space to use (GB)” in the device's Settings tab to check it).",
  "plan.reserve": "includes {reserve} reserved for the device database and the safety margin",
  "plan.frees_text": "Frees {freed}",
  "plan.reserve_kept": "keeps {reserve} reserved for the device database and the safety margin",
  "plan.confirm_reason": "Please confirm",
  "plan.w.remove": "{n} files will be removed",
  "plan.w.remove_one": "{n} file will be removed",
  "plan.remove_info": "{n} files will be removed from the device (below the delete threshold, no confirmation needed).",
  "plan.remove_info_one": "{n} file will be removed from the device (below the delete threshold, no confirmation needed).",
  "plan.remove_info_show": "Show the To remove list",
  "plan.w.remove_text": "Tick the box below to confirm, or look at the To remove list first.",
  "plan.w.db_warn": "The iPod database is getting big",
  "plan.w.db_over": "The iTunesDB would exceed its limit",
  "plan.w.db_text": "After this sync the iTunesDB would be {size} of {limit}. Consider a limit or fewer templates.",
  "plan.w.gentle": "Gentle write mode is on",
  "plan.w.health": "The device's storage may be failing",
  "plan.w.health_text": "{warning}. Writing many files may fail or hang; the Overview's Storage health card has advice.",
  "plan.w.gentle_text": "Files are written in batches of {batch} with {pause} s pauses. This sync takes about {n} min instead of {m}.",
  "plan.w.gentle_text_unknown": "Files are written in batches of {batch} with {pause} s pauses. The pauses add about {n} min to this sync.",
  "plan.w.gentle_text_secs": "Files are written in batches of {batch} with {pause} s pauses. The pauses add only about {s} s to this sync.",
  "plan.w.usb": "The last full sync stopped: the player dropped off USB",
  "plan.w.usb_text": "Gentle write mode (the device's Settings tab) may help; it is not turned on automatically.",
  "plan.gentle_on": "Turn on gentle mode",
  "plan.w.nocache": "This sync converts far more than the conversion cache holds",
  "plan.w.nocache_text": "{size} of converted files against a {max} cache: storing them would only push each other out. Without the cache, each file goes straight to the device.",
  "plan.nocache_on": "Sync without the cache",
  "plan.sg_title": "Processors for this plan",
  "plan.sg_use": "Use {name} and plan again",
  "plan.sg_use_draft": "Use the new chain and plan again",
  "plan.nocache_done": "{name} now syncs without the conversion cache.",
  "plan.gentle_done": "Gentle write mode is on for {name}. It applies from the next time the device is mounted.",
  "plan.gentle_undone": "Gentle write mode is off again for {name}.",
  "plan.resuming": "Resuming an interrupted sync",
  "plan.resuming_text": "The last sync stopped after {done} of {n} files ({when}). Those files are kept; only the rest is written now.",
  "plan.resuming_text_one": "The last sync stopped after {done} of {n} file ({when}). That file is kept.",
  "plan.no_fit_title": "Not everything fits on the device",
  "plan.no_fit_text": "Edit the templates, or force the sync: items of the lowest-priority templates are left off until the rest fits.",
  "plan.rule": "Template",
  "plan.items": "Items",
  "plan.size": "Size",
  "plan.dropped": "Left off",
  "plan.suggestions": "Suggestions",
  "plan.force": "Force: leave items off by priority…",
  "plan.fit_title": "Convert to fit",
  "plan.fit_text": "Convert lossless files, and files above the chosen bitrate, to a smaller format. Files at or below it are copied as they are.",
  "plan.fit_target": "Convert to",
  "plan.fit_estimate": "Needs {needed} of {free} free · {n} files converted",
  "plan.fit_estimate_one": "Needs {needed} of {free} free · {n} file converted",
  "plan.fit_fits": "fits",
  "plan.fit_no": "still does not fit",
  "plan.fit_ok": "Convert and plan again",
  "plan.fit_done": "The device now converts to {target}. New plan below.",
  "plan.force_title": "Force the sync?",
  "plan.force_text": "SyncMusic plans again and leaves the items of the lowest-priority templates off until the rest fits. You review the result before anything is written.",
  "plan.force_ok": "Plan with priority trimming",
  "plan.excluded": "Left out for space ({n})",
  "plan.excluded_text": "These items are not put on the device because they do not fit; the template priority decided which.",
  "plan.show_left": "Show them",
  "plan.first": "First sync of this device",
  "plan.first_text": "The device already holds {n} media files SyncMusic did not write. What should happen to them?",
  "plan.first_text_one": "The device already holds {n} media file SyncMusic did not write. What should happen to it?",
  "plan.first_empty": "The device holds no media files yet, so any choice works; it is remembered for this device.",
  "plan.limit_title": "Left out by the device limit ({n})",
  "plan.limit_text": "The device limit is {limit}. Filled first with: {order}. These items match the rules but are not put on the device.",
  "plan.limit_tracks": "{n} tracks",
  "plan.limit_tracks_one": "{n} track",
  "plan.limit_kept": "On the device: {n} items · {size}",
  "plan.limit_kept_one": "On the device: {n} item · {size}",
  "plan.limit_badge": "Limit",
  "plan.limit_edit": "Change the limit",
  "plan.fill.priority": "Rule priority",
  "plan.fill.rating": "Highest rated",
  "plan.fill.recently_played": "Recently played",
  "plan.fill.play_count": "Most played",
  "plan.fill.recently_added": "Recently added",
  "plan.fill.random": "Random (stable per device)",
  "plan.first.keep": "Keep",
  "plan.first.keep_text": "Leave them alone; SyncMusic only manages what it writes.",
  "plan.first.adopt": "Adopt",
  "plan.first.adopt_text": "Files that match library items ({n} found) are taken over instead of copied again.",
  "plan.first.wipe": "Wipe",
  "plan.first.wipe_text": "Delete everything on the device first.",
  "plan.first_missing": "Choose keep, adopt or wipe for the first sync.",
  "plan.wipe_title": "Wipe {name}?",
  "plan.wipe_text": "Every file on the device is deleted before the sync. This cannot be undone.",
  "plan.wipe_ok": "Wipe and sync",
  "plan.forced_title": "Apply the forced plan?",
  "plan.forced_text": "{n} items are left off the device. Apply anyway?",
  "plan.forced_text_one": "{n} item is left off the device. Apply anyway?",
  "plan.forced_ok": "Apply and leave them off",
  "plan.apply": "Apply",
  "plan.apply_write": "Apply · write {size}",
  "plan.ok_remove": "Yes, remove {n} files from {name}",
  "plan.ok_remove_one": "Yes, remove {n} file from {name}",
  "plan.ok_reasons": "Yes, apply despite the warnings above",
  "plan.pl_conflicts": "Playlist conflicts",
  "plan.pl_conflicts_text": "Choose what the sync does with each playlist. \"Decide later\" changes nothing on either side this time.",
  "plan.pl_reorder": "Reordered on the player and in the library",
  "plan.pl_name_taken": "Made on the player; the library has a playlist of this name",
  "plan.pl_choice_label": "What to do with {name}",
  "plan.pl.ask": "Decide later (change nothing)",
  "plan.pl.merge": "Merge both",
  "plan.pl.replace": "The player's version wins",
  "plan.pl.ignore": "The library's version wins",
  "plan.pl.rename": "Save the player's as a copy",
  "plan.db_now": "Now: {size}",
  "plan.db_title": "The iTunesDB would exceed its limit",
  "plan.db_text": "After this sync the iTunesDB would be {size}, above the {limit} limit. The iPod may fail to read its database and show no music. Transcoding does not make it smaller; only fewer items do.",
  "plan.db_ok": "Apply despite the risk",
  "plan.export": "Download list",
  "plan.nothing": "Nothing to do: the device is up to date.",
  "plan.accounts": "Signed in as: {list}",
  "plan.no_libraries": "Nothing goes to {name} yet: no template takes media from a library. Choose templates on the device's Templates tab first.",
  "plan.attach": "Choose templates",
});

export const nav = "devices";
export const title = (ctx) => (ctx.state.cfg ? t("plan.title", { name: ctx.state.cfg.name }) : t("nav.devices"));
// Devices / <device> / Sync plan
export const crumbs = (ctx) => (ctx.state.cfg ? [{ label: t("nav.devices"), href: "#/devices" },
  { label: ctx.state.cfg.name, href: `#/devices/${encodeURIComponent(ctx.state.cfg.id)}` }, { label: t("crumb.plan") }] : []);

const KINDS = ["add", "replace", "update_meta", "remove", "keep"];
const GROUPS = [...KINDS, "left"];
const LIST_LIMIT = 200;
const GROUPS_OPEN = 8; // a short list shows its album groups open, a long one collapsed
// the reason chip's look: [icon, class]
const WHY = { rule: "templates", better: "star", exc: "filter", lim: "warn", space: "warn", keep: "check" };
const KIND_VARS = { music: "--k-mu", audiobook: "--k-ab", podcast: "--k-po", other: "--k-ot" };

const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const signed = (n) => (n < 0 ? `−${bytes(-n)}` : n > 0 ? `+${bytes(n)}` : bytes(0));
const baseName = (p) => String(p || "").split("/").pop().replace(/\.[^.]+$/, "");
const dirName = (p) => String(p || "").split("/").slice(0, -1).join("/");

export async function render(ctx) {
  const id = ctx.params.id;
  const info = await get(`/devices/${encodeURIComponent(id)}`);
  ctx.state.cfg = info.config;
  if (Object.keys(info.config.library_accounts || {}).length) { // library names for "Signed in as"
    const libs = await get("/libraries").catch(() => []);
    ctx.state.libNames = Object.fromEntries(libs.map((l) => [l.id, l.name]));
    ctx.state.libAccounts = Object.fromEntries(libs.map((l) => [l.id, l.accounts || {}]));
  }
  ctx.state.info = info;
  if (!info.libraries.length) { // nothing to plan: say why and where to fix it, no Retry
    const lid = encodeURIComponent(id);
    return html`${head(ctx)}<div class="card"><div class="card-body" data-no-libraries>
      <div class="callout">${t("plan.no_libraries", { name: info.config.name })}</div>
      <a class="btn primary" style="margin-top:12px" href="#/devices/${lid}/rules">${t("plan.attach")}</a></div></div>`;
  }
  if (!ctx.params.planId) {
    // plan now with its progress on screen, then replace the URL by the plan's (Back does not plan
    // again); a plan of this device that is already running (the page was left and opened again) is joined
    const running = (await get("/planning").catch(() => [])).find((p) => p.device_id === id);
    ctx.state.progress = running || ctx.state.progress
      || { step: "libraries", index: 0, steps: 4, fraction: null, progress: null, items: 0, detail: "", started_at: new Date().toISOString() };
    if (!ctx.state.planning) {
      ctx.state.planning = post(`/devices/${encodeURIComponent(id)}/plan`, { force: ctx.query.get("force") === "1" })
        .then((plan) => goFrom(ctx, `/devices/${encodeURIComponent(id)}/plan/${encodeURIComponent(plan.id)}`, { replace: true }))
        .catch((e) => {
          if (location.hash !== ctx.hash) return;
          const box = document.querySelector("[data-planning]");
          if (box && e.code !== "cancelled") setHtml(box, html`<div class="card-body">${errorBox(e, t("plan.failed"))}
            <button class="btn primary" style="margin-top:12px" data-action="replan">${t("plan.again")}</button></div>`);
        });
    }
    return html`${head(ctx)}<div class="card plan-progress" data-planning>${progressBody(ctx)}</div>`;
  }
  let plan;
  try {
    plan = await get(`/plans/${encodeURIComponent(ctx.params.planId)}`);
  } catch (e) {
    if (e.status !== 404) throw e;
    return html`${head(ctx)}<div class="card"><div class="card-body">${errorBox({ message: t("plan.gone") }, "")}
      <button class="btn primary" style="margin-top:12px" data-action="replan">${t("plan.again")}</button></div></div>`;
  }
  ctx.state.plan = plan;
  ctx.state.rows = rows(plan);
  ctx.state.q = "";
  ctx.state.full = {}; // group -> {key (listKey), rows, total}: a page of the full list, fetched when the sample is not all of it
  ctx.state.mfield = ""; // the Metadata tab's filters
  ctx.state.flib = ctx.state.fkind = ctx.state.frule = ""; // the list's filters
  ctx.state.mdir = "";
  ctx.state.group = GROUPS.filter((g) => g !== "keep").find((g) => total(plan, g)) || "keep";
  ctx.state.fit = null; // transcode to fit: offered when bytes are what does not fit
  const fitEst = plan.fit_targets.length
    ? post(`/devices/${encodeURIComponent(id)}/fit/estimate`, plan.fit_targets[0]).catch(() => null) : null;
  // the used bytes per kind now; a device that went away meanwhile shows the plan's own numbers
  const space = plan.space.unknown ? null : await get(`/devices/${encodeURIComponent(id)}/space`).catch(() => null);
  if (fitEst) ctx.state.fit = { target: plan.fit_targets[0], est: await fitEst };
  // no-cache mode is suggested when the converted bytes are far above the cache limit
  ctx.state.cacheMax = plan.processed_bytes ? (await get("/settings/cache").catch(() => null))?.max : null;
  // the processor template that suits what this plan writes; shown when it is not in use
  ctx.state.planSuggest = plan.counts.add || plan.counts.replace
    ? await post("/devices/suggest-processor-template", { plan_id: plan.id }).catch(() => null) : null;
  const removes = plan.counts.remove || 0;
  const reasons = otherReasons(plan);
  // removals ask for a tick only above the device's delete threshold, like automatic syncs
  const removeOk = removes > 0 && plan.removals_over_threshold;
  ctx.state.needOk = removeOk || reasons.length > 0;
  return html`${head(ctx, plan)}
    <div class="plan-page">
      ${countCards(ctx)}
      ${spaceCard(plan, space)}
      ${plan.resuming ? html`<div class="callout" data-resuming><b>${t("plan.resuming")}:</b>
        ${t("plan.resuming_text", { done: plan.resuming.done, n: plan.resuming.total, when: when(plan.resuming.at) })}</div>` : ""}
      ${warnings(ctx, removes, removeOk, reasons)}
      ${playlistConflicts(plan)}
      ${playlistsCard(plan)}
      ${planSuggestion(ctx)}
      ${ctx.state.fit ? fitCard(plan, ctx.state.fit) : ""}
      ${plan.does_not_fit ? noFit(plan) : ""}
      ${plan.space.dropped.length ? excluded(plan) : ""}
      ${plan.limit?.left_out.length ? limited(plan, ctx) : ""}
      ${plan.first_sync ? firstSync(plan, ctx.state.cfg.first_sync_choice) : ""}
      ${listCard(ctx)}
      ${applyBar(ctx, removeOk ? removes : 0)}
    </div>`;
}

// --- planning progress ------------------------------------------------------------------------

const STEPS = ["libraries", "device", "compare", "checks"];
const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const elapsed = (p) => clock(Math.max(0, (Date.now() - new Date(p.started_at).getTime()) / 1000));

const count = (n) => (n === 1 ? 1 : n.toLocaleString()); // `n` for t()'s singular, else with separators
// what the step has counted: the library being read and its items so far, later the items of all libraries
function stepLine(p) {
  if (p.step === "libraries" && p.detail) return t("plan.library_items", { name: p.detail, n: count(p.items) });
  return p.items ? t("plan.items", { n: count(p.items) }) : "";
}

function progressBody(ctx) {
  const p = ctx.state.progress;
  const pct = p.progress === null || p.progress === undefined ? null : Math.round(p.progress * 100);
  return html`<div class="card-body">
    <h2 class="plan-progress-title">${t("plan.progress_title", { name: ctx.state.cfg.name })}</h2>
    <p class="muted">${t("plan.progress_hint")}</p>
    <ol class="plan-steps" aria-label="${t("plan.step_of", { n: p.index + 1, total: STEPS.length })}">
      ${STEPS.map((s, i) => html`<li data-step="${s}" class="${i < p.index ? "done" : i === p.index ? "current" : ""}"
        ${i === p.index ? html`aria-current="step"` : ""}><span class="plan-step-dot" aria-hidden="true">${i < p.index ? icon("check", { size: 12 }) : i + 1}</span>
        <span>${t(`plan.step.${s}`)}</span></li>`)}
    </ol>
    ${pct === null
      ? html`<span class="bar indet" role="progressbar" aria-label="${t(`plan.step.${p.step}`)}" data-plan-bar><i></i></span>`
      : html`<span class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" data-plan-bar><i style="width:${pct}%"></i></span>`}
    <div class="plan-progress-line">
      <span data-plan-step>${t("plan.step_of", { n: p.index + 1, total: STEPS.length })}${pct === null ? "" : ` · ${pct} %`}${stepLine(p) ? ` · ${stepLine(p)}` : ""}</span>
      <span class="mono" data-plan-elapsed>${t("plan.elapsed", { time: elapsed(p) })}</span>
    </div>
    <div class="plan-progress-actions"><button type="button" class="btn" data-action="planCancel">${t("plan.cancel_planning")}</button></div>
  </div>`;
}

function head(ctx, plan = null) {
  const cfg = ctx.state.cfg;
  const id = encodeURIComponent(ctx.params.id);
  const n = cfg.rule_set?.template_ids?.length || 0;
  const time = plan ? new Date(plan.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  return html`<div class="pagehead plan-head"><div class="plan-title"><span class="dev-ico">${deviceIcon(cfg.type, 26)}</span>
      <div><h1>${t("plan.title", { name: cfg.name })} ${helpTip("plan")}</h1><p>${plan ? t("plan.sub", { time, n }) : t("plan.sub_plain")}</p>
      ${accountsLine(ctx)}</div></div>
    <div class="actions">${plan ? html`<button type="button" class="btn" data-action="planExport">${icon("download", { size: 16 })}${t("plan.export")}</button>` : ""}
      <button type="button" class="btn" data-action="replan">${t("plan.again")}</button>
      <a class="btn" href="#/devices/${id}/rules">${t("plan.edit_rules")}</a></div></div>`;
}

// the library accounts the device signs in as: "Signed in as: Navidrome - Anna"
function accountsLine(ctx) {
  const own = Object.entries(ctx.state.cfg.library_accounts || {});
  if (!own.length) return "";
  const names = ctx.state.libNames || {};
  const list = own.map(([lid, a]) => `${names[lid] || lid} - ${ctx.state.libAccounts?.[lid]?.[a]?.name || a}`).join(", ");
  return html`<p class="muted" data-plan-accounts>${t("plan.accounts", { list })}</p>`;
}

// --- the list's rows ---------------------------------------------------------------------------------------------

/** Every row of every group: {title, sub, why (chip look), reason, size, cover, text (for the filter)}. */
function rows(plan) {
  const out = Object.fromEntries(GROUPS.map((g) => [g, []]));
  for (const a of plan.actions) out[a.kind]?.push(actionRow(a));
  for (const x of plan.limit?.left_out || []) out.left.push(leftRow(x, "lim", t("plan.why.limit")));
  const detail = plan.space.dropped_detail?.length ? plan.space.dropped_detail : plan.space.dropped.map((key) => ({ key, title: key, size: 0 }));
  for (const x of detail) out.left.push(leftRow(x, "space", t("plan.why.space")));
  for (const g of GROUPS) for (const r of out[g]) r.text = `${r.title} ${r.sub} ${r.reason}`.toLowerCase();
  return out;
}

function actionRow(a) {
  const tr = a.item?.track;
  // a removal of an item the library knows comes with its title and artist - album, else its file path
  const named = !tr && a.subtitle !== null && a.subtitle !== undefined;
  const title = tr ? tr.title || a.title : named ? a.title : baseName(a.title) || a.ref.item_id;
  const sub = tr ? [tr.artist || tr.author, tr.album].filter(Boolean).join(" - ") : named ? a.subtitle : dirName(a.title);
  const cover = tr?.has_cover ? `/api/v1/libraries/${encodeURIComponent(a.ref.library_id)}/thumb?${new URLSearchParams({ item: a.ref.item_id })}` : null;
  let why = "rule", reason = a.reason, size = "";
  if (a.kind === "add") size = bytes(a.size_delta);
  else if (a.kind === "replace") {
    why = a.reason === "source changed" ? "better" : "rule";
    size = signed(a.size_delta);
  } else if (a.kind === "update_meta") size = "-";
  else if (a.kind === "remove") {
    why = a.reason.startsWith("excluded") ? "exc" : a.reason === "over the device limit" ? "lim" : "rule";
    size = signed(a.size_delta);
  } else { // keep
    why = a.orphan ? "exc" : a.reason === "skipped for space" ? "space" : "keep";
    reason = a.reason || t("plan.why.keep");
  }
  // rows of one library + artist - album (podcast show, book) form one collapsible group
  const group = sub ? `${a.ref.library_id}\n${sub}` : null;
  const media = tr?.kind || a.entry?.kind || "music";
  return { title, sub, why, reason: cap(reason), size, cover, group, kind: a.kind, delta: a.size_delta, lib: a.ref.library_id, media, rules: a.rules || [] };
}

// a left-out item carries only its key ("library/item"): it matches a library filter, never a kind or rule one
const leftRow = (x, why, reason) => ({ title: x.title || x.key, sub: "", why, reason, size: x.size ? bytes(x.size) : "", cover: null, lib: x.key.split("/")[0], media: null, rules: [] });

/** The list's library / kind / rule filters: shown when the plan has more than one of a facet. */
function listFilters(ctx) {
  const { plan, flib, fkind, frule } = ctx.state;
  const f = plan.facets || { library: {}, kind: {}, rule: {} };
  const ruleName = Object.fromEntries((plan.space.per_rule || []).map((r) => [r.rule_id, r.name || r.rule_id]));
  const sel = (name, attr, cur, opts) => {
    const keys = Object.keys(f[name]);
    if (keys.length < 2 && !cur) return "";
    return html`<label><span class="sr">${t(`plan.fl.${name}_label`)}</span><select ${attr} aria-label="${t(`plan.fl.${name}_label`)}">
      <option value="">${t(`plan.fl.${name}_all`)}</option>
      ${keys.map((k) => html`<option value="${k}" ${k === cur ? "selected" : ""}>${opts(k)} (${f[name][k].toLocaleString()})</option>`)}</select></label>`;
  };
  const out = [sel("library", "data-fl-library", flib, (k) => k), sel("kind", "data-fl-kind", fkind, (k) => t(`cap.kind.${k}`)),
    sel("rule", "data-fl-rule", frule, (k) => ruleName[k] || k)].filter(Boolean);
  return out.length ? html`<div class="plan-mfilters" data-list-filters>${out}</div>` : "";
}

const filtered = (ctx) => Boolean(ctx.state.flib || ctx.state.fkind || ctx.state.frule);
const rowMatch = (r, { flib, fkind, frule }) => (!flib || r.lib === flib) && (!fkind || r.media === fkind) && (!frule || r.rules.includes(frule));

function rowHtml(r) {
  return html`<div class="prow" data-plan-item>${thumb(r.cover)}
    <span class="prow-text"><span class="prow-title" title="${r.title}">${r.title}</span>${r.sub ? html`<span class="prow-sub" title="${r.sub}">${r.sub}</span>` : ""}</span>
    ${r.reason ? html`<span class="why ${r.why}" data-reason title="${r.reason}">${icon(WHY[r.why], { size: 13 })}<span>${r.reason}</span></span>` : ""}
    <span class="prow-size">${r.size}</span></div>`;
}

/** `rs` with the rows of one album/show/book gathered into groups, in the order of their first row: a row stays
plain when it is alone. */
function grouped(rs) {
  const out = [], seen = new Map();
  for (const r of rs) {
    const g = r.group && seen.get(r.group);
    if (g) g.rows.push(r);
    else {
      const entry = { rows: [r] };
      if (r.group) seen.set(r.group, entry);
      out.push(entry);
    }
  }
  return out;
}

function groupSize(rs) {
  const kind = rs[0].kind, sum = rs.reduce((n, r) => n + (r.delta || 0), 0);
  return kind === "add" ? bytes(sum) : kind === "update_meta" ? "-" : kind === "keep" ? "" : signed(sum);
}

function groupHtml({ rows: rs }, open) {
  if (rs.length === 1) return rowHtml(rs[0]);
  const r = rs[0], reasons = [...new Set(rs.map((x) => x.reason).filter(Boolean))];
  const same = rs.every((x) => x.reason === r.reason && x.why === r.why);
  // mixed reasons: a neutral chip naming how many, the reasons themselves in its tooltip
  const mixed = !same && reasons.length > 1 ? html`<span class="why keep" data-reasons title="${reasons.join("\n")}">${icon("list", { size: 13 })}<span>${t("plan.group_reasons", { n: reasons.length })}</span></span>` : "";
  return html`<details class="pgroup" data-plan-group ${open ? "open" : ""}><summary class="prow">${thumb(rs.find((x) => x.cover)?.cover ?? null)}
    <span class="prow-text"><span class="prow-title" title="${r.sub}">${r.sub}</span><span class="prow-sub">${t("plan.group_n", { n: rs.length })}</span></span>
    ${same && r.reason ? html`<span class="why ${r.why}" title="${r.reason}">${icon(WHY[r.why], { size: 13 })}<span>${r.reason}</span></span>` : mixed}
    <span class="prow-size">${groupSize(rs)}</span>${icon("down", { size: 16, cls: "pgroup-chev" })}</summary>${rs.map(rowHtml)}</details>`;
}

function listBody(ctx) {
  const { group, q, plan } = ctx.state;
  const n = total(plan, group);
  const full = ctx.state.full[group];
  const remote = n > ctx.state.rows[group].length; // the sample is not the whole group: the list comes from the server
  const narrowed = q || filtered(ctx);
  const key = listKey(ctx);
  let shown, count;
  if (!remote) {
    const all = ctx.state.rows[group];
    shown = all.filter((r) => (!q || r.text.includes(q.toLowerCase())) && rowMatch(r, ctx.state));
    count = shown.length;
  } else if (full && full.key === key) {
    if (full.error) return html`${listFilters(ctx)}<div class="plan-empty" data-items-error>${t("plan.items_failed")}</div>`;
    shown = full.rows;
    count = full.total;
  } else {
    loadGroup(ctx, group, key);
    shown = narrowed ? [] : ctx.state.rows[group];
    count = narrowed ? 0 : n;
    if (narrowed) return html`${listFilters(ctx)}<div class="plan-empty" data-items-loading>${t("plan.loading_items")}</div>`;
  }
  const more = count > Math.min(shown.length, LIST_LIMIT) ? t(remote && !full ? "plan.sample" : "plan.showing", { shown: Math.min(shown.length, LIST_LIMIT), n: count.toLocaleString() }) : "";
  return html`<div class="plan-note" data-group-note>${t(`plan.note.${group}`)}</div>${n ? listFilters(ctx) : ""}
    ${shown.length ? grouped(shown.slice(0, LIST_LIMIT)).map((g, _, all) => groupHtml(g, narrowed || all.length <= GROUPS_OPEN))
      : html`<div class="plan-empty">${q ? t("plan.no_match", { q }) : n ? (filtered(ctx) ? t("plan.nothing_here") : "") : group === "keep" && !anyChange(plan) ? t("plan.nothing") : t("plan.nothing_here")}</div>`}
    ${more ? html`<div class="plan-more" data-plan-more>${more} <button type="button" class="btn small" data-action="planExport">${icon("download", { size: 14 })}${t("plan.export")}</button></div>` : ""}`;
}

const listKey = ({ state: s }) => `${s.group}\n${s.q}\n${s.flib}\n${s.fkind}\n${s.frule}`;

/** The first LIST_LIMIT items of a group (matching `q` and the filters) from GET /plans/{id}/items; redraws the list
when it comes. */
async function loadGroup(ctx, group, key) {
  if (ctx.state.loading === key) return;
  ctx.state.loading = key;
  const { q, flib, fkind, frule } = ctx.state;
  const params = new URLSearchParams({ group, q, library: flib, kind: fkind, rule: frule, limit: String(LIST_LIMIT) });
  let entry;
  try {
    const page = await get(`/plans/${encodeURIComponent(ctx.state.plan.id)}/items?${params}`);
    const why = { lim: t("plan.why.limit"), space: t("plan.why.space") };
    const rs = [...page.actions.map(actionRow), ...page.left.map((x) => leftRow(x, x.why, why[x.why]))];
    for (const r of rs) r.text = `${r.title} ${r.sub} ${r.reason}`.toLowerCase();
    entry = { key, rows: rs, total: page.total };
  } catch {
    entry = { key, rows: [], total: 0, error: true };
  }
  if (ctx.state.loading !== key) return; // another group or filter was asked for meanwhile
  ctx.state.loading = null;
  ctx.state.full[group] = entry;
  const body = document.querySelector("[data-plan-body]");
  if (body && listKey(ctx) === key) setHtml(body, listBody(ctx));
}

const anyChange = (plan) => GROUPS.some((g) => g !== "keep" && g !== "left" && total(plan, g) > 0);
// the group's size in the whole plan: the summary carries only a sample of the items
const total = (plan, g) => plan.totals?.[g] ?? 0;

// --- the Metadata tab: "field: old → new" per item, both directions, filters and search -----------

const metaTotal = (plan) => plan.meta?.total ?? 0;

function hms(s) {
  const n = Math.round(Number(s));
  const h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), sec = String(n % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

function metaValue(field, v) {
  if (v === null || v === undefined || v === "") return "-";
  if (field === "position") return hms(v);
  if (field === "rating") return v === "0" ? "☆0" : "★".repeat(Number(v));
  if (field === "played") return t(`plan.v.${v}`);
  return v;
}

function changeHtml(c) {
  const label = t(`plan.f.${c.field}`);
  const what = c.field === "metadata" ? t("plan.meta.unknown")
    : html`<span class="mono">${metaValue(c.field, c.old)}</span> → <b class="mono">${metaValue(c.field, c.new)}</b>`;
  return html`<span class="mchg ${c.direction}${c.conflict ? " conflict" : ""}" data-change="${c.field}" title="${c.conflict || t(`plan.meta.${c.direction}`)}">
    ${icon(c.direction === "to_library" ? "up" : "down", { size: 12 })}<span>${label}: ${what}</span>${c.conflict ? html`<small>${c.conflict}</small>` : ""}</span>`;
}

function metaRow(a) {
  const r = actionRow(a);
  return { ...r, changes: a.changes, text: `${r.title} ${r.sub}`.toLowerCase() };
}

function metaRowHtml(r, ctx) {
  const { mfield, mdir } = ctx.state;
  const cs = r.changes.filter((c) => (!mfield || c.field === mfield) && (!mdir || c.direction === mdir));
  return html`<div class="prow mrow" data-plan-item data-meta-item>${thumb(r.cover)}
    <span class="prow-text"><span class="prow-title" title="${r.title}">${r.title}</span>${r.sub ? html`<span class="prow-sub" title="${r.sub}">${r.sub}</span>` : ""}</span>
    <span class="mchgs">${cs.map(changeHtml)}</span></div>`;
}

function metaFilters(ctx) {
  const { plan, mfield, mdir } = ctx.state;
  const fields = [...new Set(Object.keys(plan.meta?.changes || {}).map((k) => k.split(":")[1]))];
  const n = (f) => Object.entries(plan.meta?.changes || {}).filter(([k]) => k.endsWith(`:${f}`) && (!mdir || k.startsWith(`${mdir}:`))).reduce((x, [, v]) => x + v, 0);
  return html`<div class="plan-mfilters" data-meta-filters>
    <label><span class="sr">${t("plan.meta.dir_label")}</span><select data-meta-dir>
      ${["", "to_device", "to_library"].map((d) => html`<option value="${d}" ${d === mdir ? "selected" : ""}>${t(d ? `plan.meta.${d}` : "plan.meta.dir_all")}</option>`)}</select></label>
    <label><span class="sr">${t("plan.meta.field_label")}</span><select data-meta-field>
      <option value="">${t("plan.meta.field_all")}</option>
      ${fields.map((f) => html`<option value="${f}" ${f === mfield ? "selected" : ""}>${t(`plan.f.${f}`)} (${n(f).toLocaleString()})</option>`)}</select></label>
    ${plan.meta?.conflicts ? html`<span class="mchg conflict" data-meta-conflicts>${icon("warn", { size: 12 })}<span>${t("plan.meta.conflicts", { n: plan.meta.conflicts })}</span></span>` : ""}</div>`;
}

function metaBody(ctx) {
  const { plan, q, mfield, mdir } = ctx.state;
  const n = metaTotal(plan);
  const sample = (plan.meta?.actions || []).map(metaRow);
  const key = `${q}\n${mfield}\n${mdir}`;
  let shown, count;
  if (n <= sample.length) {
    shown = sample.filter((r) => (!q || r.text.includes(q.toLowerCase()))
      && r.changes.some((c) => (!mfield || c.field === mfield) && (!mdir || c.direction === mdir)));
    count = shown.length;
  } else if (ctx.state.full.meta?.key === key) {
    if (ctx.state.full.meta.error) return html`${metaFilters(ctx)}<div class="plan-empty" data-items-error>${t("plan.items_failed")}</div>`;
    shown = ctx.state.full.meta.rows;
    count = ctx.state.full.meta.total;
  } else {
    loadMeta(ctx, key);
    return html`${metaFilters(ctx)}<div class="plan-empty" data-items-loading>${t("plan.loading_items")}</div>`;
  }
  const more = count > Math.min(shown.length, LIST_LIMIT) ? t("plan.showing", { shown: Math.min(shown.length, LIST_LIMIT), n: count.toLocaleString() }) : "";
  return html`<div class="plan-note" data-group-note>${t("plan.note.meta")}</div>${n ? metaFilters(ctx) : ""}
    ${shown.length ? shown.slice(0, LIST_LIMIT).map((r) => metaRowHtml(r, ctx))
      : html`<div class="plan-empty">${q ? t("plan.no_match", { q }) : n ? t("plan.nothing_here") : t("plan.meta.none")}</div>`}
    ${more ? html`<div class="plan-more" data-plan-more>${more} <button type="button" class="btn small" data-action="planExport">${icon("download", { size: 14 })}${t("plan.export")}</button></div>` : ""}`;
}

async function loadMeta(ctx, key) {
  if (ctx.state.loading === key) return;
  ctx.state.loading = key;
  const { q, mfield, mdir } = ctx.state;
  const params = new URLSearchParams({ group: "meta", q, field: mfield, direction: mdir, limit: String(LIST_LIMIT) });
  let entry;
  try {
    const page = await get(`/plans/${encodeURIComponent(ctx.state.plan.id)}/items?${params}`);
    entry = { key, rows: page.actions.map(metaRow), total: page.total };
  } catch {
    entry = { key, rows: [], total: 0, error: true };
  }
  if (ctx.state.loading !== key) return;
  ctx.state.loading = null;
  ctx.state.full.meta = entry;
  redrawBody(ctx);
}

function redrawBody(ctx) {
  const body = document.querySelector("[data-plan-body]");
  if (body) setHtml(body, ctx.state.group === "meta" ? metaBody(ctx) : listBody(ctx));
}

function listCard(ctx) {
  const { rows: rs, group } = ctx.state;
  return html`<section class="card plan-list" data-plan-list>
    <div class="plan-list-head"><div class="tabs plan-tabs" role="tablist">${GROUPS.map((g) =>
      html`<button type="button" role="tab" class="tab" data-action="planGroup" data-group="${g}" aria-selected="${g === group}">${t(`plan.c.${g}`)} <span class="n">${total(ctx.state.plan, g).toLocaleString()}</span></button>`)}<button type="button" role="tab" class="tab plan-meta-tab" data-action="planGroup" data-group="meta" aria-selected="${group === "meta"}">${icon("tag", { size: 14 })}${t("plan.c.meta")} <span class="n" data-meta-count>${metaTotal(ctx.state.plan).toLocaleString()}</span></button></div>
      <label class="plan-q">${icon("search", { size: 15 })}<input type="search" data-plan-q placeholder="${t("plan.filter")}" aria-label="${t("plan.filter_label")}"></label></div>
    <div data-plan-body>${group === "meta" ? metaBody(ctx) : listBody(ctx)}</div></section>`;
}

function countCards(ctx) {
  const { plan, rows: rs, group } = ctx.state;
  const sum = (k) => plan.bytes?.[k] ?? 0;
  const leftBytes = plan.bytes?.left ?? 0;
  const sub = {
    add: bytes(sum("add")), replace: signed(sum("replace")), update_meta: t("plan.cs.update_meta"),
    remove: signed(sum("remove")), keep: t("plan.cs.keep"), left: total(plan, "left") ? bytes(leftBytes) : t("plan.cs.left"),
  };
  const ico = { add: ["plus", "ok"], replace: ["sync", "in"], update_meta: ["tag", "in"], remove: ["minus", "er"], keep: ["check", "mu"], left: ["filter", "wn"] };
  for (const g of ["add", "replace", "remove"]) if (!total(plan, g)) sub[g] = "-";
  if (plan.others?.count) sub.keep = t("plan.cs.keep_others", { n: plan.others.count.toLocaleString(), size: bytes(plan.others.bytes) });
  return html`<div class="plan-counts">${GROUPS.map((g) =>
    html`<button type="button" class="pcount" data-count="${g}" data-action="planGroup" data-group="${g}" aria-pressed="${g === group}">
      <span class="pc-label"><span class="pc-ico ${ico[g][1]}">${icon(ico[g][0], { size: 14, width: 2.2 })}</span>${t(`plan.c.${g}`)}</span>
      <strong>${total(plan, g).toLocaleString()}</strong><span class="pc-sub">${sub[g]}</span></button>`)}</div>`;
}

// --- space -------------------------------------------------------------------------------------------------------

function bar(label, parts, capacity, text, attrs = "") {
  const pct = (n) => (capacity > 0 ? Math.max(0, Math.min(100, (n / capacity) * 100)) : 0);
  return html`<div class="plan-bar"><span class="pb-label">${label}</span><div class="pb-main">
    <div class="cap-bar" role="img" aria-label="${label}: ${text}">${Object.keys(KIND_VARS).filter((k) => parts[k] > 0).map((k) =>
      html`<span data-kind="${k}" style="width:${pct(parts[k]).toFixed(2)}%;background:var(${KIND_VARS[k]})"></span>`)}</div>
    <span class="pb-text" ${attrs}>${text}</span></div></div>`;
}

/** "Needs X · includes R reserved", or for a plan that frees more than it adds "Frees Y · R stays reserved". */
function neededText(s) {
  const net = s.needed - (s.reserve || 0);
  if (net < 0) return t("plan.frees_text", { freed: bytes(-net) }) + (s.reserve ? ` · ${t("plan.reserve_kept", { reserve: bytes(s.reserve) })}` : "");
  return t("plan.needed_text", { needed: bytes(s.needed) }) + (s.reserve ? ` · ${t("plan.reserve", { reserve: bytes(s.reserve) })}` : "");
}

function spaceCard(plan, sp) {
  const s = plan.space;
  if (s.unknown) { // FTP without a quota
    return html`<section class="card plan-space" data-space-unknown><div class="card-head"><h2>${t("plan.space")}</h2>${badge("neutral", t("plan.not_checked"))}</div>
      <div class="card-body"><p data-needed><b>${neededText(s)}</b></p>
      <div class="muted" style="font-size:12px">${t("plan.space_unknown")}</div></div></section>`;
  }
  const fits = s.fits && !plan.does_not_fit;
  const usedNow = Math.max(0, s.capacity - s.free);
  const freeAfter = s.free - s.needed;
  const usedAfter = Math.max(0, s.capacity - freeAfter);
  const now = { music: 0, audiobook: 0, podcast: 0, video: 0, other: usedNow, ...(sp?.by_kind || {}) };
  if (sp) now.other = Math.max(0, usedNow - now.music - now.audiobook - now.podcast);
  const after = {};
  for (const k of ["music", "audiobook", "podcast", "video"]) after[k] = Math.max(0, now[k] + (plan.by_kind_delta?.[k] || 0));
  after.other = Math.max(0, usedAfter - after.music - after.audiobook - after.podcast); // incl. the reserve
  const kinds = Object.keys(KIND_VARS).filter((k) => now[k] > 0 || after[k] > 0);
  return html`<section class="card plan-space"><div class="card-head"><h2>${t("plan.space")}</h2>
      ${badge(fits ? "ok" : "error", cap(fits ? t("plan.fits") : t("plan.no_fit")))}</div>
    <div class="card-body">
      ${bar(t("plan.now"), now, s.capacity, t("plan.now_text", { used: bytes(usedNow), free: bytes(s.free) }))}
      ${bar(t("plan.after"), after, s.capacity, t("plan.after_text", { used: bytes(usedAfter), free: bytes(Math.max(0, freeAfter)), fit: fits ? t("plan.fits") : t("plan.no_fit") }), "data-free-after")}
      <div class="cap-legend">${kinds.map((k) => html`<span><i style="background:var(${KIND_VARS[k]})"></i>${t(`cap.kind.${k}`)}</span>`)}
        <span><i class="free"></i>${t("cap.free")}</span></div>
      <div class="muted" style="font-size:12px" data-needed>${neededText(s)}</div>
      ${plan.db ? html`<div id="planDb">${dbSizeLine(plan.db.estimated, plan.db.limit, plan.db.level)}
        <div class="muted" style="font-size:12px">${t("plan.db_now", { size: plan.db.current === null ? "-" : mib(plan.db.current) })}</div></div>` : ""}</div></section>`;
}

// --- the playlists this sync writes or removes on the device; unchanged ones are only counted ----
function playlistsCard(plan) {
  const all = plan.playlist_changes || [];
  const changed = all.filter((c) => c.change !== "same");
  if (!all.length) return "";
  const same = all.length - changed.length;
  return html`<div class="card" data-plan-playlists><div class="card-head"><h2>${t("plan.pls")}</h2></div>
    <div class="card-body">${changed.length ? html`<ul class="act-list">${changed.map((c) => html`<li data-pl-change="${c.change}">
      ${icon("playlist", { size: 14 })}<span>${t(`plan.pls.${c.change}`, { name: c.name, n: c.songs })}${c.change === "remove" ? ""
        : html` <span class="muted">· ${t(c.rule ? "plan.pls.from_rule" : c.smart ? "plan.pls.from_smart" : "plan.pls.from_library")}</span>`}</span></li>`)}</ul>` : ""}
      ${same ? html`<p class="muted" data-pl-same>${t("plan.pls.same", { n: same })}</p>` : ""}</div></div>`;
}

// --- playlist conflicts: a choice per playlist, sent with Apply ------------------------------------
function playlistConflicts(plan) {
  const list = plan.playlist_conflicts || [];
  if (!list.length) return "";
  const choices = ["ask", "merge", "replace", "ignore", "rename"];
  return html`<div class="card" data-pl-conflicts><div class="card-head"><h2>${t("plan.pl_conflicts")}</h2></div>
    <div class="card-body"><p class="muted">${t("plan.pl_conflicts_text")}</p>
    <ul class="pl-conflicts">${list.map((c) => html`<li data-pl-conflict="${c.name}">
      <div><b>${c.name}</b><div class="muted">${t(c.kind === "reorder" ? "plan.pl_reorder" : "plan.pl_name_taken")}</div></div>
      <div class="field"><label class="sr" for="plc-${c.name}">${t("plan.pl_choice_label", { name: c.name })}</label><select id="plc-${c.name}" data-pl-choice="${c.name}">
        ${choices.map((x) => html`<option value="${x}">${t(`plan.pl.${x}`)}</option>`)}</select></div></li>`)}</ul></div></div>`;
}

function playlistChoices() {
  const out = {};
  for (const el of document.querySelectorAll("[data-pl-choice]")) if (el.value !== "ask") out[el.dataset.plChoice] = el.value;
  return out;
}

// --- warnings ----------------------------------------------------------------------------------------------------

// the service's confirmation reasons other than the first sync's choice (delete threshold, another registration)
const otherReasons = (plan) => plan.confirmation_reasons.filter((r) => !r.startsWith("first sync"));

/** Gentle write mode's pauses in minutes, and the time without them when the last full sync tells the speed. */
function gentleMinutes(plan, cfg, last) {
  const files = (plan.counts.add || 0) + (plan.counts.replace || 0);
  const g = cfg.sync;
  const pauses = Math.ceil(files / g.gentle_batch_files) * g.gentle_pause_s;
  let base = null;
  const secs = last?.finished_at ? (new Date(last.finished_at) - new Date(last.started_at)) / 1000 : 0;
  if (last?.bytes_written > 10_000_000 && secs > 0) base = (plan.write_bytes / (last.bytes_written / secs));
  const plain = base === null ? null : Math.max(1, Math.round(base / 60));
  return { files, pauses, extra: Math.max(1, Math.round(pauses / 60)), plain, total: base === null ? null : Math.max(1, Math.round((base + pauses) / 60)) };
}

function warnings(ctx, removes, removeOk, reasons) {
  const { plan, cfg, info } = ctx.state;
  const out = [];
  const box = (title, message, attrs, action = "") => html`<div class="plan-warn" ${attrs}>${problemBox({ kind: "warn", title, message, action })}</div>`;
  if (removeOk) out.push(box(t("plan.w.remove", { n: removes }), t("plan.w.remove_text"), html`data-warn="remove"`));
  else if (removes) {
    out.push(html`<div class="callout" data-remove-info>${t("plan.remove_info", { n: removes })}
      <button type="button" class="btn small ghost" data-action="planGroup" data-group="remove">${t("plan.remove_info_show")}</button></div>`);
  }
  for (const r of reasons) out.push(box(t("plan.confirm_reason"), cap(r), html`data-warn="confirm"`));
  if (plan.db && plan.db.level !== "ok") {
    out.push(box(t(plan.db.level === "over" ? "plan.w.db_over" : "plan.w.db_warn"),
      t("plan.w.db_text", { size: mib(plan.db.estimated), limit: mib(plan.db.limit) }), html`data-warn="db"`));
  }
  if (info.health_warning && plan.actions.some((a) => a.kind === "add" || a.kind === "replace")) {
    out.push(box(t("plan.w.health"), t("plan.w.health_text", { warning: info.health_warning }), html`data-warn="health"`));
  }
  const last = info.last_report;
  if (cfg.sync?.gentle_write && !info.network) {
    const m = gentleMinutes(plan, cfg, last);
    if (m.files) {
      const p = { batch: cfg.sync.gentle_batch_files, pause: cfg.sync.gentle_pause_s };
      // pauses under half a minute are said in seconds, not rounded up to "1 min"
      const text = m.pauses < 30 ? t("plan.w.gentle_text_secs", { ...p, s: m.pauses })
        : m.plain === null ? t("plan.w.gentle_text_unknown", { ...p, n: m.extra })
        : t("plan.w.gentle_text", { ...p, n: m.total, m: m.plain });
      out.push(box(t("plan.w.gentle"), text, html`data-warn="gentle"`));
    }
  } else if (last?.suggest_gentle && last.status === "failed" && !info.network) {
    const f = last.failures?.[0];
    const where = f ? `${cap(f.step || "write")} · ${f.item || f.title || f.ref}. ` : "";
    out.push(box(t("plan.w.usb"), `${where}${t("plan.w.usb_text")}`, html`data-warn="usb"`,
      html`<button type="button" class="btn small" data-action="gentleOn">${t("plan.gentle_on")}</button>`));
  }
  if (!cfg.sync?.no_cache && ctx.state.cacheMax && plan.processed_bytes > 2 * ctx.state.cacheMax) {
    out.push(box(t("plan.w.nocache"), t("plan.w.nocache_text", { size: bytes(plan.processed_bytes), max: bytes(ctx.state.cacheMax) }),
      html`data-warn="nocache"`, html`<button type="button" class="btn small" data-action="noCacheOn">${t("plan.nocache_on")}</button>`));
  }
  // the cards below say these already: the device limit, the forced plan's left-out items, does not fit
  const shown = (w) => (plan.limit?.left_out.length && w.includes("left out by the device limit"))
    || w.startsWith("forced sync:") || (plan.does_not_fit && w.startsWith("does not fit:"));
  for (const w of plan.warnings.filter((x) => !shown(x))) out.push(box("", cap(w), html`data-warn="plan"`));
  return out;
}

function planSuggestion(ctx) {
  const sg = ctx.state.planSuggest;
  const cfg = ctx.state.cfg;
  if (!sg?.reason.files || sg.same_as_current || (!sg.draft && (!sg.template_id || sg.template_id === cfg.processor_template_id))) return "";
  const name = sg.template_id ? sg.candidates.find((x) => x.id === sg.template_id)?.name || sg.template_id : "";
  return html`<div class="callout" data-plan-suggest="${sg.template_id || "draft"}"><b>${t("plan.sg_title")}:</b> ${suggestText(sg.reason)}
    <button type="button" class="btn small" style="margin-left:8px" data-action="planUseSuggested">${sg.template_id ? t("plan.sg_use", { name }) : t("plan.sg_use_draft")}</button></div>`;
}

function perRule(plan) {
  if (!plan.space.per_rule.length) return "";
  return html`<div class="table-wrap"><table class="table"><thead><tr><th>${t("plan.rule")}</th><th>${t("plan.items")}</th><th>${t("plan.size")}</th><th>${t("plan.dropped")}</th></tr></thead>
    <tbody>${plan.space.per_rule.map((r) => html`<tr><td>${r.name || r.rule_id}</td><td>${r.items}</td><td>${bytes(r.bytes)}</td>
      <td>${r.dropped_items ? `${r.dropped_items} · ${bytes(r.dropped_bytes)}` : "-"}</td></tr>`)}</tbody></table></div>`;
}

function noFit(plan) {
  return html`<div class="card" data-nofit><div class="card-head"><h2>${t("plan.no_fit_title")}</h2>${badge("error", cap(t("plan.no_fit")))}</div>
    <div class="card-body"><p>${t("plan.no_fit_text")}</p>${perRule(plan)}
      ${plan.space.suggestions.length ? html`<p><b>${t("plan.suggestions")}:</b></p><ul>${plan.space.suggestions.map((x) => html`<li>${x}</li>`)}</ul>` : ""}
      <button class="btn danger" data-action="force">${t("plan.force")}</button></div></div>`;
}

const fitLabel = (x) => `${x.to.toUpperCase()} ${x.bitrate_kbps}k`;

function fitEstimate(est) {
  if (!est) return "";
  return html`${t("plan.fit_estimate", { needed: bytes(est.needed), free: bytes(est.free), n: est.converted })}
    ${badge(est.fits ? "ok" : "warn", est.fits ? t("plan.fit_fits") : t("plan.fit_no"))}`;
}

function fitCard(plan, fit) {
  return html`<div class="card" data-fit data-change="fitTarget"><div class="card-head"><h2>${t("plan.fit_title")}</h2></div>
    <div class="card-body"><p>${t("plan.fit_text")}</p>
      <div class="field" style="max-width:260px"><label for="fitTarget">${t("plan.fit_target")}</label>
        <select id="fitTarget">${plan.fit_targets.map((x, i) => html`<option value="${i}" ${x === fit.target ? "selected" : ""}>${fitLabel(x)}</option>`)}</select></div>
      <p id="fitEstimate" aria-live="polite" style="margin:10px 0">${fitEstimate(fit.est)}</p>
      <button class="btn primary" data-action="fitApply">${t("plan.fit_ok")}</button></div></div>`;
}

function excluded(plan) {
  return html`<div class="card" data-excluded><div class="card-head"><h2>${t("plan.excluded", { n: plan.space.dropped.length })}</h2>${badge("warn", t("plan.dropped"))}</div>
    <div class="card-body"><p class="muted">${t("plan.excluded_text")}</p>${perRule(plan)}
      <button type="button" class="btn small" style="margin-top:10px" data-action="planGroup" data-group="left">${t("plan.show_left")}</button></div></div>`;
}

// what the device limit left out; the items themselves are in the list's Left out group
function limited(plan, ctx) {
  const l = plan.limit;
  const parts = [];
  if (l.limit_tracks !== null) parts.push(t("plan.limit_tracks", { n: l.limit_tracks }));
  if (l.limit_bytes !== null) parts.push(bytes(l.limit_bytes));
  const order = l.fill_order.map((k) => t(`plan.fill.${k}`)).join(", ");
  return html`<div class="card" data-limited><div class="card-head"><h2>${t("plan.limit_title", { n: l.left_out_count ?? l.left_out.length })}</h2>${badge("neutral", t("plan.limit_badge"))}</div>
    <div class="card-body"><p class="muted">${t("plan.limit_text", { limit: parts.join(" / "), order })}</p>
      <p>${t("plan.limit_kept", { n: l.kept_items, size: bytes(l.kept_bytes) })}</p>
      <div class="row-actions" style="display:flex;gap:8px;flex-wrap:wrap">
        <button type="button" class="btn small" data-action="planGroup" data-group="left">${t("plan.show_left")}</button>
        <a class="btn small" href="#/devices/${encodeURIComponent(ctx.params.id)}/rules">${t("plan.limit_edit")}</a></div></div></div>`;
}

function firstSync(plan, stored) {
  const opt = (v, text) => html`<label class="row" style="cursor:pointer;align-items:flex-start"><input type="radio" name="firstSync" value="${v}" ${stored === v ? "checked" : ""}>
    <div class="meta"><b>${t(`plan.first.${v}`)}</b><small>${text}</small></div></label>`;
  return html`<fieldset class="card" style="padding:0;margin:0" data-first-sync><div class="card-head"><legend><h2 style="font-size:15px;margin:0">${t("plan.first")}</h2></legend></div>
    <div class="card-body"><p class="muted" data-first-text>${plan.first_sync_existing ? t("plan.first_text", { n: plan.first_sync_existing }) : t("plan.first_empty")}</p>
      ${opt("keep", t("plan.first.keep_text"))}${opt("adopt", t("plan.first.adopt_text", { n: plan.first_sync_matches }))}${opt("wipe", t("plan.first.wipe_text"))}</div></fieldset>`;
}

function applyBar(ctx, removes) {
  const { plan, cfg, needOk } = ctx.state;
  const label = plan.write_bytes ? t("plan.apply_write", { size: bytes(plan.write_bytes) }) : t("plan.apply");
  return html`<div class="plan-bar-sticky" data-apply-bar>
    ${needOk ? html`<label class="plan-ok"><input type="checkbox" data-plan-ok data-change="planOk">
      <span>${removes ? t("plan.ok_remove", { n: removes, name: cfg.name }) : t("plan.ok_reasons")}</span></label>` : ""}
    <a class="btn" href="#/devices/${encodeURIComponent(cfg.id)}">${t("common.cancel")}</a>
    <button type="button" class="btn primary" data-action="apply" ${plan.does_not_fit || needOk ? "disabled" : ""}>${icon("sync", { size: 16, width: 2.2 })}${label}</button></div>`;
}

function showGroup(ctx, group) {
  ctx.state.group = group;
  for (const el of document.querySelectorAll("[data-action='planGroup'][data-group]")) {
    const on = String(el.dataset.group === group);
    el.setAttribute(el.getAttribute("role") === "tab" ? "aria-selected" : "aria-pressed", on);
    if (on === "true" && el.getAttribute("role") === "tab") { // the phone's strip scrolls the tab into view
      const strip = el.parentElement;
      strip.scrollLeft = Math.max(0, el.offsetLeft - strip.offsetLeft - (strip.clientWidth - el.offsetWidth) / 2);
    }
  }
  redrawBody(ctx);
}

export function mounted(ctx, root) {
  if (root.querySelector("[data-planning]")) { // the elapsed time ticks until the plan's page replaces this one
    const timer = setInterval(() => {
      const el = root.querySelector("[data-plan-elapsed]");
      if (!el || !el.isConnected || location.hash !== ctx.hash) { clearInterval(timer); return; }
      el.textContent = t("plan.elapsed", { time: elapsed(ctx.state.progress) });
    }, 1000);
  }
  const q = root.querySelector("[data-plan-q]");
  q?.addEventListener("input", () => {
    ctx.state.q = q.value.trim();
    redrawBody(ctx);
  });
  root.querySelector("[data-plan-body]")?.addEventListener("change", (e) => { // the Metadata tab's filters
    if (e.target.matches("[data-meta-dir]")) ctx.state.mdir = e.target.value;
    else if (e.target.matches("[data-meta-field]")) ctx.state.mfield = e.target.value;
    else if (e.target.matches("[data-fl-library]")) ctx.state.flib = e.target.value;
    else if (e.target.matches("[data-fl-kind]")) ctx.state.fkind = e.target.value;
    else if (e.target.matches("[data-fl-rule]")) ctx.state.frule = e.target.value;
    else return;
    redrawBody(ctx);
  });
}

export const actions = {
  planGroup(el, ctx) {
    showGroup(ctx, el.dataset.group);
    if (!el.closest("[data-plan-list]") && !el.closest(".plan-counts")) {
      document.querySelector("[data-plan-list]")?.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  },
  planOk(el, ctx) {
    const btn = document.querySelector('[data-apply-bar] [data-action="apply"]');
    if (btn) btn.disabled = ctx.state.plan.does_not_fit || !el.checked;
  },
  planExport(el, ctx) { // every item as CSV, built by the server: the page has only a sample
    const a = document.createElement("a");
    a.href = `/api/v1/plans/${encodeURIComponent(ctx.state.plan.id)}/items?format=csv`;
    a.download = `plan-${ctx.state.cfg.id}.csv`;
    a.click();
  },
  async noCacheOn(el, ctx) {
    const { cfg } = ctx.state;
    el.disabled = true;
    const { config } = await get(`/devices/${encodeURIComponent(cfg.id)}`);
    ctx.state.cfg = await patch(`/devices/${encodeURIComponent(cfg.id)}`, { sync: { ...config.sync, no_cache: true } });
    el.closest("[data-warn]")?.remove();
    toast(t("plan.nocache_done", { name: cfg.name }), "ok");
  },
  async gentleOn(el, ctx) {
    const { cfg } = ctx.state;
    el.disabled = true;
    // keep the device's other sync settings, like the job report
    const set = async (on) => {
      const { config } = await get(`/devices/${encodeURIComponent(cfg.id)}`);
      ctx.state.cfg = await patch(`/devices/${encodeURIComponent(cfg.id)}`, { sync: { ...config.sync, gentle_write: on } });
    };
    await set(true);
    el.closest("[data-warn]")?.remove();
    toast(t("plan.gentle_done", { name: cfg.name }), "ok", {
      undo: async () => {
        await set(false);
        toast(t("plan.gentle_undone", { name: cfg.name }));
      },
    });
  },
  async fitTarget(el, ctx, e) {
    if (e.target.id !== "fitTarget") return;
    const tg = ctx.state.plan.fit_targets[Number(e.target.value)];
    ctx.state.fit = { target: tg, est: null };
    const box = document.getElementById("fitEstimate");
    box.textContent = "…";
    const est = await post(`/devices/${encodeURIComponent(ctx.params.id)}/fit/estimate`, tg);
    if (ctx.state.fit.target !== tg) return; // another target was chosen meanwhile
    ctx.state.fit.est = est;
    box.innerHTML = String(fitEstimate(est));
  },
  async fitApply(el, ctx) {
    const tg = ctx.state.fit.target;
    el.disabled = true;
    try {
      const plan = await post(`/devices/${encodeURIComponent(ctx.params.id)}/fit`, tg);
      toast(t("plan.fit_done", { target: fitLabel(tg) }));
      goFrom(ctx, `/devices/${encodeURIComponent(ctx.params.id)}/plan/${encodeURIComponent(plan.id)}`);
    } finally { el.disabled = false; }
  },
  async planUseSuggested(el, ctx) {
    const sg = ctx.state.planSuggest;
    el.disabled = true;
    const body = sg.template_id ? { processor_template_id: sg.template_id } : { processors: sg.draft.processors, processor_template_id: null };
    await patch(`/devices/${encodeURIComponent(ctx.params.id)}`, body);
    const plan = await post(`/devices/${encodeURIComponent(ctx.params.id)}/plan`, { force: false });
    goFrom(ctx, `/devices/${encodeURIComponent(ctx.params.id)}/plan/${encodeURIComponent(plan.id)}`);
  },
  // planning again shows its progress: the planning screen, replaced by the new plan's page when done
  async replan(el, ctx) {
    el.disabled = true;
    if (!ctx.params.planId) { // the planning screen itself (planning failed): plan again here
      ctx.state.planning = ctx.state.progress = null;
      ctx.rerender();
      return;
    }
    goFrom(ctx, `/devices/${encodeURIComponent(ctx.params.id)}/plan`);
  },
  async force(el, ctx) {
    const ok = await confirm({ title: t("plan.force_title"), message: t("plan.force_text"), ok: t("plan.force_ok"), danger: true });
    if (!ok) return;
    el.disabled = true;
    goFrom(ctx, `/devices/${encodeURIComponent(ctx.params.id)}/plan?force=1`);
  },
  async planCancel(el, ctx) {
    el.disabled = true;
    await del(`/devices/${encodeURIComponent(ctx.params.id)}/plan`);
    toast(t("plan.cancelled"));
    goFrom(ctx, `/devices/${encodeURIComponent(ctx.params.id)}`);
  },
  async apply(el, ctx) {
    const { plan, cfg, needOk } = ctx.state;
    // deletions on the device never happen from one click (handoff: destructive removal): the bar's box is ticked
    if (needOk && !document.querySelector("[data-plan-ok]")?.checked) return;
    let firstSync = null;
    if (plan.first_sync) {
      firstSync = document.querySelector('input[name="firstSync"]:checked')?.value || null;
      if (!firstSync) { toast(t("plan.first_missing"), "error"); return; }
      if (firstSync === "wipe" && !(await confirm({
        title: t("plan.wipe_title", { name: cfg.name }), message: t("plan.wipe_text"), ok: t("plan.wipe_ok"), danger: true,
      }))) return;
    }
    if (plan.space.dropped.length && !(await confirm({
      title: t("plan.forced_title"), message: t("plan.forced_text", { n: plan.space.dropped.length }), ok: t("plan.forced_ok"), danger: true,
    }))) return;
    // over the iTunesDB limit: a confirmation of its own that names the risk
    const overDb = plan.db?.level === "over";
    if (overDb && !(await confirm({
      title: t("plan.db_title"), message: t("plan.db_text", { size: mib(plan.db.estimated), limit: mib(plan.db.limit) }), ok: t("plan.db_ok"), danger: true,
    }))) return;
    await openJob(el, ctx, cfg.id, () => post(`/plans/${encodeURIComponent(plan.id)}/apply`, { confirm: true, first_sync: firstSync, playlist_choices: playlistChoices(), ...(overDb ? { confirm_db_limit: true } : {}) }));
  },
};

// a plan is a snapshot: events do not redraw it (it would lose the first-sync choice); outdated → Apply says so.
// While planning, its progress events redraw the progress card only.
export function onEvent(ev, ctx, root) {
  if (ctx.params.planId || ev.type !== "plan.progress" || ev.data.device_id !== ctx.params.id) return;
  ctx.state.progress = ev.data;
  const box = root.querySelector("[data-planning]");
  if (box) setHtml(box, progressBody(ctx));
}
