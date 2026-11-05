// Listening data that did not reach its library: a chip on the device header, the "Not delivered"
// drawer (pending rows with the last error and try, given-up rows with their reason, Clear list) and the sync
// report's line "2 plays and 1 rating waiting for Navidrome". No mockup exists: v2 chip, drawer and list rows.
import { html, when } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, del } from "../api.js";
import { drawer, toast, emptyState } from "../ui.js";
import { icon } from "../icons.js";

addStrings({
  "fb.chip": "{n} not delivered",
  "fb.chip_one": "{n} not delivered",
  "fb.chip_title": "Plays, ratings or positions from this device that have not reached their library yet",
  "fb.title": "Not delivered - {name}",
  "fb.intro": "Plays, ratings and positions from the device that their library has not taken yet. The next reverse sync tries again.",
  "fb.pending": "Waiting",
  "fb.dropped": "Given up",
  "fb.dropped_intro": "The library refused these {n} times (for example, the song is gone), so SyncMusic stopped trying.",
  "fb.dropped_intro_one": "The library refused these {n} time (for example, the song is gone), so SyncMusic stopped trying.",
  "fb.none": "Everything from this device reached its library.",
  "fb.kind.play": "Play",
  "fb.kind.rating": "Rating",
  "fb.kind.position": "Position",
  "fb.value.play": "played {when}",
  "fb.value.rating": "{n} ★",
  "fb.value.position": "at {time}",
  "fb.attempts": "{n} refusals",
  "fb.attempts_one": "{n} refusal",
  "fb.tries": "tried {n} times",
  "fb.tries_one": "tried once",
  "fb.tried": "last try {when}",
  "fb.not_tried": "not tried yet",
  "fb.dropped_at": "given up {when}",
  "fb.clear": "Clear list",
  "fb.cleared": "The list of given-up items is empty.",
  "fb.loading": "Loading…",
  "fb.n.play": "{n} plays",
  "fb.n.play_one": "{n} play",
  "fb.n.rating": "{n} ratings",
  "fb.n.rating_one": "{n} rating",
  "fb.n.position": "{n} positions",
  "fb.n.position_one": "{n} position",
  "fb.waiting_for": "{what} waiting for {library}",
  "fb.and": "and",
  "fb.report_dropped": "{n} given up after repeated refusals",
  "fb.report_dropped_one": "{n} given up after repeated refusals",
});

const KINDS = ["play", "rating", "position"];
const MAX_ATTEMPTS = 10; // core/reverse.py MAX_ATTEMPTS

/** The header chip (only when something waits or was given up); it opens the drawer. */
export function feedbackChip(live) {
  const n = (live?.feedback_pending || 0) + (live?.feedback_dropped || 0);
  if (!n) return "";
  return html`<button type="button" class="dev-chip warn" data-action="feedbackOpen" title="${t("fb.chip_title")}" data-feedback-chip>${icon("warn", { size: 14 })}${t("fb.chip", { n })}</button>`;
}

const clock = (s) => {
  const v = Math.round(Number(s) || 0);
  const h = Math.floor(v / 3600), m = Math.floor((v % 3600) / 60), sec = v % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};

function value(r) {
  if (r.kind === "play") return t("fb.value.play", { when: when(r.value) });
  if (r.kind === "rating") return t("fb.value.rating", { n: r.value ?? "-" });
  return t("fb.value.position", { time: clock(r.value) });
}

function row(r, dropped) {
  const facts = [`${t(`fb.kind.${r.kind}`)} · ${value(r)}`, r.library,
    dropped ? t("fb.dropped_at", { when: when(r.dropped_at) }) : r.tried_at ? t("fb.tried", { when: when(r.tried_at) }) : t("fb.not_tried"),
    !dropped && r.tries ? t("fb.tries", { n: r.tries }) : "", // every failed try, not only refusals
    r.attempts ? t("fb.attempts", { n: r.attempts }) : ""].filter(Boolean).join(" · ");
  return html`<li class="fb-row" data-fb-row="${dropped ? "dropped" : "pending"}">
    <span class="fb-title" title="${r.title}">${r.title}${r.artist ? html` <span class="muted">- ${r.artist}</span>` : ""}</span>
    <span class="fb-facts">${facts}</span>${r.error ? html`<span class="fb-error">${r.error}</span>` : ""}</li>`;
}

function body(data) {
  if (!data.pending.length && !data.dropped.length) return emptyState(t("fb.none"));
  return html`<p class="muted fb-intro">${t("fb.intro")}</p>
    ${data.pending.length ? html`<h3 class="fb-head">${t("fb.pending")} <span class="muted">${data.pending.length}</span></h3>
      <ul class="fb-list">${data.pending.map((r) => row(r, false))}</ul>` : ""}
    ${data.dropped.length ? html`<h3 class="fb-head">${t("fb.dropped")} <span class="muted">${data.dropped.length}</span></h3>
      <p class="muted fb-intro">${t("fb.dropped_intro", { n: MAX_ATTEMPTS })}</p>
      <ul class="fb-list">${data.dropped.map((r) => row(r, true))}</ul>
      <button type="button" class="btn small" data-fb-clear>${t("fb.clear")}</button>` : ""}`;
}

/** The drawer for a device; `onChange` after Clear list. */
export async function openFeedback(cfg, onChange = null) {
  const d = drawer({ title: t("fb.title", { name: cfg.name }), body: html`<p class="muted">${t("fb.loading")}</p>`, label: "feedback" });
  const data = await get(`/devices/${encodeURIComponent(cfg.id)}/feedback`);
  const box = d.root.querySelector(".drawerbody");
  box.innerHTML = String(body(data));
  box.addEventListener("click", async (e) => {
    if (!e.target.closest("[data-fb-clear]")) return;
    await del(`/devices/${encodeURIComponent(cfg.id)}/feedback/dropped`);
    toast(t("fb.cleared"));
    box.innerHTML = String(body({ ...data, dropped: [] }));
    onChange?.();
  });
  return d;
}

/** "2 plays and 1 rating waiting for Navidrome; 1 given up after repeated refusals" from a reverse report, or "". */
export function feedbackLine(reverse, libraryNames = {}) {
  if (!reverse) return "";
  const parts = Object.entries(reverse.pending_by || {}).map(([lib, kinds]) => {
    const what = KINDS.filter((k) => kinds[k]).map((k) => t(`fb.n.${k}`, { n: kinds[k] }));
    const list = what.length > 1 ? `${what.slice(0, -1).join(", ")} ${t("fb.and")} ${what.at(-1)}` : what[0];
    return t("fb.waiting_for", { what: list, library: libraryNames[lib] || lib });
  });
  if (reverse.dropped) parts.push(t("fb.report_dropped", { n: reverse.dropped }));
  return parts.join("; ");
}
