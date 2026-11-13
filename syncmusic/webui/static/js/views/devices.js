// Devices: list of registered devices and unknown drives, and the device page with the tabs Overview,
// Libraries, Rules, Processors, Settings (Sync behaviour + Device), History. Destructive actions (format, delete) are
// in a danger zone and need a typed name / a confirmation; nothing is shown as done before the service says so.
import { html, bytes, when, setHtml } from "../dom.js";
import { t, addStrings } from "../i18n.js";
import { helpTip, DOCS_BASE } from "../help.js";
import { get, post, patch, del } from "../api.js";
import { badge, pageHead, toast, confirm, typedConfirm, errorBox, errorMessage, problemBox, suggestionBox, emptyState, updateLine, indetBar, capacityBar, unsavedBar } from "../ui.js";
import { schemaForm, FormError } from "../forms.js";
import { go, goFrom } from "../router.js";
import { refreshSidebar } from "../sidebar.js";
import { downloadSdtool } from "../sdtool.js";
import { deviceIcon, deviceState, dbSizeLine, dbSizeBar, etaText, isCheck, thumb, duration, hibyReboot, ejectedText, betaBadge, openJob, deviceTips, hasTips } from "./common.js";
import { icon, typeIcon, typeLabel } from "../icons.js";
import { rulesTab, rulesActions, rulesMounted } from "./rules.js";
import { processorsTab, processorActions, refreshLint } from "./processors.js";
import { renderContents, contentsActions } from "./contents.js";
import { profileCard, profileActions } from "./profile.js";
import { actions as progressActions } from "./progress.js";
import { syncTab, syncMounted, readSync } from "./devsync.js";
import { SENT_KINDS, activityList } from "./activity.js";
import { feedbackChip, openFeedback, feedbackLine } from "./feedback.js";
import { pickerButton, pickerActions } from "../picker.js";
import { accountDialog } from "./libraries.js";
import { windowWaitText } from "../windows.js";

addStrings({
  "dev.cl_title": "Wi-Fi client",
  "dev.cl_app": "App",
  "dev.cl_os": "System",
  "dev.cl_seen": "Last seen",
  "dev.cl_never": "not yet",
  "dev.cl_paired": "Paired",
  "dev.cl_formats": "Plays",
  "dev.cl_space": "Free space",
  "dev.cl_space_val": "{free} free of {total}",
  "dev.cl_revoked": "Revoked: this client can no longer sync until it is paired again (Settings → Wi-Fi clients).",
  "dev.cl_sync": "The client is syncing: file {done} of {total}, {bytes}",
  "dev.cl_planning": "The client asked to sync: the plan is being made.",
  "dev.cl_manage": "Rename or revoke",
  "devs.sub": "Players and drives SyncMusic writes to. USB devices appear when plugged in.",
  "devs.add_ftp": "Add network device",
  "devs.empty_title": "No devices yet",
  "devs.unknown_head": "Plugged in, not registered",
  "devs.registered_head": "Registered · {n}",
  "devs.unnamed": "Unnamed drive",
  "devs.looks_like": "Looks like: {type}",
  "devs.ignore": "Ignore",
  "devs.unignore": "Stop ignoring",
  "devs.ignored_toast": "{name} is ignored. It stays hidden until you show ignored drives.",
  "devs.show_ignored": "Show ignored ({n})",
  "devs.hide_ignored": "Hide ignored",
  "devs.space": "{used} of {total} · {free} free",
  "devs.space_label": "Used space on {name}",
  "devs.clock_soon": "Setting the clock… ready in a few seconds",
  "devs.clock_title": "Available when the iPod is ready (setting the clock)",
  "devs.not_connected": "Not connected",
  "devs.last_sync": "Last sync",
  "devs.never": "Never",
  "devs.result.succeeded": "Synced",
  "devs.result.partial": "Partial",
  "devs.result.failed": "Failed",
  "devs.result.cancelled": "Cancelled",
  "devs.result.running": "Running",
  "devs.result.skipped": "Skipped",
  "devs.progress": "Progress",
  "devs.review": "Review",
  "devs.fix": "Fix",
  "devs.sync": "Sync",
  "devs.open": "Open",
  "devs.choose_templates": "Templates",
  "devs.gentle": "Gentle",
  "devs.upd_running": "Updating…",
  "devs.upd_failed": "Update failed",
  "devs.upd_off": "Automatic updates off",
  "devs.upd_paused": "Update paused",
  "devs.upd_ok": "Updated {when}",
  "devs.register": "Register a device",
  "devs.none": "Connect an iPod, a Rockbox player or a USB drive.",
  "devs.folder_kind": "Folder (cannot be formatted)",
  "devs.network_kind": "Server",
  "devs.offline_since": "Not reachable since {when}",
  "dev.offline_since": "The server has not answered since {when}; automatic syncs try again at the next slot.",
  "dev.tab.overview": "Overview",
  "dev.tab.contents": "Player contents",
  "dev.tab.rules": "Templates",
  "dev.tab.processors": "Processors",
  "dev.tab.settings": "Settings",
  "dev.set_search": "Search this device's settings",
  "dev.set_search_ph": "Search settings…",
  "dev.set_search_none": "No setting matches.",
  "dev.set_what": "What goes on it",
  "dev.set_what_sub": "Templates and rules choose the music, podcasts and audiobooks for this device. Opens the Templates tab.",
  "dev.set_how": "How files are processed",
  "dev.set_how_sub": "Conversion, tags, covers and lyrics before files are written. Opens the Processors tab.",
  "dev.set_part_sync": "Sync behaviour",
  "dev.set_part_device": "Device",
  "dev.tab.history": "History",
  "hist.activity": "What it did",
  "dev.sync": "Sync",
  "dev.tabs": "Device sections",
  "dev.show_progress": "Show progress",
  "dev.review_plan": "Review plan",
  "dev.sync_offline": "Connect the device to sync",
  "dev.sync_busy": "Available when the device is idle",
  "dev.clock_text": "The iPod will be ready in a few seconds (at most 30 s). Sync and Player contents unlock then.",
  "dev.open_settings": "Open device settings",
  "dev.fs_damaged": "File system damaged",
  "dev.fs_damaged_text": "Reading or writing the device failed ({error}). Automatic syncs are paused until the file system is repaired, the device is wiped or reconnected.",
  "dev.repair": "Check and repair",
  "dev.repair_wipe": "Wipe…",
  "dev.repairing": "Checking and repairing {name}…",
  "dev.dz_unmount": "Unmount everywhere",
  "dev.foreign_title": "Also mounted by the system",
  "dev.firmware_title": "This player now runs another firmware",
  "dev.firmware_text": "It is registered for {old}, but it now runs {new}. Switch the device type to sync it the way this firmware expects; the music on it stays.",
  "dev.firmware_switch": "Switch to {new}",
  "dev.sdtool_title": "Music shows on the player only after it indexed it",
  "dev.sdtool_text": "With compas-sdtool SyncMusic builds the player's library during the sync, so new music, hearts and play counts are there right after unplugging.",
  "dev.firmware_switched": "{name} now syncs for {new}",
  "dev.lease_title": "Being synced by {name}",
  "dev.lease_text": "Another SyncMusic installation has been writing to this player since {since}. A sync started here is refused until it is done.",
  "dev.foreign_text": "Another program mounted it at {paths}. Two programs writing to it at once can damage the file system; see Troubleshooting to turn the system's automount off, or unmount it everywhere.",
  "dev.unmount_text": "Unmount the player's partitions everywhere, also where another program (the system's automount) mounted it, so Check and repair, Format or Wipe can run.",
  "dev.unmount": "Unmount everywhere",
  "dev.unmount_none": "{name} is not mounted anywhere.",
  "dev.unmount_title": "Unmount {name} everywhere?",
  "dev.unmount_list": "Pending writes are flushed first. Mounted at:",
  "dev.unmount_other": "another program",
  "dev.unmount_done": "{name} is unmounted everywhere.",
  "dev.unmount_busy_title": "{name} is still in use",
  "dev.unmount_busy_text": "{error}. Close it, or detach the mount anyway: it disappears now and the file system is released once that program lets go.",
  "dev.unmount_lazy": "Detach anyway",
  "dev.repair_blocked_title": "{name} is also mounted by another program",
  "dev.repair_blocked_text": "Another program (usually the system's automount) mounted it at {paths}. Check and repair needs it unmounted everywhere; pending writes are flushed first.",
  "dev.repair_blocked_ok": "Unmount everywhere and continue",
  "dev.gentle": "Gentle",
  "dev.gentle_title": "Gentle write mode is on - open Settings",
  "dev.gentle_why": "The last full sync stopped: the device's storage failed.",
  "dev.gentle_help": "Gentle write mode writes in small batches with pauses, which helps players with a CF or SD card.",
  "dev.update_waits": "Updates wait until the full sync ends",
  "dev.update_waits_check": "Updates wait until the check ends",
  "dev.update_now": "Update now",
  "dev.update_started": "Updating {name}…",
  "dev.capacity_head": "Capacity",
  "dev.map.head": "What takes the space",
  "dev.map.rest": "Not counted above: device database {db} · files SyncMusic did not write {foreign} · kept free as a safety margin {reserve}",
  "dev.map.by_library": "By library",
  "dev.map.by_rule": "By rule",
  "dev.map.no_rule": "No rule (written before rules were recorded)",
  "dev.map.items": "{n} items",
  "dev.map.items_one": "{n} item",
  "dev.left.head": "Did not fit in the last plan",
  "dev.left.short": "The plan needs {bytes} more than is free.",
  "dev.left.dropped": "{n} items ({bytes}) were left off for space.",
  "dev.left.dropped_one": "{n} item ({bytes}) was left off for space.",
  "dev.left.limit": "The device limit left off {n} items ({bytes}).",
  "dev.left.limit_one": "The device limit left off {n} item ({bytes}).",
  "dev.left.wants": "{bytes} wanted",
  "dev.left.lost": "{n} items left off · {bytes}",
  "dev.left.lost_one": "{n} item left off · {bytes}",
  "dev.left.advice": "How to make it fit",
  "dev.space_offline": "Capacity is shown while the device is connected.",
  "dev.last_full_sync": "Last full sync",
  "dev.books.head": "Audiobooks in progress",
  "dev.books.both_ways": "Positions sync both ways",
  "dev.books.device_ahead": "Device ahead · {diff}",
  "dev.books.library_ahead": "Library ahead · will update",
  "dev.books.in_sync": "In sync · {pct}%",
  "dev.health": "Storage health",
  "dev.ok": "OK",
  "dev.io_errors": "I/O errors in 30 days",
  "dev.io_last": "last {when}: {detail}",
  "dev.speed": "Write speed",
  "dev.speed_value": "{last} MB/s",
  "dev.speed_usual": "{last} MB/s · usually {usual} MB/s",
  "dev.speed_none": "Measured from the next full sync that writes a few MB.",
  "dev.speed_trend": "Write speed of the last full syncs",
  "dev.health_warning": "Storage may be failing",
  "dev.health_ack": "Acknowledge",
  "dev.health_acked": "Storage warning acknowledged: it comes back on a new error.",
  "dev.health_acked_at": "Acknowledged {when}.",
  "dev.io_list": "Recorded errors",
  "dev.io_job": "Open job",
  "dev.io_clear": "Clear error history",
  "dev.io_clear_title": "Clear the error history of {name}?",
  "dev.io_clear_text": "Forgets the recorded I/O errors, for example after the storage was replaced or reformatted. Write speeds are kept.",
  "dev.io_cleared": "Error history cleared.",
  "dev.health_advice": "Copy what you need from the device, run an integrity check, and try gentle write mode (Settings) for CF/SD-modded players.",
  "dev.integrity_stopped": "Stopped",
  "dev.integrity_attention": "Needs attention",
  "dev.integrity_never": "No integrity check yet. It reads every file SyncMusic wrote back from the device and lists what is missing, broken or not from SyncMusic. Nothing is changed.",
  "dev.integrity_when": "Integrity check {when}",
  "dev.integrity_run": "Run integrity check",
  "dev.integrity_deep": "Also decode every file (slow)",
  "dev.integrity_started": "Checking {name}…",
  "dev.integrity_failed": "The check stopped: {error}",
  "dev.integrity_ok": "Everything SyncMusic wrote reads back fine.",
  "dev.integrity.missing": "Missing - the next sync writes them again",
  "dev.integrity.broken": "Broken",
  "dev.integrity.foreign": "Not from SyncMusic",
  "dev.integrity.orphans": "Gone from their library",
  "dev.integrity_wasted": "{size} could be freed: Player contents removes them.",
  "dev.integrity_fix": "Delete {n} broken files",
  "dev.integrity_fix_one": "Delete {n} broken file",
  "dev.integrity_fixing": "Deleting the broken files; the next sync writes them again.",
  "dev.integrity_more": "… and {n} more",
  "dev.erased_wiped": "Wiped",
  "dev.erased_formatted": "Formatted",
  "dev.erased_wiped_text": "{when}: {n} files removed. The next sync is a first sync.",
  "dev.erased_wiped_text_one": "{when}: {n} file removed. The next sync is a first sync.",
  "dev.erased_formatted_text": "{when}: the device was formatted. The next sync is a first sync.",
  "dev.took_s": "{n} s",
  "dev.took_min": "{n} min",
  "dev.written": "{size} written",
  "dev.c.added": "Added",
  "dev.c.replaced": "Replaced",
  "dev.c.tags": "Tags",
  "dev.c.removed": "Removed",
  "dev.c.failed": "Failed",
  "dev.reverse_sent": "Reverse sync: {plays}, {ratings} and {positions} sent back.",
  "dev.full_report": "Full report",
  "dev.eject": "Eject",
  "dev.eject_title": "Eject {name}?",
  "dev.eject_text": "The device is unmounted and powered off. Unplug it when it says so.",
  "dev.ejecting": "Ejecting {name}…",
  "dev.ejected": "{name} can be unplugged now.",
  "dev.planned": "After the planned sync",
  "dev.planned_text": "{used} used · {free} free",
  "dev.planned_used": "{used} used",
  "dev.capacity": "{used} used · {free} free of {capacity}",
  "dev.space_unavailable": "Capacity is shown while the device is connected and idle.",
  "dev.space_unknown": "{used} put there by SyncMusic · free space unknown: the server cannot tell it, so syncs do not check it. Set “Space to use (GB)” in Settings to have it checked.",
  "dev.never_synced": "Never synced.",
  "dev.update_off": "Automatic updates are off.",
  "dev.updated": "Updated {when}",
  "dev.update_failed": "Last automatic update failed {when}",
  "dev.update_skipped": "Last automatic update skipped {when}",
  "dev.update_never": "Not updated automatically yet",
  "dev.update_next": "next check {when}",
  "dev.update_change_waits": "Library changed - update in {n} min",
  "dev.update_change_soon": "Library changed - update in under a minute",
  "dev.update_kind": "Update",
  "dev.identity": "Identity",
  "dev.playlog": "Rockbox play log",
  "dev.playlog_help": "Rockbox writes what you play to a log that the reverse sync reads (plays, last played). The change is written to the player's config at once (not a sync) and takes effect after the player restarts.",
  "dev.playlog_now": "On the player now: {state}.",
  "dev.playlog_state_on": "on",
  "dev.playlog_state_off": "off",
  "dev.playlog_state_unset": "not set (off)",
  "dev.playlog_state_offline": "unknown (not connected)",
  "dev.playlog_setting_on": "SyncMusic keeps it on.",
  "dev.playlog_setting_off": "SyncMusic keeps it off: the reverse sync finds no plays unless Gather Runtime Data is on.",
  "dev.playlog_switch_on": "Switch on",
  "dev.playlog_switch_off": "Switch off",
  "dev.playlog_applied": "Play log switched {state} on the player.",
  "dev.playlog_saved": "Play log {state}: the player gets it at the next sync.",
  "dev.rename_help": "Changes the name on the device itself ({what}). It is not a sync and nothing is synced.",
  "dev.rename_what_ipod": "the iPod's name, and its disk label if it has one",
  "dev.rename_what_disk": "the disk label",
  "dev.rename_what_folder": "a folder has no name of its own: only the name in SyncMusic changes",
  "dev.rename_what_server": "a server has no name of its own: only the name in SyncMusic changes",
  "dev.rename": "Rename…",
  "dev.rename_confirm": "Rename {old} to {name}?",
  "dev.rename_label": "Disk label: {label} (the file system allows only this).",
  "dev.rename_no_label": "The disk label is not changed.",
  "dev.rename_empty": "Enter the new name.",
  "dev.renamed": "Renamed to {name}.",
  "dev.format": "Format…",
  "dev.format_title": "Format {name}?",
  "dev.format_text": "Every file on the device's data partition is erased and a new file system is created. On an iPod the partition table and the firmware partition are kept.",
  "dev.formatting": "Formatting {name}… this can take a minute.",
  "dev.formatted": "{name} was formatted.",
  "dev.format_folder": "This device is a folder, not a disk, so it cannot be formatted.",
  "dev.format_mtp": "This player is reached over MTP: there is no drive to format - Wipe empties SyncMusic's folder.",
  "dev.format_server": "This device is a folder on a server: there is nothing to format or eject.",
  "dev.format_fs": "File system",
  "dev.format_fs_fixed": "File system: {fs}",
  "dev.format_ipod": "Only the data partition is formatted; the partition table and the firmware partition stay.",
  // Rockbox calls its format "Wipe": the data partition is reformatted, Rockbox itself is put back
  "dev.wipe": "Wipe…",
  "dev.dz_home": "Home instance",
  "dev.home_this": "This SyncMusic is the player's home: only the home wipes or formats it, removes files SyncMusic did not put there and writes player settings.",
  "dev.home_other": "The player's home is {home}{since}. Wipe, Format and removing files SyncMusic did not put there are left to it.",
  "dev.make_home": "Make this instance home…",
  "dev.make_home_title": "Make this instance home of {name}?",
  "dev.make_home_text": "From now on this SyncMusic is the home. The other installation ({home}) still syncs its own libraries but can no longer wipe or format the player.",
  "dev.made_home": "This SyncMusic is now the home of {name}.",
  "dev.not_home_only": "Only the home instance {home} can do this: make this instance home first.",
  "dev.fb_show": "Show what is not delivered",
  "dev.held_text": "{n} favourites vanished from the player at once, so SyncMusic kept them in the library. If you removed them on purpose, remove them from the library too.",
  "dev.held_text_one": "{n} favourite vanished from the player together with others, so SyncMusic kept it in the library. If you removed it on purpose, remove it from the library too.",
  "dev.held_title_one": "Remove {n} favourite from the library?",
  "dev.held_done_one": "{n} favourite removal is sent with the next sync.",
  "dev.held_send": "Remove them from the library",
  "dev.held_title": "Remove {n} favourites from the library?",
  "dev.held_confirm": "They are sent as removed favourites with the next sync of {name}. Their hearts are then also removed from the player.",
  "dev.held_done": "{n} favourite removals are sent with the next sync.",
  "dev.undo_text": "This sync changed {n} favourites and ratings in the library.",
  "dev.undo_text_one": "This sync changed {n} favourite or rating in the library.",
  "dev.undo_send": "Undo these changes",
  "dev.undo_title": "Undo {n} favourite and rating changes?",
  "dev.undo_title_one": "Undo {n} favourite or rating change?",
  "dev.undo_confirm": "The library gets its previous favourites and ratings back with the next sync of {name}, and the player follows it.",
  "dev.undo_done": "{n} previous values are sent with the next sync.",
  "dev.undo_done_one": "{n} previous value is sent with the next sync.",
  "dev.dz_wipe": "Wipe the player",
  "dev.dz_format": "Format the drive",
  "dev.dz_bind": "Use its disk",
  "dev.dz_delete": "Delete from SyncMusic",
  "dev.wipe_title": "Wipe {name}?",
  "dev.wipe_text": "Erases all music, playlists, Rockbox's database and play logs. Rockbox itself and its settings stay; it rebuilds the database on the next start.",
  "dev.wipe_text_compas": "Erases the music, playlists, audiobooks and podcasts SyncMusic or you put on the card, and SyncMusic's records in the Audiobooks and Podcasts plugins. The player's own files stay: its library folder, plugins and their settings, themes, radio stations, firmware updates and the episodes the Podcasts plugin downloaded itself.",
  "dev.wipe_text_hiby": "Erases all music, playlists, podcasts and audiobooks. Also clears the player's library, play counts, favourites and resume positions - sync first if you want them in your libraries. The player's own settings (EQ presets, radio stations, streaming logins) stay.",
  "dev.wipe_text_android": "Erases everything SyncMusic put into its folder on the player (music, playlists, covers). Nothing else on the phone is touched.",
  "dev.format_text_hiby": "A new file system is created on the whole player. Also clears the player's library, play counts, favourites and resume positions - sync first if you want them in your libraries. The player's own settings (EQ presets, radio stations, streaming logins) are kept.",
  "dev.wiping": "Wiping {name}… this can take a minute.",
  "dev.format_unavailable": "{fs} - {reason}",
  "dev.settings_device": "Files on the device",
  "dev.set_files_sub": "How SyncMusic names and writes files on this device.",
  "dev.set_name": "Name",
  "dev.set_name_label": "Name in SyncMusic",
  "dev.set_rename": "Also rename it on the device",
  "dev.set_rename_offline": "Connect the device to rename it on the device too.",
  "dev.set_type.ipod": "iPod",
  "dev.set_type.rockbox": "Rockbox",
  "dev.set_type.ftp": "FTP connection",
  "dev.set_type.hiby": "Player library",
  "dev.set_test": "Test and save",
  "dev.set_testing": "Connecting…",
  "dev.set_gentle": "Gentle write mode",
  "dev.set_gentle_sub": "For players that hang during a sync, e.g. iPods with a CF or SD card. Slower.",
  "dev.set_gentle_suggest": "Suggested: the last full sync stopped because the player's storage failed or it dropped off USB.",
  "dev.set_gentle_estimate": "With these values a full sync of this device takes about twice as long. Automatic updates are not affected.",
  "dev.set_saved": "Settings saved.",
  "dev.settings_reverse": "Reverse sync",
  "dev.accounts": "Library accounts",
  "dev.accounts_sub": "Whose plays, ratings, stars and positions this device uses. Pick another user of the library to sync a family member's profile; accounts are managed in the library's settings.",
  "dev.accounts_none": "None of this device's libraries keeps data per user (folders never do).",
  "dev.account_main": "Library's main account",
  "dev.account_add": "+ Add account…",
  "dev.account_browse": "Browse the library as this account",
  "dev.orphans": "Orphans",
  "dev.orphans_help": "Items on the device whose source is gone from its library, or whose library is no longer attached. Syncs keep them; remove them here.",
  "dev.orphans_none": "No orphans.",
  "dev.orphans_checking": "Checking for orphans…",
  "dev.orphans_n": "{n} orphans on the device.",
  "dev.orphans_n_one": "{n} orphan on the device.",
  "dev.orphans_offline": "Connect the device to see its orphans.",
  "dev.orphans_remove": "Remove orphans…",
  "dev.orphans_title": "Remove {n} orphans from {name}?",
  "dev.orphans_title_one": "Remove {n} orphan from {name}?",
  "dev.orphans_text": "These files are deleted from the device. Their source is gone from the library (or the library is no longer attached), so a sync does not bring them back.",
  "dev.reverse_none_device": "This device keeps no play counts, ratings or positions, so there is nothing to sync back.",
  "dev.reverse_none_library": "None of the libraries its templates and rules use takes play counts, ratings or positions back (folder libraries never do).",
  "dev.bind": "Use the disk {disk}",
  "dev.bind_text": "The folder is on {disk}. Bind the device to that disk (USB serial / file system id) to make Format possible; the files on it stay this device's.",
  "dev.bound": "{name} is now bound to its disk.",
  "dev.delete": "Delete device…",
  "dev.unknown_type": "Unknown device type",
  "dev.unknown_type_text": "This device has the type \"{type}\", which this version of SyncMusic does not know. It cannot be synced; you can delete it and register the player again.",
  "dev.delete_title": "Delete {name}?",
  "dev.delete_text": "SyncMusic forgets the device and its settings. Nothing on the device itself is deleted.",
  "dev.deleted": "{name} was deleted.",
  "dev.no_libraries": "No template takes media from a library yet, so there is nothing to sync.",
  "dev.attach": "Choose templates",
  "dev.history_empty": "No syncs yet.",
  "hist.show": "Show",
  "hist.f.all": "All",
  "hist.f.full": "Full syncs",
  "hist.f.update": "Updates",
  "hist.f.back": "Sent back",
  "hist.none": "Nothing of this kind yet.",
  "hist.kind.full": "Full sync",
  "hist.kind.update": "Update",
  "hist.updated": "Updated",
  "hist.nothing": "Nothing changed",
  "hist.back": "Sent back to the libraries",
  "hist.back_short": "Sent back: {what}",
  "hist.r.found": "{n} plays found on the device",
  "hist.r.found_one": "{n} play found on the device",
  "hist.r.sent": "{n} plays sent",
  "hist.r.sent_one": "{n} play sent",
  "hist.r.duplicate": "{n} already in the library",
  "hist.r.ratings": "{n} ratings",
  "hist.r.ratings_one": "{n} rating",
  "hist.r.stars": "{n} hearts added or removed",
  "hist.r.stars_one": "{n} heart added or removed",
  "hist.r.positions": "{n} positions",
  "hist.r.positions_one": "{n} position",
  "hist.r.finished": "{n} episodes marked finished",
  "hist.r.finished_one": "{n} episode marked finished",
  "hist.r.superseded": "{n} positions newer in the library (kept)",
  "hist.r.superseded_one": "{n} position newer in the library (kept)",
  "hist.r.pending": "{n} waiting for the library",
  "hist.r.dropped": "{n} given up",
  "hist.r.pl_saved": "{n} playlists saved to the library: {names}",
  "hist.r.pl_saved_one": "{n} playlist saved to the library: {names}",
  "hist.r.pl_created": "{n} playlists made on the player added to the library: {names}",
  "hist.r.pl_created_one": "{n} playlist made on the player added to the library: {names}",
  "hist.pl_removed": "{n} playlists made on the player are in the library and in no rule: removed from the player: {names}",
  "hist.pl_removed_one": "{n} playlist made on the player is in the library and in no rule: removed from the player: {names}",
  "hist.r.pl_conflicts": "{n} playlists changed on both sides, nothing changed (Playlist conflicts setting): {names}",
  "hist.r.pl_conflicts_one": "{n} playlist changed on both sides, nothing changed (Playlist conflicts setting): {names}",
  "hist.r.failed": "The listening data could not be read from the device.",
  "hist.verified": "verified",
  "hist.failed": "{n} failed",
  "hist.no_errors": "No errors",
  "hist.no_file": "- (no file reached)",
  "hist.step": "Step:",
  "hist.open_report": "Open report",
  "dev.report_log": "Log",
  "dev.failures": "{n} failed items",
  "dev.failures_one": "{n} failed item",
});

