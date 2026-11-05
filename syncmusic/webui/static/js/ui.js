// Shared components: status badges, page heads, toasts (with Undo), dialogs (confirm,
// typed-name confirm), the right-hand drawer, the sticky Save / Discard bar with its leave guard, and the data
// states - skeleton loading, empty state (sentence + one action), inline problem box (error/warning, one action),
// the outlined suggestion box, the indeterminate bar for short waits and the capacity bar split by media kind.
import { html, esc, raw, $, setHtml, bytes } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { icon } from "./icons.js";

addStrings({
  "common.undo": "Undo",
  "common.discard": "Discard",
  "unsaved.text": "Unsaved changes",
  "unsaved.leave_title": "Leave without saving?",
  "unsaved.leave_text": "Your changes on this screen are not saved yet.",
  "unsaved.leave_ok": "Discard changes",
  "cap.label": "{used} used of {capacity}",
  "cap.free": "Free",
  "cap.kind.music": "Music",
  "cap.kind.audiobook": "Audiobooks",
  "cap.kind.video": "Video",
  "cap.kind.podcast": "Podcasts",
  "cap.kind.video": "Video",
  "cap.kind.other": "Other",
});

// state → [css class, icon]; the text always says the state too, colour is never the only signal
const STATES = {
  ok: ["ok", "ccheck"],
  busy: ["busy", "sync"],
  warn: ["warn", "warn"],
  error: ["error", "xcircle"],
  info: ["info", "info"],
  neutral: ["neutral", "circle"],
};

/** A status badge; `glyph` replaces the state's icon (the design's badges: plug for connected, clock for waiting …). */
export function badge(state, text, glyph = null) {
  const [cls, name] = STATES[state] || STATES.neutral;
  return html`<span class="status ${cls}">${icon(glyph || name, { size: 14, width: 2, spin: (glyph || name) === "sync", cls: "si" })}${text}</span>`;
}

// the update line: state ok / running / failed / paused / off → icon
const UPD_ICONS = { ok: "ccheck", running: "sync", failed: "xcircle", paused: "clock", off: "circle" };

/** One line about a device's automatic update; `compact` for the sidebar cards (12 px), `wrap` lets a long line
 * wrap instead of ending in "…" (the device page's header). */
export function updateLine(state, text, { compact = false, wrap = false, title = "" } = {}) {
  return html`<span class="updline${compact ? " compact" : ""}${wrap ? " wrap" : ""}" data-state="${state}" data-update-line title="${title || text}"><span class="ui">${icon(UPD_ICONS[state] || "dot", { size: compact ? 13 : 14, width: 2 })}</span><span class="ut">${text}</span></span>`;
}

export function pageHead(title, sub = "", actions = "") {
  return html`<div class="pagehead"><div><h1>${title}</h1>${sub ? html`<p>${sub}</p>` : ""}</div>
    ${actions ? html`<div class="actions">${actions}</div>` : ""}</div>`;
}

const TOAST_ICONS = { ok: "ccheck", error: "xcircle", warn: "warn", info: "info" };

/** A short message bottom right. `kind`: info | ok | warn | error. `undo`: a callback - the toast gets an Undo button
 * and stays 8 s; Undo runs it once and closes the toast. Returns the toast element. */
export function toast(message, kind = "info", { undo = null } = {}) {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  setHtml(el, html`<span class="toast-icon">${icon(TOAST_ICONS[kind] || "info", { size: 17 })}</span><span class="toast-msg"></span>
    ${undo ? html`<button type="button" class="toast-undo" data-toast-undo>${t("common.undo")}</button>` : ""}`);
  el.querySelector(".toast-msg").textContent = message;
  box.append(el);
  const timer = setTimeout(() => el.remove(), kind === "error" || undo ? 8000 : 4000);
  if (undo) {
    el.querySelector("[data-toast-undo]").addEventListener("click", async () => {
      clearTimeout(timer);
      el.remove();
      try { await undo(); } catch (err) { toast(errorMessage(err), "error"); }
    }, { once: true });
  }
  return el;
}

export function errorMessage(err) {
  if (err?.code === "network") return t("error.network");
  return err?.message || String(err);
}

/** The inline problem box: an icon, a plain sentence (optional bold title) and at most one action (html). `kind`:
 * error | warn. */
