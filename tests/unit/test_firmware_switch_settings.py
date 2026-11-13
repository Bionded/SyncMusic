# SPDX-License-Identifier: GPL-3.0-or-later
"""Switching a device to its firmware sibling type carries over only the settings the new type understands."""

from syncmusic.core import registry
from syncmusic.facade import sibling_settings


def _validate(type_name, settings):
    return registry.device_class(type_name).Settings.model_validate(settings)


def test_sonixos_to_compas_defaults():
    stored = registry.device_class("hiby").Settings().model_dump(mode="json")
    out, dropped = sibling_settings("hiby", "hiby_compas", stored)
    model = _validate("hiby_compas", out)
    assert model.formats  # None became the new type's default set
    assert model.layout == registry.device_class("hiby_compas").Settings().layout
    assert {"filesystem", "ascii_only", "playlist_dir", "split_genres", "genre_separators"} <= set(
        dropped
    )


def test_user_choices_survive_the_switch():
    stored = {
        "formats": ["mp3", "flac"],
        "lyrics": "embedded",
        "playlists": False,
        "ascii_only": True,
    }
    out, dropped = sibling_settings("hiby", "hiby_compas", stored)
    model = _validate("hiby_compas", out)
    assert model.formats == {"mp3", "flac"}
    assert (model.lyrics, model.playlists) == ("embedded", False)
    assert dropped == ["ascii_only"]


def test_compas_to_sonixos():
    stored = registry.device_class("hiby_compas").Settings().model_dump(mode="json")
    stored["lyrics"] = "none"
    out, dropped = sibling_settings("hiby_compas", "hiby", stored)
    model = _validate("hiby", out)
    assert model.formats is None and model.lyrics == "none"
    assert model.layout == registry.device_class("hiby").Settings().layout
    assert dropped == []


def test_rejected_value_falls_back_to_defaults():
    out, dropped = sibling_settings("hiby", "hiby_compas", {"formats": ["nope-format"]})
    _validate("hiby_compas", out)