export const nav = "devices";

export function title(ctx) {
  return ctx.state.cfg ? ctx.state.cfg.name : t("nav.devices");
}

// no Libraries tab: the device's templates decide its libraries
const TABS = ["overview", "rules", "processors", "contents", "settings", "history"]; // "sync" merged into "settings" (632.2)

export async function render(ctx) {
  return ctx.params.id ? renderDevice(ctx) : renderList(ctx);
}

// --- list ------------------------------------------------------------------------------------------------------

// the list: plugged-in unknown drives first (Ignore with
// Undo, Register), then the registered devices - status badge and update line, used space or the sync's progress,
// the last sync, and one action by state (Progress / Review / Fix / Sync)
async function renderList(ctx) {
  const [devices, reports, jobs] = await Promise.all([get("/devices"), get("/sync-reports"), get("/jobs")]);
  const last = {};
  for (const r of reports) if (r.kind === "full" && !(r.device_id in last)) last[r.device_id] = r; // newest first
  const running = new Map(jobs.filter((j) => ["queued", "running"].includes(j.status)).map((j) => [j.id, j]));
  const reg = devices.registered;
  const ignored = devices.ignored || [];
  const space = await Promise.all(reg.map((d) => (listIdle(d) && (d.mounted_at || d.kind === "folder")
    ? get(`/devices/${encodeURIComponent(d.id)}/space`).catch(() => null) : null)));
  const showIgnored = ctx.query.get("ignored") === "1";
  const unknown = [...devices.unknown, ...(showIgnored ? ignored : [])];
  const ftp = html`<a class="btn" href="#/devices/register?target=network">${icon("ftp", { size: 16 })}${t("devs.add_ftp")}</a>`;
  const head = pageHead(t("nav.devices"), t("devs.sub"), html`${ftp}<a class="btn" href="#/devices/register">${t("devs.register")}</a>`);
  const ignoredLink = ignored.length ? html`<a class="btn ghost small" data-show-ignored href="#/devices${showIgnored ? "" : "?ignored=1"}">${t(showIgnored ? "devs.hide_ignored" : "devs.show_ignored", { n: ignored.length })}</a>` : "";
  if (!reg.length && !unknown.length) {
    return html`${head}<div class="lib-empty" data-devs-empty><span class="tile">${icon("plug", { size: 26 })}</span><b>${t("devs.empty_title")}</b>
      <p>${t("devs.none")}</p>${ftp}${ignoredLink}</div>`;
  }
  return html`${head}
    ${unknown.length || ignoredLink ? html`<section class="dev-section" aria-labelledby="hUnknown">
      <div class="dev-section-head"><h2 id="hUnknown">${t("devs.unknown_head")}</h2>${ignoredLink}</div>
      ${unknown.map((u) => unknownRow(u, ignored.includes(u)))}</section>` : ""}
    ${reg.length ? html`<section class="dev-section" aria-labelledby="hReg">
      <div class="dev-section-head"><h2 id="hReg">${t("devs.registered_head", { n: reg.length })}</h2></div>
      <div class="panel">${reg.map((d, i) => deviceRow(d, last[d.id], d.busy ? running.get(d.busy) : null, space[i]))}</div></section>` : ""}`;
}

const listIdle = (d) => d.connected && !d.busy && !d.ejecting && !d.ejected && !d.setting_clock && !d.no_medium;

