# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests never read the config of a SyncMusic installed on the host."""

import os
from pathlib import Path

import pytest

from syncmusic.infra import config


def test_default_config_path_is_neutralised() -> None:
    assert Path(os.devnull) == config.DEFAULT_CONFIG_PATH
    assert os.environ["SYNCMUSIC_CONFIG"] == os.devnull


def test_load_config_never_opens_the_system_config(monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = open
    opened: list[str] = []

    def spy(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    config.load_config()
    config.load_config(env=dict(os.environ))
    assert not any(p.startswith("/etc/syncmusic") for p in opened)
