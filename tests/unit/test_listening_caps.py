# SPDX-License-Identifier: GPL-3.0-or-later
"""Ratings and hearts as each device type has them: capabilities, the reverse sync settings shown, and no
conversion between a heart and stars in the reverse sync."""

from __future__ import annotations

import json

import pytest

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core import registry
from syncmusic.core.models import DeviceFeedback
from syncmusic.core.reverse import as_the_device_has
from syncmusic.facade import _reverse_sync_schema

# type -> (ratings, hearts)
EXPECTED = {
    "ipod": (True, False),
    "rockbox": (True, False),
    "hiby": (False, True),
    "hiby_compas": (True, True),  # narrowed by the firmware on the card
    "client": (True, True),
    "flashdrive": (False, False),
}


@pytest.mark.parametrize(("type_name", "expected"), EXPECTED.items())
def test_each_device_type_declares_ratings_and_hearts(type_name, expected):
    caps = registry.device_class(type_name).capabilities
    assert (caps.supports_ratings_read, caps.supports_favourites_read) == expected


def test_reverse_settings_show_only_what_the_device_records():
    assert "ratings" not in _reverse_sync_schema("flashdrive")["properties"]
    assert "ratings" in _reverse_sync_schema("hiby")["properties"]
    assert {"plays", "ratings", "positions"} <= set(_reverse_sync_schema("client")["properties"])
    assert "positions" not in _reverse_sync_schema("rockbox")["properties"]


def test_a_heart_never_becomes_stars_and_stars_never_a_heart():
    fb = DeviceFeedback(ratings={"m/1": 5}, stars={"m/2": True})
    hearts = as_the_device_has(fb, DeviceCapabilities(supports_favourites_read=True))
    assert hearts.ratings == {} and hearts.stars == {"m/2": True}
    stars = as_the_device_has(fb, DeviceCapabilities(supports_ratings_read=True))
    assert stars.ratings == {"m/1": 5} and stars.stars == {}
    both = DeviceCapabilities(supports_ratings_read=True, supports_favourites_read=True)
    assert as_the_device_has(fb, both) is fb


@pytest.mark.parametrize(("version", "stars"), [("1.6.0", True), ("1.5.2", False)])
def test_compas_narrows_to_stars_or_the_heart_by_its_firmware(tmp_path, version, stars):
    from syncmusic.devices.HibyR1.compas import HibyCompas

    (tmp_path / ".compas").mkdir()
    (tmp_path / ".compas" / "device.json").write_text(json.dumps({"player_version": version}))
    dev = HibyCompas.__new__(HibyCompas)
    dev.mount = tmp_path
    assert dev._listening_caps() == {
        "supports_ratings_read": stars,
        "supports_favourites_read": not stars,
    }
