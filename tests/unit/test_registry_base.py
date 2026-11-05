"""Registry and base classes: lookup, duplicates, trivial fakes instantiate."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from syncmusic.base.feedback import FeedbackSink, SubmitResult
from syncmusic.base.library import Library
from syncmusic.base.processor import Processor
from syncmusic.core import registry
from syncmusic.core.models import ItemRef, LibraryConfig, PlayEvent
from tests.factories import NOW
from tests.fakes import FakeLibrary, FakeTrailer


def test_lookup_registered_entry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(registry.LIBRARIES, "fake", "tests.fakes:FakeLibrary")
    monkeypatch.setitem(registry.PROCESSORS, "trailer", "tests.fakes:FakeTrailer")
    monkeypatch.setitem(registry.DEVICES, "fakedev", "tests.fakes:FakeDevice")
    assert registry.library_class("fake") is FakeLibrary
    assert registry.processor_class("trailer") is FakeTrailer
    assert FakeLibrary in registry.library_classes()
    assert registry.device_class("fakedev").type_name == "fake"


def test_unknown_type_lists_known(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(registry.DEVICES, "ipod", "x:Y")
    with pytest.raises(KeyError, match=r"unknown device type 'nope'.*ipod"):
        registry.device_class("nope")


def test_registered_type_name_matches_key():
    for table, load in (
        (registry.LIBRARIES, registry.library_class),
        (registry.DEVICES, registry.device_class),
        (registry.PROCESSORS, registry.processor_class),
    ):
        for name in table:
            assert load(name).type_name == name


def test_registry_source_has_no_duplicate_names():
    """The registry is a dict literal: a duplicate key would silently shadow an implementation."""
    tree = ast.parse(Path(inspect.getfile(registry)).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            assert len(keys) == len(set(keys)), f"duplicate registry keys: {keys}"


class _Sink(FeedbackSink):
    sink_name = "test"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def submit_plays(self, plays: list[PlayEvent]) -> SubmitResult:
        return SubmitResult(accepted=plays)

    async def set_rating(self, ref: ItemRef, rating: int | None) -> None:
        self.calls.append(("rating", ref, rating))

    async def set_position(self, ref: ItemRef, position_s: float, finished: bool) -> None:
        self.calls.append(("pos", ref, position_s, finished))


async def test_feedback_sink_trivial_impl():
    s = _Sink()
    ref = ItemRef(library_id="l", item_id="1")
    res = await s.submit_plays([PlayEvent(ref=ref, played_at=NOW)])
    assert len(res.accepted) == 1
    await s.set_rating(ref, 4)
    await s.set_position(ref, 10.0, False)
    assert len(s.calls) == 2


def test_incomplete_impl_cannot_instantiate(tmp_path: Path):
    class Half(Library):
        type_name = "half"

    with pytest.raises(TypeError):
        Half(LibraryConfig(id="h", type="half", name="h"), None)  # type: ignore[abstract]
    with pytest.raises(TypeError):
        FeedbackSink()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        Processor(None, None)  # type: ignore[abstract,arg-type]


def test_library_settings_validated(tmp_path: Path):
    lib = FakeLibrary(
        LibraryConfig(id="f", type="fake", name="F", settings={"root": tmp_path}), None
    )
    assert lib.id == "f"
    with pytest.raises(ValueError):
        FakeLibrary(LibraryConfig(id="f", type="fake", name="F", settings={}), None)
