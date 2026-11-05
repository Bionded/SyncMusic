from syncmusic.core import registry
from tests.contracts import harness


def test_every_registered_type_has_a_harness():
    assert set(registry.LIBRARIES) <= set(harness.LIBRARIES)
    assert set(registry.DEVICES) <= set(harness.DEVICES)
    assert set(registry.PROCESSORS) <= set(harness.PROCESSORS)
