// The notifications bell in the top bar: the number of unread notifications, kept current by the
// `notification.*` events (and asked again after a reconnect). It opens a drawer with the
// newest ones; opening one marks it read and goes to its subject; "Clear all" deletes them;
// "All notifications" opens the centre.
import { html, $, setHtml, when } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { get, post, del, onEvent, onLiveState } from "./api.js";
import { icon } from "./icons.js";
import { go } from "./router.js";
import { drawer } from "./ui.js";
import { words, age, LEVEL_ICON } from "./views/notifications.js";

addStrings({
  "bell.label": "Notifications",
  "bell.unread": "Notifications, {n} unread",
  "bell.all": "All notifications",
  "bell.none": "No notifications yet.",
  "bell.unread_n": "{n} unread",
  "bell.clear_all": "Clear all",
});

const DRAWER_ITEMS = 8;

let unread = 0;
const listeners = new Set(); // the sidebar's Notifications item shows the same count
export const unreadCount = () => unread;
export function onUnread(fn) { listeners.add(fn); }

function draw() {
  const el = $("#bell");
  const label = unread ? t("bell.unread", { n: unread }) : t("bell.label");
  el.setAttribute("aria-label", label);
  el.title = label;
  el.dataset.unread = String(unread);
  setHtml(el, html`${icon("bell")}${unread ? html`<span class="bell-count" data-bell-count>${unread > 99 ? "99+" : unread}</span>` : ""}`);
}

export function setUnread(n) {
  unread = Math.max(0, n | 0);
  draw();
  for (const fn of listeners) fn(unread);
}

let asking = null;
export async function refreshBell() {
  const my = (asking = {});
  try {
    const r = await get("/notifications?limit=1");
    if (my === asking) setUnread(r.unread);
  } catch { /* the next event or reconnect asks again */ }
}

function drawerItem(n) {
  const [head, text] = words(n);
  const ok = n.event === "sync.finished" && (n.data || {}).status === "succeeded";
  const link = n.link || (n.event === "device.unknown" ? "/devices/register" : "/notifications");
  return html`<button type="button" class="drawer-item${n.read ? "" : " unread"}" data-drawer-item="${n.id}" data-link="${link}" data-read="${n.read ? "true" : "false"}">
    <span class="notif-ico" data-tone="${ok ? "ok" : n.level}">${icon(ok ? "check" : LEVEL_ICON[n.level] || "info", { size: 15 })}</span>
    <span class="drawer-what"><b>${head}</b>${text ? html`<small>${text}</small>` : ""}<small class="muted" title="${when(n.ts)}">${age(n.ts)}</small></span>
    ${n.read ? "" : html`<span class="drawer-dot" aria-label="${t("notif.new")}"></span>`}</button>`;
}

let open = null; // the drawer on screen ({close, root})

function closeDrawer() {
  open?.close();
}

async function openDrawer() {
  if (open) { closeDrawer(); return; }
  open = drawer({
    title: t("bell.label"), label: "bell",
    body: html`<div class="drawer-head"><span class="drawer-count muted" data-drawer-count></span>
        <button type="button" class="btn small" data-drawer-clear-all disabled>${t("bell.clear_all")}</button></div><div class="drawer-list" data-drawer-list><div class="muted drawer-empty">…</div></div>
      <a class="drawer-all" href="#/notifications" data-drawer-all>${t("bell.all")}</a>`,
  });
  const { root, closed } = open;
  root.id = "bellDrawer";
  $("#bell").setAttribute("aria-expanded", "true");
  closed.then(() => { open = null; $("#bell").setAttribute("aria-expanded", "false"); });
  root.addEventListener("click", async (e) => {
    if (e.target.closest("[data-drawer-all]")) { closeDrawer(); return; }
    if (e.target.closest("[data-drawer-clear-all]")) { //
      try { show(await del("/notifications")); } catch { /* the centre still has the button */ }
      return;
    }
    const item = e.target.closest("[data-drawer-item]");
    if (!item) return;
    // the drawer's own history entry is replaced by the target screen: a delayed history.back() of a plain close
    // would race the navigation and undo it on a slow page
    open?.close(true);
    if (item.dataset.read === "false") {
      try { setUnread((await post("/notifications/read", { ids: [Number(item.dataset.drawerItem)] })).unread); } catch { /* still open it */ }
    }
    const target = `#${item.dataset.link}`;
    if (location.hash === target) go(item.dataset.link);
    else location.replace(target);
  });
  async function show(read) {
    const r = await get(`/notifications?limit=${DRAWER_ITEMS}`);
    setUnread(read ? read.unread : r.unread);
    if (!root.isConnected) return;
    root.querySelector("[data-drawer-count]").textContent = r.unread ? t("bell.unread_n", { n: r.unread }) : "";
    root.querySelector("[data-drawer-clear-all]").disabled = !r.items.length;
    setHtml(root.querySelector("[data-drawer-list]"), r.items.length ? html`${r.items.map(drawerItem)}` : html`<div class="muted drawer-empty">${t("bell.none")}</div>`);
  }
  try {
    await show();
  } catch (e) {
    if (root.isConnected) setHtml(root.querySelector("[data-drawer-list]"), html`<div class="muted drawer-empty">${e.message || String(e)}</div>`);
  }
}

export function initBell() {
  draw();
  const bell = $("#bell");
  bell.setAttribute("aria-haspopup", "dialog");
  bell.setAttribute("aria-expanded", "false");
  bell.addEventListener("click", (e) => { e.preventDefault(); openDrawer(); });
  window.addEventListener("hashchange", closeDrawer);
  refreshBell();
  onEvent((ev) => {
    if (ev.type === "notification.created") setUnread(unread + 1);
    else if (ev.type.startsWith("notification.")) refreshBell();
  });
  let was = null;
  onLiveState((up) => { if (up && was === false) refreshBell(); was = up; });
}
