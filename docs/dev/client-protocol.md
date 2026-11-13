# Wi-Fi client protocol (v1)

How an app on a phone or player syncs with SyncMusic over the network. The client opens every connection (pull
only): it pairs once, then from time to time asks whether there is something new, downloads what the server has
prepared for it and reports what the user did (plays, ratings, favourites, positions).

- Machine-readable description: [`client-api.openapi.json`](client-api.openapi.json) (OpenAPI 3, generated from the
  server and checked against it by the test suite).
- Working example: [`syncmusic/reference_client.py`](../../syncmusic/reference_client.py) — one file, only `httpx`
  and the standard library; `python -m syncmusic.reference_client --help`.
- Conformance checks a client author can run against a server:
  `python -m syncmusic.client_conformance http://server:8765 ABCD-2345 ./scratch-folder` (see the end of this page).

On the server a paired client **is a device** of type `client`: it has templates, processors, history and Player
contents like a USB player. The server prepares its files with the device's processors into an **outbox**; the
client downloads them and acknowledges each one.

## Basics
- Base path: `<server>/api/client/v1/`. The major version is in the path; new optional fields can appear in any
  answer — ignore what you do not know. The server ignores unknown request fields too.
- Authentication: `Authorization: Bearer <client token>` on every route except `hello` and `pair`. A client token
  only works on `/api/client/v1/` and only for its own device; the web UI's session and API tokens are not accepted
  here.
- Clients must be allowed on the server (Settings → System → Wi-Fi clients). While they are not, every route —
  `hello` included — answers `403 clients_disabled`.
- Plain HTTP on the local network. For anything beyond a trusted network, put SyncMusic behind a reverse proxy with
  HTTPS; the protocol does not change (the base URL becomes `https://…`).

### Errors
Every client-API error has the same shape:
```json
{"error": {"code": "not_in_plan", "message": "…", "retry_after_s": null, "details": {}}}
```
Act on `code`, show `message`. Codes in v1:

| code | HTTP | meaning |
|---|---|---|
| `clients_disabled` | 403 | Wi-Fi clients are not allowed on this server |
| `auth_required` | 401 | no or malformed `Authorization` header, or an unknown token |
| `token_revoked` | 401 | the user revoked this client: pair again |
| `code_invalid` / `code_expired` | 400 / 410 | wrong or expired pairing code |
| `rate_limited` | 429 | too many wrong codes; wait `retry_after_s` (also in `Retry-After`) |
| `not_found` | 404 | no such file in an open sync session |
| `not_in_plan` | 409 | an ack names something the session's plan does not contain |
| `inventory_required` | 409 | a delta inventory's `since` is unknown: send a full inventory |
| `session_expired` | 410 | the sync session is gone (expired or the server restarted): start a new one |
| `not_ready` | 409 | the plan is not ready yet: poll `GET sync/{session_id}` first |
| `invalid` | 400 | a bad plan section or cursor |
| `server_error` | 500 | something failed on the server; try again later |

Request validation errors (a missing field, a wrong type) answer `422`.

## Discovery
A client needs the server's **base URL**. Three ways to get it:
1. **mDNS / DNS-SD**: while clients are allowed and *Announce on the network* is on, the server announces
   `_syncmusic._tcp.local.` with TXT records `v` (highest protocol major), `id` (instance id), `name`, `path`
   (URL prefix), `tls` (0/1) and `pair` (1 while a pairing code is open).
2. **Typed address**: `nas.local:8765` — add `http://` when there is no scheme.
3. **QR code** from the server's *Add client* dialog:
   `syncmusic://pair?u=<url-encoded base URL>&c=<pairing code>&i=<instance id>&v=1`.

`GET hello` (no token) →
```json
{"protocol": {"min": 1, "max": 1}, "instance": {"id": "…", "name": "Living room"},
 "app_version": "0.2.4", "pairing_open": true, "features": [], "deprecations": []}
```
Keep `instance.id`: when the same address later answers with another id, it is a different server.

## Pairing
The user clicks **Add client** on the server, which shows a QR code and an 8-character code (`ABCD-2345`; case and
the dash do not matter). The code is single use and valid for 10 minutes by default.

`POST pair`
```json
{"code": "ABCD-2345",
 "client": {"uid": "3f0c…", "name": "Pixel", "model": "", "manufacturer": "", "os": "Android",
            "os_version": "15", "app": "MyPlayer", "app_version": "1.0"},
 "capabilities": {…}}
```
→ `201 {"device_id": "…", "token": "smc_…", "instance": {…}, "poll_s": 300, "continued": false, "settings": {}}`

- `uid`: generate it once per install (a UUID) and keep it. Pairing the same install again **continues** the same
  device (same id and history; `continued: true`).
