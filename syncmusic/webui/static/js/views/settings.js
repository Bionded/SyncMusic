// Settings (runtime settings): a side index (a jump menu on the
// phone) and one card per section - General (theme, per browser, applied at once), Sync defaults (what a new device
// starts with, devices syncing at once, the app-wide write speed limit), Cache & space (the cache bar, max size, Clear
// cache, history / log retention, low-disk warning; 336.3), Libraries (snapshot refresh; 336.4), Conversion (ffmpeg
// path with its version and encoders, priority, parallel jobs; 336.4), Notifications (an event × In app / Webhook
// matrix, the webhook URL - a secret: shown masked, kept when saved as the mask - with a Test button, quiet hours;
// 336.2), Maintenance (log level, diagnostics, factory reset; 336.5), Updates (repository, installed
// commit, check with the changes since; 332.1), Backup (download with or without passwords,
// restore after a preview), Import / export (templates, device settings, import after a preview),
// About (versions, host, paths of the bootstrap configuration, Logs, API docs). One Save / Discard bar for every stored
// setting; Backup, Import / export and About are outside its form. Update installs the newer commit (332.2).
import { html, bytes, when, setHtml } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { get, patch, post, del } from "../api.js";
import { pageHead, toast, unsavedBar, confirm, typedConfirm, badge, errorBox } from "../ui.js";
import { icon } from "../icons.js";
import { windowsEditor, bindWindows, readWindows } from "../windows.js";

