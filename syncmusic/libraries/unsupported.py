# SPDX-License-Identifier: GPL-3.0-or-later
"""A stored library of a type this version no longer offers (registry.RETIRED_LIBRARIES).

It loads, so the app starts and the library can be looked at or deleted, but it is always unavailable with the
retired type's message: its snapshot raises LibraryUnavailable, and a sync whose library is unavailable removes
nothing from the device (the outage rule)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from syncmusic.base.capabilities import LibraryCapabilities
from syncmusic.base.library import Library, LibraryUnavailable
from syncmusic.core.models import HealthStatus, LibrarySnapshot


class UnsupportedSettings(BaseModel):
    model_config = ConfigDict(extra="allow")  # the old type's settings stay as they were


class UnsupportedLibrary(Library):
    type_name = "unsupported"
    display_name = "Not supported"
    Settings: ClassVar[type[BaseModel]] = UnsupportedSettings
    capabilities = LibraryCapabilities()

    @property
    def reason(self) -> str:
        from syncmusic.core.registry import RETIRED_LIBRARIES

        return RETIRED_LIBRARIES.get(
            self.config.type, f"{self.config.type} is not supported in this version"
        )

    async def check(self) -> HealthStatus:
        return HealthStatus(ok=False, message=self.reason)

    async def state_token(self) -> str:
        raise LibraryUnavailable(self.reason)

    async def snapshot(self) -> LibrarySnapshot:
        raise LibraryUnavailable(self.reason)

    async def open_media(self, item_id: str) -> AsyncIterator[bytes] | Path:
        raise LibraryUnavailable(self.reason)
