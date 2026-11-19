#!/bin/sh
# Starts the program of both built zips (hard and soft float) under qemu-mipsel with an empty app data folder and
# checks that each writes its startup log (catches binaries that die before their first step). Run after build.sh.
set -e
cd "$(dirname "$0")"
for abi in hard soft; do
  d=$(mktemp -d)
  b="out/app-$abi/syncmusic/syncmusic-compas"
  COMPAS_APP_DATA="$d" timeout 20 qemu-mipsel "$b" >/dev/null 2>&1 || true
  grep -q "float $abi" "$d/startup.log" || { echo "smoke: $abi build wrote no startup log" >&2; exit 1; }
  echo "smoke: $abi build ok"
  rm -rf "$d"
done
