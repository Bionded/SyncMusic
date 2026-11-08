# SPDX-License-Identifier: GPL-3.0-or-later
"""Processor base class."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.context import AppContext
from syncmusic.core.models import PlanItem, ProcessorSlot, WorkItem


class Processor(ABC):
    type_name: ClassVar[str]
    display_name: ClassVar[str]
    Settings: ClassVar[type[BaseModel]]
    stage: ClassVar[Literal["audio", "tags", "cover", "sidecar"]]
    version: ClassVar[int] = 1  # bump when output changes for the same input+settings
    # library inputs the prepare pipeline fetches into the WorkItem before the chain runs:
    # "cover" → WorkItem.cover (Library.open_cover), "lyrics" → WorkItem.lyrics (Library.get_lyrics)
    needs: ClassVar[frozenset[Literal["cover", "lyrics"]]] = frozenset()
    # settings added after devices were synced: left out of `settings_hash` while at their default, so adding a
    # setting does not re-process every item already on a device. Every new setting goes here.
    hash_omit_defaults: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, settings: BaseModel | dict[str, object], ctx: AppContext | None) -> None:
        self.settings = (
            settings
            if isinstance(settings, self.Settings)
            else self.Settings.model_validate(settings if isinstance(settings, dict) else {})
        )
        self.ctx = ctx

    @classmethod
    def suggest(cls, caps: DeviceCapabilities) -> ProcessorSlot | None:
        """Default slot for a new device with these capabilities (None = not suggested)."""
        return None

    @abstractmethod
    def plan(self, item: PlanItem, caps: DeviceCapabilities) -> PlanItem:
        """Pure: predict the output (format/size). No I/O."""

    @abstractmethod
    async def run(self, item: WorkItem, caps: DeviceCapabilities) -> WorkItem:
        """Do the work on the file in the work dir."""

    def settings_hash(self) -> str:
        # once per processor and settings value (called per item while planning)
        dump = self.settings.model_dump(mode="json")
        memo: tuple[dict[str, Any], str] | None = getattr(self, "_settings_hash_memo", None)
        if memo is not None and memo[0] == dump:
            return memo[1]
        hashed = self._settings_hash(dict(dump))
        self._settings_hash_memo = (dump, hashed)
        return hashed

    def _settings_hash(self, dump: dict[str, Any]) -> str:
        defaults = self.Settings().model_dump(mode="json")
        for name in self.hash_omit_defaults:
            if dump.get(name) == defaults.get(name):
                dump.pop(name, None)
        data = json.dumps(dump, sort_keys=True)
        return hashlib.sha256(f"{self.type_name}:{self.version}:{data}".encode()).hexdigest()[:16]

    def cache_key(self, item: PlanItem) -> str | None:
        """Deterministic contribution to the cache key; None = output not cacheable.

        Called with the item as planned *after* this processor's `plan()`, so device-dependent output
        (format, sample rate, ...) can be part of the key.
        """
        return self.settings_hash()
