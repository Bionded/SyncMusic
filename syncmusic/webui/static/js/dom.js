// Escaping HTML templates: every interpolated value is escaped unless it is itself an `html` result or `raw()`.
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export class Html {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}

export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
export const raw = (s) => new Html(String(s));

function fmt(v) {
  if (v instanceof Html) return v.s;
  if (Array.isArray(v)) return v.map(fmt).join("");
  if (v === null || v === undefined || v === false) return "";
  return esc(v);
}

export function html(strings, ...values) {
  let out = strings[0];
  values.forEach((v, i) => { out += fmt(v) + strings[i + 1]; });
  return new Html(out);
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function setHtml(el, tpl) { el.innerHTML = fmt(tpl); }

// "1.2 GB" style sizes (decimal, like the device makers)
export function bytes(n) {
  if (n === null || n === undefined) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n), i = 0;
  while (Math.abs(v) >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function when(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}
