"""Processor settings hashes stay stable across releases.

A changed hash re-processes and re-copies every item already on a device. If a test here fails, the change either
must not touch the hash (a new setting → `hash_omit_defaults`) or is meant to (bump `version`, update the pin and
say why in the commit).
"""

import pytest

from syncmusic.core import registry
from syncmusic.processors.lyrics import LyricsProcessor

PINNED = {
    "transcode": "9bf46dbc09108827",
    "retag": "7b49796db11ece5c",
    "covers": "8468d7c185ba132e",
    "lyrics": "6c7fd0fd3a8704da",  # as added `word_timing`
    "video": "64f7e30a1590139d",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_default_settings_hash_is_pinned(name: str) -> None:
    assert registry.processor_class(name)({}, None).settings_hash() == PINNED[name]


def test_new_setting_at_default_keeps_the_hash() -> None:
    assert LyricsProcessor({"word_timing": False}, None).settings_hash() == PINNED["lyrics"]


def test_new_setting_changed_changes_the_hash() -> None:
    assert LyricsProcessor({"word_timing": True}, None).settings_hash() != PINNED["lyrics"]


def test_every_processor_has_a_pin() -> None:
    names = {c.type_name for c in registry.processor_classes()}
    assert names == set(PINNED), "pin the new processor's default hash here"
