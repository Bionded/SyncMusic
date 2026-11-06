#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Install SyncMusic as a systemd service from the latest GitHub release:
#   curl -fsSL https://raw.githubusercontent.com/Bionded/SyncMusic/main/install.sh | sudo bash
#   curl -fsSL https://raw.githubusercontent.com/Bionded/SyncMusic/main/install.sh | sudo bash -s -- --beta --port 8080
# Options:
#   --beta           the newest beta pre-release instead of the newest stable release
#   --version X      a given version (e.g. 0.2.1 or 0.2.1-5)
#   --port N         port of the web UI (default 8765)
#   --user NAME      let this desktop user use devices through the root helper (repeatable)
#   --install-deps   install the missing runtime packages (apt or dnf) instead of only naming them
#   --uninstall      remove the service and the program; data and config stay (with --purge: deleted too)
#   --repo OWNER/NAME  install from a fork's releases
#   --dir DIR        no service, no root (… | bash -s -- --dir ~/syncmusic): program, config, data, cache and logs in
#                    DIR, started by hand with DIR/run.sh; again upgrades, --uninstall [--purge] --dir DIR removes it
# Downloads the single-file web binary for this CPU (x86_64 or aarch64) and SHA256SUMS, checks the checksum and hands
# over to the binary's own `--install`: /usr/local/bin/syncmusic (the one before stays as syncmusic.previous), its
# runtime in /opt/syncmusic, config /etc/syncmusic/config.toml (kept when it exists), data /var/lib/syncmusic, the
# unit syncmusic.service, enabled and started. Running it again upgrades in place.
set -euo pipefail

REPO="Bionded/SyncMusic"
RELEASES_URL="${SYNCMUSIC_RELEASES_URL:-}"  # the releases API answer (default: GitHub's for $REPO)
BIN=/usr/local/bin/syncmusic

say() { printf 'syncmusic-install: %s\n' "$*"; }
die() { printf 'syncmusic-install: %s\n' "$*" >&2; exit 1; }

# the release asset for this machine: syncmusic-web-<version>-linux-<arch>
arch_name() {
  case "$1" in
    x86_64|amd64) echo x86_64 ;;
    aarch64|arm64) echo aarch64 ;;
    *) return 1 ;;
  esac
}

# from the releases API answer on stdin: "<tag> <asset url> <sums url>" of the wanted release
pick_release() { # $1 = stable|beta, $2 = version or "", $3 = arch
  python3 -c '
import json, re, sys
channel, version, arch = sys.argv[1:4]
def key(tag):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-(\d+))?", tag)
    if not m:
        return None
    a, b, c, n = m.groups()
    return (int(a), int(b), int(c), 0, int(n)) if n else (int(a), int(b), int(c), 1, 0)
best = None
for r in json.load(sys.stdin):
    tag = r.get("tag_name", "")
    if r.get("draft") or key(tag) is None:
        continue
    if version and tag.removeprefix("v") != version:
        continue
    if not version and r.get("prerelease") and channel != "beta":
        continue
    assets = {a["name"]: a["browser_download_url"] for a in r.get("assets", [])}
    binary = next((u for n, u in assets.items() if n.startswith("syncmusic-web-") and n.endswith("-linux-" + arch)), None)
    if binary and "SHA256SUMS" in assets and (best is None or key(tag) > key(best[0])):
        best = (tag, binary, assets["SHA256SUMS"])
if best:
    print(*best)
' "$@"
}

missing_deps() {
  local tool missing=()
  for tool in ffmpeg lsblk blkid mount umount mkfs.vfat mkfs.exfat sfdisk partprobe udevadm; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  printf '%s\n' "${missing[@]+"${missing[@]}"}"
}

# fetch and check the wanted binary into $4; sets TAG and NEW (the binary's path)
download() { # $1 = channel, $2 = version, $3 = arch, $4 = work dir
  local release tag url sums
  release="$(curl -fsSL -H "Accept: application/vnd.github+json" \
    "${RELEASES_URL:-https://api.github.com/repos/$REPO/releases?per_page=50}" \
    | pick_release "$1" "$2" "$3")" || die "the releases of $REPO cannot be read"
  [ -n "$release" ] || die "no ${2:-$1} release with a linux-$3 binary in $REPO"
  read -r tag url sums <<< "$release"
  say "downloading SyncMusic ${tag#v} for linux-$3"
  curl -fsSL -o "$4/${url##*/}" "$url"
  curl -fsSL -o "$4/SHA256SUMS" "$sums"
  (cd "$4" && grep -E " \*?${url##*/}\$" SHA256SUMS | sha256sum -c --status) \
    || die "checksum mismatch for ${url##*/}: not installed"
  chmod 0755 "$4/${url##*/}"
  TAG="$tag" NEW="$4/${url##*/}"
}

