#!/bin/sh
# Builds the SyncMusic app for Compás players (static little-endian MIPS32) twice: the hard-float build
# (recommended) and the soft-float build (fallback), each zipped as the folder that goes to <SD card>/.apps/syncmusic/:
#   out/syncmusic-compas-VERSION-hard.zip, out/syncmusic-compas-VERSION-soft.zip
#   clients/compas/build.sh [VERSION]      VERSION defaults to the version in pyproject.toml
set -e
cd "$(dirname "$0")"
VERSION="${1:-$(sed -n 's/^version = "\(.*\)"/\1/p' ../../pyproject.toml | head -n1)}"
mkdir -p out
# one zip per float ABI, each with a single program `syncmusic-compas`
for abi in hard soft; do
  CGO_ENABLED=0 GOOS=linux GOARCH=mipsle GOMIPS=${abi}float \
    go build -trimpath -buildvcs=false -ldflags "-s -w -X main.version=$VERSION -X main.floatABI=$abi" -o "out/syncmusic-compas-$abi" .
  rm -rf "out/app-$abi" && mkdir -p "out/app-$abi/syncmusic"
  python3 - "$VERSION" "out/app-$abi/syncmusic/app.json" <<'PY2'
import json, sys
m = json.load(open("app.json"))
m["version"] = sys.argv[1][:15]
assert m["id"] == "syncmusic" and m["exec"] == "syncmusic-compas"
json.dump(m, open(sys.argv[2], "w"), indent=2)
PY2
  cp icon.png FONT-LICENSE.txt "out/app-$abi/syncmusic/"
  cp "out/syncmusic-compas-$abi" "out/app-$abi/syncmusic/syncmusic-compas"
  cp ../../LICENSE "out/app-$abi/syncmusic/LICENSE"
  cat > "out/app-$abi/syncmusic/NOTICE" <<NOTE
SyncMusic for Compás players, version $VERSION ($abi-float build)
https://github.com/Bionded/SyncMusic - GPL-3.0-or-later (LICENSE).
Contains glyphs of the Inter typeface, (c) 2016 The Inter Project Authors (https://github.com/rsms/inter), SIL Open Font License 1.1 (FONT-LICENSE.txt).
NOTE
  zip="syncmusic-compas-$VERSION-$abi.zip"
  (cd "out/app-$abi" && rm -f "../$zip" && python3 -m zipfile -c "../$zip" syncmusic)
  echo "out/$zip"
done
(cd out && sha256sum syncmusic-compas-hard syncmusic-compas-soft)
