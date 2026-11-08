# SPDX-License-Identifier: GPL-3.0-or-later
"""Templates stored on the device: export with installation-independent library refs, merge, import as copies."""

from __future__ import annotations

import datetime as dt

from syncmusic.core import device_templates as dt_io
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import ManifestLibrary, Rule, RuleTemplate

A_LIBS = {
    "music": ManifestLibrary(source_id="subsonic:abc", type="subsonic", name="Music server"),
    "books": ManifestLibrary(source_id="abs:lib-1", type="audiobookshelf", name="Books"),
}
WHEN = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)


def tpl(name: str = "Road trip") -> RuleTemplate:
    return RuleTemplate(
        id="road-trip",
        name=name,
        library_ids=["music"],
        rules=[
            Rule(id="r1", name="Rock", libraries=["music"], criteria={"all": []}),
            Rule(id="r2", name="Books", libraries=["books"], criteria={"all": []}),
            Rule(id="r3", name="Anything", criteria={"all": []}),
        ],
        updated_at=WHEN,
    )


def test_export_replaces_local_library_ids_by_keys():
    row = dt_io.export(tpl(), A_LIBS, "inst-a", "Alpha")
    assert row.template_uid == "inst-a:road-trip"
    assert row.library_keys == ["T1"]
    assert [r.libraries for r in row.rules] == [["T1"], ["T2"], None]
    assert row.libraries["T1"].source_id == "subsonic:abc" and row.libraries["T1"].id == "music"
    assert "music" not in dt_io.dumps([row]).decode().split('"libraries"')[2]  # rules use keys


def test_merge_keeps_other_installations_rows():
    a = dt_io.export(tpl(), A_LIBS, "inst-a", "Alpha")
    b = dt_io.export(tpl(name="Other"), A_LIBS, "inst-b", "Beta")
    a2 = dt_io.export(tpl(name="Renamed"), A_LIBS, "inst-a", "Alpha")
    merged = dt_io.merge([a, b], [a2], "inst-a")
    assert [(r.instance_id, r.name) for r in merged] == [("inst-b", "Other"), ("inst-a", "Renamed")]
    assert dt_io.loads(dt_io.dumps(merged)) == merged


def test_import_maps_libraries_by_source_id_and_drops_unknown_ones():
    row = dt_io.loads(dt_io.dumps([dt_io.export(tpl(), A_LIBS, "inst-a", "Alpha")]))[0]
    ours = manifest_io.Context(
        instance_id="inst-b",
        libraries={"navi": ManifestLibrary(source_id="subsonic:abc", type="subsonic")},
    )
    assert dt_io.importable(row, ours)
    assert dt_io.unmatched(row, ours) == ["Books"]
    copy = dt_io.to_local(row, ours, "road-trip-2")
    assert copy.id == "road-trip-2" and copy.library_ids == ["navi"]
    assert [(r.name, r.libraries) for r in copy.rules] == [("Rock", ["navi"]), ("Anything", None)]
    assert copy.origin_uid == "inst-a:road-trip" and copy.origin_name == "Alpha"
    assert copy.origin_updated_at == WHEN


def test_a_template_of_only_unknown_libraries_is_not_importable():
    row = dt_io.export(tpl(), A_LIBS, "inst-a", "Alpha")
    other = manifest_io.Context(
        instance_id="inst-b", libraries={"x": ManifestLibrary(source_id="folder:zzz")}
    )
    assert not dt_io.importable(row, other)
    everything = dt_io.export(tpl(), A_LIBS, "inst-a", "A")
    everything.library_keys = []
    assert dt_io.importable(everything, other)  # draws from all libraries: fits anywhere


def test_unedited_default_carries_its_key():
    d = RuleTemplate(
        id="default.music.all", name="All music", library_ids=["music"], default_of="music"
    )
    assert dt_io.export(d, A_LIBS, "i", "n").default_key == "all"
    assert dt_io.export(d.model_copy(update={"edited": True}), A_LIBS, "i", "n").default_key is None


def test_damaged_file_reads_as_empty():
    assert dt_io.loads(b"{not json") == [] and dt_io.loads(None) == []