# --dir: everything in one folder, started by hand with DIR/run.sh
install_dir() { # $1 = dir, $2 = channel, $3 = version, $4 = arch, $5 = port
  local dir="$1" port="$5"
  mkdir -p "$dir"
  dir="$(cd "$dir" && pwd)"
  local work
  work="$(mktemp -d "$dir/.download.XXXXXX")"
  # shellcheck disable=SC2064 # expanded now: $work is local and gone by the time the script exits
  trap "rm -rf '$work'" EXIT
  download "$2" "$3" "$4" "$work"
  if [ -z "$port" ] && [ -f "$dir/syncmusic.env" ]; then
    port="$(sed -n 's/^SYNCMUSIC_PORT=//p' "$dir/syncmusic.env")"
  fi
  port="${port:-8765}"
  mkdir -p "$dir/data" "$dir/cache" "$dir/logs" "$dir/mnt" "$dir/runtime"
  if [ -e "$dir/syncmusic" ]; then cp -p "$dir/syncmusic" "$dir/syncmusic.previous"; fi
  mv -f "$NEW" "$dir/syncmusic"
  [ -e "$dir/config.toml" ] \
    || echo "# SyncMusic settings (syncmusic.env next to this file sets the folders and the port)" > "$dir/config.toml"
  printf '%s\n' \
    "SYNCMUSIC_CONFIG=$dir/config.toml" \
    "SYNCMUSIC_DATA_DIR=$dir/data" \
    "SYNCMUSIC_CACHE_DIR=$dir/cache" \
    "SYNCMUSIC_LOG_DIR=$dir/logs" \
    "SYNCMUSIC_MOUNT_ROOT=$dir/mnt" \
    "SYNCMUSIC_RUNTIME_DIR=$dir/runtime" \
    "SYNCMUSIC_MODE=web" \
    "SYNCMUSIC_HOST=0.0.0.0" \
    "SYNCMUSIC_PORT=$port" \
    "SYNCMUSIC_INSTALL_TYPE=standalone" > "$dir/syncmusic.env"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    '# Start SyncMusic from this folder in the foreground (Ctrl+C stops it).' \
    'set -euo pipefail' \
    'here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' \
    'set -a' \
    '. "$here/syncmusic.env"' \
    'set +a' \
    'echo "SyncMusic: http://$(hostname 2>/dev/null || echo localhost):$SYNCMUSIC_PORT"' \
    'if [ "$(id -u)" != 0 ] && ! sudo -n true 2>/dev/null; then' \
    '  echo "Without root or passwordless sudo only network devices and folders work: mounting and formatting"' \
    '  echo "players needs the block-device tools (https://github.com/Bionded/SyncMusic/blob/main/docs/02-installation.md)."' \
    'fi' \
    'cd "$here"' \
    'exec "$here/syncmusic" run --mode web' > "$dir/run.sh"
  chmod 0755 "$dir/run.sh"
  printf '%s\n' \
    "SyncMusic ${TAG#v}, installed in this folder (no service)." \
    "" \
    "Start:   ./run.sh  (in the foreground; the web UI is on port $port)" \
    "Stop:    Ctrl+C" \
    "Update:  Settings > Updates, or run the installer again with --dir $dir" \
    "Remove:  run the installer with --uninstall --dir $dir (add --purge to delete config and data too)" \
    "" \
    "config.toml, data/, cache/ and logs/ stay when you update; syncmusic.previous is the version before." \
    > "$dir/README.txt"
  say "SyncMusic ${TAG#v} is in $dir: start it with $dir/run.sh (port $port)"
}

uninstall_dir() { # $1 = dir, $2 = purge
  local d="${1:?}"
  [ -x "$1/syncmusic" ] || die "SyncMusic is not installed in $1 ($1/syncmusic missing)"
  rm -rf "${d:?}/syncmusic" "$d/syncmusic.previous" "$d/run.sh" "$d/README.txt" "$d/runtime" "$d/syncmusic.env"
  if [ "$2" = 1 ]; then
    rm -rf "${d:?}/config.toml" "${d:?}/data" "${d:?}/cache" "${d:?}/logs" "${d:?}/mnt"
    rmdir "$d" 2>/dev/null || true
  fi
  say "removed from $1."
}

install_deps() {
  if command -v apt-get >/dev/null; then
    apt-get update -q && apt-get install -y -q ffmpeg util-linux fdisk parted udev dosfstools exfatprogs e2fsprogs \
      ntfs-3g sg3-utils eject rsync
  elif command -v dnf >/dev/null; then
    dnf install -y -q ffmpeg util-linux parted systemd-udev dosfstools exfatprogs e2fsprogs ntfs-3g sg3_utils eject rsync
  else
    die "no apt-get or dnf: install ffmpeg, util-linux, dosfstools, exfatprogs and parted yourself"
  fi
}

