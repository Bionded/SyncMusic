# Changelog

All notable changes to SyncMusic are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/),
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- A device's Settings tab is simpler: it opens on **Basic** with the everyday settings, the rest is in advanced sections in a menu on the left (a row at the top on a phone) with the danger zone last, and sections that do not apply to the device are left out. Every change is saved at once with an *Undo* note — there is no Save button any more. On/off settings are switches, the full-sync choice is one compact row, and default values are no longer captioned (a changed one shows *Changed* with a reset). The folder layout offers ready-made choices with an example path; *Custom…* shows the templates. Longer explanations open from a *?* next to the setting, numbers show their unit inside the field, the file system is a choice (*Detected* by default), and a flash drive no longer offers settings about what was played.
- A device's Settings tab is tidier: on wide screens each setting is one row with its name on the left and the control on the right, the file naming settings have their own **Files** section, cards no longer repeat their section's title, and the section heading is hidden while the section menu already shows it. Folder and network devices no longer show mount options that do not apply to them.
- Compás app: while a sync runs, a light band moves along the current step of the phase bar (8 frames a second, redrawing only that small strip), so a long step never looks frozen.
- Settings shows one section at a time with a section menu instead of one long page; each Settings section has its own address. **Show all settings on one page** brings the long page back (remembered per browser), and the search always covers every setting.
- Updates: *Check now* in Settings → Updates only checks — it no longer installs a found version at once when automatic updates are on. Installing shows the download progress, the check and the install, and every open page shows *Restarting SyncMusic…* during the restart and reloads itself when the new version is up (or says that it went back to the previous one).
- Release pages: the downloads table no longer lists compas-sdtool (SyncMusic downloads it by itself), and the checksums file is a line under the table.
- Wi-Fi clients: approving a player (or pairing it with a code) opens its setup right away — name, what it gets and processors, the same steps as registering a USB player. Until it is finished the player waits instead of failing its first sync, and the Devices page offers **Finish setup**.
- Wi-Fi clients: a player asking to connect can be given its name on the Devices page before you approve it, and renaming a paired player on its device page (or in its app) renames it everywhere; the app shows the server's name. The player's name is set only on the server (in the request card or the setup after approving); the Compás app shows it and no longer asks for one.
- Compás app: released as two downloads — `syncmusic-compas-<version>-hard.zip` (recommended, faster) and `-soft.zip` (use it if the hard-float app does not start on your player). Updates from the server keep the build a player runs; install steps and links are in the README and the release notes.
- Every release page (stable and beta) starts with a downloads table: server and desktop binaries per architecture, the Docker image, both Compás app builds, each with who it is for and how to install it.
- Compás app: a short press of Power switches the screen off and on; with the screen off the app now only turns the backlight off and keeps the player awake, so the Wi-Fi connection stays up.
- Compás app: when the server holds a newer app, the app asks right after it connects (Update / Later) instead of only showing a button; an app newer than the server says so. The device page shows the app version the player runs and the answer.
- Wi-Fi clients: a large sync no longer copies the whole library into the server's outbox first. The SyncMusic app for Compás players gets a whole sync in one session: the server prepares each file just before the app downloads it, so a large library needs almost no disk on the server, and stopping a sync continues where it stopped next time. Apps without this get a large sync in parts of at most the device's *Outbox budget* (2 GB by default); prepared files share the processing cache's copy instead of being copied a second time.
- Ratings and favourites pass back exactly as each player has them (stars to ratings, hearts to favourites, never converted), and a device's reverse sync settings show only what that player records.
- Every Wi-Fi client app may now confirm a sync that needs a confirmation (many removals, not enough space) on the player by default; the device setting *The player may confirm a sync* only turns this off.
- Pairing codes for Wi-Fi clients are 8 digits (easy to type on a player), valid for 1 minute by default, and a client address gets at most 2 pairing attempts per second.

