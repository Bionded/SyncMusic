# Wi-Fi client protocol (v1)

How an app on a phone or player syncs with SyncMusic over the network. The client opens every connection (pull
only): it pairs once, then from time to time asks whether there is something new, downloads what the server has
prepared for it and reports what the user did (plays, ratings, favourites, positions).

- Machine-readable description: [`client-api.openapi.json`](client-api.openapi.json) (OpenAPI 3, generated from the
  server and checked against it by the test suite).
- Working example: [`syncmusic/reference_client.py`](../../syncmusic/reference_client.py) — one file, only `httpx`
  and the standard library; `python -m syncmusic.reference_client --help`.
- Conformance checks a client author can run against a server:
  `python -m syncmusic.client_conformance http://server:8765 1234-5678 ./scratch-folder` (see the end of this page).

On the server a paired client **is a device** of type `client`: it has templates, processors, history and Player
contents like a USB player. The server prepares its files with the device's processors into an **outbox**; the
client downloads them and acknowledges each one.

## Basics
- Base path: `<server>/api/client/v1/`. The major version is in the path; new optional fields can appear in any
  answer — ignore what you do not know. The server ignores unknown request fields too.
- Authentication: `Authorization: Bearer <client token>` on every route except `hello` and `pair`. A client token
  only works on `/api/client/v1/` and only for its own device; the web UI's session and API tokens are not accepted
  here.
- Clients must be allowed on the server (Settings → Wi-Fi clients). While they are not, every route —
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
| `rate_limited` | 429 | too many wrong codes or pairing requests; wait `retry_after_s` (also in `Retry-After`) |
| `pair_requests_disabled` | 403 | pairing requests are off on this server: use a pairing code |
| `too_many_requests` | 429 | too many pairing requests are waiting for the user |
| `request_expired` | 410 | unknown or expired pairing request (or a wrong secret): ask again |
| `request_rejected` | 403 | the user rejected the pairing request |
| `not_found` | 404 | no such file in an open sync session |
| `not_in_plan` | 409 | an ack names something the session's plan does not contain |
| `registration_pending` | 409 | the player is paired but the user has not finished its setup on the server yet: show *continue on the server* and poll `status` until `registration` is `done` |
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
The user clicks **Add client** on the server, which shows a QR code and an 8-digit code (`1234-5678`; the dash and
spaces do not matter; codes made by older versions also had letters). The code is single use and valid for 1 minute
by default (the user can make a new one at once). A client address gets at most 2 pairing attempts per second
(`pair`, `pair-requests` and polls of a request alike): more answer `429 rate_limited` with `Retry-After`; wrong
codes are also limited per minute and a code guessed at too often is burnt.

`POST pair`
```json
{"code": "1234-5678",
 "client": {"uid": "3f0c…", "card_uid": "", "name": "Pixel", "model": "", "manufacturer": "", "os": "Android",
            "os_version": "15", "app": "MyPlayer", "app_version": "1.0"},
 "capabilities": {…}}
```
→ `201 {"device_id": "…", "token": "smc_…", "instance": {…}, "poll_s": 300, "continued": false, "settings": {},
"templates": [{"id": "…", "name": "…", "on": true}]}`

- `uid`: generate it once per install (a UUID) and keep it. Pairing the same install again **continues** the same
  device (same id and history; `continued: true`).
- `card_uid` (optional): the id of the storage the client syncs onto - `device_uid` (else `device_id`) from the
  first line of `/.syncmusic/manifest.jsonl` (or `manifest.jsonl.bak`) on that storage. A storage without one gets
  a manifest holding only a header (`{"type":"header","manifest_version":2,"device_id":"<uuid>","device_uid":"<uuid>",
  "created_at":…,"updated_at":…}`). The same card synced over USB or in a card reader carries the same id.
  An install with a new `uid` (its data was reset or lost) whose `card_uid` belongs to a paired client
  **continues that device** too (`continued: true`); the old install's token stops working.
- `settings`: the client app's own settings as it last sent them with `PUT me/settings` (`{}` = none kept). A
  client that gets a non-empty object takes it over (it paired before and lost its data); with `{}` it sends its
  current settings. `templates`: the same list as `GET templates`, so the app shows what the device gets at once.