function unknownRow(u, isIgnored) {
  const guess = u.probes?.[0]?.type_name || "unknown";
  const model = [u.vendor, u.model].filter(Boolean).join(" ") || t("devs.unnamed");
  const facts = [u.disk, u.size ? bytes(u.size) : "", u.fstype || ""].filter(Boolean).join(" · ");
  const href = `#/devices/register?disk=${encodeURIComponent(u.disk)}`;
  return html`<div class="unk-row" data-unknown="${u.disk}" ${isIgnored ? html`data-ignored` : ""}>
    <span class="unk-ico">${typeIcon(guess, "device", 22)}</span>
    <div class="meta"><b>${model}</b><small class="mono">${facts}</small></div>
    ${guess !== "unknown" ? html`<span class="muted unk-guess">${t("devs.looks_like", { type: typeLabel(guess) })}</span>` : ""}
    ${isIgnored ? html`<button type="button" class="btn ghost" data-action="ignoreDisk" data-disk="${u.disk}" data-undo>${t("devs.unignore")}</button>`
      : html`<button type="button" class="btn ghost" data-action="ignoreDisk" data-disk="${u.disk}" data-name="${model}">${t("devs.ignore")}</button>`}
    <a class="btn primary" data-register="${u.disk}" href="${href}">${t("common.register")}</a></div>`;
}

function deviceRow(d, report, job, space) {
  const id = encodeURIComponent(d.id);
  const [state, text] = deviceState(d);
  const updating = d.update?.state === "running";
  const syncing = !!d.busy && !updating;
  const pct = syncing && job ? Math.round((job.progress || 0) * 100) : null;
  const used = space?.capacity ? Math.max(0, space.capacity - space.free) : null;
  const usedPct = used !== null ? Math.round((used / space.capacity) * 100) : 0;
  const line = pct !== null ? `${pct}%${job.message ? ` · ${job.message}` : ""}`
    : used !== null ? t("devs.space", { used: bytes(used), total: bytes(space.capacity), free: bytes(space.free) })
    : d.offline_since ? t("devs.offline_since", { when: shortWhen(d.offline_since) }) //
    : d.setting_clock ? t("devs.clock_soon") : d.no_medium ? t("side.no_medium") : d.connected ? "" : t("devs.not_connected");
  // one action by state: the running sync's progress, a plan waiting for review, an error, else Sync
  const [label, href, title] = syncing ? [t("devs.progress"), `#/devices/${id}/sync/${encodeURIComponent(d.busy)}`, ""]
    : d.waiting && d.connected ? [t("devs.review"), `#/devices/${id}/plan${d.waiting.plan_id ? `/${encodeURIComponent(d.waiting.plan_id)}` : ""}`, ""]
    : d.error ? [t("devs.fix"), `#/devices/${id}`, d.error]
    : d.no_libraries && d.connected ? [t("devs.choose_templates"), `#/devices/${id}/rules`, ""]
    : d.connected ? [t("devs.sync"), `#/devices/${id}/plan`, d.setting_clock ? t("devs.clock_title") : ""] : [t("devs.open"), `#/devices/${id}`, ""];
  const disabled = d.connected && (d.setting_clock || d.ejecting || d.ejected || d.no_medium) && !syncing;
  const upd = !syncing && !d.error ? listUpdateLine(d) : "";
  return html`<div class="dev-row linkrow" data-device-row="${d.id}" data-state="${state}">
    <div class="dev-id"><span class="dev-ico">${deviceIcon(d.type, 24)}</span>
      <div class="meta"><b><a class="stretch" href="#/devices/${id}">${d.name}</a>${d.gentle ? html` <span class="tag" data-gentle>${t("devs.gentle")}</span>` : ""}</b>
        <small data-type-label>${typeLabel(d.type)}${betaBadge(d.type)}${d.kind === "folder" ? html` · ${t("devs.folder_kind")}` : d.kind === "network" ? html` · ${t("devs.network_kind")}` : ""}</small></div></div>
    <div class="dev-state">${badge(state, text)}${upd}</div>
    <div class="dev-space">${d.setting_clock ? html`<span class="bar indet" role="progressbar" aria-label="${t("dev.state.clock")}"><i></i></span>`
      : pct !== null ? html`<span class="bar thin" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></span>`
      : used !== null ? html`<span class="space-bar" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${usedPct}" aria-label="${t("devs.space_label", { name: d.name })}"><i style="width:${usedPct}%"></i></span>` : ""}
      <small data-dev-line>${line}</small></div>
    <div class="dev-last"><small class="muted">${t("devs.last_sync")}</small><span>${d.erased ? `${shortWhen(d.erased.at)} · ${t(`dev.erased_${d.erased.how}`)}`
      : report ? `${shortWhen(report.finished_at || report.started_at)} · ${t(`devs.result.${report.status}`)}` : t("devs.never")}</span></div>
    ${disabled ? html`<span class="btn over" aria-disabled="true" title="${title}" data-dev-action>${label}</span>`
      : html`<a class="btn over" href="${href}" title="${title}" data-dev-action>${label}</a>`}</div>`;
}

// the compact update line: what the automatic update did last, while it runs, or that it failed
function listUpdateLine(d) {
  const u = d.update;
  if (!u || u.state === "none") return "";
  if (u.state === "running") return updateLine("running", t("devs.upd_running"), { compact: true });
  if (u.state === "failed") return updateLine("failed", t("devs.upd_failed"), { compact: true });
  if (u.state === "off") return updateLine("off", t("devs.upd_off"), { compact: true });
  if (u.state === "paused") return updateLine("paused", t("devs.upd_paused"), { compact: true });
  return u.last_at ? updateLine("ok", t("devs.upd_ok", { when: shortWhen(u.last_at) }), { compact: true }) : "";
}

// today: the time only; else the date
function shortWhen(iso) {
  const d = new Date(iso);
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString();
}

// --- device page ----------------------------------------------------------------------------------------------

async function load(ctx) {
  const id = ctx.params.id;
  const [info, devices, types] = await Promise.all([
    get(`/devices/${encodeURIComponent(id)}`), get("/devices"), get("/device-types"),
  ]);
  ctx.state.cfg = info.config;
  ctx.state.unknownType = !!info.unknown_type; // a type this version does not know
  if (ctx.state.unknownType) return;
  ctx.state.libs = info.libraries; // derived from its templates
  ctx.state.lastReport = info.last_report;
  ctx.state.erased = info.erased; // a wipe / format since that sync
  // the newest integrity check (servers cannot be read back) and I/O errors and write speeds
  //, fetched side by side: every tab switch loads them
  // an MTP player has no disk health either
  [ctx.state.integrity, ctx.state.health] = info.network || info.usb_mtp ? [undefined, null] : await Promise.all([
    get(`/devices/${encodeURIComponent(id)}/integrity`).catch(() => null),
    get(`/devices/${encodeURIComponent(id)}/health`).catch(() => null)]);
  // a skipped/failed update older than a full sync that wrote the device does not drive the line
  const superseded = ["failed", "skipped"].includes(info.last_update?.status) && info.update?.state === "ok";
  ctx.state.update = { last: superseded ? { status: "succeeded", finished_at: info.update.last_at } : info.last_update,
    next: info.next_update_check };
  ctx.state.live = devices.registered.find((d) => d.id === id) || null;
  ctx.state.type = types.find((x) => x.type === info.config.type) || null;
  // whose player it is (its manifest): only for a connected disk or folder, a server is not opened for it
  ctx.state.home = info.network || info.usb_mtp || !ctx.state.live ? null
    : await get(`/devices/${encodeURIComponent(id)}/home`).catch(() => null);
}

// the firmware a device type stands for (types with `firmware_siblings`); other types by their label
const FIRMWARE = { hiby: "SonixOS", hiby_compas: "Compás" };
const fwName = (type) => FIRMWARE[type] || typeLabel(type);

async function renderDevice(ctx) {
  if (ctx.params.tab === "sync") { // the old Sync settings tab
    location.replace(`#/devices/${encodeURIComponent(ctx.params.id)}/settings`);
    ctx.params.tab = "settings";
  }
  const tab = TABS.includes(ctx.params.tab) ? ctx.params.tab : "overview";
  await load(ctx);
  const { cfg, live } = ctx.state;
  if (ctx.state.unknownType) {
    return html`${pageHead(cfg.name)}
      <div data-unknown-type>${problemBox({ title: t("dev.unknown_type"), message: t("dev.unknown_type_text", { type: cfg.type }),
        action: html`<button type="button" class="btn danger small" data-action="remove">${t("dev.delete")}</button>` })}</div>`;
  }
  const id = encodeURIComponent(cfg.id);
  const updating = live?.update?.state === "running";
  const syncing = !!live?.busy && !updating && !live.ejecting;
  ctx.state.job = syncing ? await get(`/jobs/${encodeURIComponent(live.busy)}`).catch(() => null) : null;
  const clock = !!live?.setting_clock;
  const client = cfg.type === "client" ? await get(`/clients/${encodeURIComponent(cfg.id)}`).catch(() => null) : null;
  const sdtool = cfg.type === "hiby_compas" ? await get("/settings/compas-sdtool").catch(() => null) : null;
  const body = await TAB_RENDER[tab](ctx);
  ctx.redrawHead = () => { // a tab changed what the header shows (templates saved)
    const head = document.querySelector("[data-device-head]");
    if (head) head.replaceWith(document.createRange().createContextualFragment(String(deviceHeader(ctx, syncing))));
  };
  return html`${deviceHeader(ctx, syncing)}
    ${clock ? html`<div class="dev-note" role="status" data-clock-note><span class="dev-note-text">${icon("clock", { size: 16 })}<b>${t("dev.state.clock")}</b>
      <span>${t("dev.clock_text")}</span></span>${indetBar(t("dev.state.clock"))}</div>` : ""}
    ${live?.lease ? html`<div class="dev-note" role="status" data-lease><span class="dev-note-text">${icon("sync", { size: 16 })}<b>${t("dev.lease_title", { name: live.lease.name })}</b>
      <span>${t("dev.lease_text", { since: when(live.lease.since) })}</span></span></div>` : ""}
    ${live?.firmware_switch && !live.busy ? html`<div class="dev-note" role="status" data-firmware-switch><span class="dev-note-text">${icon("info", { size: 16 })}<b>${t("dev.firmware_title")}</b>
      <span>${t("dev.firmware_text", { old: fwName(cfg.type), new: fwName(live.firmware_switch) })}</span></span>
      <button type="button" class="btn small" data-action="switchFirmware">${t("dev.firmware_switch", { new: fwName(live.firmware_switch) })}</button></div>` : ""}
    ${sdtool?.error === "not found" ? html`<div class="dev-note" role="status" data-sdtool-offer><span class="dev-note-text">${icon("info", { size: 16 })}<b>${t("dev.sdtool_title")}</b>
      <span>${t("dev.sdtool_text")}</span></span>
      <button type="button" class="btn small" data-action="downloadSdtool">${t("sdtool.download")}</button></div>` : ""}
    ${live?.foreign_mounts?.length ? html`<div class="dev-note" role="status" data-foreign-mounts><span class="dev-note-text">${icon("info", { size: 16 })}<b>${t("dev.foreign_title")}</b>
      <span>${t("dev.foreign_text", { paths: live.foreign_mounts.join(", ") })}</span></span>
      <button type="button" class="btn small" data-action="unmountAll" ${live.busy ? "disabled" : ""}>${t("dev.unmount")}</button></div>` : ""}
    ${syncing ? progressStrip(cfg, live.busy, ctx.state.job) : ""}
    ${client ? clientCard(client) : ""}
    ${live?.fs_damaged ? problemBox({ title: t("dev.fs_damaged"), message: t("dev.fs_damaged_text", { error: live.fs_damaged }),
      action: html`<span data-fs-damaged><button type="button" class="btn small" data-action="repairFs" ${live.busy ? "disabled" : ""}>${t("dev.repair")}</button>
        <a class="btn small" href="#/devices/${id}/settings">${t("dev.repair_wipe")}</a></span>` })
    : live?.error ? problemBox({ message: live.error, action: tab === "settings" ? "" : html`<a class="btn small" href="#/devices/${id}/settings">${t("dev.open_settings")}</a>` }) : ""}
    <div class="tabs dev-tabs" role="tablist" aria-label="${t("dev.tabs")}">${TABS.map((x) => (clock && x === "contents"
      ? html`<span class="tab" role="tab" aria-selected="false" aria-disabled="true" title="${t("devs.clock_title")}">${t(`dev.tab.${x}`)}</span>`
      : html`<a class="tab" role="tab" href="#/devices/${id}/${x}" aria-selected="${x === tab}">${t(`dev.tab.${x}`)}</a>`))}</div>
    <div role="tabpanel">${body}</div>`;
}

function clientSession(s) {
  if (!s) return "";
  const p = s.progress || {};
  const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
  return s.status === "ready"
    ? html`<span>${t("dev.cl_sync", { done: p.done, total: p.total, bytes: `${bytes(p.bytes_done || 0)} / ${bytes(p.bytes_total || 0)}` })}</span>
      <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>`
    : html`<span>${t("dev.cl_planning")}</span>`;
}

// a paired Wi-Fi client: what it reported when it paired / last synced, and its sync while it runs
function clientCard(c) {
  const cl = c.client || {}, caps = c.capabilities || {}, st = caps.storage || {};
  const rows = [
    ["app", [cl.app, cl.app_version].filter(Boolean).join(" ")],
    ["os", cl.os],
    ["seen", c.last_seen ? when(c.last_seen) : t("dev.cl_never")],
    ["paired", when(c.created_at)],
    ["formats", (caps.formats || []).join(", ")],
    ["space", st.total ? t("dev.cl_space_val", { free: bytes(st.free || 0), total: bytes(st.total) }) : ""],
  ].filter(([, v]) => v);
  return html`<section class="card ds-card dev-client" data-client-card="${c.device_id}" aria-label="${t("dev.cl_title")}">
    ${c.revoked ? html`<p class="hint warn" data-client-revoked>${t("dev.cl_revoked")}</p>` : ""}
    <dl class="dev-client-facts">${rows.map(([k, v]) => html`<div><dt>${t(`dev.cl_${k}`)}</dt><dd>${v}</dd></div>`)}</dl>
    <div class="dev-client-sync" data-client-sync aria-live="polite">${clientSession(c.session)}</div>
    <a class="btn small ghost" href="#/settings?q=${encodeURIComponent(t("dev.cl_title"))}">${t("dev.cl_manage")}</a></section>`;
}

