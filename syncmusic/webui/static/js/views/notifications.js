// The notifications centre: what happened while nobody watched - syncs finished or failed, device
// errors, devices connected, syncs waiting for a confirmation - newest first, unread ones marked. A notification
// with a link opens what it is about (and counts as read); "×" clears one, "Clear all" every one.
// The server keeps the newest 500; the words come from here (i18n), the server stores the event and its data.
import { html, when, bytes } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, post, del } from "../api.js";
import { toast } from "../ui.js";
import { icon } from "../icons.js";
import { go } from "../router.js";
import { setUnread } from "../bell.js";
import "./activity.js"; // its act.n.* words

addStrings({
  "notif.title": "Notifications",
  "notif.update.sent": "{name}: sent {list}",
  "notif.head": "{n} unread",
  "notif.head_none": "All read",
  "notif.kept": " · kept {n} days",
  "notif.kept_one": " · kept {n} day",
  "notif.show": "Show",
  "notif.show.all": "All",
  "notif.show.unread": "Unread",
  "notif.show.problems": "Problems",
  "notif.clear_all": "Clear all",
  "notif.cleared": "Notifications cleared.",
  "notif.clear": "Clear",
  "notif.none_problems": "No problems.",
  "notif.age_now": "now",
  "notif.age_min": "{n} min",
  "notif.age_h": "{n} h",
  "notif.act.history": "Open history",
  "notif.act.report": "Open report",
  "notif.act.register": "Register",
  "notif.act.device": "Open device",
  "notif.act.plan": "Review plan",
  "notif.act.logs": "Open logs",
  "notif.act.settings": "Open settings",
  "notif.none": "No notifications yet. Syncs, device errors and connected devices show up here.",
  "notif.none_unread": "Nothing unread.",
  "notif.new": "New",
  "notif.open": "Open",
  "notif.a_device": "A device",
  "notif.device.connected": "{name} connected",
  "notif.device.unknown": "An unknown device was connected: {name}",
  "notif.device.unknown_text": "Register it to sync it.",
  "notif.device.error": "{name}: device error",
  "notif.device.formatted": "{name} was formatted ({fs})",
  "notif.storage.low": "SyncMusic's disk is almost full",
  "notif.device.health_warning": "{name}: storage may be failing",
  "notif.app.update_available": "SyncMusic {version} is available",
  "notif.app.updated": "SyncMusic was updated to {version}",
  "notif.app.updated_text": "From {previous}. Settings → Updates shows what changed.",
  "notif.app.update_failed": "SyncMusic {version} could not be installed",
  "notif.app.update_failed_text": "Still running {running}. {reason}",
  "notif.app.update_rolled_back": "The new version did not start; the previous one came back.",
  "notif.app.update_available_text": "{n} changes. Settings → Updates shows them.",
  "notif.app.update_available_text_one": "{n} change. Settings → Updates shows it.",
  "notif.storage.low_text": "{free} free. Clear the conversion cache or make room on the disk.",
  "notif.sync.needs_confirmation": "{name}: the sync waits for you",
  "notif.sync.db_size_warning": "{name}: the device database is close to its limit",
  "notif.sync.db_size_over": "{name}: the device database would be over its limit",
  "notif.sync.db_size_text": "{estimated} MiB of {limit} MiB after the sync.",
  "notif.sync.succeeded": "{name}: sync finished",
  "notif.sync.partial": "{name}: sync finished, {n} items failed",
  "notif.sync.partial_one": "{name}: sync finished, 1 item failed",
  "notif.sync.failed": "{name}: sync failed",
  "notif.sync.verify_failed": "{name}: sync failed - the device did not hold what was written",
  "notif.sync.cancelled": "{name}: sync cancelled",
  "notif.reverse.failed": "{name}: listening data was not sent back",
  "notif.reverse.errors": "{name}: some listening data was not sent back",
  "notif.other": "{name}: {event}",
});

