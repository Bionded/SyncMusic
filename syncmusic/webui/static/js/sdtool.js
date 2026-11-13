// SPDX-License-Identifier: GPL-3.0-or-later
// compas-sdtool (Compás Tools, GPL-3.0): its credit line and the confirmed, checked download from SyncMusic's
// release - shared by Settings and the HiBy R1 Compás device page
import { html } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { get, post } from "./api.js";
import { confirm, toast } from "./ui.js";

addStrings({
  "sdtool.download": "Download",
  "sdtool.dl_title": "Download compas-sdtool?",
  "sdtool.dl_text": "SyncMusic downloads {asset} from its own release on GitHub, checks its checksum, that it runs on this computer and its version, and keeps it in the data folder.",
  "sdtool.credit": "compas-sdtool is part of Compás Tools by the Compás authors, built from the Compás player's sources; free software under the {license}. SyncMusic runs it as a separate program.",
  "sdtool.source": "Source code",
  "sdtool.done": "compas-sdtool {version} installed",
});

// the source link comes from the server (GET /settings/compas-sdtool): the UI loads nothing external itself
export const sdtoolCredit = (f) => html`<div class="field"><div class="hint" data-sdtool-credit>${t("sdtool.credit", { license: f?.license || "GPL-3.0" })}
  ${f?.source_url ? html`<a href="${f.source_url}" target="_blank" rel="noopener">${t("sdtool.source")}</a>` : ""}</div></div>`;

/** Ask, download as a job and wait for it; the tool's status afterwards, or null when cancelled or failed (toasted). */
export async function downloadSdtool() {
  const info = await get("/settings/compas-sdtool").catch(() => null);
  if (!await confirm({ title: t("sdtool.dl_title"), message: t("sdtool.dl_text", { asset: info?.asset || "compas-sdtool" }),
    body: sdtoolCredit(info), ok: t("sdtool.download") })) return null;
  try {
    let job = await post("/settings/compas-sdtool/download", {});
    for (let i = 0; i < 600 && !job.finished_at; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      job = await get(`/jobs/${encodeURIComponent(job.id)}`);
    }
    if (job.status !== "succeeded") throw new Error(job.error || job.status);
    toast(t("sdtool.done", { version: job.result?.version || "" }), "ok");
    return await get("/settings/compas-sdtool");
  } catch (e) {
    toast(e.message, "error");
    return null;
  }
}