- Store the token safely; the server keeps only its hash and cannot show it again.
- After pairing the user chooses on the server what the device gets (templates); until then a sync has nothing to
  plan.

Other routes for a paired client: `GET me` (`device_id`, `name`, `instance`, `poll_s`), `PATCH me {"name": "…"}`,
`POST me/token/rotate` → `{"token": "…"}` (the old token stays valid until the new one is first used, or for 24 h,
so a client that crashes between the two calls keeps working).

## Capabilities
Sent at pairing and with every sync start (they change: card swapped, app updated). Unknown keys are ignored,
missing keys mean "unknown".
```json
{"formats": ["mp3", "aac", "flac", "opus", "vorbis"],
 "media_kinds": ["music", "podcast", "audiobook"],
 "storage": {"total": 256000000000, "free": 120000000000},
 "features": {"playlists": true, "play_events": true},
 "layout": {"stores_paths": true}}
```
`formats` decides what the server converts (a device's processors still win); `storage.free` is used for the
does-it-fit check like a USB player's free space.

## Sync
A sync is a **session** the client opens, works through at its own pace and closes. Every step can be repeated;
the connection may drop between any two requests.

### 1. Start: report what you hold
`POST sync`
```json
{"mode": "full", "capabilities": {…},
 "inventory": {"kind": "full", "items": [{"path": "Music/A/B/01 Song.mp3", "size": 4123456}]}}
```
- `inventory` lists the SyncMusic files the client holds (`path`, `size`; `fid` and `key` optional). Send `full` on
  the first sync, after a reinstall or when the server answers `inventory_required`; otherwise a `delta` with
  `since` = the `inventory_cursor` of the last finished session, the new `items` and the `removed` paths.
- The lists in request bodies (`items`, `removed`, `done`, `deleted`, `failed`, `events`) also accept an empty object `{}` as an empty list, for JSON encoders that cannot tell the two apart (Lua).
- Answers: `202 {"session_id", "status": "planning" | "needs_review", "message", "retry_after_s"}` while the server
  plans or waits for the user to confirm the plan in the web UI (first sync onto a non-empty client, many
  removals, not enough space). Poll `GET sync/{session_id}` after `retry_after_s`. `200` with
  `"status": "ready"` carries the plan. `failed` (with `message`) means the server could not plan (e.g. no templates
  linked yet).

### 2. The plan
```json
{"session_id": "…", "status": "ready",
 "plan": {"fetch":  [{"op": "add", "key": "nd/123", "fid": "<sha256>", "size": 4123456, "mime": "audio/mpeg",
                      "path": "Music/Artist/Album/01 Song.mp3", "role": "track", "url": "files/<sha256>?ticket=…",
                      "meta": {"title": "Song", "artist": "…", "album_artist": "…", "album": "…", "track": 1,
                               "disc": 1, "year": 2001, "genre": "…", "duration_s": 245.1, "kind": "music",
                               "rating": 4, "favourite": true, "play_count": 12}}],
          "delete": [{"key": "nd/77", "path": "Music/Old/02 Gone.mp3"}],
          "playlists": {"set": [{"name": "Running", "path": "Playlists/Running.m3u8"}], "remove": []},
          "state":  [{"key": "nd/123", "rating": 4, "favourite": true, "play_count": 12},
                     {"key": "abs/ep-9", "rating": null, "favourite": false, "play_count": 0, "position_s": 1834.2}],
          "next": {"fetch": "500"},
          "totals": {"fetch_bytes": 912345678, "fetch_count": 812, "delete_count": 3}}}
```
- `fid` is the SHA-256 of the file's bytes; verify it after downloading. `key` is the item the file belongs to —
  remember it per file: events name items by `key`.
- `role`: `track`, `sidecar` (cover, lyrics), `playlist` (an `.m3u8` with paths relative to it) or `file`.
- `meta` (tracks only) is what a client with its own database shows; the file's tags say the same.
- `state` lists the libraries' listening data of every item on the client: `rating` 0–5 (`null` = unrated),
  `favourite`, `play_count` and, for podcasts and audiobooks, `position_s` (where to resume). Show these in the
  app; what the user changes goes back up as events.
- A tags-only change arrives as a fetch with a new `fid` that replaces the file at the same `path`.
- Work order: **deletes first** (frees space), then fetches in list order (rule priority: the most important come
  first when space runs out).
- Long lists are paged: `GET sync/{session_id}/plan?section=fetch|delete|state&cursor=<next>&limit=500` →
  `{"items": […], "next": "<cursor>" | null}`.

### 3. Download
`GET files/{fid}` streams the file (`ETag: "<fid>"`). Resume an interrupted download with
`Range: bytes=<have>-` and `If-Range: "<fid>"` → `206` with the rest (a `200` means: start over). Write to a
temporary name, check size and SHA-256, then move it in place. Only files of the client's open session can be
downloaded.

A downloader that cannot send an `Authorization` header (a player's built-in file download) uses the fetch item's
`url` instead: `files/{fid}?ticket=…`, relative to the client API, works without the token. The ticket belongs to the
session: it opens only that session's files and stops working when the session is finished or expires. Treat it like
the token (don't log or share it).

### 4. Acknowledge
`POST sync/{session_id}/ack` — as often as you like (each file, or every few seconds):
```json
{"done": [{"path": "Music/Artist/Album/01 Song.mp3", "fid": "<sha256>"}],
 "deleted": ["Music/Old/02 Gone.mp3"],
 "failed": [{"path": "…", "error": "no_space", "message": "card full"}],
 "storage": {"free": 119000000000}}
```
→ `{"progress": {"done", "total", "bytes_done", "bytes_total"}, "cancel": false}`.
An acknowledged file counts as on the device at once, so a dropped connection loses at most the un-acked files.
Acks are idempotent. `cancel: true` means the user cancelled on the server: stop after the current file and finish
with `cancelled`. The server's web UI shows the progress live.

### 5. Finish
`POST sync/{session_id}/finish {"status": "complete" | "partial" | "cancelled"}` → `{"inventory_cursor": "…", …}`.
Keep the cursor for the next delta inventory. Files not acknowledged stay in the outbox for the next session.

### When to sync
Call `GET status` every `poll_s` seconds (from `pair`/`me`; default 300) while on a suitable network →
```json
{"sync_wanted": "none" | "full", "reason": "files are waiting for the client", "plan_ready": false,
 "poll_s": 300, "server_time": "2026-11-13T08:00:00+00:00"}
```
`full` means start a sync (the user pressed *Sync* on the server, or prepared files are waiting). The client may
also sync whenever the user asks.

## Listening data: events
Record what the user did as events and upload them; never send a table of current values.

`POST events {"events": [ … ]}` (at most 1000 per request) → `{"acked_seq": 1042, "accepted": 2, "duplicates": 0}`
```json
{"eid": "uuid", "seq": 1042, "type": "play", "key": "nd/123", "at": "2026-11-12T08:13:00Z", "clock": "ntp",
 "completed": true, "played_s": 245.0}
```
| `type` | fields | effect on the next sync |
|---|---|---|
| `play` | `completed`, `played_s` | play count +1 (scrobbled with time `at`) when `completed` or long enough |
| `rating` | `value` 0–5 (0 = cleared) | rating |
| `favourite` | `on` | favourite / star on or off |
| `position` | `position_s`, `finished` | audiobook or podcast position; the newest wins |
| `playlist_edit` | `name`, `ops`: `[{"op": "add" \| "remove" \| "move", "key", "index"}]` | the playlist as last synced plus these edits goes back to the library playlist it came from; a new `name` creates a playlist there (libraries that can save playlists, e.g. Navidrome) |
| `deleted_by_user` | — | the user deleted the file on the client: it stays off this client (an exclusion in the device's own template, as *Player contents → Remove* does); applied when the event arrives |

- `eid` is unique per event: sending the same event again (the answer got lost) is harmless.
- `seq` counts up per install; drop your copies up to `acked_seq`.
- `clock`: `ntp`/`rtc` when `at` can be trusted, `unknown` otherwise (the server then uses the time it received
  the event).
- Events are stored on the server and handed to the libraries about 20 seconds after the last upload (a burst is
  handed on once), or by the sync the client is running; events of other types are accepted and ignored. Send
  them whenever you like, also without a sync: a play on the phone reaches the music server within a minute.

## What a client must do, and what is optional
**Must**: `hello`, `pair`, `status` polling, `POST sync` with an inventory, download + verify + ack, deletes +
ack, `finish`, keep files at their `path`. A client that sends no events is valid (like a flash drive).
**Should**: resume with `Range`, delta inventories, `play`/`favourite`/`rating`/`position` events.
**May**: token rotation, renaming itself, `playlist_edit` and `deleted_by_user` events.

## Conformance checks
`python -m syncmusic.client_conformance <server> <pairing code> <folder>` pairs a throwaway client and checks what
the server answers: `hello`, a wrong code, pairing, missing and wrong tokens, `me`, starting a sync and reading the
plan, paging, a download and its SHA-256, a ranged download, an unknown file, acks and finish (acknowledged files are
not planned again), a resent event, token rotation. Checks that need files report `SKIP` until the user links
templates to the new device on the server; run it again with `-` instead of a code to reuse the pairing kept in
`<folder>`. `--json` prints the results for tools; the exit code is 1 when a check failed.
