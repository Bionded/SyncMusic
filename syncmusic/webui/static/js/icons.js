// The v2 icon set: one outline style on a 24 px grid, 1.7 px strokes in the
// current colour, round caps. `icon(name)` draws one; it is decorative (aria-hidden) next to a word, and gets its
// accessible name (i18n "icon.<name>", or `label`) when it stands alone. `typeIcon` keeps the device/library type
// icons with their data-type-icon hooks. Every name used in the sources must be in PATHS
// (tests/unit/test_webui_icons.py).
import { html } from "./dom.js";
import { t, addStrings } from "./i18n.js";

// paths from the design system; "unknown" (an unknown drive) is ours - the design has none
const PATHS = {
  dashboard: "M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z",
  devices: "M7 2h10a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM10 18h4",
  libraries: "M4 4h4v16H4zM10 4h4v16h-4zM16 5l3.5-1l3 15.5l-3.5 1z",
  templates: "M4 6h16M4 12h11M4 18h7M17 16l2 2l4-4",
  processors: "M4 7h9M17 7h3M15 5v4M4 17h3M11 17h9M9 15v4",
  // a gear (the design's rayed circle read as a sun / brightness)
  settings: "M19.3 9.8L21.9 10.3L21.9 13.7L19.3 14.2L18.7 15.5L20.2 17.8L17.8 20.2L15.5 18.7L14.2 19.3L13.7 21.9L10.3 21.9L9.8 19.3L8.5 18.7L6.2 20.2L3.8 17.8L5.3 15.5L4.7 14.2L2.1 13.7L2.1 10.3L4.7 9.8L5.3 8.5L3.8 6.2L6.2 3.8L8.5 5.3L9.8 4.7L10.3 2.1L13.7 2.1L14.2 4.7L15.5 5.3L17.8 3.8L20.2 6.2L18.7 8.5zM15 12a3 3 0 1 1-6 0a3 3 0 1 1 6 0z",
  logs: "M5 3h10l4 4v14H5zM9 12h6M9 16h6M9 8h2",
  bell: "M6 16v-5a6 6 0 0 1 12 0v5l2 2H4zM10 21h4",
  search: "M18 11a7 7 0 1 1-14 0a7 7 0 1 1 14 0zM21 21l-5-5",
  menu: "M4 6h16M4 12h16M4 18h16",
  down: "M6 9l6 6l6-6",
  right: "M9 6l6 6l-6 6",
  left: "M15 6l-6 6l6 6",
  up: "M6 15l6-6l6 6",
  plus: "M12 5v14M5 12h14",
  minus: "M5 12h14",
  x: "M6 6l12 12M18 6L6 18",
  check: "M5 12l5 5l9-10",
  ccheck: "M21 12a9 9 0 1 1-18 0a9 9 0 1 1 18 0zM8 12l3 3l5-6",
  xcircle: "M21 12a9 9 0 1 1-18 0a9 9 0 1 1 18 0zM9 9l6 6M15 9l-6 6",
  circle: "M21 12a9 9 0 1 1-18 0a9 9 0 1 1 18 0z",
  dot: "M15 12a3 3 0 1 1-6 0a3 3 0 1 1 6 0z",
  clock: "M21 12a9 9 0 1 1-18 0a9 9 0 1 1 18 0zM12 7v5l3 2",
  warn: "M12 3l10 18H2zM12 10v5M12 18h.01",
  info: "M21 12a9 9 0 1 1-18 0a9 9 0 1 1 18 0zM12 11v5M12 8h.01",
  plug: "M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5",
  sync: "M20 11a8 8 0 0 0-14.5-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.5 4.5L20 16M20 20v-4h-4",
  reverse: "M20 7H5l3-3M4 17h15l-3 3",
  eject: "M12 4l8 9H4zM4 18h16",
  grip: "M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
  edit: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4",
  copy: "M8 8h12v12H8zM16 8V4H4v12h4",
  download: "M12 4v12M7 11l5 5l5-5M4 20h16",
  upload: "M12 16V4M7 9l5-5l5 5M4 20h16",
  star: "M12 3l2.8 5.8l6.2.9l-4.5 4.4l1 6.2l-5.5-2.9l-5.5 2.9l1-6.2L3 9.7l6.2-.9z",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  pulse: "M3 12h4l2-5l4 10l2-5h6",
  video: "M3 6h13v12H3zM16 10l5-3v10l-5-3",
  podcast: "M14 10a2 2 0 1 1-4 0a2 2 0 1 1 4 0zM12 12v9M7.8 14.2a6 6 0 1 1 8.4 0M5 17a10 10 0 1 1 14 0",
  book: "M4 4h11a3 3 0 0 1 3 3v13H7a3 3 0 0 1-3-3zM4 17a3 3 0 0 1 3-3h11",
  music: "M9 18V5l11-2v13M9 18a3 3 0 1 1-6 0a3 3 0 1 1 6 0zM20 16a3 3 0 1 1-6 0a3 3 0 1 1 6 0z",
  album: "M21 12a9 9 0 1 1-18 0a9 9 0 1 1 18 0zM14 12a2 2 0 1 1-4 0a2 2 0 1 1 4 0z",
  artist: "M16 8a4 4 0 1 1-8 0a4 4 0 1 1 8 0zM4 21a8 8 0 0 1 16 0",
  playlist: "M4 6h12M4 11h12M4 16h7M15 19V13l5-1v5M15 19a2 2 0 1 1-4 0a2 2 0 1 1 4 0z",
  ext: "M7 17L17 7M8 7h9v9",
  filter: "M3 5h18l-7 8v6l-4 2v-8z",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  list: "M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01",
  play: "M7 4l13 8l-13 8z",
  pause: "M7 4h3v16H7zM14 4h3v16h-3z",
  shield: "M12 3l8 3v6c0 5-4 8-8 9c-4-1-8-4-8-9V6zM9 12l2 2l4-4",
  wand: "M4 20L16 8M14 4v2M19 9h-2M18.5 4.5l-1.5 1.5M12 10l2 2",
  bolt: "M13 2L4 14h7l-1 8l9-12h-7z",
  image: "M3 5h18v14H3zM3 16l5-5l5 5l3-3l5 5M16 9h.01",
  tag: "M3 12V4h8l10 10l-8 8zM7.5 7.5h.01",
  lyrics: "M4 6h16M4 10h16M4 14h10M4 18h7",
  sun: "M16 12a4 4 0 1 1-8 0a4 4 0 1 1 8 0zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6L19 19M5 19l1.4-1.4M17.6 6.4L19 5",
  moon: "M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z",
  monitor: "M3 4h18v12H3zM8 20h8M12 16v4",
  logo: "M7 17L17 7M9 7h8v8M4 4h16v16H4z",
  keyboard: "M3 6h18v12H3zM7 10h.01M11 10h.01M15 10h.01M7 14h10",
  undo: "M9 14L4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3",
  temp: "M14 14.8V5a2 2 0 1 0-4 0v9.8a4 4 0 1 0 4 0z",
  navidrome: "M9 18V5l11-2v13M9 18a3 3 0 1 1-6 0a3 3 0 1 1 6 0zM20 16a3 3 0 1 1-6 0a3 3 0 1 1 6 0z",
  abs: "M3 18v-5a9 9 0 0 1 18 0v5M3 15h3v6H3zM18 15h3v6h-3zM9 10h6M9 13h6",
  jellyfin: "M12 3L2.5 20h19zM12 9.5L7.8 17h8.4z",
  plex: "M7 3h5l6 9l-6 9H7l6-9z",
  folder: "M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z",
  smb: "M3 4h6l2 2h10v9H3zM12 15v4M6 21h12M12 19v2",
  ftpsrc: "M4 3h16v6H4zM7.5 6h.01M12 12v9M8 17l4 4l4-4",
  "ipod-classic": "M6 2h12a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM8 4.5h8v6H8zM15.5 16.5a3.5 3.5 0 1 1-7 0a3.5 3.5 0 1 1 7 0zM13 16.5a1 1 0 1 1-2 0a1 1 0 1 1 2 0z",
  "ipod-nano": "M8.5 2h7a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1h-7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM9.5 4.5h5v7h-5zM14.5 17a2.5 2.5 0 1 1-5 0a2.5 2.5 0 1 1 5 0z",
  "ipod-mini": "M7 3h10a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM8.5 5.5h7v3.5h-7zM16 15a4 4 0 1 1-8 0a4 4 0 1 1 8 0zM13 15a1 1 0 1 1-2 0a1 1 0 1 1 2 0z",
  // the HiBy R1: compact rounded body, a touchscreen with a thin bezel, buttons on the right edge
  hiby: "M8.5 2h7a2.5 2.5 0 0 1 2.5 2.5v15a2.5 2.5 0 0 1-2.5 2.5h-7A2.5 2.5 0 0 1 6 19.5v-15A2.5 2.5 0 0 1 8.5 2zM8.5 4.5h7v15h-7zM18 6h1.3v3.5H18M18 11h1.3v2H18",
  rockbox: "M6 2h12a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM8 4.5h8v7H8zM10 9.5l2-3l2 3M12 14.5v5M9.5 17h5",
  usb: "M9 2h6v5H9zM7 7h10v10a3 3 0 0 1-3 3h-4a3 3 0 0 1-3-3zM11 4h.01M13 4h.01M10 12h4",
  ftp: "M4 4h16v6H4zM4 14h16v6H4zM7.5 7h.01M7.5 17h.01M11 7h5M11 17h5",
  client: "M8 3h8v18H8zM11 18h2M4.5 9a5 5 0 0 0 0 6M19.5 9a5 5 0 0 1 0 6",
  android: "M7 2h10a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM6 5h12M6 18h12M11 20h2",
  unknown: "M4 5h16v14H4zM10 10a2 2 0 1 1 2.8 1.8c-.5.3-.8.7-.8 1.2v.5M12 16h.01",
};

