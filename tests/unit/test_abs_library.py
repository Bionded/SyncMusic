"""Audiobookshelf library against the in-memory fake (tests/fake_abs.py)."""

from __future__ import annotations

import pytest

from syncmusic.core.models import LibraryConfig, MediaKind
from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
from tests.fake_abs import FakeABS
from tests.media import TINY_JPEG


def make(server: FakeABS, **settings) -> AudiobookshelfLibrary:
    lib = AudiobookshelfLibrary(
        LibraryConfig(
            id="abs",
            type="audiobookshelf",
            name="ABS",
            settings={"url": "http://abs.test/", "api_token": "tok", **settings},
        ),
        None,
    )
    lib.transport = server
    return lib


@pytest.fixture
def server():
    srv = FakeABS()
    # ABS track order is authoritative (here: "Епілог" first, as ABS does for the user's "Воно")
    srv.add_book(
        "b1", "Воно", [("Епілог.mp3", 100.0), ("розділ 01.mp3", 200.0), ("розділ 02.mp3", 300.0)]
    )
    srv.add_podcast("p1", "Подкаст", [("e1", "Епізод 1"), ("e2", "Епізод 2")])
    return srv


async def test_snapshot_mapping(server):
    snap = await make(server, page_size=1).snapshot()
    assert set(snap.tracks) == {"b1:b1-1", "b1:b1-2", "b1:b1-3", "p1:e1", "p1:e2"}
    book = sorted(
        (t for t in snap.tracks.values() if t.kind is MediaKind.AUDIOBOOK),
        key=lambda t: t.chapter_index,
    )
    assert [t.title for t in book] == ["Епілог", "розділ 01", "розділ 02"]
    assert {t.series for t in book} == {"Воно"} and book[0].author == "Автор" and book[0].has_cover
    assert book[1].path == "Воно/розділ 01.mp3" and book[1].extra["abs_start_offset"] == 100.0
    assert all(t.position_s is None and not t.finished for t in book)
    ep = snap.tracks["p1:e2"]
    assert (ep.kind, ep.series, ep.format, ep.track_no, ep.disc_no) == (
        MediaKind.PODCAST,
        "Подкаст",
        "aac",
        2,
        1,
    )
    assert ep.published.year == 2025 and not ep.has_cover


async def test_ebook_libraries_ignored_and_library_filter(server):
    lib = make(server, library_ids=["lp"])
    assert {t.kind for t in (await lib.snapshot()).tracks.values()} == {MediaKind.PODCAST}
    missing = make(server, library_ids=["nope"])
    status = await missing.check()
    assert not status.ok and "nope" in status.message


async def test_book_progress_maps_onto_tracks(server):
    server.set_progress("b1", 250.0)  # 100 s into "розділ 01" (offset 100, duration 200)
    server.set_progress("p1", 42.0, episode="e1")
    server.set_progress("p1", 100.0, finished=True, episode="e2")
    tracks = (await make(server).snapshot()).tracks
    assert [
        (tracks[f"b1:b1-{i}"].position_s, tracks[f"b1:b1-{i}"].finished) for i in (1, 2, 3)
    ] == [(100.0, True), (150.0, False), (0.0, False)]
    assert (tracks["p1:e1"].position_s, tracks["p1:e1"].finished) == (42.0, False)
    assert tracks["p1:e2"].finished


async def test_token_changes_on_progress_and_items(server):
    lib = make(server)
    t0 = await lib.state_token()
    assert await lib.state_token() == t0
    server.set_progress("b1", 5.0)
    t1 = await lib.state_token()
    server.add_book("b2", "Нова", [("1.mp3", 1.0)])
    assert len({t0, t1, await lib.state_token()}) == 3


async def test_media_and_cover(server):
    lib = make(server)
    data = b"".join([c async for c in await lib.open_media("p1:e1")])
    assert data == server.files[("p1", "p1-e1")]
    data = b"".join([c async for c in await lib.open_media("b1:b1-2")])
    assert data == server.files[("b1", "b1-2")]
    assert await lib.open_cover("b1:b1-1") == TINY_JPEG and await lib.open_cover("p1:e1") is None
    for bad in ("b1", "zz:1", "b1:nope"):
        with pytest.raises(KeyError):
            await lib.open_media(bad)


