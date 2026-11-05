# devices/Android - Android phones and players

Everything Android lives here. Device type id stays `android`.

| File / folder | What it is |
|---|---|
| `mtp.py` | The `android` device over MTP (`AndroidMtp`): settings, files under the base folder, `.m3u8` playlists, wipe. Detected on USB through udev (`infra/blockdev/mtp.py`). |
| `_libmtp.py` | The libmtp backend (ctypes on `libmtp.so.9`) behind `mtp.py`'s `MtpBackend` protocol. |
| `adb.py` | *Later, not built yet:* syncing over ADB (faster than MTP, needs USB debugging). |
| `apps/` | *Later, not built yet:* per-app integrations (e.g. Poweramp, HiBy Music, FiiO Music playlists or play statistics). |