export const ICON_NAMES = Object.keys(PATHS);

addStrings({
  "icon.dashboard": "Dashboard", "icon.devices": "Devices", "icon.libraries": "Libraries", "icon.templates": "Templates",
  "icon.processors": "Processors", "icon.settings": "Settings", "icon.logs": "Logs", "icon.bell": "Notifications",
  "icon.search": "Search", "icon.menu": "Menu", "icon.down": "Down", "icon.right": "Next", "icon.left": "Back",
  "icon.up": "Up", "icon.plus": "Add", "icon.minus": "Remove", "icon.x": "Close", "icon.check": "Selected",
  "icon.ccheck": "Done", "icon.xcircle": "Error", "icon.circle": "Not connected", "icon.dot": "Dot",
  "icon.clock": "Waits", "icon.warn": "Warning", "icon.info": "Info", "icon.plug": "Connected", "icon.sync": "Sync",
  "icon.reverse": "Reverse sync", "icon.eject": "Eject", "icon.grip": "Drag to reorder", "icon.trash": "Delete",
  "icon.edit": "Edit", "icon.copy": "Copy", "icon.download": "Download", "icon.upload": "Upload", "icon.star": "Rating",
  "icon.link": "Linked", "icon.pulse": "Test connection", "icon.video": "Video", "icon.podcast": "Podcast",
  "icon.book": "Audiobook", "icon.music": "Music", "icon.album": "Album", "icon.artist": "Artist",
  "icon.playlist": "Playlist", "icon.ext": "Open", "icon.filter": "Filter", "icon.grid": "Grid", "icon.list": "List",
  "icon.play": "Play", "icon.pause": "Pause", "icon.shield": "Gentle write mode", "icon.wand": "Suggested",
  "icon.bolt": "Convert", "icon.image": "Covers", "icon.tag": "Retag", "icon.lyrics": "Lyrics", "icon.sun": "Theme", "icon.moon": "Dark theme", "icon.monitor": "System theme",
  "icon.logo": "SyncMusic", "icon.keyboard": "Keyboard shortcuts", "icon.undo": "Undo", "icon.temp": "Temperature",
  "icon.navidrome": "Subsonic-compatible server", "icon.abs": "Audiobookshelf", "icon.jellyfin": "Jellyfin", "icon.plex": "Plex", "icon.folder": "Folder library",
  "icon.smb": "SMB share", "icon.ftpsrc": "FTP source", "icon.ipod-classic": "iPod", "icon.ipod-nano": "iPod nano",
  "icon.ipod-mini": "iPod mini", "icon.rockbox": "Rockbox player", "icon.hiby": "HiBy player", "icon.usb": "USB flash drive",
  "icon.ftp": "FTP server", "icon.client": "Wi-Fi client", "icon.android": "Android player", "icon.unknown": "Unknown drive",
  // generic fallbacks of typeIcon (names of)
  "icon.device": "Device", "icon.library": "Library",
});

