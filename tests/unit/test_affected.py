# SPDX-License-Identifier: GPL-3.0-or-later
"""tests/affected.py: which test files a change can affect (scripts/test.py --changed), on a made-up tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.affected import affected

TREE = {
    "syncmusic/__init__.py": "",
    "syncmusic/core/__init__.py": "",
    "syncmusic/core/rules.py": "X = 1\n",
    "syncmusic/core/sync.py": "from . import rules\n",
    "syncmusic/devices/flash.py": "from syncmusic.core.sync import X\n",
    "syncmusic/other.py": "Y = 2\n",
    "tests/__init__.py": "",
    "tests/helpers.py": "import syncmusic.devices.flash\n",
    "tests/unit/test_rules.py": "from syncmusic.core import rules\n",
    "tests/unit/test_flash.py": "from tests.helpers import *\n",
    "tests/unit/test_other.py": "from syncmusic import other\n",
    "tests/unit/conftest.py": "",
    "tests/webui/test_page.py": "",
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for rel, text in TREE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


def test_a_module_runs_every_test_that_imports_it_through_other_modules(root):
    # rules <- sync (relative import) <- flash <- tests.helpers <- test_flash; rules <- test_rules
    assert affected(root, ["syncmusic/core/rules.py"]) == [
        "tests/unit/test_flash.py",
        "tests/unit/test_rules.py",
    ]
    assert affected(root, ["syncmusic/other.py"]) == ["tests/unit/test_other.py"]


def test_a_test_file_runs_itself_and_a_conftest_its_folder(root):
    assert affected(root, ["tests/unit/test_other.py"]) == ["tests/unit/test_other.py"]
    assert affected(root, ["tests/unit/conftest.py"]) == [
        "tests/unit/test_flash.py",
        "tests/unit/test_other.py",
        "tests/unit/test_rules.py",
    ]


def test_web_ui_files_run_the_ui_tests_and_docs_nothing(root):
    assert affected(root, ["syncmusic/webui/static/js/api.js"]) == ["tests/webui/test_page.py"]
    assert affected(root, ["docs/03-first-run.md", "README.md"]) == []


def test_a_change_no_rule_covers_runs_everything(root):
    assert affected(root, ["pyproject.toml"]) is None
    assert affected(root, ["syncmusic/other.py", "tests/integration/compose.yaml"]) is None
