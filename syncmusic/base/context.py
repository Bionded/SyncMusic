# SPDX-License-Identifier: GPL-3.0-or-later
"""AppContext: the shared services handed to every library, device and processor instance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from syncmusic.infra.config import BootstrapConfig
from syncmusic.infra.db import Database
from syncmusic.infra.events import EventBus
from syncmusic.infra.jobs import JobRunner

if TYPE_CHECKING:
    from syncmusic.infra.blockdev.guard import Guard


@dataclass
class AppContext:
    config: BootstrapConfig
    db: Database
    bus: EventBus
    jobs: JobRunner
    guard: Guard | None = None  # block device safety check (mount/format)

    @property
    def cache_dir(self) -> Path:
        return self.config.cache_dir

    @property
    def data_dir(self) -> Path:
        return self.config.data_dir
