# 4. Device guides

What SyncMusic does differently for each kind of player. Every device can be renamed, its templates and processors
changed and its history viewed at any time; **Format** and **Wipe** always ask you to type the device name first.

With two or more players connected, **Sync all connected** (dashboard, or the sync icon next to *Connected* in the
sidebar) starts a full sync or an update on each of them. Devices that are busy, ejected or have no templates are
skipped and named; a device whose plan needs a review waits for you on its own page.

**What SyncMusic touches:** only what it put on a device itself. Music, podcasts, audiobooks or videos that the player
or another app keeps there stay as they are in every mode, also with *Mirror* (you can remove them yourself in
*Player contents*). When you stop syncing a kind to a device (no template or rule selects podcasts, say, any more),
the next sync removes the podcasts SyncMusic had put there - also when the rules only add - and lists them in the
plan as *no longer synced*, with the usual confirmation for many removals.

## iPod (Apple firmware)
- **Models:** iPods with a hard disk or flash that use Apple's iTunes database (tested on the iPod Classic).
  The **iPod nano 6G and 7G are refused**: their database needs a checksum that cannot be written.
- **No iTunes needed.** SyncMusic writes the iPod database, artwork and playlists itself and saves them crash-safely.
- **On-The-Go playlists** you make on the iPod go back to your library with the next sync, named
  *On-The-Go <date time>*, and are then cleared from the iPod (an On-The-Go list that could not be saved to a library
  is kept on the iPod as a normal playlist instead).
- **Formats:** MP3, AAC, ALAC, WAV, AIFF - no FLAC, Opus or Vorbis. The suggested processor chain converts FLAC to
  ALAC (lossless) or AAC; 24-bit or high sample-rate files are converted down to what the iPod plays.
- **Artwork** in the sizes of your model; **Sound Check** from the library's ReplayGain tags.
- **Podcasts and audiobooks** appear in the iPod's Podcasts and Audiobooks menus; an audiobook made of many files can
  be combined into one track with chapters.
- **Reverse sync:** play counts, ratings (stars) and bookmarks (positions) go back to the libraries.
- **Clock:** the iPod's clock is set on every sync.
- **Format** erases only the **data partition**; the partition table and the firmware partition are never touched.
- An iPod running **Rockbox** can be registered as Rockbox or (if it still starts Apple's firmware, hold *Menu* while
  booting) as a stock iPod.

## Rockbox
- **Any Rockbox player** (iPods with Rockbox, AIGO EROS Q, …) - recognised by the `/.rockbox` folder.
- **Music only**: podcasts and audiobooks are not synced to Rockbox players.
- **Formats:** almost everything (MP3, FLAC, Ogg, Opus, AAC, ALAC, WavPack, …); the suggested chain copies files as
  they are, or "Rockbox: MP3 320" to save space.
- Playlists are written as `.m3u8` files; Rockbox rebuilds its database after the sync (*Settings → General →
  Database → Initialize now* on the player if it does not).
  Playlists you edit or make on the player go back to your library (not Navidrome smart playlists).
- **Reverse sync:** play counts from Rockbox's playback log (*Settings → Playback → Logging* must be on).
- **Wipe** removes all music, playlists, Rockbox's database and play logs; Rockbox itself and its settings stay.

## HiBy R1 (SonixOS)
- **Reboot the player after every sync** (or after unplugging) so it reads the new music, playlists and
  favourites. SyncMusic reminds you of it.
- Music goes to `Music/`, podcasts to `Podcast/<show>/`, audiobooks to `Audiobooks/`, with `.lrc` lyrics and cover
  art. All formats the R1 plays are copied as they are.
- **Playlists** are written into the player's library database (the R1 ignores `.m3u8` files). Playlists you made on
  the player stay, and are sent back to Navidrome by the reverse sync. A player-made playlist that exists in the
  library but is in none of your rules is removed from the player.
- **Favourites (hearts) ↔ Navidrome stars**, in both directions. The R1 has no star ratings.
- **Reverse sync:** play counts, favourites, audiobook and podcast positions.
- A **full sync** rebuilds the player's databases from your libraries (after the reverse sync), so the player's
  history and "now playing" state start empty. Updates change only what changed.
- **Wipe** erases music, playlists, podcasts and audiobooks and the player's library, play counts, favourites and
  resume positions; its settings stay. **Format** creates a new exFAT (or FAT32 / NTFS) file system on the whole
  player. The firmware is never touched.
- Before each change to the player's databases SyncMusic keeps a copy under `<cache>/device-backups/`.
- The player's databases are built on the computer and copied onto the player only when their content changed, by
  writing a new file and renaming it over the old one, so an unplug never leaves a half-written database behind. An
  update with nothing new writes nothing to the player.
