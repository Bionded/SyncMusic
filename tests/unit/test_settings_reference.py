"""docs/08-settings-reference.md is generated from the UI's setting explanations."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts/gen_settings_reference.py"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_settings_reference", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reference_is_up_to_date():
    gen = _gen()
    assert gen.OUT.read_text() == gen.render(), (
        "run: uv run python scripts/gen_settings_reference.py"
    )


def test_reference_has_every_group_and_known_settings():
    text = _gen().render()
    for group in (
        "### Sync",
        "### Libraries & conversion",
        "### Storage",
        "### Notifications",
        "### System",
    ):
        assert group in text
    for label in ("Write speed limit", "Webhook URL", "Ask before removing more than", "Log level"):
        assert f"| {label} |" in text