// the header: type icon, name, status badge, Gentle chip, model line, the automatic update line
// with Update now; Eject and the state's primary action (Show progress / Review plan / Sync)
function deviceHeader(ctx, syncing) {
  const { cfg, live, libs } = ctx.state;
  const id = encodeURIComponent(cfg.id);
  const [state, text] = live ? deviceState(live) : ["neutral", t("dev.state.offline")];
  const connected = !!live?.connected;
  const idle = connected && !live.busy && !live.ejecting && !live.ejected && !live.setting_clock && !live.no_medium;
  const server = live?.kind === "network";
  const model = [ctx.state.type?.display_name || typeLabel(cfg.type), live?.model].filter(Boolean).join(" · ");
  const [label, href, why] = syncing ? [t("dev.show_progress"), `#/devices/${id}/sync/${encodeURIComponent(live.busy)}`, ""]
    : live?.waiting && connected ? [t("dev.review_plan"), `#/devices/${id}/plan${live.waiting.plan_id ? `/${encodeURIComponent(live.waiting.plan_id)}` : ""}`, ""]
    : [t("dev.sync"), idle && libs.length ? `#/devices/${id}/plan` : "",
      live?.setting_clock ? t("devs.clock_title") : live?.no_medium ? t("side.no_medium") : !connected ? t("dev.sync_offline") : !libs.length ? t("dev.no_libraries") : idle ? "" : t("dev.sync_busy")];
  const primary = href ? html`<a class="btn primary" href="${href}" data-sync>${icon("sync", { size: 16, width: 2.2 })}${label}</a>`
    : html`<span class="btn primary" aria-disabled="true" title="${why}" data-sync>${icon("sync", { size: 16, width: 2.2 })}${label}</span>`;
  return html`<div class="dev-head" data-device-head>
    <span class="dev-head-ico">${deviceIcon(cfg.type, 30)}</span>
    <div class="dev-head-meta"><h1>${cfg.name}</h1>
      <div class="dev-head-line">${badge(state, text)}${live?.gentle ? html`<a class="dev-chip" href="#/devices/${id}/settings" title="${t("dev.gentle_title")}" data-gentle>${icon("shield", { size: 14 })}${t("dev.gentle")}</a>` : ""}
        ${feedbackChip(live)}<span data-model-line>${model}${betaBadge(cfg.type)}</span></div>
      ${updateRow(cfg, live, ctx.state.update, syncing)}
      ${live?.offline_since ? html`<div class="dev-upd dev-offline muted" data-offline-since>${icon("clock", { size: 14 })}<span>${t("dev.offline_since", { when: when(live.offline_since) })}</span></div>` : ""}
      ${connected && live.window_wait ? html`<div class="dev-upd muted" data-window-wait>${icon("clock", { size: 14 })} ${windowWaitText(live.window_wait)}</div>` : ""}</div>
    <div class="dev-head-actions">
      ${server ? "" : html`<button class="btn" data-action="eject" ${idle && !cfg.mount_path ? "" : "disabled"} title="${connected ? "" : t("dev.sync_offline")}">${icon("eject", { size: 16 })}${t("dev.eject")}</button>`}
      ${primary}</div></div>`;
}

// the automatic update (round 2 UpdateLine): what it did last or does now, the next check, and a small
// Update now while connected and not running; during a full sync updates wait for it
function updateRow(cfg, live, { last, next } = {}, syncing) {
  const connected = !!live?.connected;
  if (syncing && cfg.update_sync) return html`<div class="dev-upd">${updateLine("paused", t(isCheck(live?.busy_kind) ? "dev.update_waits_check" : "dev.update_waits"), { wrap: true })}</div>`;
  if (!cfg.update_sync) return html`<div class="dev-upd" data-update-status>${updateLine("off", t("dev.update_off"), { wrap: true })}</div>`;
  if (live?.update?.state === "running") return html`<div class="dev-upd" data-update-status>${updateLine("running", t("devs.upd_running"), { wrap: true })}</div>`;
  const at = last && when(last.finished_at || last.started_at);
  const head = !last ? t("dev.update_never")
    : last.status === "failed" ? t("dev.update_failed", { when: at })
    : last.status === "skipped" ? t("dev.update_skipped", { when: at }) : t("dev.updated", { when: at });
  // a skipped update or held-back removals say why
  const why = last && last.status === "skipped" ? last.message.replace(/^update sync skipped: /, "")
    : last && (last.warnings || []).length ? last.warnings.join("; ") : "";
  const state = !last ? "paused" : last.status === "failed" ? "failed" : last.status === "skipped" ? "paused" : "ok";
  // a library change waiting for its slot (update_change_gap_min) says when it runs
  const change = live?.update?.change_at;
  // relative wording; the absolute time stays in the tooltip
  const mins = change ? Math.round((new Date(change).getTime() - Date.now()) / 60000) : 0;
  const waits = !change ? "" : mins >= 1 ? t("dev.update_change_waits", { n: mins }) : t("dev.update_change_soon");
  const line = `${head}${why ? ` (${why})` : ""}${change ? ` · ${waits}`
    : next ? ` · ${t("dev.update_next", { when: when(next) })}` : ""}`;
  const canNow = connected && !live.busy && !live.ejecting && !live.ejected && !live.setting_clock && !live.no_medium && !live.fs_damaged;
  return html`<div class="dev-upd" data-update-status>${updateLine(state, line, { wrap: true, title: change ? `${line} (${when(change)})` : "" })}${canNow
    ? html`<button type="button" class="linkbtn" data-action="updateNow">${t("dev.update_now")}</button>` : ""}</div>`;
}

// a running sync: percent, step, time left; the whole strip opens the job
function progressStrip(cfg, jobId, job) {
  const pct = Math.round((job?.progress || 0) * 100);
  return html`<a class="dev-strip" href="#/devices/${encodeURIComponent(cfg.id)}/sync/${encodeURIComponent(jobId)}" data-progress-strip>
    <span class="dev-strip-top"><b>${pct}%${job?.message ? ` · ${job.message}` : ""}</b><span class="muted">${job?.status === "running" ? etaText(job.eta_s) : ""} →</span></span>
    <span class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${t("dev.show_progress")}"><i style="width:${pct}%"></i></span></a>`;
}

const TAB_RENDER = {
  overview: renderOverview,
  contents: renderContents,
  rules: (ctx) => rulesTab(ctx),
  processors: (ctx) => processorsTab(ctx),
  settings: renderDeviceSettings,
  history: renderHistory,
};

// Overview: capacity split by media kind with Sync at its bottom right, the last full sync.
// Storage health and Audiobooks in progress join it with their own tasks; Format and Delete
// live in Device settings.
async function renderOverview(ctx) {
  const { cfg, live, lastReport, libs } = ctx.state;
  const connected = !!live?.connected;
  const idle = connected && !live.busy && !live.ejecting && !live.ejected && !live.setting_clock && !live.no_medium;
  let space = null, spaceError = null, books = [];
  if (idle) {
    const pos = get(`/devices/${encodeURIComponent(cfg.id)}/positions`).catch(() => []); // the card is extra
    try { space = await get(`/devices/${encodeURIComponent(cfg.id)}/space`); }
    catch (e) { spaceError = e; }
    books = await pos;
  }
  const id = encodeURIComponent(cfg.id);
  const canSync = idle && libs.length;
  const sync = canSync ? html`<a class="btn primary" href="#/devices/${id}/plan" data-cap-sync>${t("dev.sync")}</a>` : "";
  return html`${libs.length ? "" : html`<div class="callout" data-no-libraries style="margin-bottom:16px">${t("dev.no_libraries")} <a href="#/devices/${id}/rules">${t("dev.attach")}</a></div>`}
    <section class="card dev-cap" aria-labelledby="hCap"><div class="dev-cap-head"><h2 id="hCap">${t("dev.capacity_head")}</h2>
      ${space && !space.unknown ? html`<span class="dev-cap-line" data-cap-line>${t("dev.capacity", { used: bytes(space.used), free: bytes(space.free), capacity: bytes(space.capacity) })}</span>` : ""}</div>
      ${storage(space, spaceError, idle, connected, sync)}</section>
    <div class="dev-cards">${ctx.state.erased ? erasedCard(ctx.state.erased, ctx.state.cfg.type) : lastSyncCard(ctx, lastReport)}
      ${ctx.state.integrity === undefined ? "" : healthCard(ctx, idle)}
      ${books.length ? booksCard(cfg.id, books) : ""}
      ${deviceTips(cfg.type, DOCS_BASE)}</div>`;
}

// Audiobooks in progress: the device's position as of the last sync against the library's now
function booksCard(deviceId, books) {
  const state = (b) => b.state === "device_ahead" ? t("dev.books.device_ahead", { diff: duration(b.diff_s) })
    : b.state === "library_ahead" ? t("dev.books.library_ahead")
    : b.state === "in_sync" ? t("dev.books.in_sync", { pct: Math.round((100 * b.position_s) / (b.duration_s || 1)) })
    : `${Math.round((100 * b.position_s) / (b.duration_s || 1))}%`;
  const thumbSrc = (b) => `/api/v1/devices/${encodeURIComponent(deviceId)}/contents/thumb?${new URLSearchParams({ key: b.key })}`;
  return html`<section class="card dev-card dev-books" data-books aria-labelledby="hBooks"><div class="dev-card-head"><h2 id="hBooks">${t("dev.books.head")}</h2><span class="muted">${t("dev.books.both_ways")}</span></div>
    ${books.map((b) => html`<div class="dev-book" data-book="${b.key}" data-book-state="${b.state}">${thumb(thumbSrc(b))}
      <span class="dev-book-main"><span class="dev-book-title" title="${b.title}${b.author ? ` · ${b.author}` : ""}">${b.title}${b.author ? html` <span class="muted">· ${b.author}</span>` : ""}</span>
        <span class="dev-book-bar"><span style="width:${Math.min(100, (100 * b.position_s) / (b.duration_s || 1)).toFixed(1)}%"></span></span></span>
      <span class="dev-book-state">${state(b)}</span></div>`)}</section>`;
}

/** The device after its pending plan, as the plan's After line reads it: `planned` is the bytes the plan
 * needs incl. its reserve (negative when it frees more); without a known capacity only what SyncMusic wrote plus
 * the plan's net bytes. */
function planned(space) {
  if (space.planned === null || space.planned === undefined) return "";
  const text = space.unknown
    ? t("dev.planned_used", { used: bytes(Math.max(0, (space.used || 0) + space.planned - (space.planned_reserve || 0))) })
    : t("dev.planned_text", { used: bytes(Math.max(0, space.capacity - (space.free - space.planned))), free: bytes(Math.max(0, space.free - space.planned)) });
  return html`<div class="muted" data-planned>${t("dev.planned")}: ${text}</div>`;
}

function storage(space, error, idle, connected, sync) {
  // a 409 not_connected (ejected, unplugged meanwhile) is not an error, just no storage to show
  if (!idle || error?.code === "not_connected") return html`<p class="muted" data-space-unavailable>${t(connected ? "dev.space_unavailable" : "dev.space_offline")}</p>`;
  if (error) return errorBox(error, "");
  // FTP without a quota: no capacity to draw, only what SyncMusic wrote
  if (space.unknown) return html`<div data-space-unknown>${t("dev.space_unknown", { used: bytes(space.used || 0) })}</div>
    ${planned(space)}`;
  const k = space.by_kind || {};
  const parts = { music: k.music, audiobook: k.audiobook, podcast: k.podcast, video: k.video, others: k.others, other: k.other ?? space.used };
  return html`${capacityBar({ capacity: space.capacity || 0, parts, label: t("dev.capacity", { used: bytes(space.used), free: bytes(space.free), capacity: bytes(space.capacity) }), after: sync })}
    ${planned(space)}
    ${space.db ? html`<div>${dbSizeLine(space.db.size, space.db.limit, space.db.level)}</div>` : ""}
    ${spaceMap(space)}${leftOff(space.left_off)}`;
}

// The space map: SyncMusic's bytes per library and per rule from the manifest, and what the
// capacity bar's "other" is made of
function spaceMap(space) {
  if (!space.by_library) return "";
  const rows = (list, noName) => html`<ul class="space-rows">${list.map((r) => html`<li data-space-row="${r.id}">
    <span class="space-name" title="${r.name || noName}">${r.name || noName}</span>
    <span class="space-bar-mini"><i style="width:${(100 * r.bytes / Math.max(1, space.used)).toFixed(1)}%"></i></span>
    <span class="space-num">${bytes(r.bytes)} · ${t("dev.map.items", { n: r.items })}</span></li>`)}</ul>`;
  return html`<details class="space-map" data-space-map><summary>${t("dev.map.head")}</summary>
    ${space.by_library.length ? html`<h3>${t("dev.map.by_library")}</h3>${rows(space.by_library, "")}
      <h3>${t("dev.map.by_rule")}</h3>${rows(space.by_rule, t("dev.map.no_rule"))}` : ""}
    <p class="muted" data-space-rest>${t("dev.map.rest", { db: bytes(space.database || 0), foreign: bytes(space.foreign || 0), reserve: bytes(space.reserve || 0) })}</p></details>`;
}

// What the last plan left off and why: grouped by rule, with the plan's advice
function leftOff(l) {
  if (!l) return "";
  return html`<div class="callout warn space-left" data-left-off><strong>${t("dev.left.head")}</strong>
    ${l.does_not_fit ? html`<p>${t("dev.left.short", { bytes: bytes(l.short_by) })}</p>` : ""}
    ${l.dropped_total ? html`<p>${t("dev.left.dropped", { n: l.dropped_total, bytes: bytes(l.dropped_bytes) })}</p>` : ""}
    ${l.limit_left_out ? html`<p>${t("dev.left.limit", { n: l.limit_left_out, bytes: bytes(l.limit_left_out_bytes) })}</p>` : ""}
    ${l.per_rule.length ? html`<ul class="space-rows">${l.per_rule.map((r) => html`<li data-left-rule="${r.rule_id}"><span class="space-name" title="${r.name}">${r.name}</span>
      <span class="space-num">${r.dropped_items ? t("dev.left.lost", { n: r.dropped_items, bytes: bytes(r.dropped_bytes) }) : t("dev.left.wants", { bytes: bytes(r.bytes) })}</span></li>`)}</ul>` : ""}
    ${l.suggestions.length ? html`<p><strong>${t("dev.left.advice")}</strong></p><ul data-left-advice>${l.suggestions.map((x) => html`<li>${x}</li>`)}</ul>` : ""}</div>`;
}

const STATUS_BADGE = { succeeded: "ok", partial: "warn", failed: "error", cancelled: "neutral", running: "busy", skipped: "warn" };

// "9 min" / "38 s" between two timestamps
function took(r) {
  if (!r.finished_at || !r.started_at) return "";
  const s = Math.max(0, Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 1000));
  return s < 60 ? t("dev.took_s", { n: s }) : t("dev.took_min", { n: Math.round(s / 60) });
}

