# SPDX-License-Identifier: GPL-3.0-or-later
"""Backup and restore of the application's settings (Settings → Backup).

A backup is one JSON document: the app settings, every library and device, the stored rule templates (the user's and
edited library defaults - untouched defaults are generated from their library), the user's processor templates, the
deleted library defaults and the ignored disks. History, listening data, notifications and the cache are not in it.
Secrets (library and FTP passwords, the webhook URL) are left out unless asked for; then they are in the file in plain
text (the UI says so). A restore without them keeps the secret of a library / device with the same id that is
configured now."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from syncmusic.core.models import (
    AppSettings,
    DeviceConfig,
    LibraryConfig,
    ProcessorTemplate,
    RuleTemplate,
    UtcDatetime,
    utcnow,
)

FORMAT: Literal["syncmusic-backup"] = "syncmusic-backup"
VERSION = 1

SecretKeys = Callable[[str], set[str]]  # type name -> settings keys holding secrets


class Backup(BaseModel):
    format: Literal["syncmusic-backup"] = FORMAT
    version: int = Field(VERSION, ge=1, le=VERSION)
    created_at: UtcDatetime = Field(default_factory=utcnow)
    app_version: str = ""
    secrets: bool = False  # passwords and the webhook URL are in the file
    settings: dict[str, Any] = {}
    libraries: list[LibraryConfig] = []
    devices: list[DeviceConfig] = []
    templates: list[RuleTemplate] = []
    processor_templates: list[ProcessorTemplate] = []
    deleted_default_templates: list[tuple[str, str]] = []
    ignored_disks: list[str] = []


def strip_secrets(
    backup: Backup, library_secrets: SecretKeys, device_secrets: SecretKeys
) -> Backup:
    """The backup without passwords and the webhook URL."""
    libs = [
        c.model_copy(
            update={
                "settings": {
                    k: v for k, v in c.settings.items() if k not in library_secrets(c.type)
                }
            }
        )
        for c in backup.libraries
    ]
    devs = [
        c.model_copy(
            update={
                "settings": {k: v for k, v in c.settings.items() if k not in device_secrets(c.type)}
            }
        )
        for c in backup.devices
    ]
    settings = {**backup.settings, "webhook_url": None}
    return backup.model_copy(
        update={"libraries": libs, "devices": devs, "settings": settings, "secrets": False}
    )


def keep_current_secrets(
    backup: Backup,
    libraries: dict[str, LibraryConfig],
    devices: dict[str, DeviceConfig],
    webhook_url: str | None,
    library_secrets: SecretKeys,
    device_secrets: SecretKeys,
) -> Backup:
    """A backup without secrets takes them from the library / device with the same id (and type) configured now."""
    if backup.secrets:
        return backup

    def merged(cfg: Any, now: Any, keys: set[str]) -> Any:
        if now is None or now.type != cfg.type:
            return cfg
        kept = {k: now.settings[k] for k in keys if k in now.settings and k not in cfg.settings}
        return cfg.model_copy(update={"settings": {**cfg.settings, **kept}}) if kept else cfg

    libs = [merged(c, libraries.get(c.id), library_secrets(c.type)) for c in backup.libraries]
    devs = [merged(c, devices.get(c.id), device_secrets(c.type)) for c in backup.devices]
    settings = {**backup.settings, "webhook_url": backup.settings.get("webhook_url") or webhook_url}
    return backup.model_copy(update={"libraries": libs, "devices": devs, "settings": settings})


def summary(
    backup: Backup, known_library_types: set[str], known_device_types: set[str]
) -> dict[str, Any]:
    """What a restore replaces, and what will not work after it (Restore's preview)."""
    warnings = [
        f"library {c.name!r} has the unknown type {c.type!r}"
        for c in backup.libraries
        if c.type not in known_library_types
    ] + [
        f"device {c.name!r} has the unknown type {c.type!r}"
        for c in backup.devices
        if c.type not in known_device_types
    ]
    AppSettings.model_validate(
        backup.settings
    )  # a bad value is refused before anything is replaced
    return {
        "created_at": backup.created_at.isoformat(),
        "app_version": backup.app_version,
        "secrets": backup.secrets,
        "libraries": len(backup.libraries),
        "devices": len(backup.devices),
        "templates": len(backup.templates),
        "processor_templates": len(backup.processor_templates),
        "warnings": warnings,
    }


def removed(
    backup: Backup,
    libraries: Sequence[LibraryConfig],
    devices: Sequence[DeviceConfig],
    templates: Sequence[RuleTemplate],
    processor_templates: Sequence[ProcessorTemplate],
) -> dict[str, list[str]]:
    """Names of what exists now and is not in the backup: a restore removes it. Library defaults are left
    out - they come back with their library."""

    def gone(now: Sequence[Any], kept: Sequence[Any]) -> list[str]:
        ids = {x.id for x in kept}
        return [x.name for x in now if x.id not in ids]

    return {
        "libraries": gone(libraries, backup.libraries),
        "devices": gone(devices, backup.devices),
        "templates": gone(
            [t for t in templates if not t.id.startswith("default.")], backup.templates
        ),
        "processor_templates": gone(processor_templates, backup.processor_templates),
    }
