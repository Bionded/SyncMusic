"""Shared parametrisation: every registered type plus the test fakes, each with its harness."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from syncmusic.core import registry
from tests.contracts import harness


def _case(kind: str, table: dict[str, str], harnesses: dict[str, Any], load: Callable[[str], type]):
    names = sorted(set(table) | set(harnesses))

    @pytest.fixture(params=names)
    def fixture(request: pytest.FixtureRequest) -> Any:
        name = request.param
        h = harnesses.get(name)
        if h is None:
            pytest.fail(
                f"registered {kind} type {name!r} has no harness in tests/contracts/harness.py"
            )
        expected = load(name) if name in table else None
        return name, h, expected

    return fixture


library_case = _case("library", registry.LIBRARIES, harness.LIBRARIES, registry.library_class)
device_case = _case("device", registry.DEVICES, harness.DEVICES, registry.device_class)
processor_case = _case(
    "processor", registry.PROCESSORS, harness.PROCESSORS, registry.processor_class
)
