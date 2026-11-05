// Tiny i18n helper: t("key", {name}) → string with {placeholders} filled. English only for now; another language
// is one more table in STRINGS plus a way to pick it (navigator.language). A count `n` of 1 takes the key's singular
// variant "<key>_one" when there is one; strings with several counts build them from the count.* keys.
const STRINGS = {
  en: {
    "app.tagline": "your music, on your players",
    "nav.dashboard": "Dashboard",
    "nav.libraries": "Libraries",
    "nav.devices": "Devices",
    "nav.templates": "Templates",
    "nav.proctemplates": "Processor templates",
    "nav.settings": "Settings",
    "nav.notifications": "Notifications",
    "nav.activity": "Activity",
    "live.on": "Live",
    "live.off": "Reconnecting…",
    "common.cancel": "Cancel",
    "common.close": "Close",
    "common.confirm": "Confirm",
    "common.retry": "Retry",
    "common.save": "Save",
    "common.loading": "Loading…",
    // used by several views: a key defined in one view is raw text on a deep link to another
    "common.danger": "Danger zone",
    "common.saved": "Saved.",
    "common.register": "Register",
    "libs.kind.music": "Music", // the libraries view and the rule builder
    "libs.kind.podcast": "Podcasts",
    "libs.kind.audiobook": "Audiobooks",
    "libs.kind.video": "Videos",
    "error.title": "Something went wrong",
    "error.network": "The SyncMusic service cannot be reached. Check that it is running.",
    "error.notfound": "This page does not exist.",
    "confirm.typed": "Type {name} to confirm",
    "confirm.typed_mismatch": "The name does not match - nothing was done.",
    "status.waiting": "Waiting",
    "status.running": "Running",
    "status.failed": "Failed",
    "count.device": "{n} devices",
    "count.device_one": "{n} device",
    "count.include": "{n} include rules",
    "count.include_one": "{n} include rule",
    "count.exclude": "{n} exclude rules",
    "count.exclude_one": "{n} exclude rule",
    "count.item": "{n} items",
    "count.item_one": "{n} item",
    "count.play": "{n} plays",
    "count.play_one": "{n} play",
    "count.rating": "{n} ratings",
    "count.rating_one": "{n} rating",
    "count.heart": "{n} hearts",
    "count.heart_one": "{n} heart",
    "count.position": "{n} positions",
    "count.position_one": "{n} position",
    "count.track": "{n} tracks",
    "count.track_one": "{n} track",
    "count.playlist": "{n} playlists",
    "count.playlist_one": "{n} playlist",
    "count.tag": "{n} tags",
    "count.tag_one": "{n} tag",
    "count.removed": "{n} removed",
  },
};

let lang = "en";

const lookup = (key) => STRINGS[lang][key] ?? STRINGS.en[key];

export function t(key, params = {}) {
  const s = (params.n === 1 && lookup(`${key}_one`)) || lookup(key) || key;
  return s.replace(/\{(\w+)\}/g, (_, k) => (k in params ? String(params[k]) : `{${k}}`));
}

export function addStrings(table, language = "en") {
  STRINGS[language] = { ...(STRINGS[language] || {}), ...table };
}
