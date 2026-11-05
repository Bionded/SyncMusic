#!/usr/bin/env bash
# Export the logo from the masters assets/branding/logo.svg (glyph, currentColor) and app-icon.svg
# (rounded square) into the favicon set of the web UI and the README/GitHub images. Needs rsvg-convert
# (librsvg2-bin) and ImageMagick (`magick`, only to pack favicon.ico). Re-run after changing a master.
#
# Examples:
#   scripts/export-branding.sh
#   scripts/export-branding.sh --help
set -euo pipefail
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,/^set -euo/p' "$0" | sed -e '/^set -euo/d' -e 's/^# \{0,1\}//'
  exit 0
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."
SRC=assets/branding
ICONS=syncmusic/webui/static/icons
OUT=$SRC/build
mkdir -p "$ICONS" "$OUT"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

ORANGE="#F0883E"
DARK="#1B0E03"
# the glyph's shapes without the C2PA metadata (the masters keep it; the exports need none)
GLYPH=$(sed -e 's/<metadata>.*<\/metadata>//' -e 's/^<svg[^>]*>//' -e 's/<\/svg>$//' "$SRC/logo.svg")
glyph() { echo "${GLYPH//currentColor/$1}"; }

# favicon.svg: the app icon itself, without metadata
sed -e 's/<metadata>.*<\/metadata>//' -e 's/ xmlns:c2pa="[^"]*"//' "$SRC/app-icon.svg" > "$ICONS/favicon.svg"
for s in 16 32 48 192 512; do
  rsvg-convert -w $s -h $s "$ICONS/favicon.svg" -o "$TMP/icon-$s.png"
done
cp "$TMP/icon-192.png" "$TMP/icon-512.png" "$ICONS/"
rsvg-convert -w 180 -h 180 "$ICONS/favicon.svg" -o "$ICONS/apple-touch-icon.png"
magick "$TMP/icon-16.png" "$TMP/icon-32.png" "$TMP/icon-48.png" "$ICONS/favicon.ico"

# maskable: full-bleed orange, the glyph inside the 80 % safe zone (scale 0.6 keeps the ring well inside)
cat > "$TMP/maskable.svg" <<SVG
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" fill="$ORANGE"/><g transform="translate(102.4 102.4) scale(0.6)">$(glyph "$DARK")</g></svg>
SVG
rsvg-convert -w 512 -h 512 "$TMP/maskable.svg" -o "$ICONS/maskable-512.png"

# horizontal lockup (glyph + name), one per background
lockup() {  # $1 glyph colour, $2 text colour, $3 file
  cat > "$3" <<SVG
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1120 256"><g transform="translate(0 0) scale(0.5)">$(glyph "$1")</g><text x="280" y="170" font-family="'URW Gothic', 'Century Gothic', 'Nimbus Sans', 'DejaVu Sans', sans-serif" font-weight="700" font-size="136" letter-spacing="-2" fill="$2">SyncMusic</text></svg>
SVG
}
lockup "$ORANGE" "$DARK" "$OUT/lockup-light.svg"
lockup "$ORANGE" "#FFFFFF" "$OUT/lockup-dark.svg"

# README banner (1280×320) and GitHub social preview (1280×640), dark background
banner() {  # $1 height, $2 file, $3 tagline y
  cat > "$TMP/banner.svg" <<SVG
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 $1"><rect width="1280" height="$1" fill="#16120F"/><g transform="translate(150 $(( ($1 - 256) / 2 - ${4:-0} )))">$(sed -e 's/^<svg[^>]*>//' -e 's/<\/svg>$//' "$OUT/lockup-dark.svg")</g><text x="640" y="$3" text-anchor="middle" font-family="'Nimbus Sans', 'DejaVu Sans', sans-serif" font-size="34" fill="#C9BFB6">your music, on your players</text></svg>
SVG
  rsvg-convert -w 1280 -h "$1" "$TMP/banner.svg" -o "$2"
}
banner 320 "$OUT/readme-banner.png" 290 20
banner 640 "$OUT/social-preview.png" 470 40
rsvg-convert -w 256 -h 256 "$ICONS/favicon.svg" -o "$OUT/app-icon-256.png"
echo "exported to $ICONS and $OUT"
