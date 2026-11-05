"""HiBy R1 `.local` databases: SonixOS never creates `.local/` itself, so we create the SonixOS DBs after
Format / Wipe and at every sync when they are missing; Wipe clears the player's index but keeps its settings."""

from __future__ import annotations

import shutil
import sqlite3

from syncmusic.core.models import DeviceConfig
from syncmusic.devices.HibyR1.sonixos import SCHEMAS, Hiby, clear_index, ensure_databases
from tests.contracts.harness import hiby_sql, hiby_tree
from tests.unit.test_hiby_playlists import _rows, _sync


def _tables(db) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _dev(tmp_path, with_local: bool = True) -> Hiby:
    mount = hiby_tree(tmp_path / "hiby")
    if not with_local:
        shutil.rmtree(mount / ".local")
    (mount / ".syncmusic").mkdir()
    return Hiby(DeviceConfig(id="h1", type="hiby", name="H"), mount, None)


def test_ensure_creates_local_and_every_db(tmp_path):
    done = ensure_databases(tmp_path, tmp_path / "bak")
    assert len(done) == 3
    lib = _tables(tmp_path / ".local" / "library.db")
    assert {"MEDIA_TABLE", "FAVOURITES", "PLAY_COUNTS", "PLAYBACK_STATE", "FOLDER_TABLE"} <= lib
    assert {"AUDIOBOOK_TABLE", "AUDIOBOOK_PARTS", "AUDIOBOOK_FINISHED"} <= _tables(
        tmp_path / ".local" / "audiobooks.db"
    )
    assert {"PODCAST_EPISODES", "PODCAST_SUBS"} <= _tables(tmp_path / ".local" / "podcast.db")
    assert ensure_databases(tmp_path, tmp_path / "bak") == []  # healthy ones are left alone


def test_ensure_rescues_a_malformed_db_with_its_user_rows(tmp_path):
    ensure_databases(tmp_path, tmp_path / "bak")
    hiby_sql(
        tmp_path,
        "library.db",
        "INSERT INTO FAVOURITES VALUES ('/mnt/sd_0/Music/a.flac', 'a', 'b', 1)",
    )
    db = tmp_path / ".local" / "library.db"
    raw = bytearray(db.read_bytes())
    raw[100:4096] = b"\xff" * (4096 - 100)  # the schema page
    db.write_bytes(raw)
    assert ensure_databases(tmp_path, tmp_path / "bak") == ["recreated malformed .local/library.db"]
    assert "MEDIA_TABLE" in _tables(db)
    assert list((tmp_path / "bak").glob("library-malformed-*.db"))


def test_rescue_carries_over_the_user_rows(tmp_path):
    """the rows of USER_TABLES that are still readable go into the fresh database."""
    from syncmusic.devices.HibyR1.sonixos import _rescue

    ensure_databases(tmp_path, tmp_path / "bak")
    hiby_sql(
        tmp_path,
        "library.db",
        "INSERT INTO FAVOURITES VALUES ('/mnt/sd_0/Music/a.flac', 'a', 'b', 1)",
    )
    hiby_sql(
        tmp_path, "library.db", "INSERT INTO PLAY_COUNTS VALUES ('/mnt/sd_0/Music/a.flac', 3, 1)"
    )
    db = tmp_path / ".local" / "library.db"
    _rescue(db, tmp_path / "bak" / "deep")  # the backup folder is created with its parents
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT path FROM FAVOURITES").fetchall() == [
            ("/mnt/sd_0/Music/a.flac",)
        ]
        assert con.execute("SELECT * FROM PLAY_COUNTS").fetchall() == [
            ("/mnt/sd_0/Music/a.flac", 3, 1)
        ]
    finally:
        con.close()
    (old,) = (tmp_path / "bak" / "deep").glob("library-malformed-*.db")
    assert old.stat().st_size > 0


def test_ensure_leaves_a_hot_db_to_the_player(tmp_path):
    ensure_databases(tmp_path, tmp_path / "bak")
    db = tmp_path / ".local" / "library.db"
    db.write_bytes(b"garbage" * 1000)
    db.with_name("library.db-journal").write_bytes(b"x")
    assert ensure_databases(tmp_path, tmp_path / "bak") == []