/** One icon. `label`: true → its i18n name, a string → that name; without one it is decorative (aria-hidden).
 * `spin`: turns (the sync icon of a running job; still with prefers-reduced-motion). Unknown names draw a dot. */
export function icon(name, { size = 18, label = null, spin = false, width = 1.7, cls = "" } = {}) {
  const d = PATHS[name] || PATHS.dot;
  const text = label === true ? t(`icon.${name}`) : label;
  const a11y = text ? html`role="img" aria-label="${text}"` : html`aria-hidden="true"`;
  return html`<svg class="icon${spin ? " spin" : ""}${cls ? ` ${cls}` : ""}" data-icon="${name}" ${a11y} viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round" focusable="false"><path d="${d}"/></svg>`;
}

// the SyncMusic logo glyph (assets/branding/logo.svg): filled shapes in currentColor
export function logo(size = 22) {
  return html`<svg class="logo" aria-hidden="true" viewBox="0 0 512 512" width="${size}" height="${size}" focusable="false"><circle cx="256" cy="256" r="100" fill="currentColor"/><path d="M403.6 156.5A178 178 0 1 1 268.4 78.4" fill="none" stroke="currentColor" stroke-width="32" stroke-linecap="round" stroke-linejoin="round"/><circle cx="345" cy="101.8" r="42" fill="currentColor"/></svg>`;
}

