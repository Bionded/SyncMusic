# Changelog

All notable changes to SyncMusic are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/),
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
