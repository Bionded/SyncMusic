// Read-only folder picker: browse a local path, an FTP server or an SMB share and pick a
// folder - a library root (Add library: local folder, ftp://, smb://), a network device's target folder (Register,
// device settings) or a flash drive's base folder on the mounted stick. It opens as a dialog: breadcrumb, Up, the
// folders (open one), a preview (folders and media files in the first two levels), New folder on device targets (a
// name only: the sync creates it), Select / Cancel. `pickerButton(kind)` renders the button; the view spreads
// `pickerActions` into its actions. kinds: "lib-local", "lib-remote", "dev-net", "dev-flash" (with `mount`).
import { html, setHtml } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { post } from "./api.js";
import { dialog, errorBox } from "./ui.js";
import { icon } from "./icons.js";

addStrings({
  "pick.browse": "Browse…",
  "pick.loading": "Reading the folder…",
  "pick.up": "Up one level",
  "pick.use": "Use this folder",
  "pick.title": "Choose a folder",
  "pick.shares": "Shares on this server - open one",
  "pick.empty": "No subfolders here.",
  "pick.here": "Here: {folders} · {media}",
  "pick.below": "First two levels: {folders} · {media}",
  "pick.at_least": "at least ",
  "pick.folders_one": "{n} folder",
  "pick.folders": "{n} folders",
  "pick.media_one": "{n} media file",
  "pick.media": "{n} media files",
  "pick.need_host": "Enter the server address first.",
  "pick.share_first": "Open a share first - the folder goes inside it.",
  "pick.crumbs": "Folder path",
  "pick.root": "Root",
  "pick.new": "New folder",
  "pick.new_name": "Name of the new folder",
  "pick.new_ok": "Add",
  "pick.new_bad": "A folder name without / and not . or ..",
  "pick.new_note": "New folder - created by the first sync.",
});

export function pickerButton(kind, { mount = "" } = {}) {
  return html`<div class="picker-wrap" data-pick-kind="${kind}"><button type="button" class="btn small picker-btn" data-action="pickOpen" data-pick="${kind}" aria-label="${t("pick.browse")}" title="${t("pick.browse")}" ${mount ? html`data-mount="${mount}"` : ""}>${icon("folder", { size: 15 })}<span class="picker-btn-label">${t("pick.browse")}</span></button>
    <p class="hint warn" data-pick-error hidden></p></div>`;
}

// the field each kind fills: the button goes at its right end
const TARGET = {
  "lib-local": '[id$="-src-root"]',
  "lib-remote": "#libAddress",
  "dev-net": '[id$="-n-base_path"], [id$="-n-remote_path"]',
  "dev-flash": '[id$="-base_folder"]',
  "dev-mtp": "#regMtpFolder",
};

/** Put the Browse button inline after its field (one row: field flex 1, button auto; it wraps below when the
 * field would get too narrow); its error note goes under the row. Every view's pickers, wherever rendered. */
function inline(wrap) {
  const input = formOf(wrap).querySelector(TARGET[wrap.dataset.pickKind]);
  if (!input || input.parentElement.classList.contains("picker-row")) return;
  const row = document.createElement("div");
  row.className = "picker-row";
  input.before(row);
  row.append(input, wrap.querySelector(".picker-btn"));
  row.after(wrap.querySelector("[data-pick-error]"));
  wrap.remove();
}

new MutationObserver(() => document.querySelectorAll(".picker-wrap[data-pick-kind]").forEach(inline))
  .observe(document.documentElement, { childList: true, subtree: true });

const val = (root, sel) => root.querySelector(sel)?.value?.trim() || "";
const join = (base, name) => `${base === "/" ? "" : base.replace(/\/$/, "")}/${name}`;