### Added
- Compás over Wi-Fi: **Rebuild player database** on the device page and *Rebuild player library* in the app's Settings build the player's library again from scratch at the next sync (from what the server knows about each song, no music sent again), and a device setting does it at every sync (off by default). The server also rebuilds by itself when the player's library does not match the card.
- Settings → Storage & resources lists the data kept for each Wi-Fi player with a Clean button.
- Compás app: **Settings → Reset the app** deletes only the app's own data (pairing, settings, history) — never the app, the music or the card's sync list. Connecting to the server again continues the same device with its templates, and the app gets its settings back: the server now keeps a copy of the app's settings.
- HiBy R1 with Compás: SyncMusic keeps the compas-sdtool it downloaded up to date by itself — with its own update check it fetches a newer build from its release, checks it and swaps it in while no sync runs. A tool you chose yourself is never replaced; Settings → Devices says what the release offers and when it last checked.
- Devices: when another program (a system automount) mounts a player as well, **Make read-only** on the device page — or the device setting *Make other mounts read-only when connected* — lets that program only read it while SyncMusic keeps writing through its own mount.
- Devices: **Check and repair file system** in a USB player's Settings, and a **Check and repair** button at the top of the device page when Eject failed or the file system has errors. A file system check that finds errors when ejecting now marks the player as damaged right away instead of leaving it mounted with nothing to do.
- Devices: a player that was added twice — once over USB or a card reader and once as a separate Wi-Fi device — gets a **Merge with …** offer on both device pages. Merging keeps one device with the USB record's settings and templates, the name you choose, and the Wi-Fi record's app pairing, history and listening data (plays already sent are not sent again).
- Wi-Fi clients: when a Compás player asking to connect holds the card of a device you already registered over USB, its request card offers **This is <name>: add Wi-Fi to it** — the player keeps one device record (settings, rules, history) instead of a second one. Its device page shows its own type and how it is connected now (Wi-Fi, or USB while it also syncs over Wi-Fi). A complete Wi-Fi sync writes the same manifest onto the card as a USB sync, so syncing that card over USB afterwards sees the Wi-Fi files as its own, and a Wi-Fi sync after a USB sync starts from what the USB sync left on the card.
- When a sync stops twice on an I/O error, the device page offers **Skip that file and continue**: the file it stopped at is left out for a day, the file system is repaired and the sync continues.
- Wi-Fi clients: the device page shows the real transfer speed to the player and the time left while the app downloads.
- HiBy R1 with Compás (USB and the Wi-Fi app): album ratings sync both ways with Navidrome and other Subsonic servers.
- Wi-Fi clients: the device page shows how much waits for the player on the server's disk, with **Clear** to drop it.
- Wi-Fi clients: **Sync now** on the server starts the sync in the player's app at once while the app is open (the app listens for it); until then the device page shows that it waits for the player.
- Settings → Tools can download the newest HiBy R1 Compás app from SyncMusic's release, so paired players update themselves without copying the zip by hand.
- Wi-Fi clients can ask to connect without a pairing code: the request shows up on the Devices page with a short code that the player shows too, and you approve or reject it there (Settings → Wi-Fi clients → "Allow pairing requests", on by default). The reference client does this with `pair --request`.
- SyncMusic app for HiBy players with the Compás firmware (HiBy R1), released as `syncmusic-compas-<version>-hard.zip` and `-soft.zip`: it pairs and syncs over Wi-Fi like the USB connection (music, audiobooks, podcasts, playlists, plays, hearts, positions), shows progress and a preview before a sync, keeps a sync history, guards the battery and updates itself from the server; the server builds the player's library for it so new music shows right after the sync. New user guide page: SyncMusic app for Compás players.
- Wi-Fi clients: the listening data in the sync plan also says whether a podcast episode or audiobook is finished, and podcast episodes carry their publication date.
- Wi-Fi clients: the sync plan now gives each file's modification time, can carry a set of files the server built for the client (installed all at once, with a commit file), and tells the client what to do after a sync that changed something (device setting "After a sync that changed something": nothing, refresh the library or restart the player). The reference client follows all three.
- A player that the system already mounted (Umbrel, a desktop's automount) is used through that mount and never mounted a second time; SyncMusic now waits a few seconds after a disk is plugged in for such an automounter (Settings → Disks & mounting, "Wait for automount"). "Use an existing mount" in the device settings can refuse such players instead. An eject after a sync leaves another program's mount alone unless "Unmount other mounts when connected" is on.
- "Stay ready between syncs" (device settings; on for newly added devices, default in Settings → New devices): a disk device stays connected but unmounted whenever nothing uses it, so it can be unplugged or switched off at any time; syncs mount it again by themselves. Browsing a stay-ready player's contents mounts it read-only, so looking never writes to it.
- Ejecting a FAT or exFAT player (or USB drive) now checks its file system read-only after unmounting it. If the check finds errors, the eject stops with a message and the player stays connected so you can repair it from the device page. Before, the damage only showed up at the next connect.
- If both copies of SyncMusic's sync record on a player are damaged, the device page offers **Rebuild from files**: the damaged copies are moved aside and the next sync starts as a first sync that can adopt the files already on the player, instead of wiping it.

### Fixed
- Rockbox players and the Wi-Fi app show the minimum played share for a play as a percentage (50 %) instead of a fraction (0.5).
- Device Settings → Files and paths: the folder layout choice now lines up with the other settings on wide screens.
- A player whose disk stopped answering during a sync (a hung write followed by a write error in the system log) is no longer sent into an automatic repair that hangs: the device page asks to unplug and replug it (an iPod: restart it with Menu and Select), then check and repair it.
- Gentle write mode has a new option **Keep the disk awake during pauses** (off by default): while the sync waits for a conversion, a library or a pause between batches, a few bytes are read from the player every 2 seconds (adjustable), so CF/SD adapters in modded iPods do not fall into a standby they never wake from. It only reads, nothing is written.
- Compás over Wi-Fi: a sync with no changes no longer builds and sends the player's library again just because the app uploaded the library it had just installed.
- Compás players over USB: an update sync that changes no music no longer rebuilds and rewrites the player's library on the card.
- Wi-Fi sync: after the last file is ready, the job no longer sits at 100 % with the last title while the player is still downloading — it says *waiting for the player to take N of M files* (on the job page and the device card), counting down as the player takes them.
- When a player's storage reports an input/output error, the log now also shows the kernel's recent messages about that disk (USB resets, SCSI and file system errors), and reusing another program's mount that writes through on every write (`sync`, `flush`) is warned about, since it stalls or breaks slow cards.
- Wi-Fi syncs of large libraries prepare their files much faster: the server no longer re-reads the whole outbox folder before every file it writes for the player.
- Wi-Fi players: a sync applied on the server (Apply on the device page, a schedule or an automatic sync) no longer copies the whole plan into a server-side folder first: the app is asked to sync at once and gets the songs as they are prepared, a few ahead of its downloads. A Wi-Fi sync now stays running on the server until the app has taken every song, so the device page shows it like a USB sync, and Stop on the server also stops the app.
- Compás over Wi-Fi: after a sync through the app the player's library could list only the songs of that sync (when the server had no copy of the songs put on the card over USB or before a re-pairing/clean-up). The app now sends the card's library database to the server before each sync (only when it changed), and the server adds the new songs to it, reading only the new files; it never sends a library that misses songs the card holds. Needs the new app build.
- Compás app: while the server works out a Wi-Fi sync, the app shows what it is doing (reading the player's file list, comparing with the libraries, …) with the items counted and the time so far, and says it is still working after 10 seconds instead of looking stuck; the device page on the server shows the same planning progress.
- Compás app: **Sync** pressed on the server now starts the sync in an app that was just paired (before, only after the app was restarted); the app says the server started it, and the device page tells whether the app is open to pick the request up.
- Compás app: a newer app version is offered right after the player is registered, before its first sync; the server starts fetching the app's update as soon as pairing begins instead of after the app's next connection.
- Settings, a device's Settings tab, the registration wizard and library views no longer break when the browser blocks site data (private mode, cookies blocked); view choices then last for the open page only.
- Wi-Fi players with a server-built Compás database no longer keep a whole copy of every file they took on the server: only the tags and stream headers the database needs are kept (a few hundred KB per song, the same database). Copies kept by older versions are cut down by themselves.
- Old unpacked program versions are removed again in every kind of install (home folder, custom folder, Umbrel), not only in the system service under `/opt`: at start, hourly and after an update; the running version and the one an update can go back to stay. Settings → Storage & resources has **Remove old versions**.
- Deleting a Wi-Fi player, or merging it into another device, now also removes the files the server kept for it; data left behind by earlier versions for players that no longer exist is removed at start and logged.
- Files waiting on the server for a Wi-Fi player that its sync no longer needs (left by a cancelled or replaced sync) are now removed at the next sync instead of taking disk space for good.
- The device page no longer reports an error when the player's file list and its backup are both damaged; the check estimate shows 0 files.
- Rules tab: a template just added says "Counting…" until its preview arrives instead of briefly showing "No matches".
- A device that was ejected, removed and registered again no longer stays "ejected" until SyncMusic restarts.
- A damaged MP4 file no longer hangs the video check of a sync forever.
- FTP devices: a wrong password no longer leaves connections open on the server, and a refused rename no longer loses the old copy of a file (or the player's sync record).
- The database backup made before an upgrade is no longer deleted by the regular backup clean-up (the two newest stay) and goes to the backup folder chosen in Settings.
- A failed automatic update (for example a dropped download) is tried again at the next update check instead of never for that version.
- After an update, a problem removing the old program files is logged and no longer keeps the "updated" notice from appearing.
- A cancelled sync no longer logs a stray "Future exception was never retrieved" error.
- FTP and SMB folder libraries: a dropped connection no longer makes the next sync download every file again, and a failed download leaves no partial file in the cache.
- Car mode: a " (2)" added to a duplicate file name no longer pushes the path over the length limit.
- A synced playlist renamed or deleted on the player is no longer undone without a word: the sync report says the sync writes it back, and a renamed copy is not saved to the library as a second playlist.
- Compás app: a power cut while the app updated itself no longer leaves a mix of old and new files; the next start finishes the update.
- A forced sync that skips a newer version of a song for space now lists that song's rule under what was left off, and the plan's reserve matches the smaller plan.
- Favourites changed on a player are no longer dropped without a word when the library turns out not to keep favourites: the sync report says so.
- Cancel now stops a sync at once while it waits for a long audiobook merge, and stops a Deep integrity check in the middle of a long file.
- A power cut while a sync was adding to a player's file list no longer keeps half of that change; the whole unfinished change is dropped and the next sync redoes it.
- The copy progress of a sync now reaches 100 % when a file could not be prepared.
- Listening data a Wi-Fi player sends while earlier data is still being handed to the libraries no longer interrupts that hand-over.
- The Wi-Fi client API refuses a finish while the plan is still being made, a negative page cursor and failure reports for files outside the plan.
- Compás app: a download interrupted just before it was finished no longer blocks every later sync at that file, and the progress no longer counts a file twice when the server sends it whole again.
- Compás app: a damaged SyncMusic manifest on the card is kept aside and its list of synced files is carried over, instead of being replaced by an empty one.
- A Wi-Fi sync whose player disappeared mid-sync no longer keeps the device busy for hours: Stop on the server ends it at once, and a streaming sync with no word from the player for 30 minutes ends by itself.
- Revoking a Wi-Fi client ends its open syncs at once; its download links stop working.
- A sync plan left open for review is no longer applied after an automatic update sync changed the device in the meantime: Apply asks for a fresh plan.
- The sync report now says plainly that songs from another library added to a library playlist on the device are left out of that playlist on the device too.
- A sync cancelled while it was already writing the device database now shows as succeeded in the job history, matching its report.
- Cancelling a sync that waits behind another job on the same device now ends it at once instead of after the other job.
- Clicking Apply twice on the same plan no longer shows a server error; the second click is told the plan is gone.
- Playlists: merging a player's playlist into a library playlist of the same name no longer lists the songs both have twice.
- Card readers: a card registered through one reader can now be formatted (or wiped) in another reader too, like Eject and Repair already allowed.
- Formatting, partitioning and file-system repair now also see mounts made by FUSE (exfat-fuse, ntfs-3g) and wait a moment when the disk is still busy right after a mount, instead of running on a mounted volume or failing.
- A player registered by a folder and also paired with the Wi-Fi app now syncs through the folder while it is there, as the Devices list shows, and over Wi-Fi only when the folder is gone.
- A sync planned while a player was connected over Wi-Fi can no longer be applied to it after it is plugged in over USB; the plan is made again for the disk.
- Merging two records of one player no longer drops the Wi-Fi pairing when the kept record had an old, revoked pairing (the app was then refused although the page said paired).
- Card readers: a second card in the same reader is no longer taken for the first one (every card in a reader has the reader's serial); the card's own manifest or file system decides.
- Formatting an iPod now checks that the disk really is that iPod (by its serial) before formatting, like the other device types.
- Formatting a device now keeps it locked until the new file system is mounted, so an automatic sync, Repair or Eject can no longer touch the disk while it is being formatted.
- The space after a planned sync (device Overview and the plan's After bar) no longer counts the space kept free as a reserve as used: a sync that writes nothing now shows the same usage as now.
- Devices: making a plan (pressing Sync) right after reconnecting a player could fail with 'No such file or directory': an idle-unmount left over from before the player was ejected unmounted it again under the plan.
- Devices: copies of SyncMusic's own mount that the system shows under another path are no longer treated as another program's mount, and *Unmount other mounts when connected* no longer unmounts them (which could take SyncMusic's own mount with them).
- Compás app: a saved connection file damaged on the card (for example after a file system repair) no longer stops the app with 'Cannot start'. The app falls back to the copy of its previous save, or moves the damaged file aside and asks to pair again; the player stays the same device on the server.
- Wi-Fi players: a player whose app has not talked to the server for a few minutes (or since the server restarted) is shown as not connected instead of 'connected (Wi-Fi)'; it stays paired and is connected again as soon as the app reaches the server.
- Integrity check: no longer sits at 0 % for many minutes on a large player and can always be cancelled — every step shows what it is doing, and Cancel stops it within a second or two, keeping what it found so far. The check now offers **Quick** (sizes and missing files, the default), **Full** (read every file back) and **Deep** (also decode), and says how long a full check of the player takes.
- Compás app: players are offered app updates without anyone pressing Download: when an app connects and the server holds no newer app, the server downloads its own release's app builds by itself and offers the update right away (or on the next connection). Settings → Tools and the device page say when the server holds no app.
- The SyncMusic app for Compás players no longer says "does not fit" on its first Wi-Fi sync after a USB sync of the same card: it reports the files the USB sync put on the card, so only what is new is sent.
- Compás players synced both over USB and by the Wi-Fi app keep each playlist as one `Playlists/<name>.m3u` file: a Wi-Fi sync no longer deletes the copy a USB sync wrote and sends it again.
- A registered player's card in a USB card reader (recognised by its manifest or file system) can be ejected, repaired and synced again; it was refused because the reader reports another serial.
- The SyncMusic app for Compás players from the beta build closed at once on the HiBy R1: it is built with the Go version that runs there again, and the build now checks that the app starts.
- Wi-Fi sync in the Compás app no longer fails with "ack: EOF" when a connection to the server was dropped: the app retries such calls, and the server keeps idle connections open longer.
- Wi-Fi clients: a large sync shows its progress, size and time left for the whole sync, on the device page and in the Compás app, instead of restarting the count for every part.
- A Wi-Fi client's sync that waits for your confirmation (too many removals, does not fit) or failed now says so on its device page, with a Review plan button, instead of showing that the plan is being made.
- The buttons under a sync error on the device page now wrap on phones instead of scrolling sideways.
- Wi-Fi clients: **Cancel** on the device page now stops a sync the player's app is running (before, it only withdrew a pending *Sync now*).
- Wi-Fi clients: two sync starts at the same moment (a retry or a double tap) no longer open two sessions.
- When writing to a player stops with an I/O error mid-sync, SyncMusic now checks and repairs its file system and continues the sync from where it stopped, once; if the error comes back soon after, the device page says the storage may be failing.
- The damaged-file-system banner's button is now *Repair and continue*: after the repair it syncs again from where the stopped sync left off.
- A device mounted by SyncMusic inside a container that also shows the host's file system (for example under `/mnt/root`) is no longer reported as "mounted a second time", which stopped syncs and repairs.
- Compás (USB and the SyncMusic app): star ratings now sync both ways: stars you give on the player become the song's rating in your library and your library's ratings show on the player. Hearts and stars pass as they are: players with star ratings (Compás 1.6.0 and newer) sync ratings only, older ones favourites only; a favourite is never turned into 5 stars or back.
- Devices: requests from players that want to connect show the name and code the same way on every row and are separated from each other.
- A playlist edited on a Wi-Fi client whose file name differs from the library name (for example `Rock - 80s` for `Rock: 80s`) now updates the library playlist instead of creating a new one.
- A podcast episode or audiobook marked finished in the library without a new position now shows as finished on file-based players too.
- Podcast episodes marked finished near their end are no longer reported back as played to the end again on every sync.
- HiBy R1 (Compás): an audiobook's bookmark no longer jumps back to an earlier chapter that the library marks as listened to.
- Wi-Fi clients no longer download unchanged playlists again on every sync.
- A newly paired Wi-Fi client with nothing chosen for it yet gets a clear reason (`no_templates`) instead of a server error, and the server logs one warning without a traceback.
- A player mounted by another program through FUSE (exfat-fuse, ntfs-3g) or read-only is now recognised as mounted: SyncMusic uses that mount instead of mounting the player a second time, and refuses to write through a read-only one.
- A sync stops before its next write when another program mounts the player a second time while SyncMusic is writing to it, and the device page shows why.
- The device page's Capacity card keeps showing the disk space when the sync records are damaged, instead of an error.
- A device whose size or path-length limits changed (for example a disk first seen as FAT, later as NTFS) no longer gets every file written again: files that fit the new limits stay. A disk the operating system mounted with ntfs-3g is now recognised as NTFS. The sync report lists how many files were replaced and why.
- An automatic eject after a sync no longer crashes when the operating system had already unmounted its own mount of the disk.
- A player whose sync record was deleted (for example by a file system repair) is no longer treated as a new, unmanaged device: its backup copy is used.
- A damaged SyncMusic list on a USB-storage player ("manifest is not UTF-8") no longer stops every sync. SyncMusic now keeps a second copy next to it and uses that copy if the first one is damaged.
- The log no longer fills with hundreds of "event subscriber queue full" warnings during a sync while a browser tab is slow. Progress updates for a slow tab are merged, other events always arrive, and the warning appears at most once a minute.

## [0.2.5] - 2026-11-13

### Changed
- Positions moved back on a player: less than 30 seconds before the one the last sync left there is ignored, 30 seconds or more is the listener's and goes to the library (every device and Wi-Fi clients; on Compás a rewind is no longer undone).
- The Settings page is split into clearer groups: General, New devices (when to sync, after a sync, writing), Disks & mounting, Libraries & conversion, Wi-Fi clients, Tools, Storage & backups, Notifications, Updates, Maintenance and About; rarely changed options are folded under "More options". No setting changed its value.
- A device's Settings tab is grouped the same way: General, When to sync, After a sync and mounting (now including the mount options), Safety (delete threshold, safety margins, device database backups), Listening data and Writing, with the remaining options under Advanced.
- The SonixOS device type is now called *HiBy R1 - SonixOS*, like *HiBy R1 - Compás*.

### Fixed
- Playlists: merging a player's playlist into a library playlist of the same name no longer lists the songs both have twice.
- Card readers: a card registered through one reader can now be formatted (or wiped) in another reader too, like Eject and Repair already allowed.
- Formatting, partitioning and file-system repair now also see mounts made by FUSE (exfat-fuse, ntfs-3g) and wait a moment when the disk is still busy right after a mount, instead of running on a mounted volume or failing.
- A player registered by a folder and also paired with the Wi-Fi app now syncs through the folder while it is there, as the Devices list shows, and over Wi-Fi only when the folder is gone.
- A sync planned while a player was connected over Wi-Fi can no longer be applied to it after it is plugged in over USB; the plan is made again for the disk.
- Merging two records of one player no longer drops the Wi-Fi pairing when the kept record had an old, revoked pairing (the app was then refused although the page said paired).
- Card readers: a second card in the same reader is no longer taken for the first one (every card in a reader has the reader's serial); the card's own manifest or file system decides.
- Formatting an iPod now checks that the disk really is that iPod (by its serial) before formatting, like the other device types.
- When writing to a player stops with an I/O error mid-sync, SyncMusic now checks and repairs its file system and continues the sync from where it stopped, once; if the error comes back soon after, the device page says the storage may be failing.
- The damaged-file-system banner's button is now *Repair and continue*: after the repair it syncs again from where the stopped sync left off.
- USB SSDs and USB hard-disk enclosures registered as a device (for example as a backup disk) were refused with "not removable and not an iPod"; registered fixed USB disks now mount, sync and can be formatted.
- HiBy R1: *Switch to Compás* (and back to SonixOS) failed with "invalid settings"; the switch now keeps the settings the other firmware understands and uses its defaults for the rest.

## [0.2.4] - 2026-11-13

### Added
- Android (MTP): playlist files a player app edits or adds in the *Playlists* folder go back to your library with the next sync.
- Stock iPod: On-The-Go playlists made on the iPod go back to your library with the next sync.
- Client protocol: each file in a sync plan now carries a download link that works without the token header (for players whose built-in downloader cannot send one), and empty lists sent as `{}` are accepted.
- Wi-Fi clients (first part): the server can announce itself on the network and pair client apps with a short single-use code or a QR link; each client gets its own token that only works for its own device and can be revoked or renamed. Off until you allow Wi-Fi clients (API: `/api/v1/clients`, client side `/api/client/v1`).
- Wi-Fi clients (second part): a paired client becomes a device of its own and syncs through the client API: it reports what it holds and what it plays, the server converts and prepares its files with the device's processors, and the client downloads them (interrupted downloads resume), confirms each file and gets deletions for tracks no rule selects any more. Plans that need a confirmation wait for it on the server.
- Wi-Fi clients (third part): clients send what you did on them - plays, ratings, favourites and listening positions - as events; a batch sent twice counts once, events recorded offline can be sent later, and the next sync passes them to your libraries like a player's listening data.
- The device page of a Wi-Fi client shows the app, system, formats, free space, when it was last seen and the progress while it downloads; the device guide has a Wi-Fi client section.
- Settings → System → Wi-Fi clients: allow clients, announce the server on the network, add a client with a QR code and a pairing code (with a countdown), and rename or revoke paired clients.
- Documentation of the Wi-Fi client protocol for app authors (`docs/dev/client-protocol.md`) with an OpenAPI description of the client API.
- Plays, ratings, favourites and positions a Wi-Fi client sends reach the libraries within a minute, without waiting for the client's next sync.
- A track the user deletes in a Wi-Fi client app stays off that client instead of coming back with the next sync.
- Wi-Fi clients get each track's metadata and the libraries' ratings, favourites, play counts and resume positions with the sync plan.
- Playlists edited or made in a Wi-Fi client app go back to the library (as for Rockbox and HiBy players).
- A conformance check for Wi-Fi clients (`python -m syncmusic.client_conformance <server> <pairing code> <folder>`): pairs a throwaway client and checks what the server answers (pairing, tokens, plan paging, downloads with ranges, acks, repeated events, token rotation).
- A small reference Wi-Fi client (`python -m syncmusic.reference_client pair|sync|play|rate|favourite|position|events`) that pairs, syncs into a folder (interrupted downloads resume) and sends listening events - an example for client authors and a quick way to try Wi-Fi sync.
- HiBy R1 with Compás: with compas-sdtool (from Compás Tools, GPL-3.0) the sync builds the player's library on the computer, so new music is there right after unplugging. Set its path or download it (checksum, architecture and version checked) in Settings → Sync → HiBy R1 with Compás; release downloads include it for x86_64 and arm64 with its licence notices.
- HiBy R1 with Compás: favourites (hearts), play counts and last played from your library go to the player for the songs it has already indexed; a song added today gets them at the next sync after the player indexed it.
- Registering a device goes step by step (device, what goes on it, processors, first sync, summary) with Back and Next; *Show all steps on one page* keeps the single-page form.
- HiBy R1 with Compás: audiobooks and podcasts in a format their plugin cannot play (for example WavPack or DSD) are converted.
- HiBy R1 with Compás: podcast episodes synced by SyncMusic are listed in the Podcasts plugin's downloads.
- HiBy R1 with Compás: audiobook and podcast positions from your library are written to the player's Audiobooks and Podcasts plugins.
- USB disks registered as a backup keep the library's own folder structure (new path field `{library_path}`), and the folder picker shows the disk's free space.
- HiBy R1 with the Compás firmware: a new device type, used as USB storage. The player version and installed plugins are read from the card; audiobooks and podcasts are synced only when the Audiobooks or Podcasts plugin is installed. Wipe keeps the player's and the plugins' own files, including podcast episodes the plugin downloaded.
- HiBy R1 with Compás: play counts, last played and favourites come back from the player's library database, and audiobook and podcast positions from the Audiobooks and Podcasts plugins. A rebuilt or not yet updated player library never removes favourites.
- HiBy R1 with Compás: the device profile shows the player's firmware version and installed plugins, and the device page has tips for this player.
- HiBy R1: when a registered player comes back with the other firmware (SonixOS or Compás), its device page offers to switch the device type; the music on it stays.
- USB disks can be registered as a backup: the library files are copied as they are (no format limit, no processing), verified after writing, into a `SyncMusic` folder, and nothing is deleted unless you choose mirror mode. Register asks what a USB disk is for and lets you pick the folder on it.

### Changed
- When no template or rule syncs a media kind to a device any more, the next sync removes what SyncMusic itself put there of that kind, also when the rules only add (shown in the plan as "no longer synced"); what the player or other apps keep there stays.
- A newly registered USB flash drive or disk has no format limit by default: files are copied in the format the library has unless you choose formats. Disks registered before keep their formats.

### Fixed
- Playlists: merging a player's playlist into a library playlist of the same name no longer lists the songs both have twice.
- Card readers: a card registered through one reader can now be formatted (or wiped) in another reader too, like Eject and Repair already allowed.
- Formatting, partitioning and file-system repair now also see mounts made by FUSE (exfat-fuse, ntfs-3g) and wait a moment when the disk is still busy right after a mount, instead of running on a mounted volume or failing.
- A player registered by a folder and also paired with the Wi-Fi app now syncs through the folder while it is there, as the Devices list shows, and over Wi-Fi only when the folder is gone.
- A sync planned while a player was connected over Wi-Fi can no longer be applied to it after it is plugged in over USB; the plan is made again for the disk.
- Merging two records of one player no longer drops the Wi-Fi pairing when the kept record had an old, revoked pairing (the app was then refused although the page said paired).
- Card readers: a second card in the same reader is no longer taken for the first one (every card in a reader has the reader's serial); the card's own manifest or file system decides.
- Formatting an iPod now checks that the disk really is that iPod (by its serial) before formatting, like the other device types.
- When writing to a player stops with an I/O error mid-sync, SyncMusic now checks and repairs its file system and continues the sync from where it stopped, once; if the error comes back soon after, the device page says the storage may be failing.
- The damaged-file-system banner's button is now *Repair and continue*: after the repair it syncs again from where the stopped sync left off.
- Android: playlists a player app saved with absolute paths (`/storage/emulated/0/…`, `/storage/self/primary/…`, `/sdcard/…`) now go back to the library, and a device playlist whose entries match no synced song no longer empties the library playlist.
- Taking over a player that another SyncMusic synced no longer copies everything again as `(2)` files when the library's item ids changed in between (server reinstalled or library re-created): the files are recognised and kept.
- *Make this instance home* now also works for a player that was synced by another registration of it, so Wipe and Format are available after registering it again instead of being refused.
- HiBy R1 with Compás: playlists edited or made on the player (songs added, removed or reordered) go back to your library on the next sync instead of being overwritten; Navidrome smart playlists stay read-only and are written again from the server.
- Pairing a Wi-Fi client while the web UI is opened via localhost now shows the server's network address in the QR code instead of an address the phone cannot reach.
- HiBy R1 with Compás: the "restart the player or refresh the plugins" hint now also shows when ejecting and in the sync notification, and a position the Audiobooks or Podcasts plugin overwrote with its old one is written again by the next sync instead of being sent back to the library.
- Register a device: the "What is this disk for?" question is spaced like the other fields, and on a phone Cancel and Next sit side by side.
- HiBy R1 with Compás: *Wipe* is available in Device settings (it was refused with "format it instead"); it keeps the player's library folder, plugins, themes, radio stations and firmware updates.
- HiBy R1 with Compás: audiobooks and podcast episodes finished in the library are marked finished/played in the plugins, and their lengths are written instead of 0.
- HiBy R1 with Compás: after a sync that changed audiobooks or podcasts the report tells you to restart the player (or refresh its plugins), so the new positions are picked up.
- Playlist names with a colon are written as "Name - Part" instead of "Name_ Part" on players whose file system cannot store ':'. HiBy R1 with Compás gets its playlists as `.m3u` like the player's own.
- After a library server was reinstalled or a library re-created (new item ids, same files), the next sync no longer copies everything again as "(2)" files: the items on the device are re-linked to the new ids, and their play counts, favourites and positions still reach the library.
- Hardware allowlist: an entry whose USB serial every unit of the model shares (HiBy R1) now matches a disk only when its file system UUID matches too, so a reformatted or another unit is no longer accepted by the serial alone.
- HiBy R1 with Compás: audiobooks and podcast episodes that were never played no longer send a 0-second position back to the library.
- HiBy R1 (SonixOS): a full sync no longer resets the player's podcast subscriptions and episodes, or its audiobook list, when the device's templates do not sync podcasts or audiobooks.
- Quality profiles on a USB disk without a format limit: *Maximum quality* no longer shows a convert-everything-to-MP3 step, and *Save space* converts only lossless files.

## [0.2.3] - 2026-11-10

### Added
- HiBy R1: a *Split genres* device setting (use the player's setting, off, or on with the chosen separators and a live example); the sync report warns when it differs from the player's own setting.
- *Undo these changes* in a sync's History entry sends the library's previous favourites and ratings back when a sync changed them by mistake.
- HiBy R1: a "Tips for this player" card on the device page with the recommended habits and player settings (eject, Detect changes, split settings, file system).

### Fixed
- Playlists: merging a player's playlist into a library playlist of the same name no longer lists the songs both have twice.
- Card readers: a card registered through one reader can now be formatted (or wiped) in another reader too, like Eject and Repair already allowed.
- Formatting, partitioning and file-system repair now also see mounts made by FUSE (exfat-fuse, ntfs-3g) and wait a moment when the disk is still busy right after a mount, instead of running on a mounted volume or failing.
- A player registered by a folder and also paired with the Wi-Fi app now syncs through the folder while it is there, as the Devices list shows, and over Wi-Fi only when the folder is gone.
- A sync planned while a player was connected over Wi-Fi can no longer be applied to it after it is plugged in over USB; the plan is made again for the disk.
- Merging two records of one player no longer drops the Wi-Fi pairing when the kept record had an old, revoked pairing (the app was then refused although the page said paired).
- Card readers: a second card in the same reader is no longer taken for the first one (every card in a reader has the reader's serial); the card's own manifest or file system decides.
- Formatting an iPod now checks that the disk really is that iPod (by its serial) before formatting, like the other device types.
- When writing to a player stops with an I/O error mid-sync, SyncMusic now checks and repairs its file system and continues the sync from where it stopped, once; if the error comes back soon after, the device page says the storage may be failing.
- The damaged-file-system banner's button is now *Repair and continue*: after the repair it syncs again from where the stopped sync left off.
- A player whose database lost its favourites (reset, repaired or rewritten) no longer removes them from the library: when ten or more and over half of the hearts vanish at once, SyncMusic sends no unfavourite, warns on the sync report and writes the hearts back to the player. The same goes for ratings cleared at once on an iPod or Rockbox player whose database was reset: the library keeps them and the next sync puts them back on the player. If the favourites were removed on purpose, *Remove them from the library* in the report's History entry sends the removals.
- HiBy R1: the player's file system could be damaged on the next connect after a sync. Its library databases are now built on the computer and replaced on the player in one safe step, and only when something in them changed; updates with nothing new no longer write to the player.
- A player whose file system is damaged (mounted read-only by the system, or the player's databases unreadable) is flagged with "file system damaged" and the Repair action as soon as it is connected, not only after a sync failed, and no automatic sync starts on it.

## [0.2.2] - 2026-11-08

### Added
- Upgrades are supported from 0.2.0 on: a new version updates SyncMusic's database at its first start and keeps all settings, devices, templates and history. A backup of the old database is saved first; if the update fails, the database stays as it was.
- The Storage health card links each recorded error to the job it happened in.
- The "storage may be failing" warning can be acknowledged on the dashboard or the device's Storage health card (with Undo): it stays hidden until a newer error. The card lists the recorded errors and can clear the error history after the storage was replaced or reformatted; a Format offers to clear it too.
- Plays of items another SyncMusic installation put on the player are kept on the player for that installation (an iPod resets its play counts at every sync), so each installation sends the plays of its own libraries and none are lost.
- Two SyncMusic installations never write to one player at the same time: the one that is syncing marks the player, the other stops with "being synced by …" until it is done (a forgotten mark expires after ten minutes); the device page shows it. The plan counts the items other installations put on the player.
- The templates a device uses are stored on the player. Another SyncMusic installation that syncs it lists them on the device's Templates tab and can import them as linked copies, and update a copy when the original changes.
- Unmount everywhere: a player that another program (the system's automount) mounted a second time can be unmounted everywhere from Settings → Danger zone or straight from Check and repair, which used to fail with "unmount it first". Programs that still use it are named, with an explicit "Detach anyway". The device page says when the system mounted a player too, and the new device setting "Unmount other mounts when connected" removes such mounts automatically.
- Settings → System → Storage & resources: what SyncMusic keeps on disk (database, backups, logs, caches, player database copies, temporary files, the program itself) with sizes, folders and one clear action each, plus memory and CPU use and a "Free memory now" button. The Clear buttons next to the cache settings moved there.
- A player synced by another SyncMusic installation can be continued on Register (its items stay, yours are added), started fresh or registered anew. The player's home installation alone may wipe or format it and remove files SyncMusic did not put there; the Danger zone shows the home and can make this installation home.
- Settings → Appearance has an instance name (the host name by default), shown under the logo and in About; each installation also gets its own id.

### Changed
- A pending sync plan needs less memory (about 2 KB less per track for albums without a disc-total tag).
- Cached library contents take less than half the memory they used to (about 25 MB instead of 60 MB per 15,000 tracks).
- *Free memory now* also drops a pending sync plan that would change nothing on the device; plans with changes stay.
- Syncs that change only some items of a big player write just those changes to the player's state file instead of rewriting all of it, so progress saves and small update syncs finish much faster.
- The device page's capacity bar shows the space items of other SyncMusic installations take as a segment of its own.
- Player contents filters by "Managed by" (this installation, other installations, not from SyncMusic) instead of the "Only not from SyncMusic" checkbox; items another installation manages cannot be selected for removal.
- On a player whose home is another installation, the Rockbox play log setting is left to the home: syncs do not change the player's config, and the switch says why it is refused.
- "Free memory now" also drops the cached device manifests, which the memory overview now lists.
- SyncMusic gives memory back to the system after every job and on "Free memory now", instead of staying at its peak size after a big sync.
- Adding templates to a device happens in a panel under the list instead of a side panel that covered the page: you can add several in a row and watch the preview and the Save bar meanwhile.
- Side panels (notifications, the not-delivered list) close when you go back or open another page; on a phone, Back closes the panel instead of leaving the page.
- The Audiobookshelf default template "Unlistened podcast episodes" is now "Unfinished podcast episodes": an episode you started stays on the device until you finish it (before, it dropped out as soon as you started it). Templates you edited yourself are not changed.
- The web UI and API send compressed responses: the Player contents of a big device loads several times faster over Wi-Fi.
- Player contents of a big USB drive or memory card opens quickly after a restart: tags read once are remembered, and a sync with nothing to change no longer rewrites the device's state file (which made the next contents read start over).
- The state file SyncMusic keeps on a player now names its libraries by a stable id and records which installation owns the player and which wrote it last, so another SyncMusic installation can recognise the same library and leaves items of libraries it does not have alone. Players synced by an older version are upgraded at their next sync.

### Fixed
- Playlists: merging a player's playlist into a library playlist of the same name no longer lists the songs both have twice.
- Card readers: a card registered through one reader can now be formatted (or wiped) in another reader too, like Eject and Repair already allowed.
- Formatting, partitioning and file-system repair now also see mounts made by FUSE (exfat-fuse, ntfs-3g) and wait a moment when the disk is still busy right after a mount, instead of running on a mounted volume or failing.
- A player registered by a folder and also paired with the Wi-Fi app now syncs through the folder while it is there, as the Devices list shows, and over Wi-Fi only when the folder is gone.
- A sync planned while a player was connected over Wi-Fi can no longer be applied to it after it is plugged in over USB; the plan is made again for the disk.
- Merging two records of one player no longer drops the Wi-Fi pairing when the kept record had an old, revoked pairing (the app was then refused although the page said paired).
- Card readers: a second card in the same reader is no longer taken for the first one (every card in a reader has the reader's serial); the card's own manifest or file system decides.
- Formatting an iPod now checks that the disk really is that iPod (by its serial) before formatting, like the other device types.
- When writing to a player stops with an I/O error mid-sync, SyncMusic now checks and repairs its file system and continues the sync from where it stopped, once; if the error comes back soon after, the device page says the storage may be failing.
- The damaged-file-system banner's button is now *Repair and continue*: after the repair it syncs again from where the stopped sync left off.
- A player whose database lost its favourites (reset, repaired or rewritten) no longer removes them from the library: when ten or more and over half of the hearts vanish at once, SyncMusic sends no unfavourite, warns on the sync report and writes the hearts back to the player. The same goes for ratings cleared at once on an iPod or Rockbox player whose database was reset: the library keeps them and the next sync puts them back on the player.
- Opening a notification from the bell drawer no longer bounces back to the previous screen when the page is slow.
- Device settings: the orphans card now updates by itself when the device becomes free again after a sync, instead of staying on the old state until a reload.
- Leaving the plan screen while a plan is still being computed no longer sometimes jumps back to the finished plan.
- With sign-in enabled, a browser without a session reloaded endlessly instead of showing the sign-in screen.
- The device page of a player whose device type this version does not know (for example after a type was renamed) no longer fails with "Something went wrong": it says "Unknown device type" and still lets you remove the device.
- A device whose type this version does not know (renamed or removed) no longer breaks its page: it says so and offers Delete.
- Big first syncs are much faster: the processing cache no longer re-adds up its whole size for every file (a 15,000-track first sync took less than half the time).
- Items another SyncMusic installation put on a player are no longer listed as orphans in Player contents; they are marked "Another installation".
- Installing or updating with the installer now removes old unpacked versions (only the new and the previous one stay), so they no longer pile up under /opt/syncmusic/runtime.
- The web UI no longer freezes for seconds while a plan for a large library is being built.
- Cancelling a sync no longer can leave a half-downloaded file behind in the temporary work folder.
- The web UI stays responsive while an update sync of a large device writes its database: no more half-second freezes.
- First syncs of large libraries onto USB flash drives, Rockbox and HiBy players no longer slow down with every track written.
- Opening a device, planning and Player contents no longer read and parse the whole device manifest again each time: it is read once and reused until it changes.
- Albums whose files were only touched on the server (same file, new timestamp) are no longer copied to the device again.
- The plan no longer suggests a processor template that processes files exactly like the device's own chain already does.
- Device settings open at once on players with many files: the orphan list loads on its own afterwards and no longer reads the whole player.
- Unlinking a template (for example "All podcasts") from a device no longer turns its items into orphans: in mirror mode they are planned for removal like anything else no longer selected, additive mode keeps them. Items stay orphans only when their library was removed, disabled or no longer has them.
- Saving a device's rules or linked templates is quick again on players with many files: it no longer re-reads the whole on-device sync record of every connected player, and the rest of the app no longer waits for it.

## [0.2.1] - 2026-11-06

### Added
- Settings → System → Storage & resources: what SyncMusic keeps on disk (database, backups, logs, caches, player database copies, temporary files, the program itself) with sizes, folders and one clear action each, plus memory and CPU use and a "Free memory now" button. The Clear buttons next to the cache settings moved there.
- **Install into one folder, started by hand**: `install.sh --dir DIR [--port N]`. Before, the installer could only set
  SyncMusic up as a systemd service, which needs root and changes the system. Now everything lives in one folder:
  the web build, `config.toml`, data, cache, logs, the mount root, a `syncmusic.env` with all paths and the port, and
  `run.sh` to start it. Running the installer again updates the binary and keeps config, data and port;
  `--uninstall --dir DIR` removes it (`--purge` also the data). Why: to try SyncMusic, or to run it as a normal user,
  without a service. Mounting players still needs root or passwordless `sudo` for the block-device tools.

### Fixed
- Playlists: merging a player's playlist into a library playlist of the same name no longer lists the songs both have twice.
- Card readers: a card registered through one reader can now be formatted (or wiped) in another reader too, like Eject and Repair already allowed.
- Formatting, partitioning and file-system repair now also see mounts made by FUSE (exfat-fuse, ntfs-3g) and wait a moment when the disk is still busy right after a mount, instead of running on a mounted volume or failing.
- A player registered by a folder and also paired with the Wi-Fi app now syncs through the folder while it is there, as the Devices list shows, and over Wi-Fi only when the folder is gone.
- A sync planned while a player was connected over Wi-Fi can no longer be applied to it after it is plugged in over USB; the plan is made again for the disk.
- Merging two records of one player no longer drops the Wi-Fi pairing when the kept record had an old, revoked pairing (the app was then refused although the page said paired).
- Card readers: a second card in the same reader is no longer taken for the first one (every card in a reader has the reader's serial); the card's own manifest or file system decides.
- Formatting an iPod now checks that the disk really is that iPod (by its serial) before formatting, like the other device types.
- When writing to a player stops with an I/O error mid-sync, SyncMusic now checks and repairs its file system and continues the sync from where it stopped, once; if the error comes back soon after, the device page says the storage may be failing.
- The damaged-file-system banner's button is now *Repair and continue*: after the repair it syncs again from where the stopped sync left off.
- First syncs of large libraries onto USB flash drives, Rockbox and HiBy players no longer slow down with every track written.
- Opening a device, planning and Player contents no longer read and parse the whole device manifest again each time: it is read once and reused until it changes.
- **Library Refresh button stayed disabled.** Before: on a small library (or a busy host) the refresh could finish
  before the request that started it returned; the page then never saw the end of the job and kept the button
  disabled until a reload. Now the page checks the job once after starting it and reports a job that is already done.
- **Pages stalled after playing files in the browser player.** Before: a played file was sent as one long download;
  the browser kept that connection open even after the player was closed, and with its few connections per server
  used up, later requests of the page hung. Now files for the player are sent in short parts (1 MB), so every
  connection is released right away.
- **Screens did not update while a sync was running.** Before: every incoming event (progress comes every 100 ms)
  restarted the short wait before a redraw, so the screen could wait for a redraw that never came. Now a redraw that
  is already waiting is kept.
- **Update check showed the wrong running version** in some cases: the version was read once when the module was
  loaded instead of when the update state is made. Now it is read each time, so the update status compares against
  the version that is actually running.
- **Service installer ended with an error after a successful install.** Before: at exit its clean-up referred to a
  variable that no longer existed ("unbound variable"), so the script reported an error and left its download
  folder behind. Now the download folder is removed and the script exits cleanly.

### Changed
- SyncMusic gives memory back to the system after every job and on "Free memory now", instead of staying at its peak size after a big sync.
- Beta releases list the changes collected for the next version in their notes, followed by the commit subjects.
- **README**: new tables of the supported libraries (with what each one takes back from the device: play counts,
  ratings, favourites, positions) and of the supported devices (tested, untested, beta), and a roadmap. Why: what
  works with what was spread over several pages.
- Version on `main` is 0.2.1: builds from `main` are betas of the next release (`0.2.1-<build>`), newer than 0.2.0.

## [0.2.0]

The first public release.

### Added
- Settings → System → Storage & resources: what SyncMusic keeps on disk (database, backups, logs, caches, player database copies, temporary files, the program itself) with sizes, folders and one clear action each, plus memory and CPU use and a "Free memory now" button. The Clear buttons next to the cache settings moved there.
- **Libraries**: Navidrome (Subsonic API), a plain folder of audio files, Audiobookshelf (audiobooks and podcasts),
  Jellyfin (movies and episodes, one way to the player; beta).
- **Devices**: iPods with Apple's firmware (via pygpod; tested on the iPod Classic), Rockbox players, USB flash
  drives (vfat, exFAT, ext4, NTFS), a folder on an FTP or SMB server, and **HiBy R1** (SonixOS) players.
- **Rules and templates**: what goes onto a device, compatible with Navidrome smart playlists (`.nsp`), written in
  JSON/YAML or built in the UI, shared between devices as templates; default templates per library.
- **Processors** per device: transcode (FLAC → ALAC/AAC/MP3 …), retag, covers, lyrics (`.lrc`).
- **Reverse sync**: play counts, ratings, favourites and podcast/audiobook positions go back to Navidrome and
  Audiobookshelf.
- Web UI that also works on a phone, optional desktop window, REST API and command line client.
- Packaging: single-file binaries for Linux x86_64 and arm64 (`--install` as a systemd service), a Docker image,
  install from source.

### Known limitations
- **No authentication:** run SyncMusic only on a trusted local network.
- **Fresh installs only:** there are no database migrations yet; a later version may require starting over. A
  database SyncMusic does not know is refused at start with a message (delete it, or restore a backup).
- iPod nano 6G/7G are refused (their database checksum cannot be written).
- Linux only (x86_64, arm64 incl. Raspberry Pi; glibc ≥ 2.28). No Windows, macOS or Android/MTP yet.
