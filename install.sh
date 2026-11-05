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
# Downloads the single-file web binary for this CPU (x86_64 or aarch64) and SHA256SUMS, checks the checksum and hands
# over to the binary's own `--install`: /usr/local/bin/syncmusic (the one before stays as syncmusic.previous), its
# runtime in /opt/syncmusic, config /etc/syncmusic/config.toml (kept when it exists), data /var/lib/syncmusic, the
# unit syncmusic.service, enabled and started. Running it again upgrades in place.
set -euo pipefail

REPO="Bionded/SyncMusic"
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
  local channel=stable version="" port="" deps=0 uninstall=0 purge=0 users=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --beta) channel=beta; shift ;;
      --version) [ $# -ge 2 ] || die "--version needs a value"; version="${2#v}"; shift 2 ;;
      --port) [ $# -ge 2 ] || die "--port needs a value"; port="$2"; shift 2 ;;
      --user) [ $# -ge 2 ] || die "--user needs a value"; users+=("$2"); shift 2 ;;
      --repo) [ $# -ge 2 ] || die "--repo needs a value"; REPO="$2"; shift 2 ;;
      --install-deps) deps=1; shift ;;
      --uninstall) uninstall=1; shift ;;
      --purge) purge=1; shift ;;
      -h|--help) awk 'NR > 2 && !/^#/ {exit} NR > 2 {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]:-$0}" 2>/dev/null \
        || echo "see https://github.com/$REPO#installation"; exit 0 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  [ "$(id -u)" = 0 ] || die "run it as root: curl -fsSL … | sudo bash"
  [ -z "$port" ] || [[ "$port" =~ ^[0-9]+$ ]] || die "--port must be a number"
  [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "--repo must be OWNER/NAME"

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

  local release tag url sums
  release="$(curl -fsSL -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$REPO/releases?per_page=50" \
    | pick_release "$channel" "$version" "$arch")" || die "the releases of $REPO cannot be read"
  [ -n "$release" ] || die "no ${version:-$channel} release with a linux-$arch binary in $REPO"
  read -r tag url sums <<< "$release"

  local work
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  say "downloading SyncMusic ${tag#v} for linux-$arch"
  curl -fsSL -o "$work/${url##*/}" "$url"
  curl -fsSL -o "$work/SHA256SUMS" "$sums"
  (cd "$work" && grep -E " \*?${url##*/}\$" SHA256SUMS | sha256sum -c --status) \
    || die "checksum mismatch for ${url##*/}: not installed"
  chmod 0755 "$work/${url##*/}"

  local was_active=0
  systemctl is-active --quiet syncmusic && was_active=1
  [ -e "$BIN" ] && cp -p "$BIN" "$BIN.previous"
  local args=(--install)
  [ -z "$port" ] || args+=(--port "$port")
  for u in "${users[@]+"${users[@]}"}"; do args+=(--user "$u"); done
  "$work/${url##*/}" "${args[@]}"
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
