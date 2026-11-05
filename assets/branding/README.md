# Branding

The logo, **"Satellite"**: a ring with a gap, a filled centre and a small satellite dot.

- `logo.svg`: the glyph in `currentColor`. The UI tints it (top bar: `--ac-on` on the accent square).
- `app-icon.svg`: the app icon, a rounded square `#F0883E` with the glyph in `#1B0E03`, scaled to 0.78.
- `app-icon-preview.png`: the app icon at 16–256 px on dark and light backgrounds.
- `build/`: exports (README banner, GitHub social preview 1280×640, horizontal lockups for light and dark
  backgrounds, the 256 px app icon for a `.desktop` file).

The favicon set of the web UI (`syncmusic/webui/static/icons/`: SVG, ICO 16/32/48, apple-touch 180, 192/512,
maskable 512) and everything in `build/` come from `scripts/export-branding.sh` (needs `rsvg-convert` from
`librsvg2-bin` and ImageMagick). Re-run the script after changing a master. The masters keep their C2PA
metadata; the exports carry none.

**License:** the artwork in this folder and its exports is licensed under
[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) (code: GPL-3.0-or-later). The name and the logo are also
covered by [TRADEMARKS.md](../../TRADEMARKS.md).
