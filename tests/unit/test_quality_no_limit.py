# SPDX-License-Identifier: GPL-3.0-or-later
"""Quality profiles on a device that plays every format (no format limit)."""

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core import quality


def test_max_quality_keeps_every_file_on_a_device_without_a_format_limit() -> None:
    assert quality._transcode("max", DeviceCapabilities(formats=set())) is None


def test_save_space_converts_only_lossless_on_a_device_without_a_format_limit() -> None:
    rules = quality._transcode("space", DeviceCapabilities(formats=set()))["rules"]
    assert [r["from"] for r in rules] == [quality.LOSSLESS_SOURCES]