// Storage health: I/O errors in 30 days, the write speed of the last full syncs as a trend and
// the "Storage may be failing" warning; the last integrity check -
// what it found per group, the space other files hold, Delete broken files - and Run integrity check (idle only)
function healthCard(ctx, idle) {
  const last = ctx.state.integrity;
  const r = last?.report;
  const list = (key) => {
    const rows = r?.[key] || [];
    if (!rows.length) return "";
    return html`<details class="dev-health-group" data-integrity="${key}"><summary>${t(`dev.integrity.${key}`)} · ${rows.length}</summary>
      <ul>${rows.slice(0, 20).map((x) => html`<li class="mono">${x.path}${x.reason ? html` <span class="muted">- ${x.reason}</span>` : ""}</li>`)}
      ${rows.length > 20 ? html`<li class="muted">${t("dev.integrity_more", { n: rows.length - 20 })}</li>` : ""}</ul></details>`;
  };
  const problems = r ? r.missing.length + r.broken.length : 0;
  const state = !last ? "" : last.status !== "succeeded" ? badge("error", t("dev.integrity_stopped"))
    : problems ? badge("warn", t("dev.integrity_attention")) : badge("ok", t("dev.ok"));
  const h = ctx.state.health;
  const max = Math.max(...(h?.speeds || []).map((x) => x.mb_s), 0.01);
  const vitals = h ? html`<div class="dev-health-vitals">
      <div data-io-errors="${h.io_errors}"><small class="muted">${t("dev.io_errors")}</small><b>${h.io_errors}</b>
        ${h.last_io_error ? html`<small class="muted">${t("dev.io_last", { when: when(h.last_io_error.at), detail: h.last_io_error.detail })}</small>` : ""}</div>
      <div data-speed><small class="muted">${t("dev.speed")}</small>
        ${h.last_mb_s === null ? html`<small class="muted">${t("dev.speed_none")}</small>`
          : html`<b>${h.usual_mb_s ? t("dev.speed_usual", { last: h.last_mb_s, usual: h.usual_mb_s }) : t("dev.speed_value", { last: h.last_mb_s })}</b>
            <span class="dev-trend" role="img" aria-label="${t("dev.speed_trend")}: ${h.speeds.map((x) => x.mb_s).join(", ")} MB/s">${h.speeds.map((x) =>
              html`<i style="height:${Math.max(8, Math.round((x.mb_s / max) * 100))}%" title="${when(x.at)} · ${x.mb_s} MB/s"></i>`)}</span>`}</div></div>
    ${h.warning ? html`<div class="callout warn" data-health-warning><b>${t("dev.health_warning")}</b> - ${h.warning}. ${t("dev.health_advice")}
      <div class="toolbar"><button type="button" class="btn small" data-action="healthAck">${t("dev.health_ack")}</button></div></div>`
      : h.acknowledged_at && h.io_errors ? html`<div class="muted" data-health-acked>${t("dev.health_acked_at", { when: when(h.acknowledged_at) })}</div>` : ""}
    ${h.errors?.length ? html`<details class="dev-health-group" data-io-list><summary>${t("dev.io_list")} · ${h.errors.length}</summary>
      <ul>${h.errors.map((x) => html`<li><span class="muted">${when(x.at)}</span> <span class="mono">${x.detail}</span>${x.job_id ? html` <a class="btn small ghost" data-io-job href="#/jobs/${encodeURIComponent(x.job_id)}">${t("dev.io_job")}</a>` : ""}</li>`)}</ul>
      <div class="toolbar"><button type="button" class="btn small danger" data-action="healthClear">${t("dev.io_clear")}</button></div></details>` : ""}` : "";
  const top = h?.warning ? badge("warn", t("dev.integrity_attention")) : "";
  return html`<section class="card dev-card" data-health>
    <div class="dev-card-head"><h2>${t("dev.health")}</h2>${top || state}</div>
    ${vitals}
    ${!last ? html`<p class="muted">${t("dev.integrity_never")}</p>`
      : html`<div class="muted" data-integrity-when>${t("dev.integrity_when", { when: when(last.finished_at) })}</div>
        ${last.status !== "succeeded" ? html`<div class="callout" data-integrity-error>${t("dev.integrity_failed", { error: last.error || "" })}</div>`
          : html`<div data-integrity-summary>${problems || r.foreign.length || r.orphans.length ? r.summary : t("dev.integrity_ok")}</div>
            ${list("missing")}${list("broken")}${list("foreign")}${list("orphans")}
            ${r.wasted_bytes ? html`<div class="muted">${t("dev.integrity_wasted", { size: bytes(r.wasted_bytes) })}</div>` : ""}`}`}
    <div class="dev-health-acts">
      <label class="ds-check"><input type="checkbox" id="integrityDeep"><span>${t("dev.integrity_deep")}</span></label>
      <div class="toolbar">${r?.broken?.length ? html`<button type="button" class="btn small danger" data-action="integrityFix" ${idle ? "" : "disabled"}>${t("dev.integrity_fix", { n: r.broken.length })}</button>` : ""}
        <button type="button" class="btn small" data-action="integrityRun" ${idle ? "" : "disabled"}>${t("dev.integrity_run")}</button></div></div></section>`;
}

// a Wipe / Format after the last full sync: the device is empty, its old report no longer describes it
function erasedCard(e, type) {
  const text = e.how === "wiped" ? t("dev.erased_wiped_text", { when: when(e.at), n: e.removed ?? 0 })
    : t("dev.erased_formatted_text", { when: when(e.at) });
  return html`<section class="card dev-card" data-last-sync="${e.how}"><div class="dev-card-head"><h2>${t("dev.last_full_sync")}</h2>${badge("neutral", t(`dev.erased_${e.how}`))}</div>
    <p class="muted" data-erased>${text}</p>${hibyReboot(type)}</section>`;
}

// the Last full sync card: badge, when · how long · written, the counts as tiles, one sentence (the reverse sync, the
// first failure or the message), the gentle-mode suggestion after a storage failure, Full report
function lastSyncCard(ctx, r) {
  const { cfg } = ctx.state;
  const id = encodeURIComponent(cfg.id);
  const head = html`<div class="dev-card-head"><h2>${t("dev.last_full_sync")}</h2>${r ? badge((r.warnings || []).length && r.status === "succeeded" ? "warn" : STATUS_BADGE[r.status] || "neutral", t(`devs.result.${r.status}`)) : ""}</div>`;
  if (!r) return html`<section class="card dev-card" data-last-sync>${head}<p class="muted">${t("dev.never_synced")}</p></section>`;
  const c = r.counts || {};
  const fails = (r.failures || []).length;
  const tiles = [[`+${c.add || 0}`, t("dev.c.added")], [String(c.replace || 0), t("dev.c.replaced")], [String(c.update_meta || 0), t("dev.c.tags")],
    [c.remove ? `−${c.remove}` : "0", t("dev.c.removed")], ...(fails ? [[String(fails), t("dev.c.failed")]] : [])];
  const rev = r.reverse;
  const f = r.failures?.[0];
  const note = f ? `${[f.step, f.item || f.title || f.ref].filter(Boolean).join(" · ")}: ${f.error}`
    : rev && (rev.plays_submitted || rev.ratings_pushed || rev.positions_pushed) ? t("dev.reverse_sent", {
      plays: t("count.play", { n: rev.plays_submitted || 0 }), ratings: t("count.rating", { n: rev.ratings_pushed || 0 }), positions: t("count.position", { n: rev.positions_pushed || 0 }) })
    : r.message;
  const facts = [when(r.finished_at || r.started_at), took(r), r.bytes_written ? t("dev.written", { size: bytes(r.bytes_written) }) : ""].filter(Boolean).join(" · ");
  const report = r.job_id ? `#/devices/${id}/sync/${encodeURIComponent(r.job_id)}` : `#/devices/${id}/history`;
  return html`<section class="card dev-card" data-last-sync="${r.status}">${head}
    <div class="muted" data-last-when>${facts}</div>
    <div class="count-tiles">${tiles.map(([v, l]) => html`<div class="count-tile"><b>${v}</b><small>${l}</small></div>`)}</div>
    ${note ? html`<div class="dev-card-note" data-last-note>${note}</div>` : ""}
    ${(r.warnings || []).map((w) => html`<div class="callout" data-report-warning>${w}</div>`)}
    ${r.status === "succeeded" || r.status === "partial" ? hibyReboot(cfg.type) : ""}
    ${r.suggest_gentle && r.status === "failed" && !cfg.sync?.gentle_write ? suggestionBox({
      iconName: "shield", message: html`<b>${t("dev.gentle_why")}</b> ${t("dev.gentle_help")}`, attrs: html`data-suggest-gentle`,
      action: html`<button class="btn small" data-action="gentleOn">${t("prog.gentle_on")}</button>`,
    }) : ""}
    <div><a class="btn small" href="${report}" data-full-report>${t("dev.full_report")}</a></div></section>`;
}

export function reportSummary(r) {
  const counts = Object.entries(r.counts || {}).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(", ");
  return html`<div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
      <b>${when(r.finished_at || r.started_at)}</b><span>${r.kind === "update" ? html`${badge("neutral", t("dev.update_kind"))} ` : ""}${badge((r.warnings || []).length && r.status === "succeeded" ? "warn" : STATUS_BADGE[r.status] || "neutral", r.status)}</span></div>
    ${counts ? html`<div class="muted" style="margin-top:6px">${counts}${r.bytes_written ? ` · ${bytes(r.bytes_written)}` : ""}</div>` : ""}
    ${r.message ? html`<div style="margin-top:6px;overflow-wrap:anywhere">${r.message}</div>` : ""}
    ${(r.warnings || []).map((w) => html`<div class="callout" data-report-warning style="margin-top:6px;overflow-wrap:anywhere">${w}</div>`)}
    ${r.job_id ? html`<a class="btn small" style="margin-top:6px" data-report-log href="#/logs?${new URLSearchParams({ job: r.job_id, level: "DEBUG" })}">${t("dev.report_log")}</a>` : ""}
    ${(r.failures || []).length ? html`<details style="margin-top:6px"><summary>${t("dev.failures", { n: r.failures.length })}</summary>
      <ul>${r.failures.map((f) => html`<li data-failure><b>${f.item || f.title || f.ref || ""}</b>${f.step ? html` · ${f.step}` : ""}: ${f.error || f.message || JSON.stringify(f)}
        ${f.path || f.ref ? html`<div class="muted mono" style="overflow-wrap:anywhere">${[f.ref, f.path].filter(Boolean).join(" → ")}</div>` : ""}</li>`)}</ul></details>` : ""}`;
}

// Device settings: when connected, the type's own settings (iPod: clock + time zone), reverse sync (only
// when the device and one of its libraries support it), and Format with the file system choice.
function reverseSupport(type, libraries, libTypes, attachedIds) {
  const c = type.capabilities;
  if (!(c.supports_playcounts_read || c.supports_ratings_read || c.supports_positions_read)) return "device";
  const caps = Object.fromEntries(libTypes.map((x) => [x.type, x.capabilities]));
  const ok = libraries.some((l) => attachedIds.includes(l.id) && l.type !== "folder"
    && (caps[l.type]?.supports_scrobble || caps[l.type]?.supports_ratings || caps[l.type]?.supports_positions));
  return ok ? null : "library";
}

// the type's own card: which of its settings it shows; the rest go to "Files on the device"
const TYPE_CARD = {
  ipod: ["time_sync", "timezone"],
  rockbox: ["playlist_dir", "play_min_fraction", "play_min_seconds", "keep_rotated"],
  hiby: ["split_genres", "genre_separators"],
  ftp: ["host", "port", "username", "password", "base_path", "passive", "tls", "timeout_s", "quota_gb"],
};
// a type's own i18n name for a shared field name
const TYPE_KEYS = { android: { base_path: "android_base_path" } };
const GENTLE = ["gentle_batch_files", "gentle_pause_s", "gentle_rate_mb_s"];

function orphansBody(orphans) {
  return orphans === null ? html`<p class="muted">${t("dev.orphans_offline")}</p>`
    : orphans.length ? html`<p>${t("dev.orphans_n", { n: orphans.length })}</p>
      <div><button type="button" class="btn danger" data-action="removeOrphans">${t("dev.orphans_remove")}</button></div>`
    : html`<p class="muted">${t("dev.orphans_none")}</p>`;
}

async function renderDeviceSettings(ctx) {
  const { cfg, type, live } = ctx.state;
  if (!type) return errorBox({ message: `unknown device type ${cfg.type}` }, "");
  const [libraries, libTypes, appDefaults] = await Promise.all([get("/libraries"), get("/library-types"), get("/settings").catch(() => null)]);
  ctx.state.appDefaults = appDefaults; // where each sync setting comes from (632.3)
  const rockbox = type.type === "rockbox";
  const own = TYPE_CARD[type.type] || [];
  // the Rockbox play log has its own control: it is applied at once
  const typeForm = own.length ? schemaForm(type.settings_schema, cfg.settings, { prefix: "d-", only: own }) : null;
  const skip = [...own, "enable_play_log"];
  const settings = schemaForm(type.settings_schema, cfg.settings, { prefix: "d-", skip, keys: TYPE_KEYS[type.type] });
  const hasFiles = Object.keys(type.settings_schema?.properties || {}).some((k) => !skip.includes(k));
  const noReverse = reverseSupport(type, libraries, libTypes, ctx.state.libs);
  const reverse = noReverse ? null : schemaForm(type.reverse_sync_schema, cfg.reverse_sync, { prefix: "r-" });
  const canGentle = !type.network && "gentle_write" in (type.sync_schema?.properties || {});
  const gentle = canGentle ? schemaForm(type.sync_schema, cfg.sync, { prefix: "s-", only: GENTLE }) : null;
  const accounts = accountForms(cfg, libraries, libTypes, ctx.state.libs);
  ctx.state.forms = { typeForm, settings, reverse, gentle, accounts };
  const idle = !!live?.connected && !live.busy && !live.ejecting && !live.ejected && !live.setting_clock && !live.no_medium;
  let db = null;
  if (idle && type.type === "ipod") {
    try { db = (await get(`/devices/${encodeURIComponent(cfg.id)}/space`)).db; } catch { db = null; }
  }
  // orphans (gone from their library) load on their own after the first paint: never block the forms
  ctx.state.orphans = null;
  if (idle) {
    const want = cfg.id;
    get(`/devices/${encodeURIComponent(cfg.id)}/orphans`).then((rows) => rows, () => null).then((rows) => {
      if (ctx.state.cfg?.id !== want) return;
      ctx.state.orphans = rows;
      const card = document.querySelector("#orphansCard [data-orphans-body]");
      if (card) setHtml(card, orphansBody(rows));
    });
  }
  let profile = null; // device profile; detection only while connected
  try { profile = await get(`/devices/${encodeURIComponent(cfg.id)}/profile`); } catch { profile = null; }
  let playLog = null;
  if (rockbox) {
    try { playLog = await get(`/devices/${encodeURIComponent(cfg.id)}/play-log`); } catch { playLog = null; }
  }
  const what = t(type.type === "ipod" ? "dev.rename_what_ipod" : type.network ? "dev.rename_what_server" : cfg.mount_path ? "dev.rename_what_folder" : "dev.rename_what_disk");
  const renamable = type.type === "ipod" || (!type.network && !cfg.mount_path); // a folder or a server has no name of its own
  const suggest = ctx.state.lastReport?.suggest_gentle && !cfg.sync.gentle_write;
  const id = encodeURIComponent(cfg.id);
  const sync = syncTab(ctx, updateRow(cfg, live, ctx.state.update, !!live?.busy));
  // one Settings tab: what goes on it and how files are processed link to their tabs, then
  // Sync behaviour and Device under one Save / Discard bar, with a search over every card
  return html`<div class="ds-search" data-nosave><input type="search" data-ds-search placeholder="${t("dev.set_search_ph")}" aria-label="${t("dev.set_search")}" autocomplete="off" spellcheck="false">
      <p class="muted" data-ds-none hidden>${t("dev.set_search_none")}</p></div>
    <div class="dev-settings ds-links">
      <a class="card ds-card ds-link" href="#/devices/${id}/rules"><b>${t("dev.set_what")}</b><small>${t("dev.set_what_sub")}</small></a>
      <a class="card ds-card ds-link" href="#/devices/${id}/processors"><b>${t("dev.set_how")}</b><small>${t("dev.set_how_sub")}</small></a>
      ${hasTips(cfg.type) ? html`<a class="card ds-card ds-link" href="#/devices/${id}/overview" data-tips-link><b>${t("tips.link")}</b><small>${t("tips.link_sub")}</small></a>` : ""}</div>
    <div data-devset>
    <h2 class="ds-part" id="dsPartSync">${t("dev.set_part_sync")}</h2>
    ${sync}
    <h2 class="ds-part" id="dsPartDevice">${t("dev.set_part_device")}</h2>
    <form class="dev-settings" data-settings-form>
    <section class="card ds-card" id="nameCard"><h2>${t("dev.set_name")}</h2>
      <div class="field"><label for="dsName">${t("dev.set_name_label")}</label><input id="dsName" value="${cfg.name}" maxlength="64" autocomplete="off"></div>
      ${renamable ? html`<label class="ds-check"><input type="checkbox" id="dsRename" ${idle ? "" : "disabled"}>
        <span class="ds-label">${t("dev.set_rename")}<small>${idle ? t("dev.rename_help", { what }) : t("dev.set_rename_offline")}</small></span></label>` : ""}</section>
    ${typeForm || db || playLog ? html`<section class="card ds-card" id="typeCard" data-type-card="${type.type}"><h2>${t(`dev.set_type.${type.type}`) === `dev.set_type.${type.type}` ? typeLabel(type.type) : t(`dev.set_type.${type.type}`)}</h2>
      ${db ? html`<div id="dbSize">${dbSizeLine(db.size, db.limit, db.level)}${dbSizeBar(db.size, db.limit, db.level)}</div>` : ""}
      ${typeForm ? typeForm.html : ""}
      ${playLog ? playLogBlock(playLog, live) : ""}
      ${type.network ? html`<div class="ds-row"><button type="button" class="btn" data-action="ftpTest">${t("dev.set_test")}</button>
        <span class="ds-test" data-ftp-result role="status"></span></div>` : ""}</section>` : ""}
    ${hasFiles ? html`<section class="card ds-card" id="filesCard"><div><h2>${t("dev.settings_device")}</h2><p class="ds-sub">${t("dev.set_files_sub")}</p></div>${settings.html}
      ${type.type === "flashdrive" && (cfg.mount_path || live?.mounted_at) && idle ? pickerButton("dev-flash", { mount: cfg.mount_path || live.mounted_at }) : ""}</section>` : ""}
    ${gentle ? html`<section class="card ds-card" id="gentleCard" aria-labelledby="hGentle">
      <label class="ds-check ds-switch"><span class="ds-label"><b id="hGentle">${t("dev.set_gentle")}</b><small>${t("dev.set_gentle_sub")}</small></span>
        <input type="checkbox" id="dsGentle" ${cfg.sync.gentle_write ? "checked" : ""}></label>
      ${suggest ? html`<div class="ds-hint" data-gentle-suggest>${icon("info", { size: 14 })}${t("dev.set_gentle_suggest")}</div>` : ""}
      <div data-gentle-fields ${cfg.sync.gentle_write ? "" : "hidden"}>${gentle.html}<p class="ds-sub">${t("dev.set_gentle_estimate")}</p></div></section>` : ""}
    <section class="card ds-card" id="reverseCard"><h2>${t("dev.settings_reverse")} ${helpTip("reverse")}</h2>
      ${reverse ? reverse.html : html`<p class="muted" data-no-reverse="${noReverse}">${t(`dev.reverse_none_${noReverse}`)}</p>`}</section>
    ${accountsCard(cfg, accounts)}
  </form></div>
  <div class="dev-settings ds-after">
    ${profileCard(profile)}
    <section class="card ds-card" id="orphansCard"><div><h2>${t("dev.orphans")}</h2><p class="ds-sub">${t("dev.orphans_help")}</p></div>
      <div data-orphans-body>${idle ? html`<p class="muted" data-orphans-checking>${t("dev.orphans_checking")}</p>` : orphansBody(null)}</div></section>
    ${dangerZone(cfg, type, live, idle, ctx.state.home)}</div>`;
}

