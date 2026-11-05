// Processor templates: reusable processor chains. A
// list on the left (name, Built-in / Own, the device type and how many devices are linked) and the selected template's
// editor on the right: name, "Made for", the device Processors tab's chain editor, Delete and Save. Built-ins (each
// device type's suggested chain) are read-only with "Copy to edit"; a copy is an unsaved draft until Save;
// deleting a linked template leaves the devices their chain. `#/processor-templates` shows the first template (on
// the phone only the list), `/<id>` one template (on the phone only the editor, with a link back).
import { html } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, post, patch, del } from "../api.js";
import { badge, pageHead, toast, confirm, dialog } from "../ui.js";
import { goFrom } from "../router.js";
import { chainEditor, processorActions } from "./processors.js";
import { icon } from "../icons.js";

addStrings({
  "ptpl.sub": "Reusable chains for how files are written: convert, covers, tags, lyrics.",
  "ptpl.new": "New processor template",
  "ptpl.list": "Processor templates",
  "ptpl.any": "Any device",
  "ptpl.used_by": "Linked to {names}. They follow every change.",
  "ptpl.used_n": "{n} devices",
  "ptpl.used_n_one": "{n} device",
  "ptpl.unused": "Not used",
  "ptpl.builtin": "Built-in",
  "ptpl.own": "Own",
  "ptpl.draft": "New · not saved",
  "ptpl.copy": "Copy to edit",
  "ptpl.delete": "Delete",
  "ptpl.create_title": "Create a processor template",
  "ptpl.name": "Name",
  "ptpl.type": "Made for",
  "ptpl.readonly": "Built-in templates can’t be changed. Copy one to make your own.",
  "ptpl.save": "Save",
  "ptpl.saved": "Processor template {name} saved.",
  "ptpl.saved_linked": "Processor template {name} saved. Linked devices use it now: {names}.",
  "ptpl.name_missing": "Give the template a name.",
  "ptpl.delete_title": "Delete {name}?",
  "ptpl.delete_text": "No device uses it. The template is removed.",
  "ptpl.delete_used_title": "{name} is linked",
  "ptpl.delete_used_text": "It is linked to {names}. They keep its steps as their own. Delete it?",
  "ptpl.detach_delete": "Detach and delete",
  "ptpl.deleted": "Processor template {name} deleted.",
});

export const nav = "proctemplates";
export const title = (ctx) => (ctx.path === "/processor-templates" ? t("nav.proctemplates") : ctx.state.ptpl?.name || t("nav.proctemplates"));

const LIST = "/processor-templates";
const blank = { id: "", name: "", device_type: null, processors: [], builtin: false, used_by: [] };

export async function render(ctx) {
  const isList = ctx.path === LIST, isNew = ctx.path === `${LIST}/new`;
  // Copy opens an unsaved draft: nothing exists until Save
  const from = isNew ? ctx.query.get("copy") : null;
  const [tpls, devTypes] = await Promise.all([get("/processor-templates"), get("/device-types")]);
  let tpl;
  if (isList) tpl = tpls[0] || blank;
  else if (isNew) tpl = from ? ctx.state.draft || { ...blank, ...(await post(`/processor-templates/${encodeURIComponent(from)}/copy`, {})), id: "" } : blank;
  else tpl = await get(`/processor-templates/${encodeURIComponent(ctx.params.id)}`);
  if (from) ctx.state.draft = tpl;
  ctx.state.ptpl = tpl;
  if (!ctx.state.procRows) ctx.state.procRows = structuredClone(tpl.processors);
  ctx.state.procSave = save;
  const devName = Object.fromEntries(devTypes.map((d) => [d.type, d.display_name]));
  const suggested = devTypes.find((d) => d.type === tpl.device_type)?.suggested_processors || [];
  const editor = await chainEditor(ctx, { readOnly: tpl.builtin, suggested, noSave: true });
  const names = tpl.used_by.map((d) => d.name).join(", ");
  return html`${pageHead(t("nav.proctemplates"), t("ptpl.sub"), html`<a class="btn primary" href="#${LIST}/new">${icon("plus", { size: 16 })}${t("ptpl.new")}</a>`)}
    <div class="ptpl-split" ${isList ? "data-list-route" : ""}>
      <nav class="ptpl-list" aria-label="${t("ptpl.list")}">${tpls.map((x) => item(x, x.id === tpl.id, devName))}
        ${isNew ? html`<span class="ptpl-item" aria-current="page" data-ptpl-draft><span class="ptpl-top"><b>${tpl.name || t("ptpl.create_title")}</b>${badge("info", t("ptpl.draft"), "edit")}</span></span>` : ""}</nav>
      <div class="ptpl-edit" data-ptpl-editor="${tpl.id}">
        <a class="ptpl-back" href="#${LIST}">${icon("left", { size: 16 })}${t("ptpl.list")}</a>
        <section class="ptpl-head">
          <div class="field"><label for="ptplName">${t("ptpl.name")} *</label>
            <input id="ptplName" value="${tpl.name}" ${tpl.builtin ? "readonly" : ""} autocomplete="off" placeholder="${t("ptpl.create_title")}"></div>
          <div class="field"><label for="ptplType">${t("ptpl.type")}</label>
            <select id="ptplType" ${tpl.builtin ? "disabled" : ""}><option value="">${t("ptpl.any")}</option>
              ${devTypes.map((d) => html`<option value="${d.type}" ${d.type === tpl.device_type ? "selected" : ""}>${d.display_name}</option>`)}</select></div>
          ${tpl.builtin ? html`<button class="btn" data-action="ptplCopy" data-id="${tpl.id}">${icon("copy", { size: 15 })}${t("ptpl.copy")}</button>` : ""}
        </section>
        ${tpl.builtin ? html`<p class="ptpl-note muted" data-ptpl-readonly>${t("ptpl.readonly")}</p>` : ""}
        ${names ? html`<p class="ptpl-note muted" data-ptpl-used>${t("ptpl.used_by", { names })}</p>` : ""}
        ${editor}
        ${tpl.builtin ? "" : html`<div class="ptpl-foot">
          ${tpl.id ? html`<button class="btn ghost danger ptpl-del" data-action="ptplRemove" data-id="${tpl.id}" data-name="${tpl.name}">${t("ptpl.delete")}</button>` : ""}
          <button class="btn primary" data-action="procSave">${t("ptpl.save")}</button></div>`}
      </div>
    </div>`;
}

