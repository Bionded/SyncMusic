// Sync windows editor: rows of weekdays + from/to times, "Add window", ✕ per row. Used by Settings →
// Sync defaults (`default_sync_windows`) and the device's Settings tab (`sync_windows`). No rows = any time.
import { html } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { icon } from "./icons.js";

addStrings({
  "sw.title": "Automatic syncs only in these windows",
  "sw.sub": "On-connect, update and daily syncs start only inside a window (this computer's clock; a window may run past midnight). Outside, a change waits for the next window. Sync now always works. No windows: any time.",
  "sw.add": "Add window",
  "sw.remove": "Remove this window",
  "sw.from": "From",
  "sw.to": "to",
  "sw.days": "Days",
  "sw.day.0": "Mo", "sw.day.1": "Tu", "sw.day.2": "We", "sw.day.3": "Th", "sw.day.4": "Fr", "sw.day.5": "Sa", "sw.day.6": "Su",
  "sw.any": "Any time",
  "sw.waiting": "Waiting for the sync window - starts {when}",
});

const ALL = [0, 1, 2, 3, 4, 5, 6];

function row(w) {
  return html`<div class="sw-row" data-sw-row>
    <span class="sw-days" role="group" aria-label="${t("sw.days")}">${ALL.map((d) => html`<label class="sw-day"><input type="checkbox" value="${d}" ${w.days.includes(d) ? "checked" : ""}><span>${t(`sw.day.${d}`)}</span></label>`)}</span>
    <span class="ds-num">${t("sw.from")} <input type="time" class="ds-time" data-sw-start value="${w.start}" aria-label="${t("sw.from")}">
      ${t("sw.to")} <input type="time" class="ds-time" data-sw-end value="${w.end}" aria-label="${t("sw.to")}"></span>
    <button type="button" class="btn small ghost" data-sw-del title="${t("sw.remove")}" aria-label="${t("sw.remove")}">${icon("x", { size: 14 })}</button></div>`;
}

/** The editor; `id` names it for readWindows and tests. */
export function windowsEditor(id, windows = []) {
  return html`<div class="sw" data-sw="${id}">
    <span class="ds-label">${t("sw.title")}<small>${t("sw.sub")}</small></span>
    <div class="sw-rows" data-sw-rows>${windows.map(row)}</div>
    <button type="button" class="btn small" data-sw-add>${icon("plus", { size: 14 })}${t("sw.add")}</button></div>`;
}

/** Add / remove rows inside `root` (one listener for every editor in it); a change event tells the Save bar. */
export function bindWindows(root) {
  root.addEventListener("click", (e) => {
    const add = e.target.closest("[data-sw-add]"), del = e.target.closest("[data-sw-del]");
    if (!add && !del) return;
    const editor = (add || del).closest("[data-sw]");
    if (add) editor.querySelector("[data-sw-rows]").insertAdjacentHTML("beforeend", String(row({ start: "01:00", end: "06:00", days: ALL })));
    else del.closest("[data-sw-row]").remove();
    editor.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

/** The windows of editor `id` (rows without a day are left out). */
export function readWindows(root, id) {
  return [...root.querySelectorAll(`[data-sw="${id}"] [data-sw-row]`)].map((r) => ({
    start: r.querySelector("[data-sw-start]").value || "01:00",
    end: r.querySelector("[data-sw-end]").value || "06:00",
    days: [...r.querySelectorAll(".sw-day input:checked")].map((x) => Number(x.value)),
  })).filter((w) => w.days.length);
}

/** "Waiting for the sync window - starts Tue 02:00" for a device's `window_wait` (ISO), or "". */
export function windowWaitText(iso) {
  if (!iso) return "";
  const at = new Date(iso);
  const same = at.toDateString() === new Date().toDateString();
  const when = at.toLocaleString([], same ? { hour: "2-digit", minute: "2-digit" } : { weekday: "short", hour: "2-digit", minute: "2-digit" });
  return t("sw.waiting", { when });
}