export function problemBox({ kind = "error", title = "", message = "", action = "" }) {
  return html`<div class="problem ${kind === "warn" ? "warn" : "error"}${kind === "warn" ? "" : " errorbox"}" role="${kind === "warn" ? "status" : "alert"}" data-problem="${kind}">
    ${icon(kind === "warn" ? "warn" : "xcircle")}
    <div class="msg">${title ? html`<b>${title}</b><br>` : ""}${message}</div>
    ${action ? html`<div class="problem-action">${action}</div>` : ""}</div>`;
}

// An error panel with an optional recovery action: `data-action="retry"` is handled by the caller's actions.
export function errorBox(err, retryLabel = t("common.retry")) {
  return problemBox({
    title: t("error.title"), message: errorMessage(err),
    action: retryLabel ? html`<button class="btn small" data-action="retry">${retryLabel}</button>` : "",
  });
}

/** An outlined suggestion: an icon, a sentence and one action button (no fill - a suggestion, not a warning). */
export function suggestionBox({ iconName = "info", message, action = "", attrs = "" }) {
  return html`<div class="suggestion" ${attrs}>${icon(iconName)}<div class="msg">${message}</div>
    ${action ? html`<div class="problem-action">${action}</div>` : ""}</div>`;
}

/** The empty state: one sentence (optional bold title) and at most one action. */
export function emptyState(message, { title = "", action = "" } = {}) {
  return html`<div class="empty-state" data-empty>${title ? html`<b>${title}</b>` : ""}<span>${message}</span>${action ? html`<div>${action}</div>` : ""}</div>`;
}

/** Skeleton loading blocks (`lines` grey bars) - read as "Loading…" by screen readers. */
export function skeleton(lines = 3) {
  const widths = [60, 85, 72, 48, 90, 66];
  return html`<div class="skeleton" role="status" aria-busy="true" aria-label="${t("common.loading")}" data-skeleton>
    ${Array.from({ length: lines }, (_, i) => html`<span style="width:${widths[i % widths.length]}%"></span>`)}</div>`;
}

/** The thin neutral bar for short waits whose end is unknown (setting the clock, mounting). */
export function indetBar(label) {
  return html`<span class="bar indet" role="progressbar" aria-label="${label}"><i></i></span>`;
}

const KINDS = ["music", "audiobook", "podcast", "video", "other"];
const KIND_VARS = { music: "--k-mu", audiobook: "--k-ab", podcast: "--k-po", video: "--k-vi", other: "--k-ot" };

/** Capacity split by media kind: `parts` {kind: bytes}; free space hatched; a legend in words (never colour alone);
 * `after` (html) ends the legend's row on the right (the device Overview's Sync). */
export function capacityBar({ capacity, parts = {}, label = "", after = "" }) {
  const used = KINDS.reduce((n, k) => n + (parts[k] || 0), 0);
  const free = Math.max(0, capacity - used);
  const pct = (n) => (capacity > 0 ? Math.max(0, Math.min(100, (n / capacity) * 100)) : 0);
  const shown = KINDS.filter((k) => parts[k] > 0);
  const text = label || t("cap.label", { used: bytes(used), capacity: bytes(capacity) });
  return html`<div class="capacity" data-capacity>
    <div class="cap-bar" role="img" aria-label="${text}">${shown.map((k) => html`<span data-kind="${k}" style="width:${pct(parts[k]).toFixed(2)}%;background:var(${KIND_VARS[k]})"></span>`)}</div>
    <div class="cap-legend">${shown.map((k) => html`<span><i style="background:var(${KIND_VARS[k]})"></i>${t(`cap.kind.${k}`)} ${bytes(parts[k])}</span>`)}
      <span><i class="free"></i>${t("cap.free")} ${bytes(free)}</span>${after ? html`<span class="cap-after">${after}</span>` : ""}</div></div>`;
}

// --- dialogs and drawers ---------------------------------------------------------------------------------------

let openCount = 0;
const FOCUSABLE = "button, input, select, textarea, a[href]";

// Esc closes, Tab cycles inside `box`; returns the function that removes the handler
function trapKeys(box, onEscape) {
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onEscape(); return; }
    if (e.key === "Tab") {
      const f = [...box.querySelectorAll(FOCUSABLE)].filter((x) => !x.disabled && x.offsetParent !== null);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  };
  document.addEventListener("keydown", onKey, true);
  return () => document.removeEventListener("keydown", onKey, true);
}

