# SPDX-License-Identifier: GPL-3.0-or-later
"""Import / export of device settings and templates between SyncMusic installs (Settings → Import / export).

An export is one JSON document: rule templates (the user's and edited library defaults), the user's processor
templates and - for a device export - the devices' settings without what ties them to this install (id, disk identity,
folder path, state). The libraries the templates draw from are listed by id, name and type; an import maps each to the
local library with the same type and name (else the only local library of that type), rewrites the library ids in the
templates and in the ids of library defaults (`default.<library>.<key>`), and applies a device's settings to the local
device with the same name and type - devices are never created by an import (that needs the device). Secrets are never
exported."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from syncmusic.core.models import (
    DeviceConfig,
    LibraryConfig,
    ProcessorTemplate,
    RuleTemplate,
    UtcDatetime,
    utcnow,
)
from syncmusic.core.rules.defaults import PREFIX

FORMAT: Literal["syncmusic-export"] = "syncmusic-export"
VERSION = 1
# what a device export carries: the settings a user chose, not the identity of the device on this install
DEVICE_FIELDS = (
    "name",
    "type",
    "rule_set",
    "processors",
    "processor_template_id",
    "on_connect",
    "sync_at",
    "delete_threshold_pct",
    "reverse_sync",
    "sync",
    "capability_overrides",
    "settings",
    "update_removals",
    "update_remove_played",
    "remove_deleted",
    "update_sync",
    "update_every_min",
)


class LibraryRef(BaseModel):
    id: str
    name: str
    type: str


class Export(BaseModel):
    format: Literal["syncmusic-export"] = FORMAT
    version: int = Field(VERSION, ge=1, le=VERSION)
    created_at: UtcDatetime = Field(default_factory=utcnow)
    app_version: str = ""
    libraries: list[LibraryRef] = []
    templates: list[RuleTemplate] = []
    processor_templates: list[ProcessorTemplate] = []
    devices: list[dict[str, Any]] = []  # DEVICE_FIELDS of each device


def device_part(cfg: DeviceConfig, secret_keys: set[str]) -> dict[str, Any]:
    data = cfg.model_dump(mode="json", include=set(DEVICE_FIELDS))
    data["settings"] = {k: v for k, v in data["settings"].items() if k not in secret_keys}
    return data


def library_map(exported: list[LibraryRef], local: list[LibraryConfig]) -> dict[str, str | None]:
    """Exported library id -> local library id (same type and name, else the only one of that type) or None."""
    out: dict[str, str | None] = {}
    for ref in exported:
        same = [c for c in local if c.type == ref.type and c.name.casefold() == ref.name.casefold()]
        typed = [c for c in local if c.type == ref.type]
        out[ref.id] = same[0].id if same else typed[0].id if len(typed) == 1 else None
    return out


def map_id(template_id: str, libs: dict[str, str | None]) -> str:
    """A library default's id follows its library (`default.<library>.<key>`); other ids stay."""
    if template_id.startswith(PREFIX):
        rest = template_id[len(PREFIX) :]
        for old, new in libs.items():
            if new and rest.startswith(f"{old}."):
                return f"{PREFIX}{new}.{rest[len(old) + 1 :]}"
    return template_id


def mapped_template(tpl: RuleTemplate, libs: dict[str, str | None]) -> RuleTemplate:
    return tpl.model_copy(
        update={
            "id": map_id(tpl.id, libs),
            "library_ids": [libs.get(x) or x for x in tpl.library_ids],
            "default_of": (libs.get(tpl.default_of) or tpl.default_of) if tpl.default_of else None,
            "updated_at": utcnow(),
        }
    )


def mapped_device(part: dict[str, Any], libs: dict[str, str | None]) -> dict[str, Any]:
    rule_set = dict(part.get("rule_set") or {})
    rule_set["template_ids"] = [map_id(x, libs) for x in rule_set.get("template_ids", [])]
    return {**part, "rule_set": rule_set}


def plan(
    data: Export,
    local_libraries: list[LibraryConfig],
    local_templates: dict[str, RuleTemplate],
    local_processor_templates: dict[str, ProcessorTemplate],
    local_devices: list[DeviceConfig],
) -> dict[str, Any]:
    """What an import does: the library mapping and one change per template / device (add, replace, skip)."""
    libs = library_map(data.libraries, local_libraries)
    names = {c.id: c.name for c in local_libraries}
    changes: list[dict[str, Any]] = []
    for tpl in data.templates:
        new_id = map_id(tpl.id, libs)
        missing = [x for x in tpl.library_ids if not libs.get(x)]
        if missing:
            changes.append(
                {
                    "kind": "template",
                    "name": tpl.name,
                    "action": "skip",
                    "reason": "its library is not here",
                }
            )
        else:
            action = "replace" if new_id in local_templates else "add"
            changes.append({"kind": "template", "name": tpl.name, "action": action, "id": new_id})
    for ptpl in data.processor_templates:
        if ptpl.builtin or (
            ptpl.id in local_processor_templates and local_processor_templates[ptpl.id].builtin
        ):
            changes.append(
                {
                    "kind": "processor_template",
                    "name": ptpl.name,
                    "action": "skip",
                    "reason": "built in",
                }
            )
            continue
        action = "replace" if ptpl.id in local_processor_templates else "add"
        changes.append(
            {"kind": "processor_template", "name": ptpl.name, "action": action, "id": ptpl.id}
        )
    for part in data.devices:
        match = [
            c
            for c in local_devices
            if c.type == part.get("type")
            and c.name.casefold() == str(part.get("name", "")).casefold()
        ]
        if match:
            changes.append(
                {"kind": "device", "name": part.get("name"), "action": "replace", "id": match[0].id}
            )
        else:
            changes.append(
                {
                    "kind": "device",
                    "name": part.get("name"),
                    "action": "skip",
                    "reason": "no device of this name and type here - register it first",
                }
            )
    return {
        "created_at": data.created_at.isoformat(),
        "app_version": data.app_version,
        "libraries": [
            {
                "from": ref.name,
                "type": ref.type,
                "to": libs[ref.id],
                "to_name": names.get(libs[ref.id] or ""),
            }
            for ref in data.libraries
        ],
        "changes": changes,
    }
