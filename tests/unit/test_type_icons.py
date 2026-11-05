"""every registered device and library type has its own icon in the web UI (icons.js); since the
v2 icon set through the type → icon map, whose icons all exist and have an accessible name."""

from __future__ import annotations

import re
from pathlib import Path

from syncmusic.core import registry

ICONS = Path(__file__).parents[2] / "syncmusic" / "webui" / "static" / "js" / "icons.js"


def _block(text: str, start: str) -> str:
    return text.split(start, 1)[1].split("};", 1)[0]


def test_every_registered_type_has_an_icon_and_a_name():
    text = ICONS.read_text()
    paths = set(re.findall(r'^\s*"?([a-z][a-z-]*)"?:\s*"M', _block(text, "const PATHS = {"), re.M))
    type_icons = dict(re.findall(r'([a-z_]+):\s*"([a-z-]+)"', _block(text, "const TYPE_ICONS = {")))
    labels = dict(re.findall(r'([a-z_]+):\s*"([a-z-]+)"', _block(text, "const TYPE_LABELS = {")))
    names = set(re.findall(r'"icon\.([a-z][a-z-]*)":', text))
    for type_name in [*registry.DEVICES, *registry.LIBRARIES]:
        assert type_name in type_icons, f"{type_name} has no icon in icons.js"
        assert type_icons[type_name] in paths, f"{type_name}: icon {type_icons[type_name]} missing"
        assert labels.get(type_name, type_name) in names, f"{type_name} has no accessible name"
