# SPDX-License-Identifier: GPL-3.0-or-later
"""Linked rule templates stored on the device (`.syncmusic/templates.json`), so another installation that syncs the
player can import them as copies.

Every installation writes the templates its registration of the device links and keeps the other installations'
rows. Library ids are installation-local, so each row carries its own small library table (key -> source id, type,
name) and the rules name libraries by those keys; importing maps the keys to local libraries the same way the
manifest maps its libraries (same source id, or a library the user paired)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import ManifestLibrary, Rule, RuleTemplate, UtcDatetime, utcnow
from syncmusic.core.rules import defaults

FILE_NAME = "templates.json"
FORMAT_VERSION = 1


class DeviceTemplate(BaseModel):
    template_uid: str  # "<instance id>:<template id>"
    name: str
    # key -> the library as the writing installation knows it (no credentials)
    libraries: dict[str, ManifestLibrary] = {}
    library_keys: list[str] = []  # the template's libraries; [] = all of the device's libraries
    rules: list[Rule] = []
    excludes: list[Rule] = []
    updated_at: UtcDatetime
    instance_id: str
    instance_name: str = ""
    # an unedited library default ("All music" of a library): importing links the local library's own default
    default_key: str | None = None


def template_uid(instance_id: str, template_id: str) -> str:
    return f"{instance_id}:{template_id}"


def export(
    tpl: RuleTemplate, libraries: dict[str, ManifestLibrary], instance_id: str, instance_name: str
) -> DeviceTemplate:
    """The device row of a local template; `libraries` = this installation's libraries by id."""
    used = list(tpl.library_ids)
    for r in [*tpl.rules, *tpl.excludes]:
        used += [lid for lid in r.libraries or [] if lid not in used]
    keys = {lid: f"T{n}" for n, lid in enumerate(dict.fromkeys(used), 1)}

    def rule(r: Rule) -> Rule:
        if r.libraries is None:
            return r
        return r.model_copy(update={"libraries": [keys[lid] for lid in r.libraries]})

    default_key = None
    if tpl.default_of and not tpl.edited:
        default_key = tpl.id.removeprefix(f"{defaults.PREFIX}{tpl.default_of}.")
    return DeviceTemplate(
        default_key=default_key,
        template_uid=template_uid(instance_id, tpl.id),
        name=tpl.name,
        libraries={
            k: (libraries.get(lid) or ManifestLibrary()).model_copy(
                update={"id": lid, "instance": instance_id, "probe": []}
            )
            for lid, k in keys.items()
        },
        library_keys=[keys[lid] for lid in tpl.library_ids],
        rules=[rule(r) for r in tpl.rules],
        excludes=[rule(r) for r in tpl.excludes],
        updated_at=tpl.updated_at,
        instance_id=instance_id,
        instance_name=instance_name,
    )


def merge(
    on_device: list[DeviceTemplate], ours: list[DeviceTemplate], instance_id: str
) -> list[DeviceTemplate]:
    """The device's rows with this installation's replaced by `ours` (others' kept, in their order)."""
    return [t for t in on_device if t.instance_id != instance_id] + ours


def dumps(rows: list[DeviceTemplate]) -> bytes:
    data = {"version": FORMAT_VERSION, "templates": [r.model_dump(mode="json") for r in rows]}
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def loads(raw: bytes | None) -> list[DeviceTemplate]:
    """The rows of a templates file; a damaged or unknown file reads as empty (it is rewritten at the next sync)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [DeviceTemplate.model_validate(r) for r in data.get("templates", [])]
    except (ValueError, AttributeError, ValidationError):
        return []


def local_libraries(row: DeviceTemplate, ctx: manifest_io.Context) -> dict[str, str]:
    """Row library key -> local library id, for the keys that match a local library."""
    out: dict[str, str] = {}
    for key, lib in row.libraries.items():
        lid = manifest_io.resolve(key, lib, ctx)
        if not manifest_io.is_external(lid) and (not ctx.libraries or lid in ctx.libraries):
            out[key] = lid
    return out


def unmatched(row: DeviceTemplate, ctx: manifest_io.Context) -> list[str]:
    """Names of the row's libraries this installation does not have."""
    found = local_libraries(row, ctx)
    return [lib.name or key for key, lib in row.libraries.items() if key not in found]


def importable(row: DeviceTemplate, ctx: manifest_io.Context) -> bool:
    """A copy makes sense: the template draws from all libraries, or from at least one we have."""
    found = local_libraries(row, ctx)
    return not row.library_keys or any(k in found for k in row.library_keys)


def to_local(row: DeviceTemplate, ctx: manifest_io.Context, new_id: str) -> RuleTemplate:
    """A local copy of the row under `new_id`: library keys mapped to local ids, references to libraries we do not
    have dropped (a rule limited to such libraries only is dropped)."""
    found = local_libraries(row, ctx)

    def rules(listed: list[Rule]) -> list[Rule]:
        out = []
        for r in listed:
            if r.libraries is None:
                out.append(r)
                continue
            mapped = [found[k] for k in r.libraries if k in found]
            if mapped or not r.libraries:
                out.append(r.model_copy(update={"libraries": mapped}))
        return out

    fields: dict[str, Any] = {
        "id": new_id,
        "name": row.name,
        "library_ids": [found[k] for k in row.library_keys if k in found],
        "rules": rules(row.rules),
        "excludes": rules(row.excludes),
        "updated_at": utcnow(),
        "origin_uid": row.template_uid,
        "origin_name": row.instance_name,
        "origin_updated_at": row.updated_at,
    }
    return RuleTemplate(**fields)