/** Show a modal. `body` is an html template; `buttons` [{label, value, kind}]; resolves with the clicked value
 * (null on Escape / close / backdrop). `onButton(value, root)` may return false to keep the dialog open.
 * `tone: "danger"` puts the warning icon in a red tile next to the title (destructive actions). */
export function dialog({ title, message = "", body = "", buttons = [], onButton = null, initialFocus = null, tone = "" }) {
  return new Promise((resolve) => {
    const overlay = $("#overlay");
    const back = document.createElement("div");
    back.className = "modal-back";
    const id = `dlg${++openCount}`;
    setHtml(back, html`<div class="modal${tone ? ` ${tone}` : ""}" role="dialog" aria-modal="true" aria-labelledby="${id}">
      <div class="modalhead">${tone === "danger" ? html`<span class="modal-tone">${icon("warn")}</span>` : ""}
        <div class="modal-titles"><h2 id="${id}">${title}</h2>${message ? html`<p>${message}</p>` : ""}</div>
        <button class="close" data-v="__close" aria-label="${t("common.close")}">${icon("x")}</button></div>
      ${body ? html`<div class="modalbody">${body}</div>` : ""}
      <div class="modalfoot">${buttons.map((b, i) => html`<button class="btn ${b.kind || ""}" data-v="${i}">${b.label}</button>`)}</div>
    </div>`);
    const previous = document.activeElement;
    overlay.append(back);
    let untrap = null;
    const finish = (value) => {
      back.remove();
      untrap();
      if (previous && previous.focus) previous.focus();
      resolve(value);
    };
    untrap = trapKeys(back, () => finish(null));
    back.addEventListener("click", async (e) => {
      if (e.target === back) { finish(null); return; }
      const btn = e.target.closest("[data-v]");
      if (!btn) return;
      if (btn.dataset.v === "__close") { finish(null); return; }
      const b = buttons[Number(btn.dataset.v)];
      if (onButton) {
        const keep = (await onButton(b.value, back)) === false;
        if (keep) return;
      }
      finish(b.value);
    });
    const focus = (initialFocus && back.querySelector(initialFocus)) || back.querySelector(".modalfoot .btn:not(.danger)") || back.querySelector("button");
    focus?.focus();
  });
}

/** Yes/no confirmation; danger styles the OK button as destructive. Resolves true only on OK. */
export async function confirm({ title, message, ok = t("common.confirm"), cancel = t("common.cancel"), danger = false, body = "" }) {
  const v = await dialog({
    title, message, body, tone: danger ? "danger" : "",
    buttons: [{ label: cancel, value: false }, { label: ok, value: true, kind: danger ? "danger solid" : "primary" }],
  });
  return v === true;
}

/** Destructive confirmation that needs the exact name typed (format, wipe). Resolves true only when it matches; the
 * button looks disabled (data-ready="false", described by the "Type … to confirm" label) until the text matches
 * exactly; a click before that does nothing but say so (not aria-disabled: the click must still reach it).
 * `capture(root)` reads the rest of the dialog (e.g. the chosen file system) when it is confirmed. */
export async function typedConfirm({ title, message, name, ok, body = "", capture = null }) {
  const label = esc(t("confirm.typed", { name: "\u0000" })).replace("\u0000", `<b class="mono">${esc(name)}</b>`);
  const input = html`${body}<div class="field" style="margin-top:12px"><label for="typedName" id="typedLabel">${raw(label)}</label>
    <input id="typedName" class="mono" autocomplete="off" spellcheck="false"></div>`;
  let root = null;
  const sync = () => {
    const btn = root?.querySelector('.modalfoot [data-v="1"]');
    if (!btn) return;
    btn.dataset.ready = root.querySelector("#typedName").value === name ? "true" : "false";
    btn.setAttribute("aria-describedby", "typedLabel");
  };
  const v = dialog({
    title, message, body: input, initialFocus: "#typedName", tone: "danger",
    buttons: [{ label: t("common.cancel"), value: false }, { label: ok, value: true, kind: "danger solid" }],
    onButton: (value, r) => {
      if (!value) return true;
      if (r.querySelector("#typedName").value !== name) {
        toast(t("confirm.typed_mismatch"), "error");
        return false;
      }
      if (capture) capture(r);
      return true;
    },
  });
  root = $("#overlay .modal-back:last-child");
  root?.querySelector("#typedName").addEventListener("input", sync);
  sync();
  return (await v) === true;
}

/** A panel from the right edge (notifications, details): Esc, the backdrop and × close it, focus stays inside and
 * returns where it was. `body` is an html template. Returns {root, close, closed} - `closed` resolves on close. */
