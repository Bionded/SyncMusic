"""Every UI string key a web UI module uses is defined where that module can see it.

The web UI (syncmusic/webui/static/js) registers strings per view with `addStrings({...})`; a key defined in one
view and used in another only resolves after the defining view was loaded, so a deep link shows the raw key. Static
check: each literal `t("key")` of a module must be defined in the module itself, in a module it imports
(transitively) or in i18n.js.
"""

from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parents[2] / "syncmusic" / "webui" / "static" / "js"
_USE = re.compile(r"""\bt\(\s*["']([a-z_][\w.]*)["']""")
_DEF = re.compile(r"""^\s*["']([a-z_][\w]*\.[\w.]+)["']\s*:""", re.M)
_IMPORT = re.compile(r"""\bfrom\s+["'](\.{1,2}/[^"']+\.js)["']""")
_COMMENT = re.compile(r"^\s*//.*$", re.M)


def _modules() -> dict[Path, str]:
    return {p.resolve(): p.read_text(encoding="utf-8") for p in JS.rglob("*.js")}


def _visible(path: Path, sources: dict[Path, str], seen: set[Path] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    if path in seen or path not in sources:
        return set()
    seen.add(path)
    keys = set(_DEF.findall(sources[path]))
    for rel in _IMPORT.findall(sources[path]):
        keys |= _visible((path.parent / rel).resolve(), sources, seen)
    return keys


def _missing() -> dict[str, list[str]]:
    sources = _modules()
    shared = set(_DEF.findall(sources[(JS / "i18n.js").resolve()]))
    out: dict[str, list[str]] = {}
    for path, src in sorted(sources.items()):
        known = shared | _visible(path, sources)
        missing = sorted({k for k in _USE.findall(_COMMENT.sub("", src)) if k not in known})
        if missing:
            out[str(path.relative_to(JS))] = missing
    return out


def test_the_check_finds_string_uses():
    assert sum(len(_USE.findall(s)) for s in _modules().values()) > 50


def test_every_used_string_key_is_defined_for_its_module():
    assert _missing() == {}


_STRING = re.compile(r"""^\s*["']([a-z_][\w]*\.[\w.]+)["']\s*:\s*"([^"]*)",""", re.M)
_COUNTED = re.compile(r"\{(\w+)\} (?!is\b|was\b|has\b)[a-z]+s\b")  # "{n} items", not "{name} is"


def test_every_counted_string_has_a_singular():
    """ "1 rules" → each string with "{n} <plural>" has a "<key>_one" variant (t() takes it for n === 1);
    a string with another counted placeholder builds it from the count.* keys instead."""
    strings = {k: v for src in _modules().values() for k, v in _STRING.findall(src)}
    bad = []
    for key, text in strings.items():
        if key.endswith("_one"):
            continue
        for name in _COUNTED.findall(text):
            if name != "n" or f"{key}_one" not in strings:
                bad.append((key, text))
    assert bad == []
    assert strings["tpl.rules_one"] == "{n} rule"
