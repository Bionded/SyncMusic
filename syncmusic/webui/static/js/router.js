// Hash router: "#/devices/ipod/rules" → the first route whose pattern matches, with its params.
// A view module exports `render(ctx)` → html template (async allowed) and optionally `actions` (data-action
// handlers, called with (element, ctx, event)), `refreshOn` (event type prefixes that re-render it),
// `onEvent(ev, ctx)` (custom live updates), `title(ctx)` and `nav` (the main destination it belongs to).
const routes = [];

export function route(pattern, load) {
  const names = [];
  const re = new RegExp(
    "^" + pattern.replace(/:(\w+)/g, (_, n) => { names.push(n); return "([^/]+)"; }) + "$",
  );
  routes.push({ re, names, load });
}

export function match(hash) {
  const path = (hash || "").replace(/^#/, "").replace(/^\/?/, "/").replace(/\/+$/, "") || "/";
  const [p, query = ""] = path.split("?");
  for (const r of routes) {
    const m = r.re.exec(p === "" ? "/" : p);
    if (m) {
      const params = {};
      r.names.forEach((n, i) => { params[n] = decodeURIComponent(m[i + 1]); });
      return { load: r.load, params, query: new URLSearchParams(query), path: p };
    }
  }
  return null;
}

export function go(path) {
  const target = `#${path}`;
  if (location.hash === target) window.dispatchEvent(new HashChangeEvent("hashchange"));
  else location.hash = target;
}

// navigate after an await (a plan computed, a job started, a record saved) only if the screen that started it is
// still shown: the user may have opened another screen meanwhile and must stay there
export function goFrom(ctx, path, { replace = false } = {}) {
  if (location.hash !== ctx.hash) return;
  if (replace) location.replace(`#${path}`);
  else go(path);
}