- Store the token safely; the server keeps only its hash and cannot show it again.
- After pairing the user chooses on the server what the device gets (templates); until then a sync has nothing to
  plan.

### Pairing request (registration mode)
Without a code: when `hello` lists `"pair_requests"` in `features`, a client can ask the user to approve it on the
server.

`POST pair-requests {"client": {…}, "capabilities": {…}}` →
`202 {"request_id": "…", "secret": "…", "display_code": "4821", "expires_at": "…", "poll_s": 3, "instance": {…}}`

Show `display_code` on the player: the server's web UI lists the request with the same code (Settings → Clients),
so the user approves the right player (the user may change the player's name there before approving: the
client then gets the server's name from `me` and `status`). Then poll `GET pair-requests/{request_id}` with the header
`X-Pair-Secret: <secret>` every `poll_s` seconds:
- `202 {"status": "pending"}` — the user has not answered yet;
- `200 {"status": "approved", "device_id": …, "token": …, "instance": …, "poll_s": …, "continued": …,
  "settings": {…}, "templates": […]}` — the same answer as `POST pair`; it is handed out **once**;
- `403 request_rejected` or `410 request_expired` — stop (a request is valid as long as a pairing code).

The same install asking again replaces its earlier request. Requests live in the server's memory: a restart drops
them (`request_expired`).

After pairing, the server opens the player's registration for the user (name, what it gets, processors). Until it
is finished, `me` and `status` report `"registration": "pending"` and starting a sync answers `registration_pending`;
a client shows a page like *Finish setting up this player on the server* and polls `status`. A player that paired
before (the same `uid`) continues its device and is `done` at once.

Other routes for a paired client: `GET me` (`device_id`, `name`, `registration`, `instance`, `poll_s`), `PATCH me {"name": "…"}`,
`PUT me/settings {"settings": {…}}` (the app's own settings, any JSON object up to 16 KB: send it after every
change; the server only stores it and hands it back on pairing),
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
  removals, not enough space). Poll `GET sync/{session_id}` after `retry_after_s`. When the device setting
  *The player may confirm a sync* is on (`client_may_confirm`; on by default for every client, the user may turn it off), a `needs_review` answer
  also has `"may_confirm": true` and `"review": {"fetch_count", "fetch_bytes", "delete_count",
  "skipped_for_space"}`: show it to the user and, when they agree, `POST sync/{session_id}/confirm`; the session
  goes back to `planning` and then `ready` as usual (`409 confirm_not_allowed` otherwise). A first sync onto a
  client that already holds files is always confirmed on the server. `200` with
  `"status": "ready"` carries the plan. `failed` (with `message`) means the server could not plan (e.g. no templates
  linked yet).

- **Templates** (any time): `GET templates` → `{"templates": [{"id", "name", "on"}]}` lists the server's
  templates and which of them the client's device uses; `PUT templates {"on": [ids]}` switches them (switches only:
  templates are edited in the web UI; `404 not_found` for an unknown id). The next sync follows.