def test_clear_index_keeps_player_settings(tmp_path):
    ensure_databases(tmp_path, tmp_path / "bak")
    local = tmp_path / ".local"
    hiby_sql(tmp_path, "library.db", "INSERT INTO PLAY_COUNTS VALUES ('/mnt/sd_0/x.flac', 3, 1)")
    (local / "library.db-journal").write_bytes(b"")
    (local / "thumbnails").write_bytes(b"cache")
    (local / "eq").mkdir()
    (local / "eq" / "mine.ini").write_text("x")
    (local / "radio.db").write_bytes(b"radio")
    clear_index(tmp_path)
    assert not (local / "thumbnails").exists() and not (local / "library.db-journal").exists()
    assert (local / "eq" / "mine.ini").read_text() == "x" and (
        local / "radio.db"
    ).read_bytes() == b"radio"
    con = sqlite3.connect(local / "library.db")
    assert con.execute("SELECT COUNT(*) FROM PLAY_COUNTS").fetchone() == (0,)
    con.close()
    assert all((local / n).is_file() for n in SCHEMAS)


async def test_wipe_clears_index_keeps_settings(tmp_path):
    dev = _dev(tmp_path)
    (dev.mount / "Music" / "x.flac").write_bytes(b"x")
    hiby_sql(
        dev.mount,
        "library.db",
        "INSERT INTO FAVOURITES VALUES ('/mnt/sd_0/Music/x.flac', 'x', '', 1)",
    )
    (dev.mount / ".local" / "MSEB").mkdir()
    (dev.mount / ".local" / "MSEB" / "p").write_text("p")
    assert await dev.wipe() == 1
    assert not (dev.mount / "Music").exists() and (dev.mount / ".local" / "MSEB" / "p").is_file()
    con = sqlite3.connect(dev.mount / ".local" / "library.db")
    assert con.execute("SELECT COUNT(*) FROM FAVOURITES").fetchone() == (0,)
    con.close()


async def test_sync_without_local_creates_dbs_and_writes_playlists(tmp_path):
    dev = _dev(tmp_path, with_local=False)
    await _sync(dev, tmp_path, ["Група крові"], {"Mix": ["Група крові"]})
    assert [r[2] for r in _rows(dev, "Mix")] == ["Група крові"]
    assert all((dev.mount / ".local" / n).is_file() for n in SCHEMAS)


async def test_unwritable_db_is_a_report_warning(tmp_path):
    dev = _dev(tmp_path)
    s = await dev.begin()
    from syncmusic.core.models import DevicePlaylist, Manifest, ManifestHeader

    await dev.set_playlists(s, [DevicePlaylist(name="Mix", refs=[])])
    local = dev.mount / ".local"
    local.chmod(0o500)  # a real write error (the only thing that fails the step)
    try:
        await dev.commit(s, Manifest(header=ManifestHeader(device_id="h1"), playlists=["Mix"]))
    finally:
        local.chmod(0o755)
    assert any("not rewritten" in w for w in s.data["commit_warnings"])


def test_write_favourites_counts_rows_and_refuses_a_hot_db(tmp_path):
    """exact (added, removed) counts, the stored name/artist, the player's own added_at kept."""
    import pytest

    from syncmusic.devices.HibyR1.sonixos import HotDatabase, write_favourites

    bak = tmp_path / "bak"
    assert write_favourites(tmp_path, {"Music/a.flac": ("A", "X")}, set(), bak) == (
        0,
        0,
    )  # no database
    ensure_databases(tmp_path, bak)
    assert write_favourites(tmp_path, {}, set(), bak) == (0, 0)
    hiby_sql(
        tmp_path,
        "library.db",
        "INSERT INTO FAVOURITES VALUES ('/mnt/sd_0/Music/old.flac', 'o', 'p', 7)",
    )
    add = {"Music/a.flac": ("A", "X"), "Music/b.flac": ("B", ""), "Music/old.flac": ("new", "q")}
    assert write_favourites(tmp_path, add, set(), bak) == (2, 0)
    assert write_favourites(
        tmp_path, {}, {"Music/a.flac", "Music/old.flac", "Music/none.flac"}, bak
    ) == (0, 2)
    con = sqlite3.connect(tmp_path / ".local" / "library.db")
    try:
        assert con.execute("SELECT path, name, artist FROM FAVOURITES").fetchall() == [
            ("/mnt/sd_0/Music/b.flac", "B", "")
        ]
    finally:
        con.close()
    assert list(bak.glob("library-*.db"))  # backed up before writing
    (tmp_path / ".local" / "library.db-journal").write_bytes(b"x")
    with pytest.raises(HotDatabase, match="unfinished transaction"):
        write_favourites(tmp_path, {"Music/c.flac": ("C", "")}, set(), bak)
