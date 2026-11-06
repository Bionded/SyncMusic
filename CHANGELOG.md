# Changelog

All notable changes to SyncMusic are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/),
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Install into one folder, started by hand**: `install.sh --dir DIR [--port N]`. Before, the installer could only set
  SyncMusic up as a systemd service, which needs root and changes the system. Now everything lives in one folder:
  the web build, `config.toml`, data, cache, logs, the mount root, a `syncmusic.env` with all paths and the port, and
  `run.sh` to start it. Running the installer again updates the binary and keeps config, data and port;
  `--uninstall --dir DIR` removes it (`--purge` also the data). Why: to try SyncMusic, or to run it as a normal user,
  without a service. Mounting players still needs root or passwordless `sudo` for the block-device tools.

### Fixed
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
- Beta releases list the changes collected for the next version in their notes, followed by the commit subjects.
- **README**: new tables of the supported libraries (with what each one takes back from the device: play counts,
  ratings, favourites, positions) and of the supported devices (tested, untested, beta), and a roadmap. Why: what
  works with what was spread over several pages.
- Version on `main` is 0.2.1: builds from `main` are betas of the next release (`0.2.1-<build>`), newer than 0.2.0.

## [0.2.0]

The first public release.

### Added
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
