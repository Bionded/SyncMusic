// Library health (shared): the result of one connection check per library and page load,
// shown as a badge on the Libraries list and as a dot next to each library in the sidebar's Libraries dropdown.
import { post } from "./api.js";
import { errorMessage } from "./ui.js";
import { t } from "./i18n.js";

/** library id -> {ok, text}; no entry: not checked yet. */
export const health = new Map();
const listeners = new Set();
const pending = new Map();

export function onHealth(fn) { listeners.add(fn); return () => listeners.delete(fn); }

/** Check a library now (again); listeners hear about the result. */
export function checkLibrary(id) {
  if (pending.has(id)) return pending.get(id);
  const run = post(`/libraries/${encodeURIComponent(id)}/check`)
    .then((r) => ({ ok: !!r.ok, text: r.message || t("status.failed") }), (e) => ({ ok: false, text: errorMessage(e) }))
    .then((h) => {
      health.set(id, h);
      pending.delete(id);
      listeners.forEach((f) => f(id, h));
      return h;
    });
  pending.set(id, run);
  return run;
}

/** Check the enabled libraries whose health is not known yet. */
export function checkUnknown(libs) {
  for (const l of libs || []) if (l.enabled !== false && !health.has(l.id)) checkLibrary(l.id);
}

/** Forget a library's health (its settings changed): the next view checks again. */
export const forgetHealth = (id) => health.delete(id);