- **Eject before unplugging**, and after unplugging let the R1 finish updating its library before you connect it
  again or switch it off. If the player's file system is reported as damaged, use **Repair** on the device page (or
  check the card on a computer: `fsck.exfat` / `fsck.vfat`). SyncMusic checks this as soon as the player is
  connected (the card mounted read-only, or its databases unreadable) and starts no automatic sync until it is repaired.

### Tips for the HiBy R1
The device page shows these as *Tips for this player* (Overview tab; close the card to keep it collapsed).
- **Eject** in SyncMusic before unplugging. After unplugging, let the R1 finish updating its library before you
  connect it again or switch it off - this protects its library database.
- If new music does not show after a sync, **reboot the player** (the *Last full sync* card shows this hint after
  every sync, so the tips card does not repeat it).
- **Detect changes** (on the R1: *Settings → Library*) can be off when only SyncMusic fills the player: it starts
  faster and does not rewrite its database. Keep it on if you also copy files by hand. SyncMusic cannot change it.
- **Split genres**, **Split artists** and **Join albums** are settings of the player (*Settings → Library*).
  *Device settings → Player library → Split genres* follows the player by default and can be set to **Off** or **On** (with the
  separators: `;` `/` `&` `,` `+` and a live example), but the R1 files its genres again its own way after
  unplugging: choose the same on the player (*Settings → Library*). The sync report warns while the two differ.
- **exFAT** is the recommended file system for big cards. If the player or the computer reports file system errors,
  run **Repair** on the device page.
- Do not let the computer's own automounter (or a file manager window) open the player while SyncMusic uses it.

## HiBy R1 (Compás)
- **The R1 with the Compás firmware**, connected as **USB storage** (choose *Storage* on the player when you plug it
  in). Recognised by the `.compas` folder on its card.
- **Folders:** music in `Music/`, audiobooks in `Audiobooks/`, podcasts in `Podcasts/`, playlists as `.m3u` in
  `Playlists/` like the player's own (the player lists them by file name, extension included; a `:` in a name,
  which the card cannot store, becomes ` - `); lyrics as `.lrc` next to the track, a `cover.jpg` per album.
- **Playlists edited on the player** (songs added, removed or moved, new playlists) go back to your library with the
  next sync, before SyncMusic writes the playlists again, so nothing you changed on the player is lost. Navidrome
  smart playlists are the exception: the server computes them, so the sync log notes the edit and the player gets
  the server's list again.
- **Audiobooks and podcasts need the player's plugins:** with the *Audiobooks* plugin installed audiobooks are synced
  (the plugin plays them, with positions and bookmarks); with the *Podcasts* plugin podcasts are synced into
  `Podcasts/` and kept out of the music library. Without a plugin that kind is not synced. Podcast episodes you
  synced appear under the Podcasts plugin's *File Manager*; the plugin's own subscriptions and downloads stay as they are.
- **Restart after a sync that changed audiobooks or podcasts:** the plugins read their positions only when they
  load, so the sync report, the sync notification and the *can be unplugged* message say *Restart the player or open
  Settings → System → Plugin Manager → Refresh*. Playing a book before that starts it from the old position and the
  plugin overwrites the new one; the next sync notices that (the position SyncMusic wrote is missing or went back)
  and writes it again instead of sending the old position to the library.
