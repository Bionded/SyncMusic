// Drag and drop to reorder the rows of a list: device templates (priority), include rules in a
// template, processors in a chain. A row is dragged by its grip button ([data-grip]) with pointer events, so mouse,
// pen and touch work alike (the grip has touch-action: none); Alt+↑ / Alt+↓ on the focused grip moves the row by
// keyboard. The ↑ ↓ buttons of the rows stay as the visible fallback.
//
// sortable(list, { row, key, group, move }) binds once to a list element that survives redraws:
//   row   - selector of a row inside the list
//   key   - row element -> its key (what move() gets)
//   group - row element -> a group id; rows only move inside their group (optional)
//   move  - (fromKey, toKey) => void: put `from` where `to` is (before it when moving up, after it when moving down)
//           and redraw; afterwards the moved row's grip gets the focus back (move may return the row's new key
//           when keys are positions)

import { html } from "./dom.js";
import { t, addStrings } from "./i18n.js";
import { icon } from "./icons.js";

addStrings({
  "sort.grip": "Drag to reorder {name} (or Alt+Up / Alt+Down)",
});

/** Move `a` to the place of `b` in the array of keys (the drop and keyboard rule of every sortable list). */
export function moveKey(keys, a, b) {
  const i = keys.indexOf(a), j = keys.indexOf(b);
  if (i < 0 || j < 0 || i === j) return keys;
  keys.splice(i, 1);
  keys.splice(keys.indexOf(b) + (i > j ? 0 : 1), 0, a);
  return keys;
}

export function sortable(list, { row, key, group = () => "", move }) {
  if (!list || list.dataset.sortBound) return;
  list.dataset.sortBound = "1";
  let from = null, over = null, pointer = null;
  const rows = () => [...list.querySelectorAll(row)];
  const clear = () => {
    for (const r of list.querySelectorAll(".sort-dragging, .sort-over")) r.classList.remove("sort-dragging", "sort-over");
    from = over = pointer = null;
    list.classList.remove("sorting");
  };
  const refocus = (k) => {
    const again = rows().find((r) => key(r) === k);
    again?.querySelector("[data-grip]")?.focus();
  };
  list.addEventListener("pointerdown", (e) => {
    const grip = e.target.closest("[data-grip]");
    const r = grip?.closest(row);
    if (!r || !list.contains(r) || e.button > 0 || grip.disabled) return;
    e.preventDefault(); // no text selection, no scrolling while dragging
    from = r;
    pointer = e.pointerId;
    try { grip.setPointerCapture(e.pointerId); } catch { /* a synthetic or already ended pointer: moves still bubble */ }
    r.classList.add("sort-dragging");
    list.classList.add("sorting");
  });
  list.addEventListener("pointermove", (e) => {
    if (!from || e.pointerId !== pointer) return;
    // near the top or bottom edge the page scrolls, so a long list can be dragged on a phone
    const edge = 48;
    if (e.clientY < edge) window.scrollBy(0, -16);
    else if (e.clientY > window.innerHeight - edge) window.scrollBy(0, 16);
    const r = document.elementFromPoint(e.clientX, e.clientY)?.closest(row);
    const target = r && r !== from && list.contains(r) && group(r) === group(from) ? r : null;
    if (target === over) return;
    over?.classList.remove("sort-over");
    over = target;
    over?.classList.add("sort-over");
  });
  list.addEventListener("pointerup", (e) => {
    if (!from || e.pointerId !== pointer) return;
    const a = key(from), b = over && key(over);
    clear();
    if (b != null) refocus(move(a, b) ?? a);
  });
  list.addEventListener("pointercancel", clear);
  list.addEventListener("keydown", (e) => {
    if (!e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
    const r = e.target.closest("[data-grip]")?.closest(row);
    if (!r || !list.contains(r)) return;
    e.preventDefault();
    const mates = rows().filter((x) => group(x) === group(r));
    const to = mates[mates.indexOf(r) + (e.key === "ArrowUp" ? -1 : 1)];
    if (!to) return;
    const a = key(r);
    refocus(move(a, key(to)) ?? a);
  });
}

/** The grip button of a row: drag it, or Alt+↑ / Alt+↓ while it has the focus. `inner` replaces the plain icon. */
export function grip(name, inner = null) {
  const label = t("sort.grip", { name });
  return html`<button type="button" class="grip" data-grip aria-label="${label}" title="${label}">${inner ?? icon("grip", { size: 18, width: 2.6 })}</button>`;
}