main() {
  local channel=stable version="" port="" deps=0 uninstall=0 purge=0 users=() dir=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --beta) channel=beta; shift ;;
      --version) [ $# -ge 2 ] || die "--version needs a value"; version="${2#v}"; shift 2 ;;
      --port) [ $# -ge 2 ] || die "--port needs a value"; port="$2"; shift 2 ;;
      --user) [ $# -ge 2 ] || die "--user needs a value"; users+=("$2"); shift 2 ;;
      --repo) [ $# -ge 2 ] || die "--repo needs a value"; REPO="$2"; shift 2 ;;
      --dir) [ $# -ge 2 ] || die "--dir needs a value"; dir="$2"; shift 2 ;;
      --install-deps) deps=1; shift ;;
      --uninstall) uninstall=1; shift ;;
      --purge) purge=1; shift ;;
      -h|--help) awk 'NR > 2 && !/^#/ {exit} NR > 2 {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]:-$0}" 2>/dev/null \
        || echo "see https://github.com/$REPO#installation"; exit 0 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  [ -z "$port" ] || [[ "$port" =~ ^[0-9]+$ ]] || die "--port must be a number"
  [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "--repo must be OWNER/NAME"
  if [ -n "$dir" ]; then
    if [ "$deps" = 1 ] || [ "${#users[@]}" -gt 0 ]; then
      die "--user and --install-deps are for the service install, not --dir"
    fi
    if [ "$uninstall" = 1 ]; then
      uninstall_dir "$dir" "$purge"
      return
    fi
    [ "$purge" = 0 ] || die "--purge only with --uninstall"
    [ "$(uname -s)" = Linux ] || die "only Linux is supported"
    local arch
    arch="$(arch_name "$(uname -m)")" || die "no SyncMusic build for $(uname -m): x86_64 and aarch64 only"
    for tool in curl python3 sha256sum; do command -v "$tool" >/dev/null || die "$tool is needed"; done
    install_dir "$dir" "$channel" "$version" "$arch" "$port"
    return
  fi
  [ "$(id -u)" = 0 ] || die "run it as root: curl -fsSL … | sudo bash (or --dir DIR for a folder install without root)"

  if [ "$uninstall" = 1 ]; then
    [ -x "$BIN" ] || die "SyncMusic is not installed ($BIN missing)"
    local args=(--uninstall)
    [ "$purge" = 1 ] && args+=(--purge)
    "$BIN" "${args[@]}"
    rm -f "$BIN.previous"
    say "removed."
    return
  fi
  [ "$purge" = 0 ] || die "--purge only with --uninstall"
  [ "$(uname -s)" = Linux ] || die "only Linux is supported"
  command -v systemctl >/dev/null || die "systemd is needed for the service"
  local arch
  arch="$(arch_name "$(uname -m)")" || die "no SyncMusic build for $(uname -m): x86_64 and aarch64 only"
  for tool in curl python3 sha256sum; do command -v "$tool" >/dev/null || die "$tool is needed"; done

  local missing
  missing="$(missing_deps)"
  if [ -n "$missing" ]; then
    if [ "$deps" = 1 ]; then
      install_deps
    else
      say "missing tools: $(echo "$missing" | tr '\n' ' ')"
      say "install them with: apt install ffmpeg util-linux fdisk parted udev dosfstools exfatprogs e2fsprogs ntfs-3g sg3-utils eject rsync"
      say "(or run this installer with --install-deps); continuing"
    fi
  fi

  local work
  work="$(mktemp -d)"
  # shellcheck disable=SC2064 # expanded now: $work is local and gone by the time the script exits
  trap "rm -rf '$work'" EXIT
  download "$channel" "$version" "$arch" "$work"
  local tag="$TAG"

  local was_active=0
  systemctl is-active --quiet syncmusic && was_active=1
  [ -e "$BIN" ] && cp -p "$BIN" "$BIN.previous"
  local args=(--install)
  [ -z "$port" ] || args+=(--port "$port")
  for u in "${users[@]+"${users[@]}"}"; do args+=(--user "$u"); done
  "$NEW" "${args[@]}"
  if [ "$was_active" = 1 ]; then
    systemctl restart syncmusic
  else
    systemctl enable --now syncmusic
  fi
  local p="${port:-8765}"
  say "SyncMusic ${tag#v} is running: http://$(hostname -f 2>/dev/null || hostname):$p"
  say "update: run this installer again (or Settings → Updates); remove: … | sudo bash -s -- --uninstall"
}

main "$@"