- **The player's library, built during the sync:** with **compas-sdtool** SyncMusic builds the player's library on
  the computer at the end of each sync (the player's own indexing code, so the player keeps it), and the new music,
  hearts and play counts are there right after unplugging. Set its path or download it in *Settings → Sync →
  HiBy R1 with Compás*; the device page offers the download too. Without it, Compás indexes the new music itself
  after you unplug (or use *Settings → Update Music Database* on the player). compas-sdtool is part of
  [Compás Tools](https://github.com/Starnished66/compas-tools) by the Compás authors, built from the
  [Compás player](https://github.com/Starnished66/compas-player) sources, free software under the GPL-3.0; SyncMusic
  runs it as a separate program. A compas-sdtool for another library format than the player's is not used.
- **Hearts and play counts to the player:** favourites, play counts (the higher of player and library) and last
  played from your library are written into the player's library for songs it has already indexed (a song synced
  today gets them at the next sync, after the player indexed it). Only the player's library format 8 is written.
- **Reverse sync:** play counts, last played and favourites (hearts) from the player's library; audiobook positions
  from the Audiobooks plugin and positions of the Podcasts plugin's episodes. A favourite counts as removed only for a
  file the player's library still lists, so a rebuilt library never removes favourites. Favourites from your library
  are not written to the player.
- **Positions to the player:** an audiobook or podcast you listened further in your library (Audiobookshelf) opens
  there on the player too: the sync writes the position into the Audiobooks plugin's state (one position per book)
  and into the Podcasts plugin's list for the episodes it lists. A position moved on the player since the last sync
  wins and goes to the library instead.
- **Formats per kind:** music goes in every format the player plays; audiobooks only in formats the Audiobooks plugin
  plays (MP3, AAC/ALAC in M4A/M4B, FLAC, Ogg, Opus, WAV, AIFF, APE) and podcasts only in those of the Podcasts plugin
  (MP3, AAC/ALAC in M4A, Ogg, Opus, FLAC); anything else of that kind is converted by the *Convert* step.
- **Podcasts in the plugin:** with the *Podcasts* plugin, the episodes SyncMusic puts on the player are listed in the
  plugin's *Downloads* (as new, with their title and date; their show is shown as *Unsubscribed* because the plugin
  names shows from its own subscriptions). They leave the list when SyncMusic removes them; the plugin's own
  downloads and subscriptions are not touched.
- **Wipe** removes the music, audiobooks, podcasts and playlists; the player's own files (`.compas`, plugins, themes,
  radio list, firmware updates) and the Podcasts plugin's subscriptions and downloads stay.
- **Switching firmware:** when a registered R1 comes back with the other firmware (SonixOS ↔ Compás), its device page
  says so and offers *Switch to Compás* (or *Switch to SonixOS*). That changes only the device type: rules, templates
  and the music on the card stay; the next sync writes what the new firmware expects.

## USB flash drive or memory card
- For car stereos, simple players and anything that reads a folder of files. **One way only**: nothing is read back.
- **File systems:** exFAT or FAT32 recommended (FAT32 has a 4 GB file limit); ext4 and NTFS work too. NTFS that was
  unplugged without *Eject* may refuse to mount ("volume is dirty"): repair it in Windows (`chkdsk /f`) or format it.
- **Device settings:** the formats the target player plays (none chosen = no limit: every file is copied in the
  format the library has; disks registered with an older version keep the formats they had), the **folder layout** (e.g. `Artist/Album/01 Title`),
  ASCII-only names for old players, lyrics as `.lrc` or embedded, `.m3u8` playlists on or off.
- **Format** may create a new partition table and one partition (vfat, exFAT, ext4 or NTFS).
- **Car stereo / old player mode** (*Device settings → Files on the device*): file and folder names in plain ASCII
  (Cyrillic and accents transliterated, emoji and other characters replaced) and short paths - at most 128
  characters per path and 64 per name by default, both adjustable - and at most 255 files in one folder (the next
  ones go to *Album (2)*, *Album (3)* …). After switching it, the next sync renames the files already on the drive.
- **USB disk as a backup:** on *Register*, choose *What is this disk for? → A backup* (API:
  `"settings": {"purpose": "backup"}`); *Folder on the disk* (with *Browse…*, which can also add a new folder) says
  where the files go.
  It then keeps the library files as they are: no format limit and no conversion, tags, covers and lyrics left as
  they are (the processing steps are off; turn one on if you want it), every media kind, the files verified after
  writing, and everything inside the folder `SyncMusic` unless you choose another. A backup only adds: files
  removed from the library stay on the disk unless you switch the rules to *Mirror*.
  The files keep the library's own folder structure (path template `{library_path}` for every kind): a folder
  library's relative paths, a Subsonic server's paths where it reports them; a file whose library does not know its
  path is placed by the usual template of its kind (*Device → Settings → Paths*). The free space of the disk is shown
  in the folder picker.

## Android phone or player (MTP) - Beta
- Plug it in, unlock it and choose **File transfer** in its USB menu: it appears among the found devices (dashboard,
  *Register a device*) with its model, its USB serial and a **Beta** badge; register it from there like a drive. A
  registered player is recognised whenever it is plugged in (on-connect sync, sync windows, eject after a sync).
