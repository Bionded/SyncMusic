#!/bin/sh
# The image's entrypoint: write the commented default config on first start, then `syncmusic <args>`.
set -eu
if [ ! -f "$SYNCMUSIC_CONFIG" ]; then
  mkdir -p "$(dirname "$SYNCMUSIC_CONFIG")"
  python3 -m syncmusic config init --output "$SYNCMUSIC_CONFIG"
fi
exec python3 -m syncmusic "$@"
