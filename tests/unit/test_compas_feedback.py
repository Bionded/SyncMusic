"""Compás card listening data: tagcache format 8 reader, plugin positions, reverse sync deltas."""

import asyncio
import struct
from pathlib import Path

import pytest

from syncmusic.devices.HibyR1 import compas_feedback as cf

ROOT = "/data/mnt/sd_0/"


def write_tagcache(
    mount: Path,
    rows: list[tuple[str, int, int, int]],
    *,
    gen: int = 1,
    names_gen: int | None = None,
    deleted: tuple[int, ...] = (),
    magic: int = 0x54434811,
) -> None:
    """A synthetic tagcache: rows = (card-relative path, playcount, rating, last played)."""
    db = mount / ".compas"
    db.mkdir(parents=True, exist_ok=True)
    names = bytearray(struct.pack("<3i", 0x54434810, 0, len(rows)))
    entries = bytearray()
    for slot, (path, plays, rating, last) in enumerate(rows):
        tags = [0] * cf.TAG_COUNT
        tags[cf.TAG_FILENAME] = len(names)
        raw = (ROOT + path).encode() + b"\0"
        names += struct.pack("<ii", len(raw), slot) + raw
        tags[cf.TAG_PLAYCOUNT], tags[cf.TAG_RATING], tags[cf.TAG_LASTPLAYED] = plays, rating, last
        entries += struct.pack(f"<{cf.TAG_COUNT + 1}i", *tags, 1 if slot in deleted else 0)
    master = struct.pack("<6i", magic, len(entries), len(rows), 1, 1, 0) + entries
    suffix = f".g{gen}" if gen else ""
    (db / "tagcache.gen").write_text(str(gen))
    (db / f"database_idx.tcd{suffix}").write_bytes(master)
    ng = gen if names_gen is None else names_gen
    (db / f"database_4.tcd{f'.g{ng}' if ng else ''}").write_bytes(bytes(names))
    if names_gen is not None:
        sources = [gen] * cf.REFS_COUNT
        sources[cf.REFS_FILENAME_INDEX] = names_gen
        body = struct.pack(f"<IIi{cf.REFS_COUNT}i", cf.REFS_MAGIC, 1, gen, *sources)
        (db / f"tagcache.refs.g{gen}").write_bytes(body + struct.pack("<I", cf._fnv1a(body)))


def test_reads_counts_favourites_and_skips_deleted(tmp_path):
    write_tagcache(
        tmp_path,
        [
            ("Music/A/01.flac", 3, 1, 1_700_000_000),
            ("Music/B/02.mp3", 0, 0, 0),
            ("Music/C.mp3", 9, 1, 5),
        ],
        deleted=(2,),
    )
    data = cf.read_card(tmp_path)
    assert data.indexed == {"Music/A/01.flac", "Music/B/02.mp3"}
    assert data.play_counts == {"Music/A/01.flac": (3, 1_700_000_000)}
    assert data.favourites == {"Music/A/01.flac"}


def test_file_names_from_an_older_generation_via_refs(tmp_path):
    write_tagcache(tmp_path, [("Music/x.mp3", 1, 0, 0)], gen=3, names_gen=2)
    assert cf.read_card(tmp_path).indexed == {"Music/x.mp3"}


def test_plain_rockbox_magic_and_generation_zero(tmp_path):
    write_tagcache(tmp_path, [("Music/x.mp3", 2, 0, 0)], gen=0, magic=0x54434810)
    assert cf.read_card(tmp_path).play_counts == {"Music/x.mp3": (2, None)}


def test_no_database_yet_and_damaged_database(tmp_path):
    assert cf.read_tagcache(tmp_path) is None
    write_tagcache(tmp_path, [("Music/x.mp3", 2, 1, 0)], magic=0x1234)
    with pytest.raises(cf.CompasDbError):
        cf.read_tagcache(tmp_path)
    assert cf.read_card(tmp_path).favourites == set()  # damaged: nothing, no error


def test_plugin_positions(tmp_path):
    (tmp_path / ".plugins").mkdir()
    (tmp_path / ".plugins" / ".audiobooks_state_v3").write_text(
        "P\tAuthor%2FBook\tCD1%2F02.mp3\t125\t3600\t0\t1700000000\t0\t0\n"
        "P\tsingle.m4b\tsingle.m4b\t10\t100\t1\t0\t0\t0\n"
        "B\tAuthor%2FBook\tCD1%2F02.mp3\t5\tnote\n"
    )
    (tmp_path / "Podcasts").mkdir()
    (tmp_path / "Podcasts" / ".progress.tsv").write_text(
        "P\tk\t42\t900\t0\t0\t%2Fdata%2Fmnt%2Fsd_0%2FPodcasts%2FShow%2Fep.mp3\t1700000001\tEp\t\t0\n"
    )
    assert cf.read_card(tmp_path).positions == {
        "Audiobooks/Author/Book/CD1/02.mp3": (125.0, False, 1_700_000_000),
        "Audiobooks/single.m4b": (10.0, True, None),
        "Podcasts/Show/ep.mp3": (42.0, False, 1_700_000_001),
    }