// the library account the device signs in as, per attached library that keeps data per user:
// the library's main account, one of its accounts, or "+ Add account…" (the library's account dialog)
function accountForms(cfg, libraries, libTypes, attachedIds) {
  const out = [];
  for (const lib of libraries) {
    const lt = libTypes.find((x) => x.type === lib.type);
    if (!attachedIds.includes(lib.id) || !lt?.account_fields?.length) continue;
    out.push({ lib, type: lt, current: cfg.library_accounts?.[lib.id] || "" });
  }
  return out;
}

function accountsCard(cfg, accounts) {
  return html`<section class="card ds-card" id="accountsCard"><div><h2>${t("dev.accounts")}</h2><p class="ds-sub">${t("dev.accounts_sub")}</p></div>
    ${accounts.length ? accounts.map(({ lib, current }) => html`<div class="field" data-account="${lib.id}">
      <label for="acc-${lib.id}">${lib.name}</label>
      <select id="acc-${lib.id}" data-account-pick="${lib.id}">
        <option value="" ${current ? "" : "selected"}>${t("dev.account_main")}</option>
        ${Object.entries(lib.accounts || {}).map(([aid, acc]) => html`<option value="${aid}" ${aid === current ? "selected" : ""}>${acc.name}${acc.settings?.username ? ` (${acc.settings.username})` : ""}</option>`)}
        <option value="__add">${t("dev.account_add")}</option></select>
      <a class="ds-sub" data-browse-as href="#/libraries/${encodeURIComponent(lib.id)}/browse${current ? `?as=${encodeURIComponent(current)}` : ""}">${t("dev.account_browse")}</a></div>`)
      : html`<p class="muted" data-no-accounts>${t("dev.accounts_none")}</p>`}</section>`;
}

/** The accounts card's values: library id → account id, null for the main account. */
function readAccounts(ctx, root) {
  const out = {};
  for (const { lib } of ctx.state.forms.accounts || []) {
    const v = root.querySelector(`[data-account-pick="${lib.id}"]`)?.value;
    if (v !== undefined && v !== "__add") out[lib.id] = v || null;
  }
  return out;
}

// "+ Add account…": the library's account dialog; the new account is picked, the old choice comes back otherwise
async function addAccount(select, ctx) {
  const entry = ctx.state.forms.accounts.find((a) => a.lib.id === select.dataset.accountPick);
  const r = await accountDialog(entry.lib, entry.type);
  if (!r) { select.value = entry.current; return; }
  entry.lib = r.lib;
  const acc = r.lib.accounts[r.id];
  const opt = document.createElement("option");
  opt.value = r.id;
  opt.textContent = acc.name + (acc.settings?.username ? ` (${acc.settings.username})` : "");
  select.querySelector('option[value="__add"]').before(opt);
  select.value = r.id;
  // picked and stored at once: the library change re-renders this page
  const id = encodeURIComponent(ctx.state.cfg.id);
  const picks = { ...(ctx.state.cfg.library_accounts || {}), [entry.lib.id]: r.id };
  await patch(`/devices/${id}`, { library_accounts: picks });
  ctx.rerender(); // after the library change's re-render, which may still show the old pick
}

// Unmount everywhere: list every mount (ours and other programs'), confirm once naming them, unmount; a busy
// mount names its holders and offers a lazy detach as an explicit second step. True when unmounted.
async function unmountEverywhere(cfg, ask = true) {
  const id = encodeURIComponent(cfg.id);
  const { mounts } = await get(`/devices/${id}/mounts`);
  if (!mounts.length) { toast(t("dev.unmount_none", { name: cfg.name })); return true; }
  if (ask) {
    const body = html`<p>${t("dev.unmount_list")}</p><ul class="mono" data-unmount-list>${mounts.map((m) =>
      html`<li>${m.path} <span class="muted">(${m.partition}${m.ours ? "" : html`, ${t("dev.unmount_other")}`})</span></li>`)}</ul>`;
    if (!(await confirm({ title: t("dev.unmount_title", { name: cfg.name }), body, ok: t("dev.unmount") }))) return false;
  }
  try {
    await post(`/devices/${id}/unmount-all`);
  } catch (e) {
    if (e.code !== "mount_busy") throw e;
    const lazy = await confirm({ title: t("dev.unmount_busy_title", { name: cfg.name }),
      message: t("dev.unmount_busy_text", { error: e.message }), ok: t("dev.unmount_lazy"), danger: true });
    if (!lazy) return false;
    await post(`/devices/${id}/unmount-all?lazy=true`);
  }
  return true;
}

// players that are wiped instead of formatted (their own system files stay; adds the HiBy)
const WIPE_TYPES = new Set(["rockbox", "hiby", "hiby_compas", "android"]);
const wipeText = (type) => ({ hiby: "dev.wipe_text_hiby", hiby_compas: "dev.wipe_text_compas", android: "dev.wipe_text_android" })[type] ?? "dev.wipe_text";
const formatText = (type) => (type === "hiby" ? "dev.format_text_hiby" : "dev.format_text");

// the danger zone: one row per action with its consequence - Wipe for Rockbox (files only,
// /.rockbox stays), Format for iPods and USB drives, Bind for a folder whose disk is plugged in, Delete for every
// device; each asks for the typed name
function dangerZone(cfg, type, live, idle, home) {
  const row = (attrs, title, text, button) => html`<div class="dz-row" ${attrs}><div class="dz-text"><b>${title}</b><span>${text}</span></div>${button}</div>`;
  const rows = [];
  const other = home?.home && !home.is_home;
  const homeName = other ? (home.home.name || home.home.id) : "";
  if (home?.home) {
    rows.push(row(html`data-dz="home"`, t("dev.dz_home"),
      other ? t("dev.home_other", { home: homeName, since: home.home.since ? ` (since ${new Date(home.home.since).toLocaleDateString()})` : "" }) : t("dev.home_this"),
      other ? html`<button type="button" class="btn" data-action="makeHome" ${idle ? "" : "disabled"}>${t("dev.make_home")}</button>` : ""));
  }
  // another instance's player: the home-only buttons say why they are off
  const homeOnly = other ? html`disabled title="${t("dev.not_home_only", { home: homeName })}"` : "";
  const off = other ? homeOnly : idle ? "" : "disabled";
  if (WIPE_TYPES.has(type.type)) {
    rows.push(row(html`data-dz="wipe"`, t("dev.dz_wipe"), t(wipeText(type.type)),
      html`<button type="button" class="btn danger" data-action="wipe" ${off}>${t("dev.wipe")}</button>`));
  }
  // the HiBy has both: Wipe keeps the player's databases, Format starts from an empty volume
  if ((!WIPE_TYPES.has(type.type) || type.type === "hiby") && !type.network && !cfg.mount_path) {
    rows.push(row(html`data-dz="format"`, t("dev.dz_format"), html`${t(formatText(type.type))} <span class="muted">${fsSummary(type)}</span>`,
      html`<button type="button" class="btn danger" data-action="format" ${off}>${t("dev.format")}</button>`));
  }
  if (!type.network && !cfg.mount_path) {
    rows.push(row(html`data-dz="unmount"`, t("dev.dz_unmount"), t("dev.unmount_text"),
      html`<button type="button" class="btn" data-action="unmountAll" ${live?.connected && idle ? "" : "disabled"}>${t("dev.unmount")}</button>`));
  }
  if (cfg.mount_path && live?.bindable_disk) {
    rows.push(row(html`data-dz="bind"`, t("dev.dz_bind"), t("dev.bind_text", { disk: live.bindable_disk }),
      html`<button type="button" class="btn" data-action="bind">${t("dev.bind", { disk: live.bindable_disk })}</button>`));
  }
  rows.push(row(html`data-dz="delete"`, t("dev.dz_delete"), t("dev.delete_text"),
    html`<button type="button" class="btn danger" data-action="remove" ${live?.busy ? "disabled" : ""}>${t("dev.delete")}</button>`));
  const why = type.usb_mtp ? t("dev.format_mtp") : type.network ? t("dev.format_server") : cfg.mount_path && !WIPE_TYPES.has(type.type) ? t("dev.format_folder") : "";
  return html`<section class="card dangerzone" id="formatZone" aria-labelledby="hDanger"><h2 id="hDanger">${t("common.danger")}</h2>
    ${why ? html`<p class="dz-note">${why}</p>` : ""}${rows}
    <p class="dz-note muted" data-identity>${t("dev.identity")}: <span class="mono">${identity(cfg, live)}</span></p></section>`;
}

// where the device is: the server's address, the folder, or the disk's USB serial / file system id
function identity(cfg, live) {
  const st = cfg.settings || {}, ident = cfg.identity || {};
  if (live?.kind === "network") return `${st.host}:${st.port ?? 21}${st.base_path || "/"}`;
  return cfg.mount_path || [ident.usb_serial, ident.fs_uuid].filter(Boolean).join(" · ") || "-";
}

// the Rockbox play log inside the Rockbox card: applied at once, not by Save
function playLogBlock(pl, live) {
  const busy = !!live?.busy || !!live?.ejecting || !!live?.ejected;
  const state = pl.state === null ? t("dev.playlog_state_offline") : t(`dev.playlog_state_${pl.state}`);
  return html`<div class="ds-block" id="playLogCard" data-setting="${pl.setting ? "on" : "off"}" data-state="${pl.state ?? ""}">
    <div class="ds-label"><b>${t("dev.playlog")}</b><small>${t("dev.playlog_help")}</small></div>
    <p>${t("dev.playlog_now", { state })} <span class="muted">${t(pl.setting ? "dev.playlog_setting_on" : "dev.playlog_setting_off")}</span></p>
    <div><button type="button" class="btn" data-action="playLog" data-on="${pl.setting ? "0" : "1"}" ${busy ? "disabled" : ""}>${t(pl.setting ? "dev.playlog_switch_off" : "dev.playlog_switch_on")}</button></div></div>`;
}

function fsSummary(type) {
  const fss = type.format_filesystems || [];
  if (fss.length > 1) return fss.map((f) => (f.available ? f.label : t("dev.format_unavailable", { fs: f.label, reason: f.reason }))).join(" · ");
  const f = fss[0];
  return f ? (f.available ? t("dev.format_fs_fixed", { fs: f.label }) : t("dev.format_unavailable", { fs: f.label, reason: f.reason })) : "";
}

function formatChoice(type) {
  const fss = type.format_filesystems || [];
  const ipod = type.type === "ipod" ? html`<p class="muted">${t("dev.format_ipod")}</p>` : "";
  if (fss.length <= 1) return html`${ipod}<p>${fsSummary(type)}</p>`;
  const first = fss.find((f) => f.available)?.fs;
  return html`${ipod}<fieldset class="choice" style="border:0;padding:0;margin:0"><legend class="muted" style="margin-bottom:6px">${t("dev.format_fs")}</legend>
    ${fss.map((f) => html`<label class="checkrow" ${f.available ? "" : html`title="${f.reason}"`}>
      <input type="radio" name="formatFs" value="${f.fs}" ${f.fs === first ? "checked" : ""} ${f.available ? "" : "disabled"}>
      ${f.label}${f.available ? "" : html` <small class="muted">(${f.reason})</small>`}</label>`)}</fieldset>`;
}

