#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# The "Downloads" table at the top of every release page (stable and beta): one row per file a user may want, with
# direct links to this release's assets. Used by .github/workflows/release.yml, which also runs --check against the
# files it publishes, so a renamed asset cannot leave a dead link.
# Usage: scripts/release-downloads.sh REPO TAG               print the table (TAG = vX.Y.Z or vX.Y.Z-N)
#        scripts/release-downloads.sh --check NOTES DIR      every release-asset link in NOTES names a file in DIR
# Examples:
#   scripts/release-downloads.sh Bionded/SyncMusic v0.2.7-30 > notes.md
#   scripts/release-downloads.sh --check notes.md dist
set -euo pipefail
case "${1:-}" in -h|--help) awk 'NR > 2 && !/^#/ {exit} NR > 2 {sub(/^# ?/, ""); print}' "$0"; exit 0 ;; esac

if [ "${1:-}" = --check ]; then
  [ $# -eq 3 ] || { echo "usage: $0 --check NOTES DIR" >&2; exit 2; }
  names=$(grep -o 'releases/download/[^/)]*/[^)]*' "$2" | sed 's|.*/||' | sort -u)
  [ -n "$names" ] || { echo "no release-asset links in $2" >&2; exit 1; }
  bad=0
  for n in $names; do
    if [ -f "$3/$n" ]; then echo "ok      $n"; else echo "MISSING $n" >&2; bad=1; fi
  done
  exit $bad
fi

[ $# -eq 2 ] || { echo "usage: $0 REPO TAG" >&2; exit 2; }
repo="$1" tag="$2" v="${2#v}"
url="https://github.com/$repo/releases/download/$tag"
image="ghcr.io/$(echo "$repo" | tr '[:upper:]' '[:lower:]')"
a() { echo "[$1]($url/$1)"; }
cat <<NOTES
## Downloads

| What | For whom / which to pick | Download | How to install |
|---|---|---|---|
| SyncMusic server, web UI | Most users: a server or NAS (Linux x86_64) | $(a "syncmusic-web-$v-linux-x86_64") | \`curl -fsSL https://raw.githubusercontent.com/$repo/main/install.sh \| sudo bash -s -- --version $v\` (or \`chmod +x\` the file and run it) |
| SyncMusic server, web UI | Raspberry Pi and other ARM 64-bit Linux | $(a "syncmusic-web-$v-linux-aarch64") | same as above |
| SyncMusic desktop | A Linux desktop: the app in its own window (x86_64) | $(a "syncmusic-desktop-$v-linux-x86_64") | \`chmod +x\` the file and start it |
| SyncMusic desktop | A Linux desktop on ARM 64-bit | $(a "syncmusic-desktop-$v-linux-aarch64") | \`chmod +x\` the file and start it |
| Docker image | Docker / compose users (amd64, arm64) | \`$image:$tag\` | \`docker pull $image:$tag\` |
| SyncMusic app for Compás (HiBy R1), hard float | **Use this one** (faster) | $(a "syncmusic-compas-$v-hard.zip") | Unzip into \`.apps/\` on the SD card |
| SyncMusic app for Compás (HiBy R1), soft float | Only if the hard-float app closes at once or does not start | $(a "syncmusic-compas-$v-soft.zip") | Unzip into \`.apps/\` on the SD card |
| compas-sdtool | Optional: the SyncMusic server downloads it by itself for Compás players (x86_64) | $(a "compas-sdtool-linux-x86_64") | Nothing to do |
| compas-sdtool | The same for ARM 64-bit servers | $(a "compas-sdtool-linux-arm64") | Nothing to do |
| Checksums | Verify a download: \`sha256sum -c --ignore-missing SHA256SUMS\` | $(a SHA256SUMS) | |

Compás app: after unzipping, the card has \`.apps/syncmusic/\`; open *Apps* → **SyncMusic** and approve the first start. If the player says *Could not start the app* right after the copy, restart the player. Guide: https://github.com/$repo/blob/main/docs/10-compas-app.md

NOTES
