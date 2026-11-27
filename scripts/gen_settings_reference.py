# SPDX-License-Identifier: GPL-3.0-or-later
"""Write docs/08-settings-reference.md from the UI's own setting labels and explanations.

The labels and the one-line explanations under them live next to the fields in the web UI (`set.X` / `set.X_sub`
in views/settings.js, `ds.X` / `ds.X_sub` in views/devsync.js, `field.X` / `field.X.help` in fields.js for the
fields drawn from the device settings models), so the reference cannot drift from the UI. The device fields come
from the same schemas the API hands the web UI (the shared sync and reverse sync settings, and each device type's
own settings model). A test fails when the file is out of date.

Examples:
    uv run python scripts/gen_settings_reference.py            # rewrite the file
    uv run python scripts/gen_settings_reference.py --check    # exit 1 when it is out of date
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ROOT / "syncmusic/webui/static/js/views"
FIELDS = ROOT / "syncmusic/webui/static/js/fields.js"
OUT = ROOT / "docs/08-settings-reference.md"

STRING = re.compile(r'^\s*"([a-z_.0-9]+)": "((?:[^"\\]|\\.)*)",?$', re.M)
KEY = re.compile(
    r't\("((?:set|ds)\.[a-z_0-9]+)"\)|section\("([a-z]+)"|<h2>\$\{t\("(ds\.[a-z_]+)"\)'
)


FIELD = re.compile(r'^\s*"(field\.[A-Za-z_.0-9-]+)": "((?:[^"\\]|\\.)*)"', re.M)

# the device Settings tab's groups for the shared sync settings (as views/devsync.js and views/devices.js)
MOUNT = ("use_existing_mount", "unmount_foreign", "foreign_read_only")
SAFETY = (
    "safety_margin_pct",
    "safety_margin_min_bytes",
    "safety_margin_max_bytes",
    "backup_device_db",
    "backup_keep",
)
LISTEN = ("rating_priority", "playlist_conflicts")
NOCACHE = ("no_cache_files", "no_cache_budget_mb", "no_cache_file_max_mb")
GENTLE = (
    "gentle_batch_files",
    "gentle_pause_s",
    "gentle_rate_mb_s",
    "keep_disk_awake",
    "keep_disk_awake_s",
)
OWN = ("verify", "eject_after", "stay_ready", "gentle_write", "no_cache", *GENTLE, *NOCACHE)
TYPE_KEYS = {
    "android": {"base_path": "android_base_path"}
}  # a type's own label for a shared field name


def strings(src: str) -> dict[str, str]:
    return {k: v.replace('\\"', '"') for k, v in STRING.findall(src)}


def helper(src: str, name: str) -> str:
    """The text of a top-level `function name(` in a view, up to its closing brace."""
    start = src.index(f"function {name}(")
    return src[start : src.index("\n}\n", start) + 3]


def entries(src: str, s: dict[str, str], start: str) -> list[tuple[str, str, str]]:
    """(group, label, explanation) in the order the page shows them; a group is a section or a card title."""
    body = src[src.index(start) :]
    for call, name in (("accessBox(access)", "accessBox"), ("clientsBox(clients)", "clientsBox")):
        if (
            call in body
        ):  # boxes drawn by helpers defined above render(), in the section that calls them
            body = body.replace(call, helper(src, name), 1)
    out, group, seen = [], "", set()
    for m in KEY.finditer(body):
        key, sec, card = m.groups()
        if sec:
            group = s.get(f"set.sec.{sec}", sec)
        elif card:
            group = s.get(card, card)
        elif (
            key
            and f"{key}_sub" in s
            and key in s
            and key not in seen
            and "{" not in s[f"{key}_sub"]
        ):
            seen.add(key)
            out.append((group, s[key], s[f"{key}_sub"]))
    return out


def field_text(f: dict[str, str], name: str, prop: dict[str, Any], key: str) -> tuple[str, str]:
    """The label and help forms.js shows for a schema field."""
    label = f.get(f"field.{key}") or name.replace("_", " ").capitalize()
    if prop.get("type") == "integer" and name.endswith("_bytes"):
        label += " (MB)"
    return label, f.get(f"field.{key}.help") or prop.get("description", "")


def table(rows: list[tuple[str, str]]) -> list[str]:
    out = ["", "| Setting | What it does |", "|---|---|"]
    return out + [f"| {a} | {b.replace('|', '\\|')} |" for a, b in rows]


def device_fields() -> tuple[
    dict[str, list[tuple[str, str]]], list[tuple[str, list[tuple[str, str]]]]
]:
    """Rows for the shared sync and reverse sync settings per group, and each device type's own settings."""
    from syncmusic import facade
    from syncmusic.core import registry

    f = {k: v.replace('\\"', '"') for k, v in FIELD.findall(FIELDS.read_text())}
    dv = strings((VIEWS / "devsync.js").read_text())
    dev = strings((VIEWS / "devices.js").read_text())
    classes = registry.device_classes()
    names = {c.type_name: c.display_name for c in classes}

    def where(key: str, kind: str) -> str:
        """`(only: …)` when not every device type shows the field."""
        schema = facade._sync_schema if kind == "sync" else facade._reverse_sync_schema
        has = [c.type_name for c in classes if key in schema(c.type_name)["properties"]]
        return "" if len(has) == len(classes) else f" Only: {', '.join(names[t] for t in has)}."

    groups: dict[str, list[tuple[str, str]]] = {}
    sync = facade.SyncSettings.model_json_schema()["properties"]
    for name, prop in sync.items():
        if name in OWN and name not in GENTLE and name not in NOCACHE:
            continue  # drawn by devsync.js with its own strings (above)
        group = (
            dv["ds.mount_title"]
            if name in MOUNT
            else dv["ds.safety_title"]
            if name in SAFETY
            else dv["ds.listen_title"]
            if name in LISTEN
            else dv["ds.nocache_title"]
            if name in NOCACHE
            else dev["dev.set_gentle"]
            if name in GENTLE
            else dv["ds.advanced"]
        )
        label, text = field_text(f, name, prop, name)
        groups.setdefault(group, []).append((label, (text + where(name, "sync")).strip()))
    for name, prop in facade.ReverseSyncSettings.model_json_schema()["properties"].items():
        label, text = field_text(f, name, prop, name)
        if name == "enabled":  # the master switch, drawn by devices.js with its own strings
            label, text = dev["dev.set_reverse"], dev["dev.set_reverse_sub"]
        groups.setdefault(dev["dev.settings_reverse"], []).append(
            (label, (text + where(name, "reverse")).strip())
        )
    types = []
    for c in classes:
        rows = []
        for name, prop in c.Settings.model_json_schema().get("properties", {}).items():
            rows.append(field_text(f, name, prop, TYPE_KEYS.get(c.type_name, {}).get(name, name)))
        if rows:
            types.append((c.display_name, rows))
    return groups, types


