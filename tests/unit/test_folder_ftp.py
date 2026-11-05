"""Folder library on an FTP server: the scan through the sparse mirror equals the scan of the same
folder on disk; only head and tail of big files are downloaded; the whole file only for processing; a changed file
is fetched again, a removed one leaves the mirror; wrong password / missing folder → a clear error."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from syncmusic.core.models import LibraryConfig
from syncmusic.libraries import remote
from syncmusic.libraries.folder import FolderLibrary
from tests.contracts.harness import _ftp_server
from tests.media import make_music


def _libs(tmp_path, **ftp):
    root, port = _ftp_server()
    base = root / f"lib-{tmp_path.name}"
    music = base / "Музика" / "Океан Ельзи"
    make_music(
        music / "01 Обійми.flac",
        seconds=40,
        title="Обійми",
        artist="Океан Ельзи",
        album="Земля",
        tracknumber="1",
    )
    make_music(
        music / "02 Без бою.m4a",
        seconds=90,
        title="Без бою",
        artist="Океан Ельзи",
        album="Земля",
        tracknumber="2",
    )
    make_music(
        music / "03 Майже весна.mp3", title="Майже весна", artist="Океан Ельзи", album="Земля"
    )
    (music / "03 Майже весна.lrc").write_text("[00:01.00]Майже весна\n", encoding="utf-8")
    shutil.copy(
        music / "03 Майже весна.lrc", music / "cover.jpg"
    )  # any bytes: only its presence counts
    (base / "Земля.m3u8").write_text("Музика/Океан Ельзи/01 Обійми.flac\n", encoding="utf-8")
    ctx = SimpleNamespace(cache_dir=tmp_path / "cache")
    local = FolderLibrary(
        LibraryConfig(id="loc", type="folder", name="L", settings={"root": str(base)}), ctx
    )
    settings = {
        "source": "ftp",
        "host": "127.0.0.1",
        "port": port,
        "remote_path": f"/lib-{tmp_path.name}",
        **ftp,
    }
    far = FolderLibrary(LibraryConfig(id="ftp", type="folder", name="F", settings=settings), ctx)
    return base, local, far


def _view(snap):
    return sorted(
        (
            t.path,
            t.title,
            t.artist,
            t.album,
            t.track_no,
            t.format,
            t.size_bytes,
            round(t.duration_s or 0),
            t.has_lyrics,
            t.has_cover,
        )
        for t in snap.tracks.values()
    )


async def test_scan_equals_local_and_downloads_only_heads(tmp_path):
    base, local, far = _libs(tmp_path)
    assert (await far.check()).ok
    snap = await far.snapshot()
    assert _view(snap) == _view(await local.snapshot())
    assert [len(p.track_refs) for p in snap.playlists.values()] == [1]
    big = far.root / "Музика" / "Океан Ельзи" / "01 Обійми.flac"
    assert big.stat().st_size == (base / "Музика" / "Океан Ельзи" / "01 Обійми.flac").stat().st_size
    assert big.stat().st_size > remote.WHOLE_BELOW
    assert big.stat().st_blocks * 512 < remote.HEAD + remote.TAIL + 65536  # a hole in the middle
    # processing gets the whole file, byte for byte
    item = next(t for t in snap.tracks.values() if t.title == "Обійми")
    full = await far.open_media(item.ref.item_id)
    assert full.read_bytes() == (base / item.path).read_bytes()
    lyr = await far.get_lyrics(
        next(t for t in snap.tracks.values() if t.title == "Майже весна").ref.item_id
    )
    assert lyr is not None and "Майже весна" in lyr.lines[0][1]


async def test_changes_and_removals_reach_the_mirror(tmp_path):
    base, _, far = _libs(tmp_path, username="sd", password="secret")
    first = await far.snapshot()
    token = await far.state_token()
    song = base / "Музика" / "Океан Ельзи" / "03 Майже весна.mp3"
    make_music(song, title="Майже весна (live)", artist="Океан Ельзи", album="Земля")
    (base / "Музика" / "Океан Ельзи" / "02 Без бою.m4a").unlink()
    assert await far.state_token() != token
    second = await far.snapshot()
    titles = {t.title for t in second.tracks.values()}
    assert titles == {"Обійми", "Майже весна (live)"} and len(first.tracks) == 3
    assert not (far.root / "Музика" / "Океан Ельзи" / "02 Без бою.m4a").exists()


async def test_errors_are_clear(tmp_path):
    _, _, wrong = _libs(tmp_path, username="sd", password="nope")
    health = await wrong.check()
    assert not health.ok and "ftp://127.0.0.1" in health.message and "nope" not in health.message
    with pytest.raises(remote.RemoteError):
        await wrong.snapshot()
    _, _, missing = _libs(tmp_path, remote_path="/no-such-folder")
    assert not (await missing.check()).ok
    with pytest.raises(ValidationError, match="host"):
        FolderLibrary.Settings.model_validate({"source": "ftp", "host": "ftp://x"})
    with pytest.raises(ValidationError, match="root"):
        FolderLibrary.Settings.model_validate({"source": "local"})


def test_list_dates():
    assert remote._mtime_ns({"modify": "20261024121314"}) == 1792843994 * 10**9
    assert remote._mtime_ns({}, "Oct 22 2025") > 0 and remote._mtime_ns({}, "garbage") == 0
