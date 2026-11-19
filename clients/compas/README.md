# SyncMusic app for Compás players

A native external app for players running the Compás firmware (HiBy R1): it pairs with a SyncMusic server over
Wi-Fi and syncs music, audiobooks, podcasts and playlists onto the SD card, and sends plays, hearts, positions
and playlist edits back. It speaks the generic Wi-Fi client protocol (`docs/` → Wi-Fi clients).

- Language: Go, built as static little-endian MIPS32 ELFs (no cgo) in two builds: hard float (uses the FPU,
  recommended) and soft float (floats in software, the fallback). Each runs as `syncmusic-compas` and asks the
  server for updates of its own build.
- `./build.sh [VERSION]` → `out/syncmusic-compas-VERSION-hard.zip` and `-soft.zip`, each holding the `syncmusic/`
  folder to copy into `<SD card>/.apps/` (app.json, the program, icon, LICENSE, NOTICE, FONT-LICENSE.txt);
  `./smoke.sh` starts both under qemu-mipsel. Release builds attach both zips.
- `go test ./...` runs the unit tests on any host.

Third-party: the Inter typeface (Latin + Cyrillic), (c) 2016 The Inter Project Authors, SIL Open Font License 1.1
(`FONT-LICENSE.txt`). `fontgen/` (its own Go module) pre-renders it at the sizes the screens use into
`inter.atlas.gz` (4-bit antialiased glyphs, embedded): `cd fontgen && go run . Inter-Regular.ttf Inter-SemiBold.ttf
Inter-Bold.ttf ../inter.atlas.gz` with the TTFs of an Inter release.

## Using it on the player
- Volume up/down (or previous/next) move the selection, play selects; tapping a button works too.
- Home shows the server, the last sync, free space, battery and Wi-Fi; *Sync now* starts a sync, *Stop* ends it at
  a safe point. Settings: screen dim/off timers (never during a sync), dimmed brightness, what to do after a sync
  that changed something (ask / restart the player / power off / nothing).
- Pairing: *Find servers on the network* (mDNS) or *Enter the address* (on-screen keyboard), then *Connect without a code*
  (approve the player on the server's Devices page; both show the same code) or *Enter a pairing code*.
  `pair.txt` (`<server> <code>` in the app's data folder) still works. Settings → Server → *Forget this server*
  drops the pairing; the music on the card stays.
- Wi-Fi: when it is not connected at start the app offers to turn it on with the player's own `wifi_on.sh`
  (then `udhcpc` if needed) or explains how to do it in the player's settings. Battery and charging are only read
  (the firmware's charge limiter is never touched); the app runs on a charger too.
- Forced exit: hold next or previous + volume up together for 3 seconds (volume up + volume down also works on players that can report both keys at once) (a running sync stops first; the library
  database is never left half installed).
- Test hooks: `SYNCMUSIC_FB` (framebuffer file), `SYNCMUSIC_INPUT` (input events file/fifo), `SYNCMUSIC_SYSFS`
  (fake root for /sys and /var/run), `SYNCMUSIC_RESTART` (command instead of reboot/poweroff, gets `$SYNCMUSIC_HOW`),
  `SYNCMUSIC_MDNS` (host:port to query instead of the mDNS group), `SYNCMUSIC_SHOTS` (`go test -run TestScreenshots`
  draws sample pages as raw RGB565 files there).

## Profiling on the player
Put a file named `profile` (optionally containing the number of seconds, default 60) into the app's data folder
(`.apps-data/syncmusic/`); the next start records a CPU profile into `cpu.pprof` there. Read it with
`go tool pprof -top cpu.pprof` (the profile carries the function names).
