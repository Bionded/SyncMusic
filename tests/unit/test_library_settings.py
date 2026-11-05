"""Library settings are user input (UI/API): typos and nonsense values must be rejected."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from syncmusic.libraries.audiobookshelf import AudiobookshelfSettings
from syncmusic.libraries.folder import FolderSettings
from syncmusic.libraries.subsonic import SubsonicSettings

ND = {"url": "http://x", "username": "u", "password": "p"}
ABS = {"url": "http://x", "api_token": "t"}


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (FolderSettings, {"root": "/x", "subfolder_kind": {"Books": "audiobook"}}),  # typo
        (SubsonicSettings, {**ND, "use_native_ap": False}),  # typo
        (SubsonicSettings, {**ND, "page_size": 0}),  # search3 paging never ends
        (SubsonicSettings, {**ND, "timeout_s": -1}),
        (AudiobookshelfSettings, {**ABS, "page_size": 0}),
        (AudiobookshelfSettings, {**ABS, "library_id": ["x"]}),  # typo
    ],
)
def test_bad_settings_rejected(model, data):
    with pytest.raises(ValidationError):
        model.model_validate(data)