def render() -> str:
    lines = [
        "# Settings reference",
        "",
        "Every setting with the explanation shown under it in the web UI. Generated by",
        "`scripts/gen_settings_reference.py` from the UI's own strings; see [Settings](05-settings.md) for how they work",
        "together. The search box at the top of *Settings* and of a device's *Settings* tab searches the same texts.",
    ]
    pages = (
        ("App settings (Settings page)", "settings.js", "export async function render"),
        (
            "Per device (a device's Settings tab)",
            "devsync.js",
            "export function syncTab",
        ),
    )
    extra, types = device_fields()
    for title, file, start in pages:
        src = (VIEWS / file).read_text()
        lines += ["", f"## {title}"]
        grouped: dict[str, list[tuple[str, str]]] = {}
        for g, label, text in entries(src, strings(src), start):
            grouped.setdefault(g, []).append((label, text))
        if file == "devsync.js":
            for g, rows in extra.items():
                grouped.setdefault(g, []).extend(rows)
        for g, rows in grouped.items():
            lines += ["", f"### {g}", *table(rows)]
    lines += [
        "",
        "## Per device type (a device's Settings tab, its own fields)",
        "",
        "The fields only one device type has, shown in its own card and under *Files on the device*.",
    ]
    for title, rows in types:
        lines += ["", f"### {title}", *table(rows)]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--check", action="store_true", help="exit 1 when the file is out of date")
    text = render()
    if ap.parse_args().check:
        return 0 if OUT.exists() and OUT.read_text() == text else 1
    OUT.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