const STRINGS = {
  "set.sub": "SyncMusic {version} on {host}",
  "set.logs": "Logs",
  "set.jump": "Go to section",
  "set.sec.general": "Appearance",
  "set.sec.access": "Access",
  "set.acc_on": "Require a password",
  "set.acc_on_sub": "Off: anyone on your network can use SyncMusic (fine at home). On: the web UI and the API ask for a password. Exposed beyond your network? Put SyncMusic behind a reverse proxy with HTTPS.",
  "set.acc_local": "This window runs on the SyncMusic computer itself: it never needs the password.",
  "set.acc_pw": "New password",
  "set.acc_pw2": "Repeat it",
  "set.acc_pw_sub": "At least 8 characters. Setting it turns the login on and signs out every other browser. Lost it? On the host run syncmusic set-password.",
  "set.acc_set": "Set password",
  "set.acc_need_pw": "Set a password first: enter it below.",
  "set.acc_mismatch": "The two passwords differ.",
  "set.acc_short": "The password needs at least 8 characters.",
  "set.acc_done_on": "Login is on.",
  "set.acc_done_off": "Login is off: no password needed.",
  "set.acc_off_title": "Turn the login off?",
  "set.acc_off_text": "Anyone on your network can then use SyncMusic without a password.",
  "set.acc_off_ok": "Turn off",
  "set.acc_signout": "Sign out",
  "set.acc_everywhere": "Sign out everywhere",
  "set.acc_everywhere_sub": "Ends the session of every browser, this one too. API tokens keep working.",
  "set.acc_tokens": "API tokens",
  "set.acc_tokens_sub": "For scripts and the command line (environment variable SYNCMUSIC_TOKEN): sent as Authorization: Bearer <token>.",
  "set.acc_token_name": "Token name",
  "set.acc_token_ph": "e.g. backup script",
  "set.acc_token_add": "Create token",
  "set.acc_token_new": "Copy this token now - it is not shown again:",
  "set.acc_token_none": "No API tokens yet.",
  "set.acc_token_used": "last used {when}",
  "set.acc_token_unused": "never used",
  "set.acc_token_created": "created {when}",
  "set.acc_revoke": "Revoke",
  "set.acc_revoke_title": "Revoke token “{name}”?",
  "set.acc_revoke_text": "Scripts using it get no access any more.",
  "set.sec.sync": "Sync",
  "set.sec.storage": "Storage",
  "set.sec.system": "System",
  "set.sec.cache": "Cache & space",
  "set.search": "Search settings",
  "set.search_ph": "Search settings…",
  "set.search_none": "No setting matches.",
  "set.perdev": "This is set per device:",
  "set.perdev_open": "Open the settings of {name}",
  "set.system_sub": "Appearance, logs, the system check, updates, moving settings between installs and the factory reset.",
  "set.when_sub": "Never: you start full syncs yourself. Ask me: a notification asks first. Automatically: a full sync starts when it is plugged in. Default: Never.",
  "set.verify_sub": "Reads written files back and compares them. Every file is safest, Sample 10 % is faster on big syncs. Default: Every file.",
  "set.eject_sub": "Unmounts the device when the sync is done, so you can unplug it. Default: No.",
  "set.nc_files_sub": "Without the cache, converted files are kept in memory until they are written; more files keep slow devices busy.",
  "set.nc_budget_sub": "The most memory those files may use together. Lower it on computers with little RAM.",
  "set.nc_max_sub": "Files larger than this are written to a temporary file instead of memory, e.g. long audiobooks.",
  "set.diag_sub": "Checks ffmpeg, disk space, permissions and connected devices. Runs when this page opens.",
  "set.sec.conv": "Conversion",
  "set.sec.notif": "Notifications",
  "set.sec.about": "About",
  "set.theme": "Theme",
  "set.theme_dark": "Dark",
  "set.theme_light": "Light",
  "set.theme_system": "System",
  "set.theme_note": "Stored in this browser only. System follows the light or dark setting of your device.",
  "set.sync_sub": "New devices start with these. Each device can change them in its Settings tab.",
  "set.upd": "Keep new devices updated automatically",
  "set.upd_sub": "Plays, ratings, positions, tags and smart playlists. No new files.",
  "set.when": "Full sync when a device is connected",
  "set.when.manual": "Never",
  "set.when.ask": "Ask me",
  "set.when.auto": "Automatically",
  "set.verify": "Verify after writing",
  "set.eject": "Eject after a sync",
  "set.eject.off": "No",
  "set.eject.full": "Full syncs",
  "set.eject.any": "All syncs",
  "set.verify.all": "Every file",
  "set.verify.sample": "Sample 10 %",
  "set.verify.off": "No",
  "set.cache_use": "Processing cache",
  "set.cache_use_sub": "What new devices start with; each device can change it in its Settings tab.",
  "set.cache_use.with": "With cache",
  "set.cache_use.without": "Without cache",
  "set.nc_files": "Files held in memory (without cache)",
  "set.upd_every": "Check new devices for changes every",
  "set.upd_every_sub": "While connected. Empty: only when plugged in and when a library changes.",
  "set.upd_gap": "Update after a library change at most every",
  "set.upd_gap_sub": "A change waits for the device's next slot; Update now on the device runs at once.",
  "set.minutes": "min",
  "set.nc_budget": "Memory for them (without cache)",
  "set.nc_max": "Larger files go to a temporary file (without cache)",
  "set.limit": "Write speed limit",
  "set.limit_sub": "Every device's full sync; keeps this computer responsive during big syncs. Empty: no limit.",
  "set.limit_ph": "no limit",
  "set.upd_every_ph": "off",
  "set.syncs": "Devices syncing at once",
  "set.syncs_sub": "Other device syncs wait until one finishes. Empty: no limit.",
  "set.cache_max": "Max cache size",
  "set.cache_max_sub": "Converted files are kept for the next sync; the oldest go first above this size.",
  "set.cache_books": "Always cache combined audiobooks",
  "set.cache_books_sub": "Combining a book from many files means re-encoding hours of audio (often 20–60 min per book on this computer). Keep the result in the media cache even for devices without the cache, so it is done once. Uses about as much space as the books themselves.",
  "set.cache_head": "Media cache",
  "set.cache_line": "{used} of {max}{inuse} · disk {pct} % full",
  "set.cache_in_use": " (in use by a sync)",
  "set.cache_label": "Media cache: {used} of {max}",
  "set.sec.res": "Storage & resources",
  "set.res_disk": "On disk",
  "set.res_total": "{size} in total",
  "set.res_fs": "{path}: {free} free of {total}",
  "set.res_copy": "Copy the path",
  "set.res_copied": "Path copied.",
  "set.res_busy": "A job is running: parts it may use can be cleared when it has finished.",
  "set.res_item_clear": "Delete",
  "set.res_confirm_title": "{action}: {label}?",
  "set.res_confirm_text": "Frees about {size}.",
  "set.res_confirm_db": "Compacts the database (frees unused space inside it). Takes a moment; nothing is deleted.",
  "set.res_confirm_backups": "Deletes every database backup except the newest one (about {size} in all backups).",
  "set.res_confirm_devbak": "These are restore points of the players' databases. Frees about {size}.",
  "set.res_done": "{label}: {size} freed.",
  "set.res_mem": "Memory and CPU",
  "set.res_rss": "Memory used",
  "set.res_children": "Helper processes",
  "set.res_cpu": "CPU now / on average",
  "set.res_uptime": "Running for",
  "set.res_threads": "Threads",
  "set.res_lag": "Response delay",
  "set.res_caches": "In-memory caches",
  "set.res_cache_line": "entries: {entries} · items: {items}",
  "set.res_free": "Free memory now",
  "set.res_free_sub": "Drops what is rebuilt when needed: library contents in memory, Player contents, file tags. Plans stay.",
  "set.res_freed": "{size} of memory freed.",
  "set.history": "Keep sync history",
  "set.history_sub": "Reports and jobs; each device's last sync stays.",
  "set.logs_keep": "Keep logs",
  "set.logs_keep_sub": "Older rotated log files are deleted.",
  "set.disk_warn": "Warn when the disk is below",
  "set.disk_warn_sub": "A notification when the disk holding SyncMusic's data and cache runs low. Empty: no warning.",
  "set.days": "days",
  "set.notif_keep": "Remove notifications after",
  "set.notif_keep_sub": "Read and unread ones; checked every hour.",
  "set.notif_keep.1": "1 day",
  "set.notif_keep.3": "3 days",
  "set.notif_keep.7": "7 days",
  "set.notif_keep.30": "30 days",
  "set.notif_keep.never": "Never",
  "set.ev.storage.low": "SyncMusic's disk almost full",
  "set.sec.libs": "Libraries & conversion",
  "set.refresh": "Refresh the library snapshot",
  "set.refresh_sub": "Plans use the snapshot; reloading a big library takes a while. Refresh in a library always reloads it.",
  "set.refresh.sync": "Every sync",
  "set.refresh.hourly": "Hourly",
  "set.refresh.manual": "Manually",
  "set.instance": "Instance name",
  "set.instance_sub": "How this SyncMusic is named on players it syncs and in other instances. Empty: the host name. Id:",
  "set.ffmpeg": "ffmpeg",
  "set.ffmpeg_sub": "Empty: the ffmpeg on the PATH. Its ffprobe must be next to it.",
  "set.ffmpeg_check": "Check",
  "set.ffmpeg_found": "Found ffmpeg {version} · encoders:",
  "set.ffmpeg_missing": "Missing encoders: {list}",
  "set.nice": "Priority (nice)",
  "set.nice_sub": "Conversions at a lower priority keep this computer and its other services responsive.",
  "set.nice.0": "0 · normal",
  "set.nice.5": "5",
  "set.nice.10": "10 · low",
  "set.nice.15": "15",
  "set.nice.19": "19 · lowest",
  "set.sec.maint": "Maintenance",
  "set.loglevel": "Log level",
  "set.loglevel_sub": "Applied at once. Debug writes a lot; use it while looking for a problem.",
  "set.loglevel.": "From the configuration file",
  "set.loglevel.INFO": "Info",
  "set.loglevel.DEBUG": "Debug",
  "set.loglevel.WARNING": "Warning",
  "set.diag": "System check",
  "set.diag_fix": "Fix: {fix}",
  "set.diag_run": "Run again",
  "set.bundle": "Diagnostics bundle",
  "set.bundle_sub": "A zip for a bug report: versions, settings, libraries, devices, recent sync reports and logs. Passwords and tokens are masked; no media files or databases.",
  "set.bundle_dl": "Collect diagnostics",
  "set.diag_running": "Running the checks…",
  "set.diag_done": "System check finished.",
  "set.diag.ok": "OK",
  "set.diag.warning": "Warning",
  "set.diag.error": "Error",
  "set.diag.unknown": "Not available",
  "set.reset": "Factory reset",
  "set.reset_sub": "Removes all libraries, devices, templates, history and settings. Files on devices and in libraries stay.",
  "set.reset_btn": "Factory reset…",
  "set.reset_title": "Factory reset SyncMusic?",
  "set.reset_text": "Everything SyncMusic knows is deleted: libraries, devices, templates, history, notifications and settings. Libraries on your servers and files on your devices are not touched.",
  "set.sec.backup": "Backup",
  "set.backup_sub": "Libraries, devices, templates and settings in one file. History, listening data and the cache are not in it.",
  "set.backup_dl": "Download backup",
  "set.backup_secrets": "Include passwords and the webhook URL",
  "set.backup_secrets_sub": "They are written into the file as plain text: keep it somewhere safe.",
  "set.restore": "Restore…",
  "set.restore_file": "Backup file",
  "set.restore_will": "Replaces everything by the backup's {libraries}, {devices}, {templates}, {procs} and settings. History is not restored.",
  "set.restore_removes": "Removed because they are not in the backup: {list}.",
  "set.restore_secrets": "Passwords are in the backup.",
  "set.restore_no_secrets": "Passwords are not in the backup: libraries and devices that are configured now keep theirs, others need them again.",
  "set.restore_from": "{file} · made {when} by SyncMusic {version}",
  "set.restore_do": "Restore",
  "set.restore_bad": "This file cannot be read as JSON.",
  "set.restored": "Backup restored.",
  "set.sec.dbb": "Database backups",
  "set.dbb": "Backups",
  "set.dbb_sub": "Copies of the database, newest first: download one, or restore it to go back to that state.",
  "set.dbb_every": "Back up the database",
  "set.dbb_every_sub": "A full copy of the database: settings, libraries, devices, templates, history and listening data, passwords included. Also made before an update, a factory reset and a restore.",
  "set.dbb_every.off": "Never",
  "set.dbb_every.6h": "Every 6 hours",
  "set.dbb_every.daily": "Daily",
  "set.dbb_every.weekly": "Weekly",
  "set.dbb_keep": "Keep",
  "set.dbb_keep_sub": "The newest backups kept; older ones are deleted.",
  "set.dbb_keep_unit": "backups",
  "set.dbb_dir": "Backup folder",
  "set.dbb_dir_sub": "Empty: the backups folder in the data directory.",
  "set.dbb_now": "Back up now",
  "set.dbb_none": "No database backups yet.",
  "set.dbb_reason.auto": "Scheduled",
  "set.dbb_reason.manual": "Manual",
  "set.dbb_reason.update": "Before update",
  "set.dbb_reason.reset": "Before factory reset",
  "set.dbb_reason.before-restore": "Before restore",
  "set.dbb_download": "Download",
  "set.dbb_restore": "Restore",
  "set.dbb_restore_title": "Restore this database backup?",
  "set.dbb_restore_text": "Everything is replaced by the backup from {when}: settings, libraries, devices, templates, history and listening data. The current database is backed up first. SyncMusic restarts.",
  "set.dbb_made": "Database backed up.",
  "count.library": "{n} libraries",
  "count.library_one": "{n} library",
  "count.template": "{n} templates",
  "count.template_one": "{n} template",
  "count.proctemplate": "{n} processor templates",
  "count.proctemplate_one": "{n} processor template",
  "set.sec.io": "Import / export",
  "set.io_sub": "Move device settings and templates between SyncMusic installs. Passwords are never exported.",
  "set.export_t": "Export templates",
  "set.export_d": "Export device settings",
  "set.import": "Import…",
  "set.import_file": "Export file",
  "set.import_from": "{file} · made {when} by SyncMusic {version}",
  "set.import_libs": "Libraries",
  "set.import_lib": "{from} ({type}) → {to}",
  "set.import_lib_none": "{from} ({type}) → not here: its templates are skipped",
  "set.import_nothing": "Nothing in this file can be imported here.",
  "set.import_do": "Import",
  "set.imported": "Imported: {n} changes.",
  "set.imported_one": "Imported: {n} change.",
  "set.io.kind.template": "Template",
  "set.io.kind.processor_template": "Processor template",
  "set.io.kind.device": "Device",
  "set.io.add": "Add",
  "set.io.replace": "Replace",
  "set.io.skip": "Skip",
  "set.ev.app.update_available": "Update available",
  "set.ev.app.updated": "Updated",
  "set.ev.app.update_failed": "Update failed",
  "set.ev.device.health_warning": "Device storage may be failing",
  "set.sec.updates": "Updates",
  "set.allowlist": "Test allowlist",
  "set.allowlist_sub": "tests/hardware.toml for SyncMusic's own hardware tests, with all registered players. The tests may format every player in it.",
  "set.allowlist_dl": "Download",
  "set.upd_channel": "Update channel",
  "set.upd_channel_sub": "Stable: releases only. Beta: also the test builds made from every change; they may have bugs.",
  "set.upd_channel.stable": "Stable",
  "set.upd_channel.beta": "Beta",
  "set.upd_installed": "Installed: {version}",
  "set.upd_kind.desktop": "desktop app",
  "set.upd_kind.standalone": "single-file program",
  "set.upd_kind.docker": "Docker image",
  "set.upd_kind.service": "system service",
  "set.upd_kind.source": "from source",
  "set.upd_commit": "commit {commit}",
  "set.upd_off": "Update checks are turned off in the configuration file.",
  "set.upd_available": "Update available: {version} · {date}",
  "set.upd_release": "Release notes",
  "set.upd_download": "Download",
  "set.upd_docker": "Pull the new image and restart the container to update.",
  "set.upd_current": "Up to date.",
  "set.upd_never": "Not checked yet.",
  "set.upd_check": "Check now",
  "set.upd_checked": "Update check finished.",
  "set.upd_install": "Update now",
  "set.upd_install_busy": "Syncs are running: update once they have finished, or now - the running syncs stop and continue after the restart.",
  "set.upd_install_sub": "Installs {commit} and restarts SyncMusic; this page reconnects by itself.",
  "set.upd_install_wait": "After the syncs",
  "set.upd_install_now": "Now",
  "set.upd_pending": "Waiting for the running syncs, then SyncMusic updates to {version}. Automatic syncs wait until then.",
  "set.upd_waiting": "SyncMusic updates once the running syncs have finished.",
  "set.upd_auto": "Automatic updates",
  "set.upd_auto_sub": "Off: SyncMusic only tells you about a new version.",
  "set.upd_auto.off": "Off - notify only",
  "set.upd_auto.after_syncs": "After the running syncs",
  "set.upd_auto.now": "Right away",
  "set.upd_auto_now_warn": "Right away interrupts running syncs; they continue after the restart.",
  "set.upd_confirm_title": "Install the update?",
  "set.upd_confirm": "SyncMusic installs {commit} and restarts. Devices stay connected; nothing syncs during the restart.",
  "set.upd_confirm_now": "SyncMusic installs {commit} and restarts now. The running syncs stop and continue after the restart.",
  "set.upd_installing": "Installing {commit}… SyncMusic restarts, this page reconnects.",
  "set.upd_installed_now": "Updated to {commit}.",
  "set.upd_timeout": "SyncMusic did not come back with the new version yet. If it does not start, it goes back to the previous version by itself.",
  "set.conc": "Parallel jobs",
  "set.conc_sub": "Processing threads. Applied at the next start. Empty: one less than the CPU cores.",
  "set.conc_ph": "auto",
  "set.webhook": "Webhook URL",
  "set.webhook_sub": "Sends a JSON POST for the events ticked under Webhook. Empty: no webhook.",
  "set.ev.head": "Event",
  "set.ev.app": "In app",
  "set.ev.hook": "Webhook",
  "set.ev.cell": "{event}: {channel}",
  "set.ev.sync.finished": "Full sync finished",
  "set.ev.sync.failed": "Full sync failed or partial",
  "set.ev.update.failed": "Automatic update failed",
  "set.ev.update.sent": "Update sent listening data back",
  "set.ev.sync.needs_confirmation": "A sync waits for you",
  "set.ev.sync.db_size_warning": "iPod database almost full",
  "set.ev.device.unknown": "New device plugged in",
  "set.ev.device.connected": "Device connected",
  "set.ev.device.error": "Device error",
  "set.ev.reverse.finished": "Listening data not sent back",
  "set.ev.device.formatted": "Device formatted",
  "set.ev.sync.started": "Full sync started",
  "set.ev.device.ejected": "Device ejected",
  "set.ev.device.time_sync_unsupported": "Clock cannot be set",
  "set.ev.no_app": "Not shown in the app",
  "set.quiet": "Quiet hours (no webhook)",
  "set.quiet_sub": "Notifications still appear in the app.",
  "set.quiet_from": "Quiet hours start",
  "set.quiet_to": "Quiet hours end",
  "set.to": "to",
  "set.webhook_test": "Send a test notification",
  "set.webhook_ok": "Test notification sent.",
  "set.webhook_failed": "The test notification failed: {message}",
  "set.version": "SyncMusic",
  "set.pygpod": "pygpod",
  "set.python": "Python",
  "set.host": "Host",
  "set.model": "Hardware",
  "set.uptime": "Uptime",
  "set.uptime_days": "{n} days",
  "set.uptime_days_one": "{n} day",
  "set.uptime_hours": "{n} h",
  "set.data_dir": "Data",
  "set.cache_dir": "Cache",
  "set.mount_root": "Mount points",
  "set.paths_note": "Change the paths in the configuration file and restart SyncMusic.",
  "set.api_docs": "API documentation",
  "set.saved": "Settings saved.",
  "set.bad_number": "{field}: a number is needed.",
};
addStrings(STRINGS);