export const title = () => t("notif.title");
export const nav = "notifications";

const LEVEL = { info: "ok", warning: "warn", error: "error" };
const mib = (n) => (Number(n || 0) / 1048576).toFixed(1);

/** [title, text] of one notification. */
export function words(n) {
  const d = n.data || {};
  const name = n.device_name || d.model || d.disk || t("notif.a_device");
  switch (n.event) {
    case "device.connected": return [t("notif.device.connected", { name }), ""];
    case "device.unknown": return [t("notif.device.unknown", { name: [d.vendor, d.model].filter(Boolean).join(" ") || d.disk || "?" }), t("notif.device.unknown_text")];
    case "device.error": return [t("notif.device.error", { name }), d.error || ""];
    case "device.formatted": return [t("notif.device.formatted", { name, fs: d.fs || "?" }), ""];
    case "app.update_available": return [t("notif.app.update_available", { version: d.latest || "" }), t("notif.app.update_available_text", { n: d.notes ?? "?" })];
    case "app.updated": return [t("notif.app.updated", { version: d.version || "" }), t("notif.app.updated_text", { previous: d.previous || "?" })];
    case "app.update_failed": return [t("notif.app.update_failed", { version: d.version || "" }), t("notif.app.update_failed_text", { running: d.running || "?", reason: d.reason || t("notif.app.update_rolled_back") })];
    case "device.health_warning": return [t("notif.device.health_warning", { name }), d.warning || ""];
    case "storage.low": return [t("notif.storage.low"), t("notif.storage.low_text", { free: bytes(d.free || 0) })];
    case "sync.needs_confirmation": return [t("notif.sync.needs_confirmation", { name }), d.reason || ""];
    case "sync.db_size_warning":
      return [t(d.level === "over" ? "notif.sync.db_size_over" : "notif.sync.db_size_warning", { name }),
        t("notif.sync.db_size_text", { estimated: mib(d.estimated), limit: Math.round(Number(d.limit || 0) / 1048576) })];
    case "sync.finished":
      if (d.status === "partial") return [t(d.failed === 1 ? "notif.sync.partial_one" : "notif.sync.partial", { name, n: d.failed ?? "?" }), ""];
      if (d.status === "failed") return [t("notif.sync.verify_failed", { name }), ""];
      if (d.status === "cancelled") return [t("notif.sync.cancelled", { name }), ""];
      return [t("notif.sync.succeeded", { name }), d.notice || ""];
    case "sync.failed": return [t("notif.sync.failed", { name }), d.error || ""];
    case "update.sent": { //
      const list = ["plays", "ratings", "hearts", "positions", "finished"].filter((k) => d[k]).map((k) => t(`act.n.${k}`, { n: d[k] })).join(", ");
      return [t("notif.update.sent", { name, list }), ""];
    }
    case "reverse.finished":
      return [t(d.failed ? "notif.reverse.failed" : "notif.reverse.errors", { name }), (d.errors || []).slice(0, 3).join(" · ")];
    default: return [t("notif.other", { name, event: n.event }), ""];
  }
}

export const LEVEL_ICON = { info: "info", warning: "warn", error: "xcircle" };

