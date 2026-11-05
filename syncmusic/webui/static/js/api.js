// REST client for /api/v1 and the server-sent event stream.
export class ApiError extends Error {
  constructor(status, code, message, body) {
    super(message);
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

// activity on <html>, for the UI tests to wait for a quiet page instead of fixed pauses: data-api-pending (requests
// in flight), data-api-last (performance.now() of the last start or end), data-events (stream events handled)
const marks = document.documentElement.dataset;
let pending = 0;
marks.apiPending = "0";
marks.events = "0";
function mark(step) {
  pending += step;
  marks.apiPending = String(pending);
  marks.apiLast = String(Math.round(performance.now()));
}

/** `signal` (an AbortController's): a newer request cancels this one; it then rejects with the AbortError. */
export async function api(method, path, body, signal = undefined) {
  const send = () => fetch(`/api/v1${path}`, {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  mark(1);
  try {
    return await request(method, send);
  } finally {
    mark(-1);
  }
}

async function request(method, send) {
  let res;
  try {
    try {
      res = await send();
    } catch (e) {
      // a GET once more: the server may close an idle keep-alive connection just as the browser reuses it
      if (e.name === "AbortError" || method !== "GET") throw e;
      await new Promise((ok) => setTimeout(ok, 250));
      res = await send();
    }
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, "network", "network");
  }
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (!res.ok) {
    // the optional login: the session ended (signed out elsewhere, expired) → the login screen
    if (res.status === 401 && data?.error === "auth_required") location.reload();
    const msg = data?.message || (typeof data?.detail === "string" ? data.detail : res.statusText);
    throw new ApiError(res.status, data?.error || "error", msg, data);
  }
  return data;
}

export const get = (p) => api("GET", p);
export const post = (p, b = {}, signal = undefined) => api("POST", p, b, signal);
export const patch = (p, b) => api("PATCH", p, b);
export const del = (p) => api("DELETE", p);

// One EventSource for the whole app; listeners get {type, ts, data}. `onState(true|false)` follows the connection.
const listeners = new Set();
const stateListeners = new Set();
let source = null;

export function onEvent(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function onLiveState(fn) { stateListeners.add(fn); return () => stateListeners.delete(fn); }

export function connectEvents() {
  if (source) return;
  source = new EventSource("/api/v1/events");
  // the browser retries a broken stream every few seconds; one the server closed for good is opened again
  source.addEventListener("error", () => { if (source?.readyState === EventSource.CLOSED) setTimeout(reconnectEvents, 5000); });
  source.onopen = () => stateListeners.forEach((f) => f(true));
  source.onerror = () => stateListeners.forEach((f) => f(false)); // EventSource reconnects by itself
  source.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    listeners.forEach((f) => { try { f(ev); } catch (e) { console.error(e); } });
    marks.events = String(Number(marks.events) + 1);
  };
}

/** Drop the event stream and open it again now ("Retry now" on the reconnect banner). */
export function reconnectEvents() {
  source?.close();
  source = null;
  connectEvents();
}
