# 2. Installation

SyncMusic runs on **Linux** (x86_64 and arm64, e.g. a Raspberry Pi; glibc 2.28 or newer). Choose one way:

| Way | Good for |
|---|---|
| [Single-file binary](#single-file-binary) | Most people: one file, nothing else to install except the system tools |
| [systemd service](#as-a-service) | A server or Raspberry Pi that is always on (the binary installs it for you) |
| [Docker](#docker) | Network libraries and network devices (FTP, SMB); USB only with care |
| [From source](#from-source) | Developers |

## System tools
SyncMusic uses standard tools to mount, format and convert. On Debian, Ubuntu or Raspberry Pi OS:

```sh
sudo apt install ffmpeg util-linux fdisk parted udev dosfstools exfatprogs e2fsprogs ntfs-3g sg3-utils eject rsync
# optional: udisks2 (power a disk off after eject)
# desktop window only: libsecret-1-0 libegl1 libgl1 libgles2 and a Chromium-based browser
```

Mounting and formatting USB devices needs root. The service runs as root; if you run SyncMusic as a normal user,
install it with `--user NAME` (below) so that user may call the small root helper `syncmusic-priv`.

## Single-file binary
There are two flavours of the same program:

- `syncmusic-web-<version>-linux-<arch>` starts the **web interface** (open `http://<host>:8765` in a browser);
- `syncmusic-desktop-<version>-linux-<arch>` opens its own **desktop window**.

```sh
chmod +x syncmusic-web-0.2.0-linux-aarch64
./syncmusic-web-0.2.0-linux-aarch64          # foreground; Ctrl+C stops it
./syncmusic-web-0.2.0-linux-aarch64 -d       # in the background (pid and log file in the data dir)
./syncmusic-web-0.2.0-linux-aarch64 status   # is it running, which devices are connected
./syncmusic-web-0.2.0-linux-aarch64 stop     # stop the background instance
```

Check the download against the release's `SHA256SUMS` (`sha256sum -c --ignore-missing SHA256SUMS`). The first start unpacks the
bundled Python to `~/.cache/syncmusic/runtime/` (set `SYNCMUSIC_RUNTIME_DIR` to put it elsewhere); later starts are
fast.

### Paths and options
Without options SyncMusic uses:

| What | As root | As a normal user | Option |
|---|---|---|---|
| Config file | `/etc/syncmusic/config.toml` if it exists | same | `--config FILE`, `SYNCMUSIC_CONFIG` |
| Database and settings | `/var/lib/syncmusic` | `~/.local/share/syncmusic` | `--data-dir DIR` |
| Cache (converted files) | `/var/cache/syncmusic` | `~/.cache/syncmusic` | `--cache-dir DIR` |
| Logs | `<data dir>/logs` | `<data dir>/logs` | `--log-dir DIR` |
| Device mounts | `/run/syncmusic` | `/run/syncmusic` | `--mount-root DIR` |
| Web address | `0.0.0.0:8765` | `0.0.0.0:8765` | `--host`, `--port` |

Order of precedence: command-line options, then `SYNCMUSIC_<KEY>` environment variables (e.g.
`SYNCMUSIC_PORT=8080`), then the config file, then the defaults. To get a config file with every key, its default
and a one-line explanation:

```sh
syncmusic config init                                  # print it
syncmusic config init --output ~/.config/sm.toml       # write it (--force overwrites)
```

Everything else (libraries, devices, templates, processors, notifications, sync defaults) is set in the web
interface under **Settings** and stored in the database.

## As a service
The quickest way, from the newest release (downloads and checks the binary for your CPU, then does the steps below):

```sh
curl -fsSL https://raw.githubusercontent.com/Bionded/SyncMusic/main/install.sh | sudo bash
curl -fsSL https://raw.githubusercontent.com/Bionded/SyncMusic/main/install.sh | sudo bash -s -- --beta --port 8080
```

Options: `--beta` (newest test build), `--version X`, `--port N`, `--user NAME` (see below), `--install-deps` (install
missing system tools with apt or dnf), `--uninstall` (`--purge` also deletes data and config). Running it again
upgrades in place and keeps the previous program as `/usr/local/bin/syncmusic.previous`.

Or, with a binary you downloaded yourself, let it install itself as a systemd service:

```sh
sudo ./syncmusic-web-0.2.0-linux-aarch64 --install --port 8080
sudo systemctl enable --now syncmusic
```

`--install` copies the binary to `/usr/local/bin/syncmusic`, writes the unit `syncmusic.service` with the path
options you gave, writes `/etc/syncmusic/config.toml` with the defaults if it does not exist yet and creates the
data, cache and log directories. Add `--user alice` to let user `alice` use devices through the root helper.

Logs go to the journal (`journalctl -u syncmusic -f`) and to `<data dir>/logs/syncmusic.log`.

To remove it: `sudo syncmusic --uninstall` (keeps data, cache, logs and config) or `sudo syncmusic --uninstall
--purge` (deletes them too).

## Docker
The image `ghcr.io/bionded/syncmusic` (amd64 and arm64) is published with every release: `latest` is the newest
stable release, `beta` the newest beta, and each version has its own tag. The compose file
`packaging/docker/compose.yaml` takes it from `SYNCMUSIC_IMAGE` (by default a local build of
`packaging/docker/build.sh`): e.g. `export SYNCMUSIC_IMAGE=ghcr.io/bionded/syncmusic:latest`.
There are two variants:

- **network** - libraries over the network and network devices (FTP, SMB). Unprivileged.
  ```sh
  SYNCMUSIC_MUSIC=/srv/music docker compose -f packaging/docker/compose.yaml --profile network up -d
  ```
  `SYNCMUSIC_MUSIC` is mounted read-only as `/music` for a folder library.
- **usb** - also USB players. The container is **privileged** and sees every disk of the host, which is the same
  as running as root on the host: SyncMusic's own safety checks still apply, but the container cannot protect your
  disks. For USB iPods prefer the [service](#as-a-service).
  ```sh
  docker compose -f packaging/docker/compose.yaml --profile usb up -d
  ```

Both serve the web interface on port **8080**. Settings and the database live in named volumes; `docker compose …
down -v` deletes them.

## From source
```sh
git clone <repository> SyncMusic && cd SyncMusic
uv sync                       # needs Python 3.12+ and uv
uv run syncmusic run          # or: uv run syncmusic --help
```

### Building a release (maintainers)
`scripts/release.sh 0.2.0` prepares a release: it checks that you are on a clean `main` and that `CHANGELOG.md` has
a `## [0.2.0]` section, sets the version, creates the git tag `v0.2.0` and moves `main` on to the next patch version
(`0.2.1`). Nothing is pushed or published; `--dry-run` shows the steps without changing anything. Then
`git push origin main v0.2.0`: the release workflow on GitHub builds the web and desktop binaries for x86_64 and
arm64, the Docker image for both architectures and `SHA256SUMS`, and publishes the release with the notes of the
changelog entry. To look at the files before publishing, `--build` builds them locally into `dist/release-0.2.0/`
(with `RELEASE-NOTES.md`; `--docker` adds the arm64 image).

Every push to `main` also publishes a **beta** pre-release `v<version>-<build>` (e.g. `v0.2.1-5`: build 5 on the way
to 0.2.1) with the commit subjects since the last release as notes; the newest 10 are kept. SyncMusic's update check
offers them when Settings → Updates → *Update channel* is *Beta*. The order is 0.2.0 < 0.2.1-3 < 0.2.1-10 < 0.2.1.

## Upgrading
SyncMusic 0.2.0 has **no database migrations yet**: a later version may need a fresh start. To
upgrade, stop SyncMusic, replace the binary (or `git pull` and `uv sync`), and start it again. A database
SyncMusic cannot open is refused at start with a message instead of being changed. If a release
note says a fresh start is needed, move the data directory away first and set SyncMusic up again - your libraries
and the music on your players are not affected, only SyncMusic's own settings.

## Security
By default SyncMusic has **no login**: anyone who can reach its port can change its settings and write to connected
devices. That is fine on a home network you trust; otherwise bind it to `127.0.0.1` (`--host 127.0.0.1`) or turn
on the **optional password** under *Settings → System → Access* (or with `syncmusic set-password` on the host). With
it on, the web interface shows a login page, the API needs the session of a login or an **API token**
(`Authorization: Bearer <token>`, created under Access and shown once), and `/api/v1/health` stays open. Lost the
password? Run `sudo syncmusic set-password --config /etc/syncmusic/config.toml` on the host (it asks for the new
one). The desktop app never asks for the password on its own computer. A password is no substitute for HTTPS: if
SyncMusic must be reachable from outside your network, put it behind a reverse proxy with HTTPS (the session cookie
is then marked secure).

Web pages on other sites cannot use your browser to control SyncMusic: requests that change something are refused
when they come from another site, and the interface only answers to IP addresses, `localhost` and this computer's
own name (also `<name>.local`). If you reach it under another name (a reverse proxy, a DNS name of your own), add it
to the configuration file: `allowed_hosts = ["music.example.lan"]` (`["*"]` turns the check off). The configuration
file and the data directory (database with your library passwords, logs, backups) are readable only by the user
SyncMusic runs as.