// device/library type → icon; data-type-icon keeps the type names of
const TYPE_ICONS = {
  ipod: "ipod-classic", rockbox: "rockbox", hiby: "hiby", hiby_compas: "hiby", flashdrive: "usb", ftp: "ftp", smb: "smb", android: "android", client: "client",
  subsonic: "navidrome", folder: "folder", audiobookshelf: "abs", jellyfin: "jellyfin", plex: "plex",
  device: "devices", library: "libraries", unknown: "unknown",
};
const TYPE_LABELS = { ipod: "ipod-classic", flashdrive: "usb", hiby_compas: "hiby", subsonic: "navidrome", audiobookshelf: "abs" };
const LIBRARY_TYPES = new Set(["subsonic", "navidrome", "folder", "audiobookshelf", "jellyfin", "plex"]);

// the type name's data-type-icon hook; unknown types fall back to a generic device or library
function typeHook(type, kind) {
  const key = type === "navidrome" ? "subsonic" : type;
  if (key in TYPE_ICONS && !["device", "library", "unknown"].includes(key)) return key;
  return type === "unknown" ? "unknown" : LIBRARY_TYPES.has(type) || kind === "library" ? "library" : "device";
}

/** The words for a device or library type ("USB flash drive", "Rockbox player"), the icon's accessible name. */
export function typeLabel(type, kind = "device") {
  const hook = typeHook(type, kind);
  return t(`icon.${TYPE_LABELS[hook] || hook}`);
}

/** The icon of a device or library type (`kind`: "device" | "library", used for unknown types). */
export function typeIcon(type, kind = "device", size = 22) {
  const hook = typeHook(type, kind);
  return html`<svg class="type-icon" data-type-icon="${hook}" role="img" aria-label="${typeLabel(type, kind)}" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" focusable="false"><path d="${PATHS[TYPE_ICONS[hook]]}"/></svg>`;
}