/** "now", "12 min", "3 h", then the date (as drawn); the full time is the tooltip. */
export function age(iso) {
  const d = new Date(iso), s = (Date.now() - d.getTime()) / 1000;
  if (Number.isNaN(s)) return "";
  if (s < 60) return t("notif.age_now");
  if (s < 3600) return t("notif.age_min", { n: Math.floor(s / 60) });
  if (s < 86400) return t("notif.age_h", { n: Math.floor(s / 3600) });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** The button of a notification, named after what it opens. */
function actionLabel(n) {
  const link = n.link || "";
  if (n.event === "device.unknown") return t("notif.act.register");
  if (link.startsWith("/logs")) return t("notif.act.logs");
  if (link.startsWith("/settings")) return t("notif.act.settings");
  if (/\/sync\/[^/]+$/.test(link) || n.event === "sync.finished") return t("notif.act.report");
  if (n.event === "sync.needs_confirmation") return t("notif.act.plan");
  if (link.includes("/history")) return t("notif.act.history");
  if (link.startsWith("/devices/")) return t("notif.act.device");
  return t("notif.open");
}

function row(n) {
  const [head, text] = words(n);
  const ok = n.event === "sync.finished" && (n.data || {}).status === "succeeded";
  const level = ok ? "ok" : n.level;
  // an unknown drive is opened by the Register screen even without a stored link
  const link = n.link || (n.event === "device.unknown" ? "/devices/register" : "");
  return html`<div class="notif-item${n.read ? "" : " unread"}" data-notification="${n.id}" data-level="${n.level}" data-read="${n.read ? "true" : "false"}">
    <span class="notif-ico" data-tone="${level}">${icon(ok ? "check" : LEVEL_ICON[n.level] || "info", { size: 16 })}</span>
    <div class="notif-body"><div class="notif-top"><b>${head}</b><small class="muted" title="${when(n.ts)}">${age(n.ts)}</small></div>
      ${text ? html`<p class="notif-text">${text}</p>` : ""}
      <div class="notif-acts">${link ? html`<a class="btn small" href="#${link}" data-action="open" data-id="${n.id}" data-link="${link}">${actionLabel({ ...n, link })}</a>` : ""}
        <button type="button" class="iconbtn notif-clear" data-action="clearOne" data-id="${n.id}" title="${t("notif.clear")}" aria-label="${t("notif.clear")}">${icon("x", { size: 14 })}</button></div></div>
  </div>`;
}

const SHOW = ["all", "unread", "problems"];

export async function render(ctx) {
  const show = SHOW.includes(ctx.query.get("show")) ? ctx.query.get("show") : ctx.query.get("unread") === "1" ? "unread" : "all";
  const r = await get("/notifications?limit=500");
  setUnread(r.unread);
  const items = r.items.filter((n) => show === "all" || (show === "unread" ? !n.read : n.level === "warning" || n.level === "error"));
  const empty = !r.items.length ? "notif.none" : show === "unread" ? "notif.none_unread" : "notif.none_problems";
  return html`<div class="pagehead"><div><h1>${t("notif.title")}</h1><div class="sub" data-notif-head>${r.unread ? t("notif.head", { n: r.unread }) : t("notif.head_none")}${r.keep_days ? t("notif.kept", { n: r.keep_days }) : ""}</div></div>
      <div class="actions"><button type="button" class="btn" data-action="clearAll" ${r.items.length ? "" : "disabled"}>${t("notif.clear_all")}</button></div></div>
    <div class="seg notif-seg" role="tablist" aria-label="${t("notif.show")}">${SHOW.map((k) => html`<a class="seg-link" role="tab" aria-checked="${k === show}" aria-selected="${k === show}" href="#/notifications${k === "all" ? "" : `?show=${k}`}" data-notif-show="${k}">${t(`notif.show.${k}`)}</a>`)}</div>
    <div class="card notif-list" data-notifications>${items.length ? items.map(row) : html`<div class="empty">${t(empty)}</div>`}</div>`;
}

export const actions = {
  async open(el, ctx) {
    const id = Number(el.dataset.id);
    const unread = el.closest("[data-notification]")?.dataset.read === "false";
    if (unread) {
      try { setUnread((await post("/notifications/read", { ids: [id] })).unread); } catch { /* still open it */ }
    }
    go(el.dataset.link);
  },
  async clearOne(el, ctx) {
    el.disabled = true;
    setUnread((await del(`/notifications/${Number(el.dataset.id)}`)).unread);
    ctx.rerender();
  },
  async clearAll(el, ctx) {
    el.disabled = true;
    setUnread((await del("/notifications")).unread);
    toast(t("notif.cleared"));
    ctx.rerender();
  },
};

export const refreshOn = ["notification."];