/** What to browse: the form's current server and login, the starting path (throws a message to show). */
function request(root, kind, mount) {
  if (kind === "lib-local") return { source: "local", path: val(root, '[id$="-src-root"]') || "/" };
  if (kind === "dev-flash") {
    const base = val(root, '[id$="-base_folder"]').replace(/^\/+|\/+$/g, "");
    return { source: "local", path: base ? join(mount, base) : mount, root: mount };
  }
  if (kind === "dev-mtp") {
    const storage = val(root, "#regMtpStorage");
    return { source: "mtp", disk: root.dataset.disk, storage: storage || null, path: `/${val(root, "#regMtpFolder").replace(/^[./]+/, "")}` };
  }
  if (kind === "lib-remote") {
    let url = null;
    try { url = new URL(val(root, "#libAddress")); } catch { url = null; }
    if (!url || !url.hostname) throw new Error(t("pick.need_host"));
    const guest = root.querySelector("#libGuest")?.checked;
    return {
      source: url.protocol === "smb:" ? "smb" : "ftp", path: decodeURIComponent(url.pathname || "/") || "/",
      host: url.hostname, port: url.port ? Number(url.port) : null, tls: url.protocol === "ftps:",
      username: guest ? "" : val(root, '[id$="-src-username"]'), password: guest ? "" : root.querySelector('[id$="-src-password"]')?.value || "",
    };
  }
  const smb = !!root.querySelector('[id$="-n-remote_path"]');
  const host = val(root, '[id$="-n-host"]');
  if (!host) throw new Error(t("pick.need_host"));
  const port = Number(val(root, '[id$="-n-port"]')) || null;
  return {
    source: smb ? "smb" : "ftp", host, port, tls: !!root.querySelector('[id$="-n-tls"]')?.checked,
    path: val(root, smb ? '[id$="-n-remote_path"]' : '[id$="-n-base_path"]') || "/",
    username: val(root, '[id$="-n-username"]'), password: root.querySelector('[id$="-n-password"]')?.value || "",
  };
}

/** Write the chosen folder back into the form. */
function apply(root, kind, req, path) {
  const set = (sel, v) => {
    const field = root.querySelector(sel);
    if (!field) return;
    field.value = v;
    field.dispatchEvent(new Event("input", { bubbles: true })); // the unsaved bar follows it
  };
  if (kind === "lib-local") return set('[id$="-src-root"]', path);
  if (kind === "dev-flash") return set('[id$="-base_folder"]', path === req.root ? "" : path.slice(req.root.replace(/\/$/, "").length + 1));
  if (kind === "dev-mtp") return set("#regMtpFolder", path === "/" ? "./" : path.slice(1)); // relative to the storage
  if (kind === "lib-remote") {
    const url = new URL(val(root, "#libAddress"));
    const enc = path.split("/").map(encodeURIComponent).join("/");
    return set("#libAddress", `${url.protocol}//${url.host}${enc.endsWith("/") ? enc : `${enc}/`}`);
  }
  return set(req.source === "smb" ? '[id$="-n-remote_path"]' : '[id$="-n-base_path"]', path);
}

const count = (n, key, partial) => (partial ? t("pick.at_least") : "") + t(n === 1 ? `${key}_one` : key, { n });

/** The path as clickable parts; a flash drive's start at its mount point (shown as Root). */
function crumbs(path, top) {
  const out = [{ label: top === "/" ? "/" : t("pick.root"), path: top }];
  const rest = path.slice(top === "/" ? 1 : top.length).split("/").filter(Boolean);
  let at = top;
  for (const part of rest) { at = join(at, part); out.push({ label: part, path: at }); }
  return html`<nav class="picker-crumbs" aria-label="${t("pick.crumbs")}">${out.map((c, i) => i === out.length - 1
    ? html`<span aria-current="location">${c.label}</span>`
    : html`<button type="button" class="linkbtn" data-pick-go="${c.path}">${c.label}</button><span aria-hidden="true">›</span>`)}</nav>`;
}