- **App update** (any time, e.g. at start): `GET app-update?app=<app>&version=<running version>[&variant=<build>]` →
  `{"available": false}`, or `{"available": true, "app", "version", "files": [{"path", "size", "sha256", "url"}]}`
  when the server holds a newer release zip of that app (`<app>-<version>[-<variant>].zip` next to the SyncMusic binary or in
  the data directory's `tools/` folder). `variant` names the client's build (`hard` or `soft` float for the Compás
  app; missing or unknown = `hard`): the client is only offered zips of its own build or zips without a variant. `url` is relative to the client API and needs the bearer token. The client
  checks size and SHA-256 of every file before it replaces anything, and replaces its program file last. Every
  answer also carries `server_version` (the newest release the server holds, or `null`) and `client_newer` (the
  running version is newer than that: tell the user to update the server). The server shows the running version on
  the device page. `POST app-update/answer {"version", "answer": "update" | "later"}` records the user's choice.
- **Diagnostics** (any time, when the user asks for it): `POST diagnostics {"log": "<text, at most 512 KiB>",
  "summary": {…}}` → `202 {"received_at", "size"}`. The server keeps the latest upload per client for the device
  page (*Diagnostics: Download*). Leave tokens out; what still looks like one is masked by the server.

### 2. The plan
```json
{"session_id": "…", "status": "ready",
 "plan": {"fetch":  [{"op": "add", "key": "nd/123", "fid": "<sha256>", "size": 4123456, "mime": "audio/mpeg",
                      "mtime": 1763290000, "path": "Music/Artist/Album/01 Song.mp3", "role": "track",
                      "url": "files/<sha256>?ticket=…",
                      "meta": {"title": "Song", "artist": "…", "album_artist": "…", "album": "…", "track": 1,
                               "disc": 1, "year": 2001, "genre": "…", "duration_s": 245.1, "kind": "music",
                               "rating": 4, "favourite": true, "play_count": 12}}],
          "delete": [{"key": "nd/77", "path": "Music/Old/02 Gone.mp3"}],
          "playlists": {"set": [{"name": "Running", "path": "Playlists/Running.m3u8"}], "remove": []},
          "state":  [{"key": "nd/123", "rating": 4, "favourite": true, "play_count": 12},
                     {"key": "abs/ep-9", "rating": null, "favourite": false, "play_count": 0, "position_s": 1834.2,
                      "finished": false}],
          "generated": null, "card_manifest": null, "after_sync": "none", "more": 0,
          "next": {"fetch": "500"},
          "totals": {"fetch_bytes": 912345678, "fetch_count": 812, "delete_count": 3}}}
```
- `fid` is the SHA-256 of the file's bytes; verify it after downloading. `key` is the item the file belongs to —
  remember it per file: events name items by `key`.
- `role`: `track`, `sidecar` (cover, lyrics), `playlist` (an `.m3u8` with paths relative to it) or `file`.
- `meta` (tracks only) is what a client with its own database shows; the file's tags say the same. Podcast
  episodes also carry `series` (the show; `album` when empty) and `published` (ISO 8601 or `null`).
- `state` lists the libraries' listening data of every item on the client: `rating` 0–5 (`null` = unrated),
  `favourite`, `play_count` and, for podcasts and audiobooks, `position_s` (where to resume) and `finished`. Show these in the
  app; what the user changes goes back up as events. Music whose library rates albums (Subsonic servers) also carries
  `album_ref` (the album's key), `album_artist`, `album` and `album_rating` (0–5, `null` = unrated).
- `mtime` (Unix seconds) is the file's modification time on the server; set it on the file after moving it in
  place (a player's own database may compare file times with the ones it was built from).
- `generated` (or `null`): files the server built for this client — e.g. a player database over the files the
  client holds — installed as **one set**: `{"set_id": "…", "commit": "<path>", "files": [{"path", "fid", "size",
  "mtime", "url"}]}`. If the `commit` file already contains `set_id`, skip the set. Otherwise download and verify every
  file to a temporary name first, then move them all in place, then write `set_id` into the `commit` file. Generated
  files are not acknowledged and not part of the inventory. Which devices get a set is decided on the server.
- `card_manifest` (or `null`): for a client that syncs a removable card the server also syncs over USB (the
  same device, paired with the card's `card_uid`): `{"path": ".syncmusic/manifest.jsonl", "fid", "size", "mtime",
  "url"}`. After a **complete** sync (every file fetched, nothing failed, nothing cancelled) download and verify it,
  move it in place at `path` on the card, then write the same bytes to `path` + `.bak`. A USB sync of the card then
  sees the files the client took as its own. Not acknowledged and not part of the inventory (nothing under
  `.syncmusic/` is).
- `after_sync`: `none`, `refresh` (reload the library view) or `restart` (restart the player): what to do after a
  sync that changed something on the client (files fetched or deleted, or a new generated set installed). A sync
  that changed nothing does nothing. Set per device on the server.
- **Streaming** (recommended): a client that sends `"stream": true` in its `capabilities` gets the whole plan in
  one session. The server prepares (converts, retags) the files only a few ahead of the client's acks, so it never
  holds a copy of the library, and lists them as they are ready: `plan.stream` is `true`, `totals` count the whole
  plan, and a `fetch` page waits (up to ~25 s) for the next files — keep paging while `next` is not `null`,
  downloading and acking each file as it comes (a file that is not acked holds back the next ones). When the last
  page has `next: null`, read the session again (`GET sync/{session_id}`): with `plan.complete` true its
  `delete`, `state`, `playlists` and `generated` are final.
- `more` (clients without `stream`): how many files the server held back for a later **window**. The server
  prepares only as much as fits its outbox budget at a time, so a large first sync comes in several sessions.
  Install `generated` only from the last window (`more` = 0).
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
`"more": true` in the answer (after a `complete` finish of a plan with `more` > 0): start the next sync right away
for the next window, until it is `false`. A client that does not stays correct: its next sync gets the rest.

### When to sync
Call `GET status` every `poll_s` seconds (from `pair`/`me`; default 300) while on a suitable network →
```json
{"sync_wanted": "none" | "full", "reason": "files are waiting for the client", "requested": false, "plan_ready": false,
 "poll_s": 300, "server_time": "2026-11-13T08:00:00+00:00", "registration": "done",
 "name": "Kitchen player"}
```
`name` is the player's name on the server. The server's name wins: when the user renames the player there, show the
new name. The name is set on the server only; `PATCH me` stays for older apps that rename from the player.
`full` means start a sync (the user pressed *Sync* on the server, or prepared files are waiting). The client may
also sync whenever the user asks. `"requested": true` means the user pressed *Sync now* on the server: an app that is
open starts the sync at once. To learn that without delay, an open app keeps a **long poll** running:
`GET status?wait=25` answers as soon as *Sync now* is pressed, or after `wait` seconds (at most 30). The request
ends when the client starts its sync; nothing is prepared on the server before that.

**One sync per client at a time.** The server holds at most one session per client:

| Situation | Result |
|---|---|
| *Sync now* on the server | the app is asked (status / long poll); the server starts nothing itself |
| The app starts while a session is still planning (a retry, a second tap, both at once) | the same session comes back |
| The app starts while a session is ready (it started over) | the old session ends; its file ids and acks answer `410 session_expired`; what it acked stays done |
| *Sync now* while the app is syncing | the running session stays the only one; the request ends when the app next starts |
| The user stops the sync on the server (`DELETE /api/v1/clients/{id}/sync`) | a pending *Sync now* is withdrawn and the next ack answers `"cancel": true`: the client stops, finishes with `cancelled`; acked files stay done, the rest waits |
| The user stops the sync in the app | `finish {"status": "cancelled"}`; acked files stay done, `status` says `full` until the rest is fetched |
| *Sync now*, but the app is closed | the request waits (the device page says so) until the app starts, or the user withdraws it |
| Two clients of one server at once | each has its own session and outbox; both finish |
| A streaming client syncs a large library | one session: the server prepares a few files ahead of the acks, fetch pages wait for them; finishing early stops the server's work, the next sync goes on from there |
| The library is larger than the outbox budget (no `stream`) | the server prepares one window (plain files hard-linked, converted ones from the cache), `plan.more` and `finish → more` ask the client to sync again; acked files leave the outbox at once |
| The connection drops in the middle of a window | the un-acked file stays in the outbox; the next session plans it again and the client continues its `.part` with a `Range` request |

Each row is covered by a test in `tests/unit/test_client_sync.py` (the reference client follows the same rules).

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
| `album_rating` | `value` 0–5 (0 = cleared); `key` is the `album_ref` from `state` | the album's rating |
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
the server answers: `hello`, a wrong code, pairing, missing and wrong tokens, `me`, keeping the app settings (`me/settings`), starting a sync and reading the
plan, paging, a download and its SHA-256, a ranged download, an unknown file, acks and finish (acknowledged files are
not planned again), a resent event, token rotation. Checks that need files report `SKIP` until the user links
templates to the new device on the server; run it again with `-` instead of a code to reuse the pairing kept in
`<folder>`. `--json` prints the results for tools; the exit code is 1 when a check failed.