- On *Register a device* the USB serial is shown read-only (it is the player's identity). Choose the **Storage**
  (read from the player, with size and free space; an SD card is preselected) and the **Folder on the device**:
  `./` (the storage's top folder, the default) or one picked with the folder button next to the field.
  Playlists go as `.m3u8` into its *Playlists* folder; a player app that edits them or saves new ones there sends
  those changes back to your library with the next sync (not Navidrome smart playlists). **Wipe** deletes only the files SyncMusic put there; the
  player is never formatted.
- **Settings → Files on the device** has the same **path layout** as a USB drive (music, multi-disc music, podcasts,
  audiobooks, video, each with a live example), the **Folder on the player**, the playable formats and playlists on
  or off. After a layout change the next sync re-writes the files at their new paths and deletes the old ones.
- **Eject** ends SyncMusic's use of it; unplug it afterwards. There is no storage health or integrity check.
- Known limitations: no play counts, ratings or positions come back (they live in each player app); the phone's
  media scanner may take a while to show new files; a file manager (gvfs, KDE) that holds the phone makes it
  **busy** - close it or re-plug; a locked phone shows **no storage** - unlock it. Needs `libmtp` on the host.
- Tested on a GammaOS Next handheld (sync, playlists, second sync, eject, Wipe). Whether a player app picks the
  files up depends on the phone's media scanner and has not been checked yet - tell us how it goes on yours.
- Without detection (e.g. in a container) add it by hand under *A server … or an Android player (MTP)* with its USB
  serial.

## FTP server or SMB share
- For players, streamers or NAS folders reachable over the network. Register with *A server (FTP or SMB share)*:
  address, user (empty = anonymous on FTP, guest on SMB), password and the **folder** to write into.
- The folder gets the same layout as a USB drive; SyncMusic keeps its bookkeeping in a hidden `.syncmusic/` folder
  there. The FTP server must allow renaming and files whose names start with a dot.
- **Space:** SMB reports free space; FTP cannot, so set a **quota** (GB) in the device settings if the plan should
  check it.
- Network devices are never formatted or ejected.
- **Automatic syncs:** a server is never "plugged in", so it gets an **update every 6 hours** by default and, if you
  set one, a daily full sync (*Sync settings*). Before each automatic start SyncMusic logs in; when the server does
  not answer, the sync is skipped quietly, the device says **Not reachable since …**, and the next slot tries again.
  *Sync all* skips it too ("not reachable").

![Device settings of an iPod](img/device-settings.png)

## Wi-Fi client (phone or player app)
A client app that speaks the SyncMusic client protocol syncs over your network instead of a cable: SyncMusic
prepares the files (with the device's processors) and the app downloads them when it checks in.

1. **Settings → System → Wi-Fi clients**: turn on **Allow Wi-Fi clients** (and keep **Announce on the network** on so
   the app finds the server by itself).
2. **Add client** shows a QR code, the server address and a pairing code (valid for 10 minutes by default, single
   use). Scan the code in the app or type the address and code there. The dialog says when the app has paired.
3. The client appears as a device: link templates on its **Templates** tab like for any other player. It takes
   what is new the next time it checks in (every 5 minutes by default) or when you sync it in the app.

The device page shows what the app reported (app, system, formats it plays, free space, last seen) and the
progress while the app downloads. Plays, ratings, favourites and positions the app sends go back to your libraries
with its next sync. **Rename** or **Revoke** a client under Settings → Wi-Fi clients; a revoked client keeps its
device and history but can no longer sync until it is paired again. The connection is plain HTTP: use it only on a
network you trust.

## A player used by more than one SyncMusic
A player remembers which SyncMusic installation set it up: its **home**. When you register a player that another
installation (or an earlier registration) synced, the *First sync* step asks how to go on:

- **Continue** (the default) keeps everything on the player, including the other installation's items, and adds
  your libraries. Automatic sync on connect stays off here: it belongs to the home.
- **Start fresh** wipes the music on the first sync. Only the home can do this.
- **Keep files** registers the player as a new device; what is on it counts as files SyncMusic did not put there.

On a player whose home is another installation, **Wipe**, **Format**, removing files SyncMusic did not put there and
writing player settings are left to the home: the *Danger zone* says which installation that is.
**Make this instance home…** moves the home here (the other installation keeps syncing its own libraries).

Every full sync also stores the templates the device uses on the player (`.syncmusic/templates.json`; rules only,
no processors or device settings). On another installation the device's **Templates** tab then lists them under
**From other installations**: **Import** makes a copy of your own, linked to this device, with its libraries matched
to yours (the same server, or a library you paired). A template that draws only from libraries you do not have is
listed but cannot be imported. Your copy is never changed behind your back: when the original changes, the row says
*Updated on <installation>* and **Update my copy** takes the new version.

Only one installation writes to a player at a time: a sync, update or wipe marks the player as busy
(`.syncmusic/lease.json`, refreshed every minute). Another installation that wants to sync it meanwhile stops with
"being synced by <installation> since <time>"; a mark that has not been refreshed for ten minutes (that installation
stopped, or the player was unplugged mid-sync) is taken over.

## Gentle write mode
Some players (old iPods with tired disks, cheap cards) fail under long writes. *Settings tab → Gentle write mode*
writes in small batches with pauses and a rate limit. The plan suggests it when a device had write errors.
