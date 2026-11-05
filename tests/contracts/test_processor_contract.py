"""ProcessorContract."""

from __future__ import annotations

import pytest

from syncmusic.base.capabilities import DeviceCapabilities

CAPS = DeviceCapabilities(formats={"mp3", "aac", "alac"})


@pytest.fixture
def proc_case(processor_case):
    name, h, expected = processor_case
    proc = h.make({})
    if expected is not None:
        assert isinstance(proc, expected)
    assert proc.type_name == name
    return proc, h


def test_plan_is_pure(proc_case, tmp_path):
    proc, h = proc_case
    item = h.supported(tmp_path)
    before = item.plan.model_dump()
    a = proc.plan(item.plan, CAPS)
    b = proc.plan(item.plan, CAPS)
    assert a == b and item.plan.model_dump() == before


async def test_size_prediction_within_tolerance(proc_case, tmp_path):
    proc, h = proc_case
    item = h.supported(tmp_path)
    predicted = proc.plan(item.plan, CAPS).file.size
    out = await proc.run(item.model_copy(update={"plan": proc.plan(item.plan, CAPS)}), CAPS)
    actual = out.path.stat().st_size
    assert abs(predicted - actual) <= h.size_tolerance * actual


def test_cache_key_stable(proc_case, tmp_path):
    proc, h = proc_case
    plan = h.supported(tmp_path).plan
    assert proc.cache_key(plan) == h.make({}).cache_key(plan)
    if h.alt_settings:
        assert h.make(h.alt_settings).cache_key(plan) != proc.cache_key(plan)


async def test_unsupported_input_passes_through(proc_case, tmp_path):
    proc, h = proc_case
    if h.unsupported is None:
        pytest.skip(f"{proc.type_name}: accepts every input")
    item = h.unsupported(tmp_path)
    data = item.path.read_bytes()
    assert proc.plan(item.plan, CAPS) == item.plan
    out = await proc.run(item, CAPS)
    assert out.path.read_bytes() == data and out.plan == item.plan
