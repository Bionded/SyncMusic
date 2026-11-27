# SPDX-License-Identifier: GPL-3.0-or-later
"""Every UI string table defines a key once: a second definition silently replaces the first text."""

import re
from collections import Counter
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[2] / "syncmusic" / "webui" / "static" / "js"
KEY = re.compile(r'^\s*"([a-z][a-z_.0-9]*)":', re.M)


@pytest.mark.parametrize("path", sorted(JS.rglob("*.js")), ids=lambda p: p.name)
def test_no_duplicate_string_keys(path):
    counts = Counter(KEY.findall(path.read_text(encoding="utf-8")))
    assert [k for k, n in counts.items() if n > 1] == []
