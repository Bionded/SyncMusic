// SPDX-License-Identifier: GPL-3.0-or-later
// Server updates on every open page: `app.update_progress` events fill the progress line of Settings → Updates;
// when the server restarts into the new version every page shows an overlay, waits for the server to answer
// again and reloads itself, then says which version runs now (or that the update was rolled back).
import { session } from "./storage.js";
import { html, setHtml } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { get, onEvent } from "./api.js";
import { toast } from "./ui.js";

addStrings({
  "upd.downloading": "Downloading {version}… {pct}",
  "upd.downloading_bytes": "{done} of {total} MB, {speed} MB/s",
  "upd.installing": "Checking and installing {version}…",
  "upd.restarting": "Restarting SyncMusic…",
  "upd.failed": "The update to {version} failed: {error}",
  "upd.overlay_title": "Restarting SyncMusic",
  "upd.overlay_body": "Updating to {version}. The page reloads by itself when SyncMusic is back.",
  "upd.overlay_slow": "This takes longer than usual. If the new version does not start, SyncMusic goes back to the previous one by itself.",
  "upd.done": "Updated to {version}.",
  "upd.rolled_back": "The update to {version} did not start: SyncMusic went back to {running}. Details in the logs.",
});

const KEY = "syncmusic.update";
const MB = 1 << 20;
let started = null;

/** The progress text of an install step (`progress` from GET /updates or an event). */
export function progressText(p) {
  if (!p) return "";
  if (p.step === "downloading") {
    const pct = p.total ? `${Math.floor((100 * p.done) / p.total)} %` : "";
    const secs = started ? (Date.now() - started) / 1000 : 0;
    const bytes = p.total ? t("upd.downloading_bytes", {
      done: (p.done / MB).toFixed(1), total: (p.total / MB).toFixed(1), speed: secs > 0 ? (p.done / MB / secs).toFixed(1) : "–",
    }) : "";
    return [t("upd.downloading", { version: p.version, pct }), bytes].filter(Boolean).join(" · ");
  }
  if (p.step === "installing") return t("upd.installing", { version: p.version });
  if (p.step === "restarting") return t("upd.restarting");
  if (p.step === "failed") return t("upd.failed", { version: p.version, error: p.error });
  return "";
}

function drawProgress(p) {
  document.querySelectorAll("[data-update-progress]").forEach((el) => {
    el.dataset.step = p.step;
    setHtml(el, html`<small>${progressText(p)}</small>`);
  });
}

async function waitAndReload(version, previous) {
  session.set(KEY, JSON.stringify({ version, previous }));
  let box = document.getElementById("updateOverlay");
  if (!box) {
    box = document.createElement("div");
    box.id = "updateOverlay";
    box.className = "update-overlay";
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-live", "polite");
    document.body.append(box);
  }
  setHtml(box, html`<div class="update-card"><span class="spinner" aria-hidden="true"></span><h2>${t("upd.overlay_title")}</h2>
    <p>${t("upd.overlay_body", { version })}</p><p class="muted" data-update-slow hidden>${t("upd.overlay_slow")}</p></div>`);
  const t0 = Date.now();
  let wentAway = false;
  for (;;) {
    await new Promise((ok) => setTimeout(ok, 2000));
    if (Date.now() - t0 > 120000) box.querySelector("[data-update-slow]").hidden = false;
    const v = await get("/updates").catch(() => null);
    if (!v) { wentAway = true; continue; }
    // back once it answers with another version, or answers again after it was gone (a rollback)
    if ((v.running && v.running !== previous) || wentAway) { location.reload(); return; }
  }
}

/** After the reload: say what happened to the update. */
async function reportAfterReload() {
  const raw = session.get(KEY);
  if (!raw) return;
  session.remove(KEY);
  const { version, previous } = JSON.parse(raw);
  const v = await get("/updates").catch(() => null);
  if (!v) return;
  if (v.running === previous) toast(t("upd.rolled_back", { version, running: previous }), "error");
  else toast(t("upd.done", { version: v.running }), "ok");
}

export function initUpdateOverlay() {
  onEvent((ev) => {
    if (ev.type !== "app.update_progress") return;
    const p = ev.data;
    if (p.step === "downloading" && (!started || p.done === 0)) started = Date.now();
    drawProgress(p);
    if (p.step === "restarting") waitAndReload(p.version, p.previous);
  });
  reportAfterReload();
}