def _manifest(paths: list[str]):
    from syncmusic.core.models import ItemRef, Manifest, ManifestEntry, ManifestHeader

    items = {}
    for i, p in enumerate(paths):
        ref = ItemRef(library_id="lib", item_id=str(i))
        items[ref.key()] = ManifestEntry(ref=ref, device_ref=p, path=p, size=1, duration_s=200)
    return Manifest(header=ManifestHeader(device_id="c"), items=items)


def test_reverse_sync_hearts_once_and_never_on_unindexed_files(tmp_path):
    from syncmusic.core.models import DeviceConfig
    from syncmusic.devices.HibyR1.compas import HibyCompas
    from tests.contracts.harness import compas_tree

    compas_tree(tmp_path)
    dev = HibyCompas(DeviceConfig(id="c", type="hiby_compas", name="C"), tmp_path, None)
    m = _manifest(["Music/a.mp3", "Music/b.mp3"])
    write_tagcache(tmp_path, [("Music/a.mp3", 2, 1, 1_700_000_000), ("Music/b.mp3", 0, 1, 0)])
    fb = asyncio.run(dev.read_feedback(m))
    assert len(fb.plays) == 2
    assert sorted(p.played_at_estimated for p in fb.plays) == [False, True]
    assert sorted(fb.stars.values()) == [True, True] and len(fb.stars) == 2
    asyncio.run(dev.reset_feedback(None, fb))  # type: ignore[arg-type]
    # the player rebuilt its database and has not indexed b yet: no unstar for b, a still a favourite
    write_tagcache(tmp_path, [("Music/a.mp3", 2, 1, 1_700_000_000)], gen=2)
    assert asyncio.run(dev.read_feedback(m)).stars == {}
    # a heart removed on the player on an indexed file
    write_tagcache(tmp_path, [("Music/a.mp3", 2, 0, 1_700_000_000)], gen=3)
    fb = asyncio.run(dev.read_feedback(m))
    assert list(fb.stars.values()) == [False]


# --- positions to the player ---


def test_audiobook_positions_merge_keeps_every_other_line():
    old = (
        "P\tBook%20One\tch01.mp3\t10\t600\t0\t100\t0\t1\n"
        "P\tOther\tOther\t5\t50\t1\t90\t1\t0\n"
        "B\tBook%20One\tch01.mp3\t42\tmark\n"
        "H\tOther\tOther\t3\n"
    )
    new, dropped = cf.merge_audiobook_positions(
        old,
        {
            "Audiobooks/Book One/ch02.mp3": cf.Position(77.9),
            "Audiobooks/Новая книга.m4b": cf.Position(12.0),
        },
        now=500,
    )
    lines = new.splitlines()
    assert not dropped
    assert lines[0] == "P\tBook%20One\tch02.mp3\t77\t0\t0\t500\t0\t1"  # direct_only kept
    assert lines[1] == "P\tOther\tOther\t5\t50\t1\t90\t1\t0"
    key = cf.encode("Новая книга.m4b")
    assert lines[2] == f"P\t{key}\t{key}\t12\t0\t0\t500\t0\t0"
    assert lines[3:] == old.splitlines()[2:]


def test_audiobook_positions_round_trip(tmp_path):
    cf.write_positions(tmp_path, {"Audiobooks/Книга/01 Розділ.mp3": cf.Position(321.0)}, now=1000)
    got = cf.audiobook_positions(tmp_path)
    assert got == {"Audiobooks/Книга/01 Розділ.mp3": (321.0, False, 1000)}


def test_audiobook_positions_stay_inside_the_plugin_limits(monkeypatch):
    monkeypatch.setattr(cf, "STATE_MAX_LINES", 2)
    old = "P\tA\tA\t1\t1\t0\t1\t0\t0\nP\tB\tB\t1\t1\t0\t1\t0\t0\n"
    new, dropped = cf.merge_audiobook_positions(
        old, {"Audiobooks/A": cf.Position(9.0), "Audiobooks/C.mp3": cf.Position(3.0)}, now=5
    )
    assert dropped == ["Audiobooks/C.mp3"]
    assert new.splitlines()[0].split("\t")[3] == "9" and len(new.splitlines()) == 2


def test_podcast_positions_only_for_listed_episodes(tmp_path):
    (tmp_path / "Podcasts").mkdir()
    listed = f"P\tk1\t0\t900\t0\t1\t{ROOT}Podcasts/Show/ep1.mp3\t0\tTitle\t2026-01-01\t1\textra\n"
    (tmp_path / cf.PODCAST_PROGRESS).write_text(listed + "X\tunknown line\n", "utf-8")
    cf.write_positions(
        tmp_path,
        {"Podcasts/Show/ep1.mp3": cf.Position(120.0), "Podcasts/Show/ep2.mp3": cf.Position(5.0)},
        now=7,
    )
    lines = (tmp_path / cf.PODCAST_PROGRESS).read_text("utf-8").splitlines()
    assert lines[0].split("\t") == [
        "P", "k1", "120", "900", "0", "1", f"{ROOT}Podcasts/Show/ep1.mp3", "7",
        "Title", "2026-01-01", "1", "extra",
    ]  # fmt: skip
    assert lines[1] == "X\tunknown line"
    assert cf.podcast_positions(tmp_path)["Podcasts/Show/ep1.mp3"] == (120.0, False, 7)