// every setting with an explanation, for the command palette: it opens #/settings?q=<label>
export const settingsIndex = () => Object.keys(STRINGS).filter((k) => `${k}_sub` in STRINGS)
  .map((k) => ({ key: k, label: t(k), sub: t(`${k}_sub`) }));

const SECTIONS = ["sync", "libs", "storage", "notif", "system", "about"]; // six groups
const IO_BADGE = { add: "ok", replace: "warn", skip: "neutral" };
const LOG_LEVELS = ["", "INFO", "DEBUG", "WARNING"];
const UPDATE_CHANNELS = ["stable", "beta"];
const AUTO_UPDATES = ["off", "after_syncs", "now"];
const DIAG_BADGE = { ok: "ok", warning: "warn", error: "error", unknown: "neutral" };
const RESET_PHRASE = "reset syncmusic";
const REFRESH = ["sync", "hourly", "manual"];
const NICE = [0, 5, 10, 15, 19];
const DBB_EVERY = ["off", "6h", "daily", "weekly"];
const NOTIF_KEEP = [1, 3, 7, 30, null]; // Null = never
const WHEN = ["manual", "ask", "auto"];
const VERIFY = ["all", "sample", "off"];
const EJECT = ["off", "full", "any"];
// number fields: [setting, label key, integer, unit]
const NUMBERS = {
  write_limit_mb_s: ["set.limit", false],
  max_device_syncs: ["set.syncs", true],
  cache_max_gb: ["set.cache_max", false],
  history_days: ["set.history", true],
  log_days: ["set.logs_keep", true],
  disk_warn_gb: ["set.disk_warn", false],
  concurrency: ["set.conc", true],
  default_no_cache_files: ["set.nc_files", true], // required, an emptied field keeps the stored value
  default_no_cache_budget_mb: ["set.nc_budget", true],
  default_no_cache_file_max_mb: ["set.nc_max", true],
  update_change_gap_min: ["set.upd_gap", true], //
  default_update_every_min: ["set.upd_every", true], // empty = none
  db_backup_keep: ["set.dbb_keep", true], //
};
const CACHE_USE = ["with", "without"];

// the Notifications matrix: [event, can it be shown in the app]; a partial or failed full sync is
// "sync.failed" for both columns (infra/notify.py filter_key)
const EVENTS = [["sync.finished", true], ["sync.failed", true], ["update.failed", true], ["update.sent", true], ["sync.needs_confirmation", true],
  ["sync.db_size_warning", true], ["device.unknown", true], ["device.connected", true], ["device.error", true],
  ["reverse.finished", true], ["device.formatted", true], ["sync.started", false], ["device.ejected", false],
  ["device.time_sync_unsupported", false], ["storage.low", true], ["app.update_available", true],
  ["app.updated", true], ["app.update_failed", true], ["device.health_warning", true]];

function matrix(v) {
  const app = new Set(v.inbox_events || []), hook = new Set(v.webhook_events || []);
  const cell = (ev, col, on, label) => html`<td class="set-cell"><input type="checkbox" data-ev="${ev}" data-col="${col}" ${on ? "checked" : ""}
    aria-label="${t("set.ev.cell", { event: t(`set.ev.${ev}`), channel: label })}"></td>`;
  return html`<table class="table set-matrix" data-set-matrix data-set-row><thead><tr><th>${t("set.ev.head")}</th><th class="set-cell">${t("set.ev.app")}</th><th class="set-cell">${t("set.ev.hook")}</th></tr></thead>
    <tbody>${EVENTS.map(([ev, inApp]) => html`<tr data-event="${ev}"><td>${t(`set.ev.${ev}`)}</td>
      ${inApp ? cell(ev, "app", app.has(ev), t("set.ev.app")) : html`<td class="set-cell muted" title="${t("set.ev.no_app")}" aria-label="${t("set.ev.no_app")}">-</td>`}
      ${cell(ev, "hook", hook.has(ev), t("set.ev.hook"))}</tr>`)}</tbody></table>`;
}

const radios = (name, values, current, label, text) => html`<div class="segr" role="radiogroup" aria-label="${label}">${values.map((v) =>
  html`<label><input type="radio" name="${name}" value="${v}" ${v === current ? "checked" : ""}><span>${text(v)}</span></label>`)}</div>`;

const row = (label, sub, control) => html`<div class="ds-row" data-set-row><span class="ds-label">${label}${sub ? html`<small>${sub}</small>` : ""}</span>${control}</div>`;

const num = (name, value, { unit = "", placeholder = "" } = {}) => html`<span class="ds-num"><input id="set-${name}" inputmode="decimal"
  value="${value ?? ""}" placeholder="${placeholder}" aria-label="${t(NUMBERS[name][0])}">${unit}</span>`;

const sub = (id, body) => html`<div class="set-subsec" id="set-${id}" data-set-sub="${id}"><h3>${t(`set.sec.${id}`)}</h3>${body}</div>`;
const section = (id, body, sub = "") => html`<section class="card ds-card set-sec" id="set-${id}" data-set-section="${id}" aria-labelledby="set-${id}-h">
  <div><h2 id="set-${id}-h" tabindex="-1">${t(`set.sec.${id}`)}</h2>${sub ? html`<p class="ds-sub">${sub}</p>` : ""}</div>${body}</section>`;

function uptime(s) {
  if (!s) return "";
  const days = Math.floor(s / 86400);
  return days ? t("set.uptime_days", { n: days }) : t("set.uptime_hours", { n: Math.max(1, Math.round(s / 3600)) });
}

// the cache's size against its limit, and how full the disk holding it is
// the limit as typed in Max cache size (cache_max_gb counts decimal GB, 1000³ bytes)
const gb = (n) => `${Number((n / 1000 ** 3).toFixed(1))} GB`;

function cacheBar(c) {
  if (!c) return "";
  const pct = c.max ? Math.min(100, Math.round((c.used / c.max) * 100)) : 0;
  const disk = c.disk_total ? Math.round(((c.disk_total - c.disk_free) / c.disk_total) * 100) : 0;
  return html`<div class="set-cache" data-cache><div class="ds-row"><span class="ds-label">${t("set.cache_head")}</span>
      <span class="muted" data-cache-line>${t("set.cache_line", { used: bytes(c.used), max: gb(c.max), pct: disk,
        inuse: c.used > c.max && c.pinned ? t("set.cache_in_use") : "" })}</span></div>
    <span class="space-bar" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"
      aria-label="${t("set.cache_label", { used: bytes(c.used), max: gb(c.max) })}"><i style="width:${pct}%"></i></span></div>`;
}

// Storage & resources: one row per part on disk with its size, path and its one clear action
function storageBox(st) {
  if (!st) return "";
  const btn = (p, item, size, label) => html`<button type="button" class="btn small" data-action="storageClear" data-part="${p.id}"
    ${item ? html`data-item="${item}"` : ""} data-label="${label}" data-size="${size}" data-verb="${item ? t("set.res_item_clear") : p.action}"
    ${!size && p.id !== "database" ? "disabled" : ""}>${item ? t("set.res_item_clear") : p.action}</button>`;
  return html`<div class="set-storage" data-storage><div class="ds-row"><span class="ds-label">${t("set.res_disk")}<small>${t("set.res_total", { size: bytes(st.total) })}
      ${st.filesystems.map((f) => html` · ${t("set.res_fs", { path: f.path, free: bytes(f.free), total: bytes(f.total) })}`)}</small></span></div>
    ${st.busy ? html`<p class="muted" data-storage-busy>${t("set.res_busy")}</p>` : ""}
    <div class="set-io-list">${st.parts.map((p) => html`<div class="set-io-row set-part" data-part-row="${p.id}">
      <span class="set-part-main"><b>${p.label}</b> <small class="muted">${p.hint}${p.detail ? html` · ${p.detail}` : ""}</small>
        <span class="set-part-path"><code class="mono">${p.paths[0]}</code><button type="button" class="iconbtn" data-action="copyPath" data-path="${p.paths[0]}"
          title="${t("set.res_copy")}" aria-label="${t("set.res_copy")}">${icon("copy", { size: 14 })}</button></span>
        ${p.items.length > 1 || (p.items.length && p.id !== "snapshots") ? html`<span class="set-part-items">${p.items.map((i) => html`<span class="set-part-item" data-part-item="${i.id}">
          <span class="mono">${i.label}</span> <small class="muted">${bytes(i.size)}</small>${btn(p, i.id, i.size, `${p.label} · ${i.label}`)}</span>`)}</span>` : ""}</span>
      <span class="set-part-size" data-part-size>${bytes(p.size)}</span>
      <span>${p.action ? btn(p, null, p.size, p.label) : ""}</span></div>`)}</div></div>`;
}

