#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Builds compas-sdtool (the Compás player's library code as a command-line tool, part of Compás Tools,
# GPL-3.0) for this machine and puts it with its licence notices into OUT_DIR:
#
#   compas-sdtool-<os>-<arch>          the program
#   compas-sdtool-<os>-<arch>.json     build id, compas-tools and compas-player revisions, database_format
#   compas-sdtool-notices/             LICENSE texts of Compás Tools, the player and its bundled libraries, NOTICE
#
#   scripts/build-compas-sdtool.sh [OUT_DIR]     (default: dist/)
#
# SyncMusic only runs the program as a separate process. Sources:
#   https://github.com/Starnished66/compas-tools  (sdtool/, revision COMPAS_TOOLS_REF below)
#   https://github.com/Starnished66/compas-player (the revision compas-tools pins as PLAYER_REF)
# JOBS (default 2) limits the parallel compile; WORK (default a temp dir) keeps the checkouts.
#
# Examples:
#   scripts/build-compas-sdtool.sh                        # into dist/, 2 compile jobs
#   JOBS=4 WORK=/var/tmp/sdtool scripts/build-compas-sdtool.sh /var/tmp/out
set -euo pipefail

case "${1:-}" in
  -h | --help)
    sed -n '3,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
    exit 0
    ;;
esac

COMPAS_TOOLS_REPO=https://github.com/Starnished66/compas-tools.git
COMPAS_TOOLS_REF=${COMPAS_TOOLS_REF:-458b12f}
PLAYER_REPO=https://github.com/Starnished66/compas-player.git

out=$(realpath -m "${1:-dist}")
jobs=${JOBS:-2}
work=${WORK:-$(mktemp -d)}
mkdir -p "$out" "$work"

fetch() { # repo ref dir
  if [ ! -d "$3/.git" ]; then git clone -q "$1" "$3"; fi
  git -C "$3" fetch -q origin "$2" 2>/dev/null || git -C "$3" fetch -q origin
  git -C "$3" checkout -q --detach "$2"
}

fetch "$COMPAS_TOOLS_REPO" "$COMPAS_TOOLS_REF" "$work/tools"
player_ref=$(sed -n 's/^ *PLAYER_REF: *\([0-9a-f]*\).*/\1/p' "$work/tools/.github/workflows/build.yml" | head -1)
[ -n "$player_ref" ] || { echo "PLAYER_REF not found in compas-tools' build.yml" >&2; exit 1; }
fetch "$PLAYER_REPO" "$player_ref" "$work/player"
git -C "$work/player" submodule update -q --init --recursive 2>/dev/null || true

make -C "$work/player" -j"$jobs" -f Makefile -f "$work/tools/sdtool/sdtool.mk" sdtool
bin="$work/tools/build/sdtool/compas-sdtool"
"$work/tools/scripts/verify-sdtool.sh" "$work/player" "$bin"

os=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$(uname -m)" in aarch64 | arm64) arch=arm64 ;; x86_64 | amd64) arch=x86_64 ;; *) arch=$(uname -m) ;; esac
name="compas-sdtool-$os-$arch"
install -m 0755 "$bin" "$out/$name"
card=$(mktemp -d)
mkdir "$card/.compas"
info=$("$out/$name" info "$card")
rm -rf "$card"
python3 - "$out/$name" "$info" "$(git -C "$work/tools" rev-parse HEAD)" "$player_ref" <<'PY'
import hashlib, json, sys
path, info, tools, player = sys.argv[1:]
data = json.loads(info.splitlines()[-1])
meta = {
    "name": "compas-sdtool",
    "build": data.get("build"),
    "database_format": data.get("database_format"),
    "compas_tools": {"url": "https://github.com/Starnished66/compas-tools", "revision": tools},
    "compas_player": {"url": "https://github.com/Starnished66/compas-player", "revision": player},
    "license": "GPL-3.0",
    "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
}
open(path + ".json", "w").write(json.dumps(meta, indent=2) + "\n")
PY

notices="$out/compas-sdtool-notices"
mkdir -p "$notices"
cp "$work/tools/LICENSE" "$notices/compas-tools-LICENSE.txt"
cp "$work/tools/THIRD_PARTY.md" "$notices/compas-tools-THIRD_PARTY.md"
cp "$work/player/LICENSE" "$notices/compas-player-LICENSE.txt"
(cd "$work/player" && { find licenses -type f 2>/dev/null; ls third_party/lv_xml/src/libs/expat/LICENSE.txt sonic/LICENSE 2>/dev/null; }) |
  while read -r f; do cp "$work/player/$f" "$notices/${f//\//__}"; done
cat > "$notices/NOTICE.txt" <<TXT
compas-sdtool is part of Compás Tools and contains the library code of the Compás player.
It is free software under the GNU General Public License version 3 (see compas-tools-LICENSE.txt and
compas-player-LICENSE.txt); the licences of the libraries it bundles are in this folder.

Sources it was built from:
  Compás Tools   https://github.com/Starnished66/compas-tools  revision $(git -C "$work/tools" rev-parse HEAD) (sdtool/)
  Compás player  https://github.com/Starnished66/compas-player revision $player_ref

SyncMusic runs compas-sdtool as a separate program; it is not linked into SyncMusic.
TXT
echo "built $out/$name"