def test_finished_flags_and_durations_reach_the_plugins(tmp_path):
    (tmp_path / "Podcasts").mkdir()
    ep = f"P\tk1\t0\t0\t0\t1\t{ROOT}Podcasts/Show/ep1.mp3\t0\n"
    (tmp_path / cf.PODCAST_PROGRESS).write_text(ep, "utf-8")
    cf.write_positions(
        tmp_path,
        {
            "Audiobooks/Воно/05.mp3": cf.Position(20.0, True, 1834.6),
            "Podcasts/Show/ep1.mp3": cf.Position(1500.0, True, 1800.0),
        },
        now=9,
    )
    book = (tmp_path / cf.AUDIOBOOK_STATE).read_text("utf-8").split("\t")
    assert book[3:6] == ["20", "1834", "1"]
    assert cf.audiobook_positions(tmp_path)["Audiobooks/Воно/05.mp3"][1] is True
    episode = (tmp_path / cf.PODCAST_PROGRESS).read_text("utf-8").split("\t")
    assert episode[2:5] == ["1500", "1800", "1"]


def test_no_podcast_file_is_created(tmp_path):
    cf.write_positions(tmp_path, {"Podcasts/Show/ep1.mp3": cf.Position(1.0)}, now=1)
    assert not (tmp_path / cf.PODCAST_PROGRESS).exists()


def _ep(path, show="Шоу", title="Епізод 1"):
    import datetime as dt

    return cf.Episode(path, show, title, dt.datetime(2026, 3, 1, tzinfo=dt.UTC), 1800.0)


def test_our_podcast_episodes_are_listed_and_removed_with_the_files():
    own = f"P\tabc|guid-1\t10\t50\t0\t0\t{ROOT}Podcasts/Plugin%20Show/own.mp3\t5\tOwn\t2026-01-01\t1\n"
    text, dropped = cf.merge_podcast_episodes(
        own, [_ep("Podcasts/Шоу/ep1.mp3"), _ep("Podcasts/Plugin Show/own.mp3")]
    )
    lines = text.splitlines()
    assert not dropped and lines[0] == own.rstrip("\n") and len(lines) == 2
    p = lines[1].split("\t")
    assert p[0] == "P" and cf.unquote(p[1]).endswith("|syncmusic:Podcasts/Шоу/ep1.mp3")
    assert cf.unquote(p[6]) == f"{ROOT}Podcasts/Шоу/ep1.mp3" and p[5] == "1"  # new
    assert cf.unquote(p[8]) == "Епізод 1" and p[9] == "2026-03-01" and p[3] == "1800"
    # a position on our record is kept by the next refresh; the episode leaving removes only ours
    moved = text.replace(f"{p[1]}\t0\t", f"{p[1]}\t99\t")
    again, _ = cf.merge_podcast_episodes(moved, [_ep("Podcasts/Шоу/ep1.mp3")])
    assert again == moved
    gone, _ = cf.merge_podcast_episodes(moved, [])
    assert gone == own


def test_podcast_list_limit(monkeypatch):
    monkeypatch.setattr(cf, "MAX_PODCAST_RECORDS", 1)
    text, dropped = cf.merge_podcast_episodes(
        "", [_ep("Podcasts/S/a.mp3"), _ep("Podcasts/S/b.mp3")]
    )
    assert dropped == 1 and len(text.splitlines()) == 1


def test_no_list_file_without_episodes(tmp_path):
    assert cf.write_podcast_episodes(tmp_path, []) == 0
    assert not (tmp_path / cf.PODCAST_PROGRESS).exists()


def test_untouched_items_are_no_listening_data(tmp_path):
    (tmp_path / ".plugins").mkdir()
    (tmp_path / ".plugins" / ".audiobooks_state_v3").write_text(
        "P\tBook\t01.mp3\t0\t3600\t0\t0\t0\t0\nP\tRewound\t01.mp3\t0\t3600\t0\t1700000005\t0\t0\n"
    )
    (tmp_path / "Podcasts").mkdir()
    (tmp_path / "Podcasts" / ".progress.tsv").write_text(
        "P\tk\t0\t900\t0\t1\t%2Fdata%2Fmnt%2Fsd_0%2FPodcasts%2FShow%2Fnew.mp3\t0\tEp\t\t0\n"
    )
    # never played: nothing; played and rewound to 0 (has a last played): still sent back
    assert cf.read_card(tmp_path).positions == {
        "Audiobooks/Rewound/01.mp3": (0.0, False, 1_700_000_005)
    }
