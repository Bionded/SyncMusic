// Site storage that never throws: when the browser blocks site data (private mode, cookies
// blocked) reads and writes fall back to an in-memory copy that lasts for this page only.

function make(area) {
  const memory = new Map();
  const real = () => {
    try { return window[area]; } catch { return null; }
  };
  return {
    get(key) {
      try { const s = real(); if (s) return s.getItem(key); } catch { /* blocked: memory */ }
      return memory.has(key) ? memory.get(key) : null;
    },
    set(key, value) {
      memory.set(key, String(value));
      try { const s = real(); if (s) s.setItem(key, String(value)); } catch { /* blocked: memory */ }
    },
    remove(key) {
      memory.delete(key);
      try { const s = real(); if (s) s.removeItem(key); } catch { /* blocked: memory */ }
    },
  };
}

export const local = make("localStorage");
export const session = make("sessionStorage");