function duration(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

// memory and CPU of the process, refreshed every few seconds while the page is open
function resourcesBox(r) {
  if (!r) return "";
  const rows = [["rss", bytes(r.rss)], ["children", r.children ? `${r.children} · ${bytes(r.children_rss)}` : "0"],
    ["cpu", `${r.cpu_percent} % / ${r.cpu_percent_avg} %`], ["uptime", duration(r.uptime_s)], ["threads", r.threads], ["lag", `${r.loop_lag_ms} ms`]];
  return html`<div class="set-res" data-resources><div class="ds-row"><span class="ds-label">${t("set.res_mem")}</span></div>
    <table class="table set-res-table"><tbody>${rows.map(([k, x]) => html`<tr><td>${t(`set.res_${k}`)}</td><td class="mono" data-res="${k}">${x}</td></tr>`)}
      ${r.caches.map((c) => html`<tr><td>${t("set.res_caches")}: ${c.label}</td><td class="mono" data-res-cache="${c.id}">${t("set.res_cache_line", { entries: c.entries, items: c.items })}</td></tr>`)}</tbody></table>
    <div class="ds-row"><span class="ds-label">${t("set.res_free")}<small>${t("set.res_free_sub")}</small></span>
      <button type="button" class="btn small" data-action="freeMemory" ${r.busy ? "disabled" : ""}>${t("set.res_free")}</button></div></div>`;
}

// "Found ffmpeg 6.1.1 · encoders: aac alac …", or why it does not run
function ffmpegLine(f) {
  if (!f) return "";
  if (f.error) return html`<div class="set-ff err" data-ffmpeg-state="error">${icon("xcircle", { size: 16 })}<span>${f.error}</span></div>`;
  return html`<div class="set-ff" data-ffmpeg-state="${f.missing.length ? "missing" : "ok"}"><span class="set-ff-head">${icon(f.missing.length ? "warn" : "ccheck", { size: 16 })}
    <span>${t("set.ffmpeg_found", { version: f.version || "?" })}</span></span>${f.encoders.map((e) => html`<code class="set-codec">${e}</code>`)}
    ${f.missing.length ? html`<span class="set-ff-missing">${t("set.ffmpeg_missing", { list: f.missing.join(", ") })}</span>` : ""}</div>`;
}

function diagRows(checks) {
  return html`<div class="set-diag-list">${checks.map((c) => html`<div class="set-diag-row" data-diag-id="${c.id}" data-diag-state="${c.state}">
    <b>${c.label}</b><span class="muted">${c.detail}${c.fix ? html`<small class="set-diag-fix" data-diag-fix>${t("set.diag_fix", { fix: c.fix })}</small>` : ""}</span>${badge(DIAG_BADGE[c.state] || "neutral", t(`set.diag.${c.state}`))}</div>`)}</div>`;
}

async function runDiag(root) {
  const box = root.querySelector("[data-diag]");
  if (!box) return;
  box.innerHTML = String(html`<p class="muted">${t("set.diag_running")}</p>`);
  try {
    const checks = await get("/diagnostics");
    if (box.isConnected) box.innerHTML = String(diagRows(checks));
  } catch (e) {
    if (box.isConnected) box.innerHTML = String(errorBox(e, ""));
  }
}

// installed version and commit, the last check's result with the changes since
function updatesBox(u) {
  if (!u) return "";
  const short = (c) => (c ? c.slice(0, 9) : "");
  const state = !u.enabled ? html`<p class="muted" data-update-state="off">${t("set.upd_off")}</p>`
    : u.error ? html`<div class="set-ff set-upd err" data-update-state="error">${icon("xcircle", { size: 16 })}<span>${u.error}</span></div>`
    : u.available ? html`<div class="set-ff set-upd" data-update-state="available">${icon("info", { size: 16 })}<span>${t("set.upd_available", { version: u.latest, date: u.latest_date || "" })}</span></div>`
    : u.checked ? html`<div class="set-ff set-upd" data-update-state="current">${icon("ccheck", { size: 16 })}<span>${t("set.upd_current")}</span></div>`
    : html`<p class="muted" data-update-state="never">${t("set.upd_never")}</p>`;
  const detail = [t(`set.upd_kind.${u.install_type}`), u.commit ? t("set.upd_commit", { commit: short(u.commit) }) : ""].filter(Boolean).join(" · ");
  const links = u.available ? html`<div class="toolbar" data-update-links>
      ${u.url ? html`<a class="btn small" href="${u.url}" target="_blank" rel="noopener">${t("set.upd_release")}</a>` : ""}
      ${u.asset && !u.installable ? html`<a class="btn small" href="${u.asset.url}" data-update-asset>${icon("download", { size: 16 })}${t("set.upd_download")}</a>` : ""}</div>` : "";
  return html`<div class="ds-row" data-set-row><span class="ds-label"><b data-update-installed>${t("set.upd_installed", { version: u.running })}</b>
      <small>${detail}</small></span>
      ${u.enabled ? html`<button type="button" class="btn small" data-action="checkUpdates">${t("set.upd_check")}</button>` : ""}</div>
    ${state}
    ${u.available && u.notes.length ? html`<ul class="set-notes" data-update-notes>${u.notes.map((n) => html`<li>${n}</li>`)}</ul>` : ""}
    ${links}
    ${u.available && u.install_type === "docker" ? html`<p class="muted" data-update-docker>${t("set.upd_docker")}</p>` : ""}
    ${u.available && u.installable && u.pending ? html`<div class="set-ff set-upd" data-update-pending>${icon("clock", { size: 16 })}<span>${t("set.upd_pending", { version: u.latest })}</span></div>` : ""}
    ${u.available && u.installable && !u.pending ? html`<div class="ds-row" data-update-install><span class="ds-label"><small>${u.jobs ? t("set.upd_install_busy") : t("set.upd_install_sub", { commit: u.latest })}</small></span>
      ${u.jobs ? html`<span class="toolbar"><button type="button" class="btn small" data-action="installUpdate" data-when="after_syncs" data-commit="${u.latest}">${t("set.upd_install_wait")}</button>
        <button type="button" class="btn small primary" data-action="installUpdate" data-when="now" data-commit="${u.latest}">${icon("download", { size: 16 })}${t("set.upd_install_now")}</button></span>`
      : html`<button type="button" class="btn small primary" data-action="installUpdate" data-when="now" data-commit="${u.latest}">${icon("download", { size: 16 })}${t("set.upd_install")}</button>`}</div>` : ""}`;
}

function dbBackups(list) {
  if (!list) return "";
  if (!list.length) return html`<p class="muted" data-db-backup-none>${t("set.dbb_none")}</p>`;
  return html`<div class="set-io-list">${list.map((b) => html`<div class="set-io-row set-dbb" data-db-backup="${b.name}">
    <span><b>${when(b.created_at)}</b> <small class="muted">${t(`set.dbb_reason.${b.reason}`)} · ${bytes(b.size)}</small></span>
    <span class="toolbar"><a class="btn small" href="/api/v1/db-backups/${encodeURIComponent(b.name)}" download>${t("set.dbb_download")}</a>
      <button type="button" class="btn small" data-action="dbRestore" data-name="${b.name}" data-when="${b.created_at}">${t("set.restore")}</button></span></div>`)}</div>`;
}

// Access: the optional login, outside the Save bar's form values (its own buttons)
function accessBox(a) {
  if (!a) return "";
  const tokens = a.tokens || [];
  return html`<div data-nosave data-access="${a.enabled ? "on" : "off"}">
    <label class="ds-check" data-set-row><input type="checkbox" id="acc-on" data-change="accessToggle" ${a.enabled ? "checked" : ""}>
      <span class="ds-label">${t("set.acc_on")}<small>${t("set.acc_on_sub")}</small></span></label>
    ${a.local_bypass ? html`<p class="muted" data-acc-local>${t("set.acc_local")}</p>` : ""}
    <p class="hint warn" data-acc-msg role="status" hidden></p>
    <div class="field" data-set-row><label for="acc-pw">${t("set.acc_pw")}</label>
      <div class="set-inline"><input id="acc-pw" type="password" autocomplete="new-password" aria-describedby="acc-pw-h">
        <input id="acc-pw2" type="password" autocomplete="new-password" aria-label="${t("set.acc_pw2")}" placeholder="${t("set.acc_pw2")}">
        <button type="button" class="btn small" data-action="accessPassword">${t("set.acc_set")}</button></div>
      <div class="hint" id="acc-pw-h">${t("set.acc_pw_sub")}</div></div>
    ${a.enabled ? html`
    <div class="ds-row" data-set-row><span class="ds-label">${t("set.acc_everywhere")}<small>${t("set.acc_everywhere_sub")}</small></span>
      <div class="toolbar"><button type="button" class="btn small" data-action="accessLogout">${t("set.acc_signout")}</button>
        <button type="button" class="btn small" data-action="accessEverywhere">${t("set.acc_everywhere")}</button></div></div>` : ""}
    <div class="field" data-set-row><label for="acc-token-name">${t("set.acc_tokens")}</label>
      <div class="set-inline"><input id="acc-token-name" type="text" maxlength="60" placeholder="${t("set.acc_token_ph")}" aria-label="${t("set.acc_token_name")}" autocomplete="off">
        <button type="button" class="btn small" data-action="accessToken">${t("set.acc_token_add")}</button></div>
      <div class="hint">${t("set.acc_tokens_sub")}</div></div>
    <div data-acc-new></div>
    <div class="set-io-list" data-acc-tokens>${tokens.length ? tokens.map((x) => html`<div class="set-io-row" data-token="${x.id}">
      <span><b>${x.name}</b> <small class="muted">${t("set.acc_token_created", { when: when(x.created) })} · ${x.last_used ? t("set.acc_token_used", { when: when(x.last_used) }) : t("set.acc_token_unused")}</small></span>
      <button type="button" class="btn small danger" data-action="accessRevoke" data-id="${x.id}" data-name="${x.name}">${t("set.acc_revoke")}</button></div>`)
      : html`<p class="muted">${t("set.acc_token_none")}</p>`}</div>
  </div>`;
}

async function redrawAccess(root) {
  const box = root.querySelector("[data-access]");
  if (box) box.outerHTML = String(accessBox(await get("/auth")));
}

function accessMsg(root, text) {
  const m = root.querySelector("[data-acc-msg]");
  m.textContent = text;
  m.hidden = !text;
}

function general(v, version) {
  const now = window.syncmusicTheme.mode();
  const inst = version.instance || {};
  // the theme is not a stored setting: buttons, applied at once (outside the Save bar's form)
  return sub("general", html`<div class="field" data-set-row><label for="set-instance_name">${t("set.instance")}</label>
    <input id="set-instance_name" type="text" maxlength="64" value="${v.instance_name ?? ""}" placeholder="${version.host || ""}"
      autocomplete="off" spellcheck="false" aria-describedby="set-instance_name-h">
    <div class="hint" id="set-instance_name-h">${t("set.instance_sub")} <span class="mono" data-instance-id>${inst.id || ""}</span></div></div>
    <div id="themeCard">${row(t("set.theme"), t("set.theme_note"), html`<div class="seg" role="radiogroup" aria-label="${t("set.theme")}">${window.syncmusicTheme.modes.map((x) =>
    html`<button type="button" role="radio" aria-checked="${x === now}" data-action="theme" data-value="${x}">${t(`set.theme_${x}`)}</button>`)}</div>`)}</div>`);
}

export const nav = "settings";

export async function render(ctx) {
  const [meta, v, version, cache, ffinfo, upd, dbb, access, store, res] = await Promise.all([get("/settings/schema"), get("/settings"), get("/version"),
    get("/settings/cache").catch(() => null), get("/settings/ffmpeg").catch(() => null), get("/updates").catch(() => null),
    get("/db-backups").catch(() => null), get("/auth").catch(() => null), get("/system/storage").catch(() => null),
    get("/system/resources").catch(() => null)]);
  ctx.state.devices = (await get("/devices").catch(() => null))?.registered || [];
  ctx.state.values = v;
  const index = html`<nav class="set-index" aria-label="${t("set.jump")}">${SECTIONS.map((s, i) =>
    html`<a href="#/settings" data-action="jump" data-sec="${s}" ${i === 0 ? html`aria-current="true"` : ""}>${t(`set.sec.${s}`)}</a>`)}</nav>
    <label class="set-jump"><select data-change="jumpSelect" aria-label="${t("set.jump")}">${SECTIONS.map((s) =>
      html`<option value="${s}">${t(`set.sec.${s}`)}</option>`)}</select></label>`;
  const about = [["version", version.version], ["pygpod", version.pygpod], ["python", version.python], ["host", version.host], ["instance", version.instance?.name],
    ["model", version.model], ["uptime", uptime(version.uptime_s)]].filter(([, x]) => x);
  return html`${pageHead(t("nav.settings"), t("set.sub", { version: version.version, host: version.host || "localhost" }),
    html`<a class="btn" href="#/logs">${icon("logs", { size: 16 })}${t("set.logs")}</a>`)}
    <div class="set-layout">${index}
      <form class="set-body set-form" data-set-form>
        <div class="set-search" data-nosave><input type="search" class="set-search-in" data-set-search placeholder="${t("set.search_ph")}"
          aria-label="${t("set.search")}" autocomplete="off" spellcheck="false"><p class="muted" data-set-none hidden>${t("set.search_none")}</p>
          <div class="set-perdev" data-set-perdev hidden></div></div>
        ${section("sync", html`
          <label class="ds-check" data-set-row><input type="checkbox" id="set-default_update_sync" ${v.default_update_sync ? "checked" : ""}>
            <span class="ds-label">${t("set.upd")}<small>${t("set.upd_sub")}</small></span></label>
          ${row(t("set.upd_every"), t("set.upd_every_sub"), num("default_update_every_min", v.default_update_every_min, { unit: t("set.minutes"), placeholder: t("set.upd_every_ph") }))}
          ${row(t("set.upd_gap"), t("set.upd_gap_sub"), num("update_change_gap_min", v.update_change_gap_min, { unit: t("set.minutes") }))}
          ${row(t("set.when"), t("set.when_sub"), radios("setWhen", WHEN, v.default_on_connect, t("set.when"), (x) => t(`set.when.${x}`)))}
          <div data-set-row>${windowsEditor("default", v.default_sync_windows || [])}</div>
          ${row(t("set.verify"), t("set.verify_sub"), radios("setVerify", VERIFY, v.default_verify, t("set.verify"), (x) => t(`set.verify.${x}`)))}
          ${row(t("set.eject"), t("set.eject_sub"), radios("setEject", EJECT, v.default_eject_after || "off", t("set.eject"), (x) => t(`set.eject.${x}`)))}
          ${row(t("set.cache_use"), t("set.cache_use_sub"), radios("setCacheUse", CACHE_USE, v.default_no_cache ? "without" : "with", t("set.cache_use"), (x) => t(`set.cache_use.${x}`)))}
          ${row(t("set.nc_files"), t("set.nc_files_sub"), num("default_no_cache_files", v.default_no_cache_files))}
          ${row(t("set.nc_budget"), t("set.nc_budget_sub"), num("default_no_cache_budget_mb", v.default_no_cache_budget_mb, { unit: "MB" }))}
          ${row(t("set.nc_max"), t("set.nc_max_sub"), num("default_no_cache_file_max_mb", v.default_no_cache_file_max_mb, { unit: "MB" }))}
          ${row(t("set.limit"), t("set.limit_sub"), num("write_limit_mb_s", v.write_limit_mb_s, { unit: "MB/s", placeholder: t("set.limit_ph") }))}
          ${row(t("set.syncs"), t("set.syncs_sub"), num("max_device_syncs", v.max_device_syncs, { placeholder: t("set.limit_ph") }))}`, t("set.sync_sub"))}
        ${section("libs", html`
          ${row(t("set.refresh"), t("set.refresh_sub"), radios("setRefresh", REFRESH, v.snapshot_refresh, t("set.refresh"), (x) => t(`set.refresh.${x}`)))}
          ${sub("conv", html`
          <div class="field" data-set-row><label for="set-ffmpeg_path">${t("set.ffmpeg")}</label>
            <div class="set-inline"><input id="set-ffmpeg_path" type="text" class="mono" value="${v.ffmpeg_path ?? ""}" placeholder="${ffinfo?.path || "ffmpeg"}"
              autocomplete="off" spellcheck="false" aria-describedby="set-ffmpeg_path-h">
              <button type="button" class="btn small" data-action="checkFfmpeg">${t("set.ffmpeg_check")}</button></div>
            <div class="hint" id="set-ffmpeg_path-h">${t("set.ffmpeg_sub")}</div></div>
          <div data-ffmpeg>${ffmpegLine(ffinfo)}</div>
          ${row(t("set.conc"), t("set.conc_sub"), num("concurrency", v.concurrency, { placeholder: t("set.conc_ph") }))}
          ${row(t("set.nice"), t("set.nice_sub"), html`<select id="set-convert_nice" class="set-select" aria-label="${t("set.nice")}">${NICE.map((n) =>
            html`<option value="${n}" ${n === v.convert_nice ? "selected" : ""}>${t(`set.nice.${n}`)}</option>`)}</select>`)}`)}`)}
        ${section("storage", html`
          ${sub("cache", html`${cacheBar(cache)}
          ${row(t("set.cache_max"), t("set.cache_max_sub"), num("cache_max_gb", v.cache_max_gb, { unit: "GB" }))}
          <label class="ds-check" data-set-row><input type="checkbox" id="set-cache_combined_books" ${v.cache_combined_books ? "checked" : ""}>
            <span class="ds-label">${t("set.cache_books")}<small>${t("set.cache_books_sub")}</small></span></label>
          ${row(t("set.history"), t("set.history_sub"), num("history_days", v.history_days, { unit: t("set.days") }))}
          ${row(t("set.logs_keep"), t("set.logs_keep_sub"), num("log_days", v.log_days, { unit: t("set.days") }))}
          ${row(t("set.disk_warn"), t("set.disk_warn_sub"), num("disk_warn_gb", v.disk_warn_gb, { unit: "GB", placeholder: t("set.limit_ph") }))}`)}
          ${sub("backup", html`<div data-nosave>
            <label class="ds-check" data-set-row><input type="checkbox" id="set-backup_secrets">
              <span class="ds-label">${t("set.backup_secrets")}<small>${t("set.backup_secrets_sub")}</small></span></label>
            <div class="ds-row" data-set-row><span class="ds-label">${t("set.backup_dl")}<small>${t("set.backup_sub")}</small></span>
              <div class="toolbar"><a class="btn small" href="/api/v1/settings/backup" download data-backup-dl>${t("set.backup_dl")}</a>
              <label class="btn small set-file">${t("set.restore")}<input type="file" accept="application/json,.json" data-change="restoreFile" aria-label="${t("set.restore_file")}"></label></div></div>
            <div data-restore></div></div>`)}
          ${sub("dbb", html`
          ${row(t("set.dbb_every"), t("set.dbb_every_sub"), html`<select id="set-db_backup_every" class="set-select" aria-label="${t("set.dbb_every")}">${DBB_EVERY.map((x) =>
            html`<option value="${x}" ${x === v.db_backup_every ? "selected" : ""}>${t(`set.dbb_every.${x}`)}</option>`)}</select>`)}
          ${row(t("set.dbb_keep"), t("set.dbb_keep_sub"), num("db_backup_keep", v.db_backup_keep, { unit: t("set.dbb_keep_unit") }))}
          <div class="field" data-set-row><label for="set-db_backup_dir">${t("set.dbb_dir")}</label>
            <input id="set-db_backup_dir" type="text" class="mono" value="${v.db_backup_dir ?? ""}" placeholder="${meta.paths.data_dir}/backups"
              autocomplete="off" spellcheck="false" aria-describedby="set-db_backup_dir-h">
            <div class="hint" id="set-db_backup_dir-h">${t("set.dbb_dir_sub")}</div></div>
          <div data-nosave><div class="ds-row" data-set-row><span class="ds-label">${t("set.dbb")}<small>${t("set.dbb_sub")}</small></span>
            <button type="button" class="btn small" data-action="dbBackupNow">${t("set.dbb_now")}</button></div>
          <div data-db-backups>${dbBackups(dbb)}</div></div>`)}`)}
        ${section("notif", html`${matrix(v)}
          ${row(t("set.notif_keep"), t("set.notif_keep_sub"), html`<select id="set-notification_days" class="set-select" aria-label="${t("set.notif_keep")}">${(NOTIF_KEEP.includes(v.notification_days) ? NOTIF_KEEP : [...NOTIF_KEEP, v.notification_days]).map((d) =>
            html`<option value="${d ?? ""}" ${d === v.notification_days ? "selected" : ""}>${d == null ? t("set.notif_keep.never") : NOTIF_KEEP.includes(d) ? t(`set.notif_keep.${d}`) : `${d} ${t("set.days")}`}</option>`)}</select>`)}
          <div class="field" data-set-row><label for="set-webhook_url">${t("set.webhook")}</label>
            <input id="set-webhook_url" type="text" class="mono" value="${v.webhook_url ?? ""}" autocomplete="off" spellcheck="false" aria-describedby="set-webhook_url-h">
            <div class="hint" id="set-webhook_url-h">${t("set.webhook_sub")}</div>
            <div><button class="btn small" type="button" data-action="testWebhook">${t("set.webhook_test")}</button></div></div>
          <div class="ds-row" data-set-row><label class="ds-check ds-grow"><input type="checkbox" id="set-quiet" ${v.quiet_hours ? "checked" : ""}>
            <span class="ds-label">${t("set.quiet")}<small>${t("set.quiet_sub")}</small></span></label>
            <span class="ds-num"><input type="time" id="set-quiet_start" class="ds-time" value="${v.quiet_hours?.start || "23:00"}" aria-label="${t("set.quiet_from")}" ${v.quiet_hours ? "" : "disabled"}>
              ${t("set.to")} <input type="time" id="set-quiet_end" class="ds-time" value="${v.quiet_hours?.end || "07:30"}" aria-label="${t("set.quiet_to")}" ${v.quiet_hours ? "" : "disabled"}></span></div>`)}
        ${section("system", html`
          ${general(v, version)}
          ${sub("access", accessBox(access))}
          ${sub("res", html`<div data-nosave>${storageBox(store)}${resourcesBox(res)}</div>`)}
          ${sub("maint", html`
          ${row(t("set.loglevel"), t("set.loglevel_sub"), html`<select id="set-log_level" class="set-select" aria-label="${t("set.loglevel")}">${LOG_LEVELS.map((l) =>
            html`<option value="${l}" ${l === (v.log_level || "") ? "selected" : ""}>${t(`set.loglevel.${l}`)}</option>`)}</select>`)}
          <div class="ds-row" data-set-row><span class="ds-label">${t("set.diag")}<small>${t("set.diag_sub")}</small></span>
            <button type="button" class="btn small" data-action="runDiag">${t("set.diag_run")}</button></div>
          <div class="set-diag" data-diag aria-live="polite"><p class="muted">${t("set.diag_running")}</p></div>
          <div class="ds-row" data-set-row><span class="ds-label">${t("set.bundle")}<small>${t("set.bundle_sub")}</small></span>
            <a class="btn small" href="/api/v1/diagnostics/bundle" download data-bundle-dl>${t("set.bundle_dl")}</a></div>
          <div class="ds-row" data-set-row><span class="ds-label">${t("set.allowlist")}<small>${t("set.allowlist_sub")}</small></span>
            <a class="btn small" href="/api/v1/devices/allowlist.toml" download="hardware.toml" data-allowlist-dl>${t("set.allowlist_dl")}</a></div>`)}
          ${sub("updates", html`
          ${row(t("set.upd_channel"), t("set.upd_channel_sub"), html`<select id="set-update_channel" class="set-select" aria-label="${t("set.upd_channel")}">${UPDATE_CHANNELS.map((c) =>
            html`<option value="${c}" ${c === v.update_channel ? "selected" : ""}>${t(`set.upd_channel.${c}`)}</option>`)}</select>`)}
          ${upd?.installable ? row(t("set.upd_auto"), html`${t("set.upd_auto_sub")} <span data-auto-warn ${v.auto_update === "now" ? "" : "hidden"}>${t("set.upd_auto_now_warn")}</span>`, html`<select id="set-auto_update" class="set-select" aria-label="${t("set.upd_auto")}" data-change="autoUpdate">${AUTO_UPDATES.map((c) =>
            html`<option value="${c}" ${c === v.auto_update ? "selected" : ""}>${t(`set.upd_auto.${c}`)}</option>`)}</select>`) : ""}
          <div data-updates>${updatesBox(upd)}</div>`)}
          ${sub("io", html`<div data-nosave>
            <div class="ds-row" data-set-row><span class="ds-label">${t("set.export_d")}<small>${t("set.io_sub")}</small></span>
              <div class="toolbar"><a class="btn small" href="/api/v1/settings/export?what=templates" download data-export="templates">${t("set.export_t")}</a>
              <a class="btn small" href="/api/v1/settings/export?what=devices" download data-export="devices">${t("set.export_d")}</a>
              <label class="btn small set-file">${t("set.import")}<input type="file" accept="application/json,.json" data-change="importFile" aria-label="${t("set.import_file")}"></label></div></div>
            <div data-import></div></div>`)}
          <div class="set-danger" data-set-row><span class="ds-label"><b>${t("set.reset")}</b><small>${t("set.reset_sub")}</small></span>
            <button type="button" class="btn danger" data-action="factoryReset">${t("set.reset_btn")}</button></div>`, t("set.system_sub"))}
        ${section("about", html`<table class="table" id="about"><tbody>${about.map(([k, x]) => html`<tr><td>${t(`set.${k}`)}</td><td class="mono" data-version="${k}">${x}</td></tr>`)}
            ${Object.entries(meta.paths).map(([k, x]) => html`<tr><td>${t(`set.${k}`)}</td><td class="mono" style="overflow-wrap:anywhere">${x}</td></tr>`)}</tbody></table>
          <p class="muted" style="font-size:12px;margin:0">${t("set.paths_note")}</p>
          <div class="toolbar"><a class="btn small" href="#/logs" data-about-logs>${t("set.logs")}</a>
            <a class="btn small" href="/docs" target="_blank" rel="noopener">${t("set.api_docs")}</a></div>`)}
      </form>
    </div>`;
}

export function mounted(ctx, root) {
  const form = root.querySelector("[data-set-form]");
  if (!form) return;
  if (root.querySelector("[data-resources]")) {
    const hash = location.hash;
    const timer = setInterval(async () => {
      const box = root.querySelector("[data-resources]");
      if (!box || !box.isConnected || location.hash !== hash) { clearInterval(timer); return; }
      if (document.hidden || box.contains(document.activeElement)) return;
      const r = await get("/system/resources").catch(() => null);
      if (r && box.isConnected) box.outerHTML = String(resourcesBox(r));
    }, 5000);
  }
  // the download link follows "Include passwords"
  root.querySelector("#set-backup_secrets")?.addEventListener("change", (e) => {
    root.querySelector("[data-backup-dl]").href = `/api/v1/settings/backup${e.target.checked ? "?secrets=true" : ""}`;
  });
  unsavedBar(form, { save: () => save(ctx, form), discard: () => ctx.rerender() });
  bindWindows(form);
  form.addEventListener("submit", (e) => e.preventDefault());
  const search = root.querySelector("[data-set-search]");
  search.addEventListener("input", () => { filter(root, search.value); perDevice(root, ctx.state.devices, search.value); });
  const asked = ctx.query?.get("q"); // from the command palette
  if (asked) {
    search.value = asked;
    search.dispatchEvent(new Event("input"));
    root.querySelector("[data-set-row]:not([hidden])")?.scrollIntoView({ block: "center" });
  }
  const words = (q) => q.trim().length > 0;
  search.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    (words(search.value) ? root.querySelector("[data-set-row]:not([hidden])") : null)?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  runDiag(root);
  form.addEventListener("change", (e) => {
    if (e.target.id !== "set-quiet") return;
    for (const id of ["#set-quiet_start", "#set-quiet_end"]) form.querySelector(id).disabled = !e.target.checked;
  });
  const mark = (id) => {
    for (const a of root.querySelectorAll(".set-index a")) a.toggleAttribute("aria-current", a.dataset.sec === id);
    const sel = root.querySelector(".set-jump select");
    if (sel) sel.value = id;
  };
  // the section being read is the one across a line 40 % down the screen (the first one touching the top
  // was marked, i.e. often the section above the one in view); between two sections, the nearest one
  const layout = root.querySelector(".set-layout");
  let queued = false;
  let pinned = null, pinTimer = null;
  layout.addEventListener("set-jump", (e) => {
    pinned = e.detail;
    mark(pinned);
    layout.dataset.pinned = pinned; // while the jump's smooth scroll runs, the scroll does not move the mark
    clearTimeout(pinTimer);
    pinTimer = setTimeout(() => { pinned = null; delete layout.dataset.pinned; }, 1200);
  });
  const pick = () => {
    queued = false;
    if (!layout.isConnected) { document.removeEventListener("scroll", onScroll, true); return; } // left the page
    if (pinned) return;
    const line = 56 + (window.innerHeight - 56) * 0.4; // below the sticky top bar
    let best = null, dist = Infinity;
    for (const s of root.querySelectorAll("[data-set-section]")) {
      const r = s.getBoundingClientRect();
      const d = r.top > line ? r.top - line : r.bottom < line ? line - r.bottom : 0;
      if (d < dist) { dist = d; best = s.dataset.setSection; }
    }
    if (best) mark(best);
  };
  const onScroll = () => { if (!queued) { queued = true; requestAnimationFrame(pick); } };
  document.addEventListener("scroll", onScroll, { capture: true, passive: true });
  pick();
}

// the settings search: a row stays when its label or explanation contains every word; a section
// whose title matches keeps all its rows; hits are highlighted with the CSS Custom Highlight API (no DOM changes)
// settings that exist only per device: the app search points to each device's Settings tab
const DEVICE_TERMS = ["gentle", "path layout", "folder layout", "playable formats", "file name", "reverse", "threshold",
  "every night", "nightly", "schedule", "playlist conflicts", "rename", "name", "format", "wipe", "clock", "time zone",
  "lyrics", "ascii", "orphans", "profile", "play log", "removals", "played", "delete", "safety margin", "space to use"];

function perDevice(root, devices, query) {
  const box = root.querySelector("[data-set-perdev]");
  const q = query.trim().toLowerCase();
  const hit = q.length >= 3 && DEVICE_TERMS.some((x) => x.includes(q) || q.includes(x));
  box.hidden = !hit || !devices?.length;
  if (box.hidden) return;
  setHtml(box, html`<span>${t("set.perdev")}</span>${devices.map((d) =>
    html`<a class="btn small" href="#/devices/${encodeURIComponent(d.id)}/settings?q=${encodeURIComponent(query.trim())}">${t("set.perdev_open", { name: d.name })}</a>`)}`);
}

function filter(root, query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const has = (el) => { const x = el.textContent.toLowerCase(); return words.every((w) => x.includes(w)); };
  let any = false;
  for (const sec of root.querySelectorAll("[data-set-section]")) {
    const whole = !words.length || has(sec.querySelector("h2"));
    for (const r of sec.querySelectorAll("[data-set-row]")) r.hidden = !whole && !has(r);
    for (const s of sec.querySelectorAll("[data-set-sub]")) {
      s.hidden = !whole && !has(s.querySelector("h3")) && !s.querySelector("[data-set-row]:not([hidden])");
      if (!s.hidden && !whole && has(s.querySelector("h3"))) for (const r of s.querySelectorAll("[data-set-row]")) r.hidden = false;
    }
    sec.hidden = !whole && !sec.querySelector("[data-set-row]:not([hidden])");
    any ||= !sec.hidden;
  }
  root.querySelector("[data-set-none]").hidden = any;
  if (!globalThis.CSS?.highlights) return;
  const ranges = [];
  if (words.length) {
    const walk = document.createTreeWalker(root.querySelector("[data-set-form]"), NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      if (n.parentElement.closest("[hidden], select")) continue;
      const x = n.data.toLowerCase();
      for (const w of words) for (let i = x.indexOf(w); i >= 0; i = x.indexOf(w, i + w.length)) {
        const r = new Range(); r.setStart(n, i); r.setEnd(n, i + w.length); ranges.push(r);
      }
    }
  }
  CSS.highlights.set("set-hit", new Highlight(...ranges));
}

function number(form, name) {
  const raw = form.querySelector(`#set-${name}`).value.trim().replace(",", ".");
  if (!raw) return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || (NUMBERS[name][1] && !Number.isInteger(n))) throw new Error(t("set.bad_number", { field: t(NUMBERS[name][0]) }));
  return n;
}