function item(x, on, devName) {
  const n = x.used_by.length;
  const used = n ? t("ptpl.used_n", { n }) : t("ptpl.unused");
  return html`<a class="ptpl-item" href="#${LIST}/${encodeURIComponent(x.id)}" data-ptpl="${x.id}" aria-current="${on ? "page" : "false"}">
    <span class="ptpl-top"><b>${x.name}</b>${x.builtin ? badge("neutral", t("ptpl.builtin")) : badge("info", t("ptpl.own"), "edit")}</span>
    <small title="${x.used_by.map((d) => d.name).join(", ")}">${x.device_type ? devName[x.device_type] || x.device_type : t("ptpl.any")} · ${used}</small></a>`;
}

export function mounted(ctx, root) {
  root.querySelector('.ptpl-item[aria-current="page"]')?.scrollIntoView({ block: "nearest" });
}

async function save(ctx, rows) {
  const name = document.getElementById("ptplName").value.trim();
  if (!name) { toast(t("ptpl.name_missing"), "error"); return; }
  const body = { name, device_type: document.getElementById("ptplType").value || null, processors: rows };
  const tpl = ctx.state.ptpl.id
    ? await patch(`/processor-templates/${encodeURIComponent(ctx.state.ptpl.id)}`, body)
    : await post("/processor-templates", body);
  toast(tpl.used_by.length ? t("ptpl.saved_linked", { name: tpl.name, names: tpl.used_by.map((d) => d.name).join(", ") }) : t("ptpl.saved", { name: tpl.name }));
  const path = `${LIST}/${encodeURIComponent(tpl.id)}`;
  if (location.hash === `#${path}`) { // saved where it is: show the saved state
    ctx.state.procRows = null;
    ctx.rerender();
  } else goFrom(ctx, path);
}

export const actions = {
  ...processorActions,
  ptplCopy(el, ctx) {
    goFrom(ctx, `${LIST}/new?${new URLSearchParams({ copy: el.dataset.id })}`);
  },
  async ptplRemove(el, ctx) {
    const id = el.dataset.id, name = el.dataset.name;
    const tpl = await get(`/processor-templates/${encodeURIComponent(id)}`);
    if (tpl.used_by.length) {
      const names = tpl.used_by.map((d) => d.name).join(", ");
      const v = await dialog({
        title: t("ptpl.delete_used_title", { name }), message: t("ptpl.delete_used_text", { names }),
        buttons: [{ label: t("common.cancel"), value: false }, { label: t("ptpl.detach_delete"), value: true, kind: "danger solid" }],
      });
      if (v !== true) return;
      await del(`/processor-templates/${encodeURIComponent(id)}?detach=true`);
    } else {
      if (!(await confirm({ title: t("ptpl.delete_title", { name }), message: t("ptpl.delete_text"), ok: t("ptpl.delete"), danger: true }))) return;
      await del(`/processor-templates/${encodeURIComponent(id)}`);
    }
    toast(t("ptpl.deleted", { name }));
    goFrom(ctx, LIST);
  },
};

export function onEvent() {} // an editor never redraws under the user