function view(st, lst) {
  const folderRows = lst.folders.map((f) => html`<button type="button" class="found-row" data-pick-into="${f}">${icon(lst.shares ? "smb" : "folder", { size: 16 })}<b>${f}</b></button>`);
  const canNew = st.writable && !lst.shares && !lst.virtual;
  setHtml(st.panel, html`<div class="picker-path"><button type="button" class="btn small" data-pick-up ${lst.parent === null || lst.path === st.top ? "disabled" : ""} aria-label="${t("pick.up")}" title="${t("pick.up")}">${icon("up", { size: 15 })}</button>
      ${crumbs(lst.path, st.top)}<code class="sr" data-pick-path>${lst.path}</code></div>
    ${lst.shares ? html`<p class="hint">${t("pick.shares")}</p>`
      : lst.virtual ? html`<p class="hint" data-pick-preview>${t("pick.new_note")}</p>`
      : html`<p class="hint" data-pick-preview>${t("pick.here", { folders: count(lst.folders.length, "pick.folders", false), media: count(lst.media_files, "pick.media", false) })}<br>
      ${t("pick.below", { folders: count(lst.preview_folders, "pick.folders", lst.partial), media: count(lst.preview_media, "pick.media", lst.partial) })}</p>`}
    <div class="found-list picker-list">${folderRows.length ? folderRows : html`<p class="hint">${lst.virtual ? "" : t("pick.empty")}</p>`}</div>
    ${canNew ? html`<form class="picker-new" data-pick-new><label class="sr" for="pickNewName">${t("pick.new_name")}</label>
      <input class="rb-input" id="pickNewName" placeholder="${t("pick.new_name")}" autocomplete="off">
      <button type="submit" class="btn small">${icon("plus", { size: 15 })}${t("pick.new")}</button></form>` : ""}
    ${lst.shares ? html`<p class="hint">${t("pick.share_first")}</p>` : ""}`);
  const use = st.back.querySelector('.modalfoot [data-v="1"]');
  if (use) use.disabled = !!lst.shares;
}

async function load(st, path) {
  setHtml(st.panel, html`<p class="hint" role="status">${t("pick.loading")}</p>`);
  try {
    const lst = await post("/browse", { ...st.req, path });
    st.path = lst.path;
    st.parent = lst.parent ?? null;
    view(st, lst);
  } catch (e) {
    setHtml(st.panel, html`${errorBox(e, "")}`);
  }
}

const formOf = (el) => el.closest("[data-pick-form]") || el.closest("form") || el.closest("#regNetwork") || document;

export const pickerActions = {
  async pickOpen(el) {
    const kind = el.dataset.pick;
    const form = formOf(el);
    const note = el.closest(".picker-wrap")?.querySelector("[data-pick-error]") || el.closest(".picker-row").nextElementSibling;
    let req;
    try {
      req = request(form, kind, el.dataset.mount || "");
    } catch (e) {
      note.textContent = e.message;
      note.hidden = false;
      return;
    }
    note.hidden = true;
    const { root, ...browse } = req;
    const st = { req: browse, top: root || "/", writable: kind.startsWith("dev-"), path: req.path, parent: null };
    const shown = dialog({
      title: t("pick.title"),
      body: html`<div class="picker" data-picker data-pick="${kind}"></div>`,
      buttons: [{ label: t("common.cancel"), value: null }, { label: t("pick.use"), value: "use", kind: "primary" }],
    });
    st.back = [...document.querySelectorAll("#overlay .modal-back")].pop();
    st.panel = st.back.querySelector("[data-picker]");
    st.panel.addEventListener("click", (e) => {
      const into = e.target.closest("[data-pick-into]");
      const go = e.target.closest("[data-pick-go]");
      if (into) load(st, join(st.path, into.dataset.pickInto));
      else if (go) load(st, go.dataset.pickGo);
      else if (e.target.closest("[data-pick-up]")) load(st, st.parent || "/");
    });
    st.panel.addEventListener("submit", (e) => { // New folder: a name under the current one, created by the sync
      e.preventDefault();
      const name = st.panel.querySelector("#pickNewName").value.trim();
      if (!name || name.includes("/") || name === "." || name === "..") {
        st.panel.querySelector("#pickNewName").setAttribute("aria-invalid", "true");
        st.panel.querySelector("#pickNewName").title = t("pick.new_bad");
        return;
      }
      st.parent = st.path;
      st.path = join(st.path, name);
      view(st, { path: st.path, parent: st.parent, folders: [], virtual: true });
    });
    load(st, req.path);
    if ((await shown) === "use" && st.path) apply(form, kind, req, st.path);
  },
};
