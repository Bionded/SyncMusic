// Theme before the first paint: a classic script in <head>, not a module, so the page never
// flashes in the other theme. The choice (dark, light or system -) is stored per browser; Settings
// and the top-bar button switch it through window.syncmusicTheme. "system" follows prefers-color-scheme live.
(function () {
  var KEY = "syncmusic.theme", MODES = ["dark", "light", "system"];
  var query = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
  var mode = "dark";
  try { var v = localStorage.getItem(KEY); if (MODES.indexOf(v) >= 0) mode = v; } catch (e) { /* private mode */ }
  function apply() {
    document.documentElement.dataset.theme = mode === "system" ? (query && query.matches ? "light" : "dark") : mode;
    document.documentElement.dataset.themeMode = mode;
  }
  if (query && query.addEventListener) query.addEventListener("change", function () { if (mode === "system") apply(); });
  apply();
  window.syncmusicTheme = {
    modes: MODES,
    mode: function () { return mode; },
    set: function (m) {
      mode = MODES.indexOf(m) >= 0 ? m : "dark";
      try { localStorage.setItem(KEY, mode); } catch (e) { /* private mode: this page only */ }
      apply();
      document.dispatchEvent(new CustomEvent("syncmusic-theme", { detail: mode }));
    },
  };
})();