export function drawer({ title, body = "", label = "" }) {
  const overlay = $("#overlay");
  const back = document.createElement("div");
  back.className = "drawer-back";
  const id = `drw${++openCount}`;
  setHtml(back, html`<aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="${id}" ${label ? html`data-drawer="${label}"` : ""}>
    <div class="drawerhead"><h2 id="${id}">${title}</h2>
      <button class="close" data-drawer-close aria-label="${t("common.close")}">${icon("x")}</button></div>
    <div class="drawerbody">${body}</div></aside>`);
  const previous = document.activeElement;
  overlay.append(back);
  let done;
  const closed = new Promise((r) => { done = r; });
  let untrap = null;
  const close = () => {
    if (!back.isConnected) return;
    back.remove();
    untrap();
    if (previous && previous.focus) previous.focus();
    done();
  };
  untrap = trapKeys(back, close);
  back.addEventListener("click", (e) => {
    if (e.target === back || e.target.closest("[data-drawer-close]")) close();
  });
  (back.querySelector(".drawerbody " + FOCUSABLE) || back.querySelector("[data-drawer-close]")).focus();
  return { root: back.querySelector(".drawer"), close, closed };
}

// --- unsaved changes ------------------------------------------------------------------------------------------

let leaveGuard = null; // the one screen with unsaved changes: {dirty(), discard()}
// the page's room for the floating bar goes with the bar (a screen change drops a hidden or dirty bar's form)
const syncSavebarRoom = () => document.body.classList.toggle("has-savebar", !!document.querySelector(".savebar:not([hidden])"));
window.addEventListener("hashchange", () => setTimeout(syncSavebarRoom, 0));

/** The sticky Save / Discard bar for a form: it appears on the first change inside `form`; Save calls `save()`
 * (the bar goes once it resolves), Discard calls `discard()` (usually a re-render). While dirty, leaving the screen
 * asks first (`confirmLeave`, called by the router) and closing the tab warns. Returns {clear, dirty}. */
export function unsavedBar(form, { save, discard }) {
  let dirty = false;
  const bar = document.createElement("div");
  bar.className = "savebar";
  bar.setAttribute("role", "region");
  bar.setAttribute("aria-label", t("unsaved.text"));
  bar.hidden = true;
  setHtml(bar, html`<span class="savebar-text">${icon("info", { size: 16 })}${t("unsaved.text")}</span>
    <button type="button" class="btn ghost" data-savebar="discard">${t("common.discard")}</button>
    <button type="button" class="btn primary" data-savebar="save">${t("common.save")}</button>`);
  form.after(bar);
  const set = (v) => {
    dirty = v;
    bar.hidden = !v;
    syncSavebarRoom();
    leaveGuard = v ? guard : leaveGuard === guard ? null : leaveGuard;
  };
  const guard = { dirty: () => dirty && form.isConnected, discard: () => set(false) };
  const onChange = (e) => { if (!dirty && !e.target.closest?.("[data-nosave]")) set(true); }; // search, backup, import
  form.addEventListener("input", onChange);
  form.addEventListener("change", onChange);
  bar.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-savebar]");
    if (!b) return;
    if (b.dataset.savebar === "discard") { set(false); await discard?.(); return; }
    b.disabled = true;
    try {
      if ((await save()) !== false) set(false);
    } catch (err) {
      toast(errorMessage(err), "error");
    } finally {
      b.disabled = false;
    }
  });
  return { clear: () => set(false), dirty: () => dirty };
}

/** A screen with its own unsaved-changes bar (the template editor) shares the leave guard: `dirty()` says
 * whether something is unsaved, `discard()` runs when the user leaves without saving. */
export function guardLeave(dirty, discard = () => {}) {
  leaveGuard = { dirty, discard };
}

/** Asked before the screen changes: true when there is nothing unsaved or the user chose to discard it. */
export async function confirmLeave() {
  if (!leaveGuard || !leaveGuard.dirty()) { leaveGuard = null; return true; }
  const ok = await confirm({ title: t("unsaved.leave_title"), message: t("unsaved.leave_text"), ok: t("unsaved.leave_ok"), danger: true });
  if (ok) { leaveGuard.discard(); leaveGuard = null; }
  return ok;
}

window.addEventListener("beforeunload", (e) => {
  if (leaveGuard && leaveGuard.dirty()) e.preventDefault();
});

export { esc };
