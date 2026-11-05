// Inline help: a small "?" button next to a hard concept's heading opens a popover with 2–4 sentences
// and a "Read more" link to the matching docs page. `helpTip("rules")` in a heading; the texts are i18n strings
// `help.<topic>` like the settings explanations, the links live in DOCS below. One popover at a time;
// Escape or a click outside closes it and gives the focus back to its button.
import { html, $ } from "./dom.js";
import { t, addStrings } from "./i18n.js";

// Where the user docs are read: one constant to change when the repository moves.
export const DOCS_BASE = "https://github.com/Bionded/SyncMusic/blob/main/docs/";

const DOCS = {
  rules: "01-overview.md#templates-and-rules",
  processors: "01-overview.md#processors",
  plan: "03-first-run.md#5-review-the-plan",
  cache: "05-settings.md#storage-cache--space-backup",
  reverse: "01-overview.md#reverse-sync",
  update_full: "01-overview.md#sync-plan-then-apply",
};

addStrings({
  "help.label": "Help: {topic}",
  "help.more": "Read more",
  "help.rules.title": "Rules",
  "help.rules": "Rules decide what goes onto the device, like a Navidrome smart playlist: conditions on tags, ratings, play counts or dates, combined with all/any. Save rules as a template to reuse them on several devices - a linked template follows later changes of the template.",
  "help.processors.title": "Processors",
  "help.processors": "Processors change the files on the way to the device: convert to a format the player can play, retag, add covers and lyrics. They run in the order shown, and the results are kept in the processing cache so the next sync does not convert again.",
  "help.plan.title": "The sync plan",
  "help.plan": "Nothing is written yet. The plan lists what will be added, updated and removed and how much space it needs. Check it, then start the sync; if something does not fit, the plan says what was left off and why.",
  "help.cache.title": "Conversion cache",
  "help.cache": "Converted and downloaded files are normally kept in the cache on this computer, so the next sync of this or another device reuses them. Without the cache each file is held in memory only until it is on the device: it saves disk space here, but the next sync converts again.",
  "help.reverse.title": "Reverse sync",
  "help.reverse": "Before every sync SyncMusic reads back what happened on the player - play counts, last played, ratings, favourites and audiobook or podcast positions - and sends it to the libraries. When the library and the player disagree on a position, the newer one wins.",
  "help.update_full.title": "Update or full sync",
  "help.update_full": "An update brings plays, ratings and positions back to the libraries and pushes changed tags, ratings, playlists and positions to the device; it never copies new files and can run by itself while the device is connected. A full sync copies new and replaced files after you review a plan.",
});

/** The "?" button for a help topic; put it inside the heading it explains. */
export function helpTip(topic) {
  const title = t(`help.${topic}.title`);
  return html`<button type="button" class="help-tip" data-help="${topic}" aria-label="${t("help.label", { topic: title })}"
    aria-expanded="false" aria-haspopup="dialog">?</button>`;
}

let open = null; // { btn, pop }

function close({ focus = false } = {}) {
  if (!open) return;
  const { btn, pop } = open;
  open = null;
  pop.remove();
  btn.setAttribute("aria-expanded", "false");
  if (focus && btn.isConnected) btn.focus();
}

function place(btn, pop) {
  const r = btn.getBoundingClientRect();
  const w = pop.offsetWidth;
  const left = Math.max(8, Math.min(r.left + window.scrollX - 12, window.scrollX + document.documentElement.clientWidth - w - 8));
  pop.style.left = `${left}px`;
  pop.style.top = `${r.bottom + window.scrollY + 6}px`;
}

function show(btn) {
  const topic = btn.dataset.help;
  const pop = document.createElement("div");
  pop.className = "help-pop";
  pop.id = `help-pop-${topic}`;
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", t(`help.${topic}.title`));
  pop.innerHTML = String(html`<p>${t(`help.${topic}`)}</p>
    <a href="${DOCS_BASE}${DOCS[topic]}" target="_blank" rel="noopener">${t("help.more")}</a>`);
  document.body.append(pop);
  place(btn, pop);
  btn.setAttribute("aria-expanded", "true");
  btn.setAttribute("aria-controls", pop.id);
  open = { btn, pop };
  $("a", pop).focus();
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest?.(".help-tip");
  if (btn) {
    e.preventDefault();
    e.stopPropagation(); // a "?" inside a <summary> or <label> must not toggle it
    const same = open?.btn === btn;
    close();
    if (!same) show(btn);
    return;
  }
  if (open && !open.pop.contains(e.target)) close();
}, true);

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && open) {
    e.stopPropagation();
    close({ focus: true });
  }
}, true);

window.addEventListener("hashchange", () => close());
window.addEventListener("resize", () => open && place(open.btn, open.pop)); // phones resize on scroll (URL bar)