async function save(ctx, form) {
  const picked = (col) => [...form.querySelectorAll(`[data-col="${col}"]:checked`)].map((el) => el.dataset.ev);
  const checked = (name) => form.querySelector(`input[name="${name}"]:checked`)?.value;
  let values;
  try {
    values = Object.fromEntries(Object.keys(NUMBERS).map((k) => [k, number(form, k)]));
  } catch (e) { toast(e.message, "error"); return false; }
  for (const k of ["cache_max_gb", "default_no_cache_files", "default_no_cache_budget_mb", "default_no_cache_file_max_mb", "update_change_gap_min", "db_backup_keep"]) {
    if (values[k] === null) delete values[k]; // required: an emptied field keeps the stored value
  }
  ctx.state.values = await patch("/settings", {
    ...values,
    default_update_sync: form.querySelector("#set-default_update_sync").checked,
    cache_combined_books: form.querySelector("#set-cache_combined_books").checked,
    default_on_connect: checked("setWhen"),
    default_sync_windows: readWindows(form, "default"),
    default_verify: checked("setVerify"),
    default_eject_after: checked("setEject"),
    default_no_cache: checked("setCacheUse") === "without",
    snapshot_refresh: checked("setRefresh"),
    instance_name: form.querySelector("#set-instance_name").value.trim(),
    ffmpeg_path: form.querySelector("#set-ffmpeg_path").value.trim() || null,
    convert_nice: Number(form.querySelector("#set-convert_nice").value),
    notification_days: Number(form.querySelector("#set-notification_days").value) || null,
    log_level: form.querySelector("#set-log_level").value || null,
    db_backup_every: form.querySelector("#set-db_backup_every").value,
    db_backup_dir: form.querySelector("#set-db_backup_dir").value.trim() || null,
    update_channel: form.querySelector("#set-update_channel").value,
    ...(form.querySelector("#set-auto_update") ? { auto_update: form.querySelector("#set-auto_update").value } : {}),
    webhook_url: form.querySelector("#set-webhook_url").value.trim() || null,
    inbox_events: picked("app"),
    webhook_events: picked("hook"),
    quiet_hours: form.querySelector("#set-quiet").checked
      ? { start: form.querySelector("#set-quiet_start").value || "23:00", end: form.querySelector("#set-quiet_end").value || "07:30" } : null,
  });
  const brandSub = document.getElementById("brandSub"); // the instance name under the logo
  if (brandSub) brandSub.textContent = ctx.state.values.instance_name || (await get("/version").catch(() => null))?.instance?.name || brandSub.textContent;
  toast(t("set.saved"));
  return true;
}