// History: filter chips All / Full syncs / Updates / Sent back with counts; one row per report -
// badge, when, kind · duration, what changed, errors - that expands to its failures (track, step, error), the gentle
// suggestion after a storage failure, Open report and Log. The newest row starts open.
const HIST_KINDS = ["all", "full", "update", "back"];
// "Sent back": every sync or update whose reverse step sent, kept or failed to send anything
const hasBack = (r) => Boolean(r.reverse && (reverseParts(r.reverse).length || r.reverse.failed || (r.reverse.errors || []).length));
const histMatch = (r, k) => k === "all" || (k === "back" ? hasBack(r) : r.kind === k);

async function renderHistory(ctx) {
  const { cfg } = ctx.state;
  const [reports, libs] = await Promise.all([get(`/sync-reports?device=${encodeURIComponent(cfg.id)}`), get("/libraries").catch(() => [])]);
  ctx.state.libNames = Object.fromEntries(libs.map((l) => [l.id, l.name])); // the "waiting for <library>" line
  const show = HIST_KINDS.includes(ctx.query.get("show")) ? ctx.query.get("show") : "all";
  const base = `#/devices/${encodeURIComponent(cfg.id)}/history`;
  const n = (k) => reports.filter((r) => histMatch(r, k)).length;
  const rows = reports.filter((r) => histMatch(r, show));
  const chips = html`<div class="hist-filters" role="group" aria-label="${t("hist.show")}">${HIST_KINDS.map((k) => html`<a class="fchip" href="${base}${k === "all" ? "" : `?show=${k}`}"
    aria-current="${k === show}" data-hist-filter="${k}">${t(`hist.f.${k}`)} <span>${n(k)}</span></a>`)}</div>`;
  if (!reports.length) return emptyState(t("dev.history_empty"));
  return html`${chips}<div class="card hist-list" data-history>${rows.length
    ? rows.map((r, i) => historyRow(ctx, r, i === 0, show))
    : html`<p class="muted hist-none">${t("hist.none")}</p>`}</div>`;
}

const HIST_BADGE = { succeeded: ["ok", "devs.result.succeeded"], partial: ["warn", "devs.result.partial"], failed: ["error", "devs.result.failed"],
  cancelled: ["neutral", "devs.result.cancelled"], running: ["busy", "devs.result.running"], skipped: ["warn", "devs.result.skipped"] };

// what the reverse step of a report sent back: plays, ratings, positions, finished, kept, waiting
function reverseParts(v) {
  if (!v) return [];
  return [["plays_found", "hist.r.found"], ["plays_submitted", "hist.r.sent"], ["plays_duplicate", "hist.r.duplicate"],
    ["ratings_pushed", "hist.r.ratings"], ["stars_pushed", "hist.r.stars"], ["positions_pushed", "hist.r.positions"], ["finished_pushed", "hist.r.finished"],
    ["positions_superseded", "hist.r.superseded"], ["pending", "hist.r.pending"], ["dropped", "hist.r.dropped"]]
    .filter(([k]) => v[k]).map(([k, key]) => ({ k, text: t(key, { n: v[k] }) }))
    // playlists: saved from the player's edits, made on the player, waiting for a choice
    .concat([["playlists_pushed", "hist.r.pl_saved"], ["playlists_created", "hist.r.pl_created"], ["playlist_conflicts", "hist.r.pl_conflicts"]]
      .map(([k, key]) => [k, key, Array.isArray(v[k]) ? v[k] : Object.keys(v[k] || {})])
      .filter(([, , names]) => names.length)
      .map(([k, key, names]) => ({ k, text: t(key, { n: names.length, names: names.join(", ") }) })));
}

const sentBack = (v) => [[v?.plays_submitted, "count.play"], [v?.ratings_pushed, "count.rating"], [v?.stars_pushed, "count.heart"],
  [v?.positions_pushed, "count.position"]]
  .filter(([x]) => x).map(([x, k]) => t(k, { n: x }));

// what a report changed, in words: a full sync "+88 · ~12 · −4 · 31 tags · verified", an update "3 playlists,
// 12 ratings, 1 position"
function changed(r) {
  const c = r.counts || {};
  if (r.kind === "update") {
    const parts = [[c.playlists, "count.playlist"], [c.ratings, "count.rating"], [c.positions, "count.position"], [c.update_meta, "count.tag"],
      [c.remove, "count.removed"]].filter(([x]) => x).map(([x, k]) => t(k, { n: x }));
    const back = sentBack(r.reverse); // an update that only sent listening data back is not "nothing"
    return [...parts, ...(back.length ? [t("hist.back_short", { what: back.join(", ") })] : [])].join(", ") || t("hist.nothing");
  }
  const back = sentBack(r.reverse);
  const parts = [c.add ? `+${c.add}` : "", c.replace ? `~${c.replace}` : "", c.remove ? `−${c.remove}` : "",
    c.update_meta ? t("count.tag", { n: c.update_meta }) : "", c.verified ? t("hist.verified") : "",
    back.length ? t("hist.back_short", { what: back.join(", ") }) : ""].filter(Boolean);
  return parts.join(" · ") || t("hist.nothing");
}

function historyRow(ctx, r, open, show = "all") {
  const { cfg } = ctx.state;
  // "Sent back" shows only what went to the libraries; the full list stays in the other views
  const acts = (r.activity || []).filter((a) => show !== "back" || SENT_KINDS.includes(a.kind));
  const [state, key] = r.status === "succeeded" && (r.warnings || []).length ? ["warn", "devs.result.succeeded"] : HIST_BADGE[r.status] || ["neutral", "devs.result.skipped"];
  const fails = r.failures || [];
  const errs = fails.length ? t("hist.failed", { n: fails.length }) : r.status === "failed" ? t("hist.failed", { n: 1 }) : t("hist.no_errors");
  const items = fails.length ? fails.map((f) => ({ track: f.item || f.title || f.ref || "", step: f.step, error: f.error || f.message || "" }))
    : r.status === "failed" ? [{ track: t("hist.no_file"), step: "", error: r.message }] : [];
  const id = encodeURIComponent(cfg.id);
  return html`<details class="hist-row" data-report="${r.id}" data-kind="${r.kind}" ${open ? "open" : ""}>
    <summary><span class="hist-badge">${badge(state, r.kind === "update" && r.status === "succeeded" ? t("hist.updated") : t(key))}</span>
      <span class="hist-when"><b>${when(r.finished_at || r.started_at)}</b><small>${t(`hist.kind.${r.kind}`)}${took(r) ? ` · ${took(r)}` : ""}</small></span>
      <span class="hist-counts" data-hist-counts>${changed(r)}</span>
      <span class="hist-errs ${fails.length || r.status === "failed" ? "bad" : ""}">${errs}</span>
      <span class="hist-chev">${icon("down", { size: 16 })}</span></summary>
    <div class="hist-body">
      ${items.map((f) => html`<div class="hist-fail" data-failure><b>${f.track}</b>${f.step ? html`<span><span class="muted">${t("hist.step")}</span> ${f.step}</span>` : html`<span></span>`}<span>${f.error}</span></div>`)}
      ${(r.warnings || []).map((w) => html`<div class="callout" data-report-warning>${w}</div>`)}
      ${acts.length ? html`<div class="hist-activity" data-hist-activity><b>${t("hist.activity")}</b>${activityList(acts)}</div>` : ""}
      ${(r.playlists_removed || []).length ? html`<p class="muted" data-pl-removed>${t("hist.pl_removed", { n: r.playlists_removed.length, names: r.playlists_removed.join(", ") })}</p>` : ""}
      ${r.reverse && (reverseParts(r.reverse).length || r.reverse.failed || (r.reverse.errors || []).length) ? html`<div class="hist-back" data-hist-back>
        <b>${t("hist.back")}</b>${r.reverse.failed ? html`<p class="muted">${t("hist.r.failed")}</p>` : ""}
        <ul>${reverseParts(r.reverse).map((p) => html`<li data-back="${p.k}">${p.text}</li>`)}
          ${[...(r.reverse.errors || []), ...(r.reverse.warnings || [])].map((e) => html`<li class="muted">${e}</li>`)}</ul></div>` : ""}
      ${(r.reverse?.held_unstars || []).length ? suggestionBox({
        iconName: "star", message: t("dev.held_text", { n: r.reverse.held_unstars.length }), attrs: html`data-held-unstars`,
        action: html`<button class="btn small" data-action="heldUnstars" data-id="${r.id}" data-n="${r.reverse.held_unstars.length}">${t("dev.held_send")}</button>`,
      }) : ""}
      ${(r.reverse?.changes || []).length ? suggestionBox({
        iconName: "undo", message: t("dev.undo_text", { n: r.reverse.changes.length }), attrs: html`data-undo-listening`,
        action: html`<button class="btn small" data-action="undoListening" data-id="${r.id}" data-n="${r.reverse.changes.length}">${t("dev.undo_send")}</button>`,
      }) : ""}
      ${feedbackLine(r.reverse, ctx.state.libNames) ? html`<div class="callout" data-feedback-line>${feedbackLine(r.reverse, ctx.state.libNames)}
        <button type="button" class="linkbtn" data-action="feedbackOpen">${t("dev.fb_show")}</button></div>` : ""}
      ${r.suggest_gentle && r.status === "failed" && !cfg.sync?.gentle_write ? suggestionBox({
        iconName: "shield", message: t("dev.gentle_help"), attrs: html`data-suggest-gentle`,
        action: html`<button class="btn small" data-action="gentleOn">${t("prog.gentle_on")}</button>`,
      }) : ""}
      ${r.job_id ? html`<div class="hist-actions"><a class="btn small" href="#/devices/${id}/sync/${encodeURIComponent(r.job_id)}" data-open-report>${t("hist.open_report")}</a>
        <a class="btn small" data-report-log href="#/logs?${new URLSearchParams({ job: r.job_id, level: "DEBUG" })}">${t("dev.report_log")}</a></div>` : ""}
    </div></details>`;
}

// after "format after registering" (registration wizard): ask for the typed confirmation right away
export function mounted(ctx, root) {
  // phones: the tab bar scrolls sideways; keep the active tab in view
  const tabs = root.querySelector(".tabs"), active = tabs?.querySelector('[aria-selected="true"]');
  if (tabs && active) tabs.scrollLeft = active.offsetLeft - tabs.offsetLeft - (tabs.clientWidth - active.offsetWidth) / 2;
  if (ctx.params.tab === "rules") rulesMounted(ctx, root); // the live preview
  if (ctx.params.tab === "processors") refreshLint(ctx); // chains that make no sense
  if (ctx.query.get("feedback") === "1" && ctx.params.id && !ctx.state.feedbackShown) { // the dashboard's row
    ctx.state.feedbackShown = true;
    openFeedback(ctx.state.cfg, () => ctx.rerender());
  }
  const settings = ctx.params.tab === "settings" && root.querySelector("[data-settings-form]");
  if (settings) { // one Save / Discard bar for Sync behaviour and Device (632.2); gentle write mode's values show while it is on
    const syncForm = root.querySelector("[data-sync-form]"), both = root.querySelector("[data-devset]");
    if (syncForm) syncMounted(syncForm, ctx.state.appDefaults);
    settings.addEventListener("change", (e) => {
      if (e.target.id === "dsGentle") settings.querySelector("[data-gentle-fields]").hidden = !e.target.checked;
      if (e.target.dataset.accountPick && e.target.value === "__add") addAccount(e.target, ctx); //
      else if (e.target.dataset.accountPick) { // "Browse … as this account" follows the pick
        const link = e.target.closest("[data-account]").querySelector("[data-browse-as]");
        link.href = `#/libraries/${encodeURIComponent(e.target.dataset.accountPick)}/browse${e.target.value ? `?as=${encodeURIComponent(e.target.value)}` : ""}`;
      }
    });
    unsavedBar(both, { save: () => saveBoth(ctx, both, syncForm), discard: () => ctx.rerender() });
    const search = root.querySelector("[data-ds-search]");
    search?.addEventListener("input", () => filterCards(root, search.value));
    if (search && ctx.query.get("q")) { search.value = ctx.query.get("q"); filterCards(root, search.value); } // from the app search
  }
  if (ctx.query.get("format") === "1" && ctx.params.id && !ctx.state.formatAsked) {
    ctx.state.formatAsked = true;
    const btn = root.querySelector('[data-action="format"], [data-action="wipe"]'); // Rockbox: wipe
    if (btn && !btn.disabled) btn.click();
  }
}

// --- actions ---------------------------------------------------------------------------------------------------

/** Sync behaviour and Device in one PATCH (632.2): gentle write mode's values join the sync settings. */
function saveBoth(ctx, root, syncForm) {
  const s = syncForm ? readSync(ctx, syncForm) : {};
  if (!s) return false;
  const d = readSettings(ctx, root);
  if (!d) return false;
  return saveSettings(ctx, root, { ...s, ...d, sync: { ...(s.sync || ctx.state.cfg.sync), ...(d.sync || {}) } });
}

// the device Settings search (632.2): a card stays when its text contains every word; hits are highlighted
function filterCards(root, query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  let any = false;
  for (const card of root.querySelectorAll("[role=tabpanel] :is(.ds-card, #profileCard, .dangerzone)")) {
    const x = card.textContent.toLowerCase();
    card.hidden = !words.every((w) => x.includes(w));
    any ||= !card.hidden;
  }
  for (const h of root.querySelectorAll(".ds-part")) {
    let n = h.nextElementSibling;
    h.hidden = !!words.length && !(n && n.querySelector(":is(.ds-card, #profileCard, .dangerzone):not([hidden])"));
  }
  root.querySelector("[data-ds-none]").hidden = any;
  if (!globalThis.CSS?.highlights) return;
  const ranges = [];
  if (words.length) {
    const walk = document.createTreeWalker(root.querySelector("[role=tabpanel]"), NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      if (n.parentElement.closest("[hidden], select, script")) continue;
      const x = n.data.toLowerCase();
      for (const w of words) for (let i = x.indexOf(w); i >= 0; i = x.indexOf(w, i + w.length)) {
        const r = new Range(); r.setStart(n, i); r.setEnd(n, i + w.length); ranges.push(r);
      }
    }
  }
  CSS.highlights.set("set-hit", new Highlight(...ranges));
}

/** The Device settings form's changes ({settings, reverse_sync, sync}), or null after a toast saying what is wrong. */
function readSettings(ctx, root) {
  const { cfg, forms } = ctx.state;
  const parts = [forms.typeForm, forms.settings, forms.reverse, forms.gentle].map((f) => (f ? readForm(f, root) : {}));
  if (parts.some((x) => x === null)) return null;
  const [typed, files, rev, gentle] = parts;
  const out = { settings: { ...cfg.settings, ...typed, ...files } };
  if (forms.reverse) out.reverse_sync = { ...cfg.reverse_sync, ...rev };
  if (forms.gentle) out.sync = { ...gentle, gentle_write: root.querySelector("#dsGentle").checked }; // saveBoth adds the rest
  if (forms.accounts?.length) {
    out.library_accounts = readAccounts(ctx, root);
  }
  return out;
}

/** Save bar: a new name - on the device too when asked (it shows what the disk label becomes first) -
 * and the settings. False keeps the bar. */
