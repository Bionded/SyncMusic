"""The v2 icon set: every icon name the web UI uses exists in icons.js, every icon has an accessible
name, and the unicode glyphs the v1 screens used as icons are gone.

Static check over syncmusic/webui/static: `icon("name", …)` calls (string literals in the first argument, so a
ternary `icon(open ? "up" : "down")` counts both), the sidebar's `icon: "name"` entries, the badge states in ui.js
and the type → icon map in icons.js. An unknown name would silently draw a dot.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "syncmusic" / "webui" / "static"
JS = STATIC / "js"
_COMMENT = re.compile(r"^\s*//[^\n]*|/\*.*?\*/", re.M | re.S)
_CALL = re.compile(r"\bicon\(([^,)]*)")
_LITERAL = re.compile(r'"([a-z][a-z-]*)"')
_NAV = re.compile(r'\bicon:\s*"([a-z][a-z-]*)"')
# glyphs v1 used as icons; arrows inside prose ("FLAC → MP3", "tag ← template") are typography, not icons
_GLYPHS = re.compile("[⌂♫▣☰⛭⚙＋▾▴●◐▲✕ℹ○↻▶↑↓×♪↗]")


def _source(path: Path) -> str:
    return _COMMENT.sub("", path.read_text(encoding="utf-8"))


def _icons_js() -> str:
    return (JS / "icons.js").read_text(encoding="utf-8")


def _defined() -> set[str]:
    block = _icons_js().split("const PATHS = {", 1)[1].split("\n};", 1)[0]
    return set(re.findall(r'^\s*"?([a-z][a-z-]*)"?:\s*"M', block, re.M))


def _used() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in sorted(JS.rglob("*.js")):
        src = _source(path)
        names = {n for arg in _CALL.findall(src) for n in _LITERAL.findall(arg)}
        names |= set(_NAV.findall(src))
        if path.name == "ui.js":
            states = src.split("const STATES = {", 1)[1].split("};", 1)[0]
            names |= set(re.findall(r'\["[a-z]+",\s*"([a-z-]+)"\]', states))
        if path.name == "icons.js":
            types = src.split("const TYPE_ICONS = {", 1)[1].split("};", 1)[0]
            names |= set(re.findall(r':\s*"([a-z-]+)"', types))
        if names:
            used[str(path.relative_to(STATIC))] = names
    return used


def test_every_used_icon_exists():
    defined = _defined()
    used = _used()
    assert len(used) >= 10  # the scan sees the screens
    unknown = {f: sorted(n - defined) for f, n in used.items() if n - defined}
    assert not unknown, f"icon names not in icons.js: {unknown}"


def test_every_icon_has_an_accessible_name():
    src = _icons_js()
    named = set(re.findall(r'"icon\.([a-z][a-z-]*)":', src))
    assert _defined() <= named, sorted(_defined() - named)


def test_no_v1_glyph_icons_left():
    hits = []
    for path in [*sorted(JS.rglob("*.js")), STATIC / "index.html", STATIC / "app.css"]:
        for no, line in enumerate(_source(path).splitlines(), 1):
            if _GLYPHS.search(line):
                hits.append(f"{path.relative_to(STATIC)}:{no}: {line.strip()[:100]}")
    assert not hits, "\n".join(hits)


def test_favicon_set_and_manifest_exist():
    """every icon index.html and the web manifest point to is exported (scripts/export-branding.sh)."""
    import json

    html = (STATIC / "index.html").read_text()
    refs = re.findall(r'<link rel="(?:icon|apple-touch-icon|manifest)" href="([^"]+)"', html)
    assert len(refs) == 4
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())
    refs += [i["src"] for i in manifest["icons"]]
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])
    for ref in refs:
        assert (STATIC / ref).is_file(), ref