function jumpTo(root, id) {
  const el = root.querySelector(`#set-${id}`);
  if (!el) return;
  // the index marks the section jumped to while the smooth scroll runs (the last one may never reach the top)
  root.querySelector(".set-layout")?.dispatchEvent(new CustomEvent("set-jump", { detail: id }));
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  el.querySelector("h2").focus({ preventScroll: true });
}

export const actions = {
  async accessToggle(el) {
    const root = el.closest("#content"), a = await get("/auth");
    if (el.checked && !a.has_password) {
      el.checked = false;
      accessMsg(root, t("set.acc_need_pw"));
      root.querySelector("#acc-pw").focus();
      return;
    }
    if (!el.checked && !await confirm({ title: t("set.acc_off_title"), message: t("set.acc_off_text"), ok: t("set.acc_off_ok"), danger: true })) {
      el.checked = true;
      return;
    }
    await post("/auth/access", { enabled: el.checked });
    toast(t(el.checked ? "set.acc_done_on" : "set.acc_done_off"));
    await redrawAccess(root);
  },
  async accessPassword(el) {
    const root = el.closest("#content"), pw = root.querySelector("#acc-pw").value, pw2 = root.querySelector("#acc-pw2").value;
    if (pw.length < 8) return accessMsg(root, t("set.acc_short"));
    if (pw !== pw2) return accessMsg(root, t("set.acc_mismatch"));
    await post("/auth/access", { enabled: true, password: pw });
    toast(t("set.acc_done_on"));
    await redrawAccess(root);
  },
  async accessLogout() {
    await post("/auth/logout");
    location.reload();
  },
  async accessEverywhere() {
    await post("/auth/signout-everywhere");
    location.reload();
  },
  async accessToken(el) {
    const root = el.closest("#content"), name = root.querySelector("#acc-token-name");
    const r = await post("/auth/tokens", { name: name.value.trim() });
    await redrawAccess(root);
    setHtml(root.querySelector("[data-acc-new]"), html`<div class="hint" role="status">${t("set.acc_token_new")}
      <input type="text" class="mono" readonly value="${r.token}" data-acc-token aria-label="${t("set.acc_tokens")}" style="width:100%"></div>`);
    root.querySelector("[data-acc-token]").select();
  },
  async accessRevoke(el) {
    if (!await confirm({ title: t("set.acc_revoke_title", { name: el.dataset.name }), message: t("set.acc_revoke_text"), ok: t("set.acc_revoke"), danger: true })) return;
    await del(`/auth/tokens/${el.dataset.id}`);
    await redrawAccess(el.closest("#content"));
  },
  theme(el) {
    window.syncmusicTheme.set(el.dataset.value); // app.js redraws the top-bar button and these radios
  },
  jump(el, _ctx, e) {
    e.preventDefault();
    jumpTo(el.closest("#content"), el.dataset.sec);
  },
  jumpSelect(el) {
    jumpTo(el.closest("#content"), el.value);
  },
  autoUpdate(el) {
    el.closest("[data-set-row]").querySelector("[data-auto-warn]").hidden = el.value !== "now";
  },
  async storageClear(el) {
    const { part, item, label, size, verb } = el.dataset;
    const message = part === "database" ? t("set.res_confirm_db") : part === "db_backups" ? t("set.res_confirm_backups", { size: bytes(+size) })
      : part === "device_backups" ? t("set.res_confirm_devbak", { size: bytes(+size) }) : t("set.res_confirm_text", { size: bytes(+size) });
    if (!await confirm({ title: t("set.res_confirm_title", { action: verb, label }), message, ok: verb, danger: part === "device_backups" })) return;
    el.disabled = true;
    try {
      const r = await post(`/system/storage/${encodeURIComponent(part)}/clear`, item ? { item } : {});
      toast(t("set.res_done", { label, size: bytes(r.freed) }));
      const box = el.closest("#content")?.querySelector("[data-storage]");
      if (box) box.outerHTML = String(storageBox(r));
      const bar = document.querySelector("#content [data-cache]");
      const cache = part === "media_cache" && bar ? await get("/settings/cache").catch(() => null) : null;
      if (cache) bar.outerHTML = String(cacheBar(cache));
    } catch (e) { el.disabled = false; toast(e.message, "error"); }
  },
  async copyPath(el) {
    try { await navigator.clipboard.writeText(el.dataset.path); toast(t("set.res_copied"), "ok"); } catch { toast(el.dataset.path); }
  },
  async freeMemory(el) {
    el.disabled = true;
    try {
      const r = await post("/system/resources/free", {});
      toast(t("set.res_freed", { size: bytes(r.freed) }));
      const box = el.closest("[data-resources]");
      if (box) box.outerHTML = String(resourcesBox(r));
    } catch (e) { el.disabled = false; toast(e.message, "error"); }
  },
  async checkFfmpeg(el) {
    const path = el.closest("form").querySelector("#set-ffmpeg_path").value.trim();
    el.disabled = true;
    try {
      const f = await get(`/settings/ffmpeg${path ? `?${new URLSearchParams({ path })}` : ""}`);
      el.closest("form").querySelector("[data-ffmpeg]").innerHTML = String(ffmpegLine(f));
    } finally { el.disabled = false; }
  },
  async runDiag(el) {
    el.disabled = true;
    try {
      await runDiag(el.closest("#content"));
      toast(t("set.diag_done"));
    } finally { el.disabled = false; }
  },
  async factoryReset() {
    if (!await typedConfirm({ title: t("set.reset_title"), message: t("set.reset_text"), name: RESET_PHRASE, ok: t("set.reset") })) return;
    await post("/settings/factory-reset", { confirm: RESET_PHRASE });
    location.hash = "#/";
    location.reload();
  },
  async dbBackupNow(el) {
    el.disabled = true;
    try {
      await post("/db-backups");
      el.closest("#content").querySelector("[data-db-backups]").innerHTML = String(dbBackups(await get("/db-backups")));
      toast(t("set.dbb_made"));
    } finally { el.disabled = false; }
  },
  async dbRestore(el) {
    if (!await confirm({ title: t("set.dbb_restore_title"), message: t("set.dbb_restore_text", { when: when(el.dataset.when) }), ok: t("set.dbb_restore"), danger: true })) return;
    el.disabled = true;
    try {
      await post(`/db-backups/${encodeURIComponent(el.dataset.name)}/restore`);
    } finally { el.disabled = false; }
    toast(t("set.restored"));
    location.reload();
  },
  async restoreFile(el, ctx) {
    const file = el.files?.[0];
    const box = el.closest("#content").querySelector("[data-restore]");
    el.value = ""; // the same file can be picked again
    if (!file || !box) return;
    let data;
    try { data = JSON.parse(await file.text()); } catch { toast(t("set.restore_bad"), "error"); return; }
    const p = await post("/settings/backup/preview", data);
    ctx.state.restore = data;
    const n = (key, v) => t(`count.${key}`, { n: v });
    // what exists now and is not in the file goes with the restore: "1 template (Мій шаблон)"
    const removes = [["libraries", "library"], ["devices", "device"], ["templates", "template"], ["processor_templates", "proctemplate"]]
      .map(([k, c]) => [c, p.removes?.[k] || []]).filter(([, names]) => names.length)
      .map(([c, names]) => `${n(c, names.length)} (${names.join(", ")})`);
    box.innerHTML = String(html`<div class="callout set-restore" data-restore-preview>
      <b>${t("set.restore_from", { file: file.name, when: when(p.created_at), version: p.app_version || "?" })}</b>
      <p>${t("set.restore_will", { libraries: n("library", p.libraries), devices: n("device", p.devices), templates: n("template", p.templates), procs: n("proctemplate", p.processor_templates) })}</p>
      ${removes.length ? html`<p class="err" data-restore-removes>${t("set.restore_removes", { list: removes.join("; ") })}</p>` : ""}
      <p>${t(p.secrets ? "set.restore_secrets" : "set.restore_no_secrets")}</p>
      ${p.warnings.map((w) => html`<p class="err" data-restore-warning>${w}</p>`)}
      <div class="toolbar"><button type="button" class="btn small" data-action="restoreCancel">${t("common.cancel")}</button>
        <button type="button" class="btn small primary" data-action="restoreDo">${t("set.restore_do")}</button></div></div>`);
  },
  restoreCancel(el, ctx) {
    ctx.state.restore = null;
    el.closest("[data-restore]").innerHTML = "";
  },
  async restoreDo(el, ctx) {
    el.disabled = true;
    try {
      await post("/settings/backup/restore", ctx.state.restore);
    } finally { el.disabled = false; }
    toast(t("set.restored"));
    location.reload(); // every screen and the sidebar from the restored state
  },
  async importFile(el, ctx) {
    const file = el.files?.[0];
    const box = el.closest("#content").querySelector("[data-import]");
    el.value = "";
    if (!file || !box) return;
    let data;
    try { data = JSON.parse(await file.text()); } catch { toast(t("set.restore_bad"), "error"); return; }
    const p = await post("/settings/import/preview", data);
    ctx.state.importing = data;
    const doable = p.changes.filter((c) => c.action !== "skip").length;
    box.innerHTML = String(html`<div class="callout set-restore" data-import-preview>
      <b>${t("set.import_from", { file: file.name, when: when(p.created_at), version: p.app_version || "?" })}</b>
      ${p.libraries.length ? html`<p><b>${t("set.import_libs")}</b></p>${p.libraries.map((l) => html`<p data-import-lib>${l.to
        ? t("set.import_lib", { from: l.from, type: l.type, to: l.to_name || l.to }) : t("set.import_lib_none", { from: l.from, type: l.type })}</p>`)}` : ""}
      <div class="set-io-list">${p.changes.map((c) => html`<div class="set-io-row" data-import-change="${c.action}">
        ${badge(IO_BADGE[c.action] || "neutral", t(`set.io.${c.action}`))}<span><b>${c.name}</b> <small class="muted">${t(`set.io.kind.${c.kind}`)}${c.reason ? ` · ${c.reason}` : ""}</small></span></div>`)}</div>
      ${doable ? "" : html`<p>${t("set.import_nothing")}</p>`}
      <div class="toolbar"><button type="button" class="btn small" data-action="importCancel">${t("common.cancel")}</button>
        ${doable ? html`<button type="button" class="btn small primary" data-action="importDo">${t("set.import_do")}</button>` : ""}</div></div>`);
  },
  importCancel(el, ctx) {
    ctx.state.importing = null;
    el.closest("[data-import]").innerHTML = "";
  },
  async importDo(el, ctx) {
    el.disabled = true;
    let r;
    try {
      r = await post("/settings/import", ctx.state.importing);
    } finally { el.disabled = false; }
    ctx.state.importing = null;
    el.closest("[data-import]").innerHTML = "";
    toast(t("set.imported", { n: r.changes.filter((c) => c.action !== "skip").length }));
  },
  async checkUpdates(el) {
    el.disabled = true;
    try {
      const u = await post("/updates/check", {});
      el.closest("#content").querySelector("[data-updates]").innerHTML = String(updatesBox(u));
      toast(t("set.upd_checked"));
    } finally { if (el.isConnected) el.disabled = false; }
  },
  async installUpdate(el) {
    const commit = el.dataset.commit, short = commit.slice(0, 9), when = el.dataset.when || "now";
    const busy = el.closest("[data-update-install]")?.querySelector('[data-when="after_syncs"]');
    const message = busy && when === "now" ? t("set.upd_confirm_now", { commit: short }) : t("set.upd_confirm", { commit: short });
    if (!(await confirm({ title: t("set.upd_confirm_title"), message, ok: t("set.upd_install") }))) return;
    el.disabled = true;
    let r;
    try {
      r = await post("/updates/install", { when });
    } catch (e) {
      if (el.isConnected) el.disabled = false;
      throw e;
    }
    if (r.waiting) {
      toast(t("set.upd_waiting"));
      const u = await get("/updates").catch(() => null);
      const box = el.closest("[data-updates]");
      if (u && box) box.innerHTML = String(updatesBox(u));
      return;
    }
    const box = el.closest("[data-update-install]");
    if (box) box.innerHTML = String(html`<span class="ds-label" data-update-progress><small>${t("set.upd_installing", { commit: short })}</small></span>`);
    // the service goes away while install-systemd.sh runs and comes back with the new commit
    for (let i = 0; i < 200; i++) {
      await new Promise((ok) => setTimeout(ok, 3000));
      const u = await get("/updates").catch(() => null);
      if (u && u.running === commit) {
        toast(t("set.upd_installed_now", { commit: short }));
        location.reload();
        return;
      }
    }
    toast(t("set.upd_timeout"), "error");
  },
  async testWebhook(el) {
    const url = el.closest("form").querySelector("#set-webhook_url").value.trim();
    el.disabled = true;
    try {
      const r = await post("/settings/webhook-test", { url: url || null });
      toast(r.ok ? t("set.webhook_ok") : t("set.webhook_failed", { message: r.message }), r.ok ? "info" : "error");
    } finally { el.disabled = false; }
  },
};

export function onEvent() {} // a form being edited is never redrawn