async function saveSettings(ctx, root, changes = readSettings(ctx, root)) {
  if (!changes) return false;
  const { cfg } = ctx.state;
  const id = encodeURIComponent(cfg.id);
  const name = root.querySelector("#dsName").value.split(/\s+/).filter(Boolean).join(" ");
  if (!name) { toast(t("dev.rename_empty"), "error"); return false; }
  const onDevice = name !== cfg.name && root.querySelector("#dsRename")?.checked;
  if (onDevice) {
    const preview = await post(`/devices/${id}/rename`, { name, preview: true });
    if (!(await confirm({
      title: t("dev.rename_confirm", { old: cfg.name, name }),
      message: preview.label ? t("dev.rename_label", { label: preview.label }) : t("dev.rename_no_label"),
      ok: t("dev.rename").replace("…", ""),
    }))) return false;
    ctx.state.cfg = (await post(`/devices/${id}/rename`, { name })).config;
  }
  ctx.state.cfg = await patch(`/devices/${id}`, { ...changes, ...(onDevice || name === cfg.name ? {} : { name }) });
  toast(name !== cfg.name ? t("dev.renamed", { name }) : t("dev.set_saved"));
  if (name !== cfg.name) refreshSidebar();
  ctx.rerender();
  return true;
}

function readForm(form, root) {
  try {
    return form.read(root);
  } catch (e) {
    if (e instanceof FormError) { toast(e.message, "error"); return null; }
    throw e;
  }
}

export const actions = {
  ...pickerActions, // the flash drive's base folder
  gentleOn: (el, ctx) => progressActions.gentleOn(el, ctx),
  async heldUnstars(el, ctx) { // favourites a suspected database reset held back: removed on purpose after all
    const { cfg } = ctx.state, n = Number(el.dataset.n);
    if (!(await confirm({ title: t("dev.held_title", { n }), message: t("dev.held_confirm", { name: cfg.name }), ok: t("dev.held_send"), danger: true }))) return;
    el.disabled = true;
    const res = await post(`/devices/${encodeURIComponent(cfg.id)}/sync-reports/${encodeURIComponent(el.dataset.id)}/send-held-unstars`);
    toast(t("dev.held_done", { n: res.queued }), "ok");
    ctx.rerender();
  },
  async undoListening(el, ctx) { // send back the library's favourites and ratings from before this sync
    const { cfg } = ctx.state, n = Number(el.dataset.n);
    if (!(await confirm({ title: t("dev.undo_title", { n }), message: t("dev.undo_confirm", { name: cfg.name }), ok: t("dev.undo_send"), danger: true }))) return;
    el.disabled = true;
    const res = await post(`/devices/${encodeURIComponent(cfg.id)}/sync-reports/${encodeURIComponent(el.dataset.id)}/undo-listening`);
    toast(t("dev.undo_done", { n: res.queued }), "ok");
    ctx.rerender();
  },
  feedbackOpen(el, ctx) { // what did not reach its library
    openFeedback(ctx.state.cfg, () => ctx.rerender());
  },
  async integrityRun(el, ctx) {
    const deep = el.closest("[data-health]").querySelector("#integrityDeep").checked;
    el.disabled = true;
    await post(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/integrity${deep ? "?deep=true" : ""}`);
    toast(t("dev.integrity_started", { name: ctx.state.cfg.name }));
  },
  async healthAck(el, ctx) {
    const url = `/devices/${encodeURIComponent(ctx.state.cfg.id)}/health/ack`;
    el.disabled = true;
    ctx.state.health = await post(url);
    ctx.rerender();
    toast(t("dev.health_acked"), "ok", {
      undo: async () => {
        ctx.state.health = await post(`${url}?undo=true`);
        ctx.rerender();
      },
    });
  },
  async healthClear(el, ctx) {
    const { cfg } = ctx.state;
    if (!(await confirm({ title: t("dev.io_clear_title", { name: cfg.name }), message: t("dev.io_clear_text"), ok: t("dev.io_clear"), danger: true }))) return;
    ctx.state.health = await del(`/devices/${encodeURIComponent(cfg.id)}/health/errors`);
    ctx.rerender();
    toast(t("dev.io_cleared"), "ok");
  },
  async integrityFix(el, ctx) {
    el.disabled = true;
    await post(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/integrity/fix`);
    toast(t("dev.integrity_fixing"));
  },
  async repairFs(el, ctx) { // fsck on the data partition; the job view shows what it fixed
    const { cfg } = ctx.state;
    const start = () => post(`/devices/${encodeURIComponent(cfg.id)}/repair`);
    await openJob(el, ctx, cfg.id, async () => {
      try {
        return await start();
      } catch (e) {
        if (e.code !== "mounted_elsewhere") throw e;
        // a host automounter mounted it a second time: fsck needs it unmounted everywhere
        const { mounts } = await get(`/devices/${encodeURIComponent(cfg.id)}/mounts`);
        const paths = mounts.filter((m) => !m.ours).map((m) => m.path).join(", ");
        const ok = await confirm({ title: t("dev.repair_blocked_title", { name: cfg.name }),
          message: t("dev.repair_blocked_text", { paths }), ok: t("dev.repair_blocked_ok") });
        if (!ok || !(await unmountEverywhere(cfg, false))) throw Object.assign(new Error(""), { quiet: true });
        return await start();
      }
    }, t("dev.repairing", { name: cfg.name })).catch((e) => { if (!e.quiet) throw e; });
  },
  async unmountAll(el, ctx) {
    el.disabled = true;
    try {
      if (await unmountEverywhere(ctx.state.cfg)) toast(t("dev.unmount_done", { name: ctx.state.cfg.name }));
    } finally {
      ctx.rerender();
    }
  },
  async updateNow(el, ctx) {
    el.disabled = true;
    await post(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/update`);
    toast(t("dev.update_started", { name: ctx.state.cfg.name }));
  },
  async ignoreDisk(el, ctx) {
    // Ignore (with Undo in the toast) or, for a shown ignored drive, stop ignoring it
    const disk = el.dataset.disk;
    const ignore = !("undo" in el.dataset);
    el.disabled = true;
    await post("/devices/ignore", { disk, ignore });
    if (ignore) {
      toast(t("devs.ignored_toast", { name: el.dataset.name }), "ok", {
        undo: async () => { await post("/devices/ignore", { disk, ignore: false }); ctx.rerender(); },
      });
    }
    ctx.rerender();
  },
  ...rulesActions,
  ...processorActions,
  ...contentsActions,
  ...profileActions,
  async removeOrphans(el, ctx) {
    const { cfg, orphans } = ctx.state;
    const n = orphans.length;
    const list = html`<ul class="orphan-list" style="max-height:240px;overflow:auto">${orphans.map((r) => html`<li>${r.title} <small class="muted mono">${r.path}</small></li>`)}</ul>`;
    if (!(await confirm({
      title: t("dev.orphans_title", { n, name: cfg.name }), message: t("dev.orphans_text"), body: list, ok: t("pc.remove_ok", { n }), danger: true,
    }))) return;
    await openJob(el, ctx, cfg.id, () => post(`/devices/${encodeURIComponent(cfg.id)}/orphans/remove`, {}));
  },
  async ftpTest(el, ctx) {
    // log in with the typed connection first; only a working one is saved
    const form = el.closest("form");
    const out = form.querySelector("[data-ftp-result]");
    const changes = readSettings(ctx, form);
    if (!changes) return;
    el.disabled = true;
    out.textContent = t("dev.set_testing");
    out.dataset.state = "";
    try {
      await post(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/check`, { settings: changes.settings });
    } catch (e) {
      out.textContent = errorMessage(e);
      out.dataset.state = "error";
      el.disabled = false;
      return;
    }
    el.disabled = false;
    await saveSettings(ctx, form, changes);
  },
  async eject(el, ctx) {
    const { cfg } = ctx.state;
    if (!(await confirm({ title: t("dev.eject_title", { name: cfg.name }), message: t("dev.eject_text"), ok: t("dev.eject") }))) return;
    el.disabled = true;
    toast(t("dev.ejecting", { name: cfg.name }));
    try {
      await post(`/devices/${encodeURIComponent(cfg.id)}/eject`);
    } finally { el.disabled = false; }
    toast(ejectedText(cfg, t("dev.ejected", { name: cfg.name }), ctx.state.live?.player_notice));
    ctx.rerender();
  },
  async playLog(el, ctx) {
    const on = el.dataset.on === "1";
    el.disabled = true;
    let res;
    try {
      res = await post(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/play-log`, { on });
    } finally { el.disabled = false; }
    ctx.state.cfg = { ...ctx.state.cfg, settings: { ...ctx.state.cfg.settings, enable_play_log: on } };
    const state = t(on ? "dev.playlog_state_on" : "dev.playlog_state_off");
    toast(t(res.applied ? "dev.playlog_applied" : "dev.playlog_saved", { state }));
    ctx.rerender();
  },
  async format(el, ctx) {
    const { cfg } = ctx.state;
    const k = (key) => key;
    let fs = null;
    const ok = await typedConfirm({
      title: t(k("dev.format_title"), { name: cfg.name }), message: t(formatText(cfg.type)), name: cfg.name, ok: t(k("dev.format")).replace("…", ""),
      body: ctx.state.type ? formatChoice(ctx.state.type) : "",
      capture: (root) => { fs = root.querySelector('input[name="formatFs"]:checked')?.value || null; },
    });
    if (!ok) return;
    el.disabled = true;
    el.textContent = t(k("dev.formatting"), { name: cfg.name });
    try {
      await post(`/devices/${encodeURIComponent(cfg.id)}/format`, { confirm: cfg.name, ...(fs ? { fs } : {}) });
    } catch (e) {
      el.disabled = false;
      el.textContent = t(k("dev.format"));
      throw e;
    }
    toast(t(k("dev.formatted"), { name: cfg.name }));
    // fresh storage: offer to forget the old I/O errors
    if (ctx.state.health?.io_errors && await confirm({ title: t("dev.io_clear_title", { name: cfg.name }), message: t("dev.io_clear_text"), ok: t("dev.io_clear") })) {
      await del(`/devices/${encodeURIComponent(cfg.id)}/health/errors`);
      toast(t("dev.io_cleared"), "ok");
    }
    ctx.rerender();
  },
  async downloadSdtool(el, ctx) {
    el.disabled = true;
    try {
      if (await downloadSdtool()) ctx.rerender();
    } finally { el.disabled = false; }
  },
  async switchFirmware(el, ctx) {
    const { cfg, live } = ctx.state;
    el.disabled = true;
    try {
      ctx.state.cfg = await patch(`/devices/${encodeURIComponent(cfg.id)}`, { type: live.firmware_switch });
    } finally {
      el.disabled = false;
    }
    toast(t("dev.firmware_switched", { name: cfg.name, new: fwName(live.firmware_switch) }), "ok");
    ctx.rerender();
  },
  async bind(el, ctx) {
    await post(`/devices/${encodeURIComponent(ctx.state.cfg.id)}/bind-disk`);
    toast(t("dev.bound", { name: ctx.state.cfg.name }));
    ctx.rerender();
  },
  async makeHome(el, ctx) {
    const { cfg, home } = ctx.state;
    const name = home.home.name || home.home.id;
    if (!(await confirm({ title: t("dev.make_home_title", { name: cfg.name }), message: t("dev.make_home_text", { home: name }), ok: t("dev.make_home").replace("…", "") }))) return;
    el.disabled = true;
    try {
      ctx.state.home = await post(`/devices/${encodeURIComponent(cfg.id)}/make-home`);
    } finally { el.disabled = false; }
    toast(t("dev.made_home", { name: cfg.name }));
    ctx.rerender();
  },
  async wipe(el, ctx) {
    const { cfg } = ctx.state;
    if (!(await typedConfirm({ title: t("dev.wipe_title", { name: cfg.name }), message: t(wipeText(cfg.type)), name: cfg.name, ok: t("dev.wipe").replace("…", "") }))) return;
    await openJob(el, ctx, cfg.id, () => post(`/devices/${encodeURIComponent(cfg.id)}/wipe`, { confirm: cfg.name }), t("dev.wiping", { name: cfg.name }));
  },
  async remove(el, ctx) {
    const { cfg } = ctx.state;
    if (!(await typedConfirm({ title: t("dev.delete_title", { name: cfg.name }), message: t("dev.delete_text"), name: cfg.name, ok: t("dev.delete").replace("…", "") }))) return;
    await del(`/devices/${encodeURIComponent(cfg.id)}`);
    toast(t("dev.deleted", { name: cfg.name }));
    goFrom(ctx, "/devices");
  },
};

// list: any device change; device page: changes of this device (editing tabs keep what the user typed); a running
// job's progress moves the header's progress strip in place on every tab
function refreshOrphans(ctx, id) {
  get("/devices").then((all) => {
    const live = all.registered.find((d) => d.id === id);
    if (!live?.connected || live.busy || live.ejecting || live.ejected || live.setting_clock || live.no_medium) return null;
    return get(`/devices/${encodeURIComponent(id)}/orphans`);
  }).then((rows) => {
    if (rows === null || rows === undefined || ctx.params.id !== id) return;
    ctx.state.orphans = rows;
    const card = document.querySelector("#orphansCard [data-orphans-body]");
    if (card) setHtml(card, orphansBody(rows));
  }, () => {});
}

export function onEvent(ev, ctx, root) {
  const id = ctx.params.id;
  if (!id) {
    if (/^(device\.|sync\.|job\.(started|finished)|live\.)/.test(ev.type)) ctx.rerender();
    return;
  }
  const tab = ctx.params.tab || "overview";
  if (ev.data?.device_id && ev.data.device_id !== id) return;
  if (ev.type === "client.progress") {
    const box = root?.querySelector("[data-client-sync]");
    if (box) setHtml(box, clientSession({ status: "ready", progress: ev.data }));
    return;
  }
  if (/^client\.(changed|revoked|paired)$/.test(ev.type)) { ctx.rerender(); return; }
  if (ev.type === "job.progress") {
    const strip = root?.querySelector("[data-progress-strip]");
    if (!strip || !strip.getAttribute("href").endsWith(`/${encodeURIComponent(ev.data.job_id)}`)) return;
    const pct = Math.round((ev.data.progress || 0) * 100);
    strip.querySelector(".bar i").style.width = `${pct}%`;
    strip.querySelector(".bar").setAttribute("aria-valuenow", String(pct));
    strip.querySelector(".dev-strip-top b").textContent = `${pct}%${ev.data.message ? ` · ${ev.data.message}` : ""}`;
    strip.querySelector(".dev-strip-top .muted").textContent = `${etaText(ev.data.eta_s)} →`;
    return;
  }
  // Player contents: only when the device becomes (un)available - its own load mounts, so not on device.mounted;
  // a job of the device (e.g. the update sync on connect) makes it busy and idle again
  if (tab === "contents") {
    if (/^(device\.(clock_done|connected|changed|removed|ejected|ejecting|formatted|error)|job\.(started|finished))$/.test(ev.type)) ctx.rerender();
    return;
  }
  // Settings never re-renders (unsaved edits): only its orphans card follows the device becoming idle again
  if (tab === "settings") {
    if (/^(device\.(connected|changed|mounted)|job\.finished|live\.)/.test(ev.type)) refreshOrphans(ctx, id);
    return;
  }
  if (!["overview", "history"].includes(tab)) return;
  if (/^(device\.|sync\.|update\.|job\.(started|finished)|reverse\.|live\.)/.test(ev.type)) ctx.rerender();
}