async def test_token_not_in_repr_or_messages(server):
    lib = make(server, api_token="super-secret-token")
    status = await lib.check()
    assert not status.ok and "super-secret" not in status.message
    assert "super-secret" not in repr(lib) and "super-secret" not in repr(lib.settings)


@pytest.mark.timeout(10)
async def test_server_ignoring_page_does_not_hang(server):
    server.ignore_page = True
    snap = await make(server, page_size=1).snapshot()
    assert snap.tracks


async def test_cover_change_changes_source_state(server):
    """a replaced cover file (libraryFiles size/mtime) or a new cover path changes source_state."""
    book = next(i for i in server.items.values() if i["mediaType"] == "book")
    pod = next(i for i in server.items.values() if i["mediaType"] == "podcast")
    book["libraryFiles"] = [
        {"fileType": "image", "metadata": {"path": "/meta/cover.jpg", "size": 10, "mtimeMs": 1}}
    ]

    async def states():
        lib = make(server)
        out = {k: t.source_state for k, t in (await lib.snapshot()).tracks.items()}
        await lib.close()
        return out

    s0 = await states()
    assert await states() == s0
    book["libraryFiles"][0]["metadata"].update(size=11, mtimeMs=2)  # cover file replaced
    s1 = await states()
    changed = {k for k in s0 if s0[k] != s1[k]}
    assert changed and all(k.startswith(book["id"]) for k in changed)
    pod["media"]["coverPath"] = "/srv/x/cover.jpg"  # podcast gets a cover
    s2 = await states()
    changed = {k for k in s1 if s1[k] != s2[k]}
    assert changed and all(k.startswith(pod["id"]) for k in changed)


async def test_series_and_collections():
    """ABS's own series go into `bookseries` (rule field; `series` stays the book title), collections are
    playlists of their books' files in collection order; a rule picks a series and an inPlaylist a collection."""
    from datetime import UTC, datetime

    from syncmusic.core.models import Rule
    from syncmusic.core.rules.evaluator import EvalContext, evaluate_rule

    srv = FakeABS()
    srv.add_book(
        "b1", "Колір магії", [("1.mp3", 10.0), ("2.mp3", 10.0)], series=(("Discworld", "1"),)
    )
    srv.add_book("b2", "Мор", [("1.mp3", 10.0)], series=(("Discworld", "4"), ("Death", "1")))
    srv.add_book("b3", "Воно", [("1.mp3", 10.0)])
    lib = make(srv)
    token = await lib.state_token()
    srv.add_collection("c1", "Літо", ["b3", "b1"])
    assert await lib.state_token() != token  # a new collection is a library change
    snap = await lib.snapshot()
    b2 = snap.tracks["b2:b2-1"]
    assert b2.series == "Мор" and b2.extra["abs_series"] == ["Discworld", "Death"]
    assert b2.extra["abs_series_sequence"] == "4"
    assert "abs_series" not in snap.tracks["b3:b3-1"].extra
    [pl] = snap.playlists.values()
    assert pl.name == "Літо" and [r.item_id for r in pl.track_refs] == [
        "b3:b3-1",
        "b1:b1-1",
        "b1:b1-2",
    ]

    ctx = EvalContext({"abs": snap}, datetime.now(UTC))

    def ids(criteria):
        return {t.ref.item_id for t in evaluate_rule(Rule(id="r", criteria=criteria), ctx).tracks}

    assert ids({"is": {"bookseries": "Discworld"}}) == {"b1:b1-1", "b1:b1-2", "b2:b2-1"}
    assert ids({"is": {"bookseries": "Death"}}) == {"b2:b2-1"}
    assert ids({"inPlaylist": {"name": "Літо"}}) == {"b3:b3-1", "b1:b1-1", "b1:b1-2"}
