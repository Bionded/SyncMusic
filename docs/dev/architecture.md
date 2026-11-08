# Architecture (for contributors)

SyncMusic is one Python process: an asyncio daemon with a REST API, a web UI served from the same port, an optional
desktop window and a CLI client. This page is the map; the code docstrings are the details.

## The sync pipeline
```
libraries ──snapshot──▶ rules ──selection──▶ planner ──plan──▶ processors ──prepared files──▶ applier ──▶ device
    ▲                                                                                                    │
    └──────────────────────────── reverse sync (plays, ratings, positions, playlists) ◀─────────────────┘
```
1. **Libraries** (`syncmusic/libraries/`, base `syncmusic/base/library.py`) return a `LibrarySnapshot`: every track,
   podcast episode and audiobook with its tags, plus a cheap `state_token()` so an unchanged library is not re-read.
2. **Rules** (`syncmusic/core/rules/`) select what goes to a device. They are Navidrome smart-playlist compatible;
   saved rules are **templates** linked to devices (`core/rules/templates.py`).
3. The **planner** (`core/planner.py`) compares the selection with the device's **manifest** (`core/manifest.py`,
   what we put there last time) and produces a `Plan`: add, replace, re-tag, remove, keep - with sizes predicted by
   the processors and checked against the free space (`core/space.py`, `core/fit.py`).
4. **Processors** (`syncmusic/processors/`, base `base/processor.py`) form a per-device chain (transcode, retag,
   covers, lyrics, video). Each has a pure `plan()` (predict the output) and an async `run()` (do it in a work dir).
   Outputs are cached by `cache_key()` (`infra/cache.py`), so a second device with the same chain reuses them.
5. The **applier** (`core/applier.py`) runs the plan against a device session: `begin()` → `put_track` /
   `remove_track` / `rewrite_tags` / `set_playlists` → `commit(manifest)`. A crash before `commit` leaves the old
   manifest valid; devices clean up half-written files on the next `begin()`.
6. **Devices** (`syncmusic/devices/`, base `base/device.py`; a device with helpers has its own folder: `iPod/`,
   `HibyR1/`, `Rockbox/`) know the player: how to detect it (`probe`), where files
   go, its database (iPod iTunesDB through pygpod, HiBy SQLite, Rockbox tagcache), playlists, and how to read back
   what was listed to.

**Reverse sync** (`core/reverse.py`, `core/positions.py`, `core/playlist_reverse.py`) runs before every plan: devices
return a `DeviceFeedback` (`read_feedback`), libraries that are a `FeedbackSink` (`base/feedback.py`) receive plays,
ratings, favourites and positions; what was delivered is cleared on the device (`reset_feedback`).

## Services around it
- **Sync service** (`core/sync.py`): registered devices, connected disks (detection in `core/detection.py`, udev
  watcher in `infra/watch.py`), mounting through `infra/blockdev/` - every mount/format/write goes through the
  **guard** (`infra/blockdev/guard.py`), which refuses anything that is not a removable USB disk or a known player.
- **Jobs** (`infra/jobs.py`): one job per device at a time; progress and log lines are published as **events**
  (`infra/events.py`) to the UI over server-sent events (`api/events.py`).
- **Store** (`infra/store.py`, `infra/db.py`): SQLite for configs, templates, reports, notifications;
  `infra/migrations/` holds the schema: `baseline.sql` (0.2.0) and the numbered migrations after it, applied at
  start after a backup.
- **Registry** (`core/registry.py`): the explicit table type name → class. No auto-discovery, no plugins.
- **API** (`api/server.py`, `api/routes/`): REST under `/api/v1`. The CLI (`cli/main.py`) is a thin client of it.
- **Web UI** (`webui/static/`): plain ES modules, no build step. `js/router.js` maps `#/…` routes to `js/views/*.js`;
  forms for settings are generated from each class's Pydantic `Settings` model (`api` `settings_schema`); every
  string goes through `js/i18n.js`.

## Where to start reading
`base/device.py`, `base/library.py`, `base/processor.py` (the three contracts), then `core/planner.py` and
`core/applier.py`. `devices/flashdrive.py` is the smallest complete device, `libraries/folder.py` the simplest
library, `processors/retag.py` a typical processor.
