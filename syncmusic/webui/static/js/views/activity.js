// Activity: what SyncMusic did, newest first, across devices - listening data sent to the libraries,
// playlists written to or removed from a device, device playlists saved to a library, files added and removed.
// The server derives the entries from the sync reports (GET /activity); the words come from here (i18n).
// `activityText` is shared with the sync report and the device History.
import { html, when } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get } from "../api.js";
import { icon } from "../icons.js";
import { age } from "./notifications.js";

addStrings({
  "act.title": "Activity",
  "act.sub": "What SyncMusic did on your devices and libraries, newest first.",
  "act.device": "Device",
  "act.all_devices": "All devices",
  "act.show": "Show",
  "act.kind.all": "All",
  "act.kind.sent": "Sent back",
  "act.kind.playlists": "Playlists",
  "act.kind.files": "Files",
  "act.none": "Nothing yet. Syncs and updates list what they did here.",
  "act.open": "Open report",
  "act.sync.full": "Full sync",
  "act.sync.update": "Update",
  "act.sync.reverse": "Reverse sync",
  "act.sent": "Sent {list} to {library}",
  "act.n.plays": "{n} plays",
  "act.n.plays_one": "{n} play",
  "act.n.ratings": "{n} ratings",
  "act.n.ratings_one": "{n} rating",
  "act.n.hearts": "{n} hearts",
  "act.n.hearts_one": "{n} heart",
  "act.n.positions": "{n} positions",
  "act.n.positions_one": "{n} position",
  "act.n.finished": "{n} finished",
  "act.n.finished_one": "{n} finished",
  "act.n.add": "added {n} files",
  "act.n.add_one": "added {n} file",
  "act.n.replace": "replaced {n} files",
  "act.n.replace_one": "replaced {n} file",
  "act.n.remove": "removed {n} files",
  "act.n.remove_one": "removed {n} file",
  "act.n.update_meta": "updated the tags of {n} files",
  "act.n.update_meta_one": "updated the tags of {n} file",
  "act.files": "On {device}: {list}",
  "act.playlist_written": "Wrote playlist “{name}” ({n} songs) to {device}",
  "act.playlist_written_one": "Wrote playlist “{name}” ({n} song) to {device}",
  "act.playlist_removed": "Removed playlist “{name}” from {device}",
  "act.playlist_to_library": "Saved the device's changes to playlist “{name}” in {library} ({n} songs)",
  "act.playlist_to_library_one": "Saved the device's changes to playlist “{name}” in {library} ({n} song)",
  "act.playlist_new_in_library": "Saved playlist “{name}”, made on the device, to {library}",
  "act.the_device": "the device",
  "act.the_library": "the library",
  "act.failed": "{n} items failed",
  "act.failed_one": "{n} item failed",
});

const SENT = ["plays", "ratings", "hearts", "positions", "finished"];
const FILES = ["add", "replace", "remove", "update_meta"];
// what went from the device to the libraries (the Sent filter here and History's "Sent back")
export const SENT_KINDS = ["sent", "playlist_to_library", "playlist_new_in_library"];
const KINDS = { all: null, sent: SENT_KINDS, playlists: ["playlist_written", "playlist_removed", "playlist_to_library", "playlist_new_in_library"], files: ["files", "failed"] };
const ICON = { sent: "upload", playlist_written: "playlist", playlist_removed: "playlist", playlist_to_library: "upload",
  playlist_new_in_library: "upload", files: "sync", failed: "xcircle" };

const list = (keys, counts) => keys.filter((k) => counts[k]).map((k) => t(`act.n.${k}`, { n: counts[k] })).join(", ");

/** One entry in words. */
export function activityText(a) {
  const c = a.counts || {};
  // where it went: the device's or the libraries' names the service gives
  const device = a.target || t("act.the_device"), library = a.target || t("act.the_library");
  switch (a.kind) {
    case "sent": return t("act.sent", { list: list(SENT, c), library });
    case "files": return t("act.files", { list: list(FILES, c), device });
    case "playlist_written": return t("act.playlist_written", { name: a.name, n: c.songs || 0, device });
    case "playlist_to_library": return t("act.playlist_to_library", { name: a.name, n: c.songs || 0, library });
    case "playlist_removed": return t("act.playlist_removed", { name: a.name, device });
    case "playlist_new_in_library": return t("act.playlist_new_in_library", { name: a.name, library });
    case "failed": return t("act.failed", { n: c.items || 0 });
    default: return a.kind;
  }
}

/** The entries as a list (the report and History show the same). */
export function activityList(items) {
  return html`<ul class="act-list" data-activity-list>${items.map((a) => html`<li data-act="${a.kind}" data-tone="${a.kind === "failed" ? "error" : ""}">
    ${icon(ICON[a.kind] || "info", { size: 14 })}<span>${activityText(a)}</span></li>`)}</ul>`;
}

/** The page's address with a kind filter and a device. */
function link(show, device) {
  const p = new URLSearchParams();
  if (show !== "all") p.set("show", show);
  if (device) p.set("device", device);
  return `#/activity${p.size ? `?${p}` : ""}`;
}

export const title = () => t("act.title");
export const nav = "activity";

export async function render(ctx) {
  const show = ctx.query.get("show") in KINDS ? ctx.query.get("show") : "all";
  const device = ctx.query.get("device") || "";
  const [items, devices] = await Promise.all([get(`/activity?limit=500${device ? `&device=${encodeURIComponent(device)}` : ""}`),
    get("/devices").then((d) => d.registered).catch(() => [])]);
  const names = Object.fromEntries(devices.map((d) => [d.id, d.name]));
  const shown = items.filter((a) => !KINDS[show] || KINDS[show].includes(a.kind));
  return html`<div class="pagehead"><div><h1>${t("act.title")}</h1><div class="sub">${t("act.sub")}</div></div></div>
    <div class="act-filters"><div class="seg" role="tablist" aria-label="${t("act.show")}">${Object.keys(KINDS).map((k) =>
      html`<a class="seg-link" role="tab" aria-selected="${k === show}" aria-checked="${k === show}" href="${link(k, device)}" data-act-show="${k}">${t(`act.kind.${k}`)}</a>`)}</div>
      <select class="set-select" data-change="actDevice" aria-label="${t("act.device")}"><option value="">${t("act.all_devices")}</option>
        ${devices.map((d) => html`<option value="${d.id}" ${d.id === device ? "selected" : ""}>${d.name}</option>`)}</select></div>
    <div class="card act-card" data-activity>${shown.length ? shown.map((a) => {
      const href = a.job_id ? `#/devices/${encodeURIComponent(a.device_id)}/sync/${encodeURIComponent(a.job_id)}` : `#/devices/${encodeURIComponent(a.device_id)}/history`;
      return html`<div class="act-row" data-act="${a.kind}" data-tone="${a.kind === "failed" ? "error" : ""}">
        <span class="act-ico">${icon(ICON[a.kind] || "info", { size: 16 })}</span>
        <div class="act-body"><div>${activityText(a)}</div>
          <small class="muted">${names[a.device_id] || a.device_id} · ${t(`act.sync.${a.sync_kind}`)} · <span title="${when(a.at)}">${age(a.at)}</span></small></div>
        <a class="btn small ghost" href="${href}">${t("act.open")}</a></div>`;
    }) : html`<div class="empty">${t("act.none")}</div>`}</div>`;
}

export const actions = {
  actDevice(el, ctx) {
    location.hash = link(ctx.query.get("show") in KINDS ? ctx.query.get("show") : "all", el.value);
  },
};
