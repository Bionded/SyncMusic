#!/bin/sh
# Builds the SyncMusic app for Compás players (static little-endian MIPS32: a hard-float binary plus a soft-float
# one it starts on a CPU without FPU) and assembles the
# folder that goes to <SD card>/.apps/syncmusic/.
#   clients/compas/build.sh [VERSION]      VERSION defaults to the version in pyproject.toml
set -e
cd "$(dirname "$0")"
VERSION="${1:-$(sed -n 's/^version = "\(.*\)"/\1/p' ../../pyproject.toml | head -n1)}"
mkdir -p out
CGO_ENABLED=0 GOOS=linux GOARCH=mipsle GOMIPS=hardfloat \
  go build -trimpath -ldflags "-s -w -X main.version=$VERSION -X main.floatABI=hard" -o out/syncmusic-compas .
CGO_ENABLED=0 GOOS=linux GOARCH=mipsle GOMIPS=softfloat \
  go build -trimpath -ldflags "-s -w -X main.version=$VERSION" -o out/syncmusic-compas-softfloat .
rm -rf out/app && mkdir -p out/app/syncmusic
python3 - "$VERSION" <<'PY'
import json, sys
m = json.load(open("app.json"))
m["version"] = sys.argv[1][:15]
assert m["id"] == "syncmusic"
json.dump(m, open("out/app/syncmusic/app.json", "w"), indent=2)
PY
cp icon.png out/syncmusic-compas out/syncmusic-compas-softfloat FONT-LICENSE.txt out/app/syncmusic/
cp ../../LICENSE out/app/syncmusic/LICENSE
cat > out/app/syncmusic/NOTICE <<NOTE
SyncMusic for Compás players, version $VERSION
https://github.com/Bionded/SyncMusic - GPL-3.0-or-later (LICENSE).
Contains glyphs of the Inter typeface, (c) 2016 The Inter Project Authors (https://github.com/rsms/inter), SIL Open Font License 1.1 (FONT-LICENSE.txt).
NOTE
(cd out/app && rm -f "../syncmusic-compas-$VERSION.zip" && python3 -m zipfile -c "../syncmusic-compas-$VERSION.zip" syncmusic)
echo "out/syncmusic-compas-$VERSION.zip"
