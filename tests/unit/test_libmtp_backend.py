"""The libmtp backend over a fake libmtp: opening by serial, a busy player, a wrong serial, storages,
path resolution with the per-session listing cache, MTP's no-overwrite and not-empty-folder rules."""

from __future__ import annotations

import pytest

from syncmusic.devices.Android._libmtp import LibMtpBackend
from syncmusic.devices.Android.mtp import MtpUnavailable
from tests.fake_libmtp import FakeLibMtp, FakePlayer


def _player(tmp_path, serial="S1", **kw) -> FakePlayer:
    root = tmp_path / serial
    (root / "int").mkdir(parents=True)
    (root / "sd").mkdir()
    return FakePlayer(
        serial, {"Internal shared storage": root / "int", "SD card": root / "sd"}, **kw
    )


def test_opens_the_player_with_the_serial(tmp_path):
    a, b = _player(tmp_path, "A"), _player(tmp_path, "B")
    backend = LibMtpBackend("B", FakeLibMtp(a, b))
    assert b.open and not a.open  # the other player was released again
    backend.close()
    assert not b.open


def test_not_found_busy_and_wrong_serial(tmp_path):
    with pytest.raises(MtpUnavailable, match="no MTP player found"):
        LibMtpBackend("S1", FakeLibMtp())
    with pytest.raises(MtpUnavailable, match="busy"):
        LibMtpBackend("S1", FakeLibMtp(_player(tmp_path, "S1", busy=True)))
    with pytest.raises(MtpUnavailable, match=r"serial 'X' \(found: S1\)"):
        LibMtpBackend("X", FakeLibMtp(_player(tmp_path / "2")))


def test_storages_and_paths(tmp_path, tmp_path_factory):
    p = _player(tmp_path)
    b = LibMtpBackend("S1", FakeLibMtp(p))
    assert [s.name for s in b.storages()] == ["Internal shared storage", "SD card"]
    b.mkdir(None, "Music")
    b.mkdir(None, "Music")  # existing is fine
    b.mkdir("SD card", "Music")
    b.mkdir("SD card", "Music/Sub")
    b.send_bytes("SD card", b"hello", "Music/Sub/a.txt")
    src = tmp_path_factory.mktemp("src") / "t.mp3"
    src.write_bytes(b"x" * 1000)
    b.send(None, src, "Music/t.mp3")
    assert (p.storages["SD card"] / "Music/Sub/a.txt").read_bytes() == b"hello"
    assert (p.storages["Internal shared storage"] / "Music/t.mp3").stat().st_size == 1000
    assert b.read("SD card", "Music/Sub/a.txt") == b"hello"
    assert b.read(None, "Music/Sub/a.txt") is None
    assert b.read(None, "Nope/x") is None
    assert [(e.name, e.is_dir, e.size) for e in b.list(None, "Music")] == [("t.mp3", False, 1000)]
    with pytest.raises(FileNotFoundError):
        b.list(None, "Nope")
    with pytest.raises(FileExistsError):
        b.send_bytes(None, b"y", "Music/t.mp3")  # MTP cannot overwrite: the device deletes first
    with pytest.raises(OSError, match="not empty"):
        b.delete("SD card", "Music/Sub")
    b.delete("SD card", "Music/Sub/a.txt")
    b.delete("SD card", "Music/Sub")
    b.delete("SD card", "Music/Sub")  # missing is fine
    assert not (p.storages["SD card"] / "Music/Sub").exists()
    with pytest.raises(MtpUnavailable, match="no storage 'X'"):
        b.list("X", "")


def test_listings_are_cached(tmp_path):
    p = _player(tmp_path)
    (p.storages["Internal shared storage"] / "Music").mkdir()
    b = LibMtpBackend("S1", FakeLibMtp(p))
    b.list(None, "Music")
    b.list(None, "Music")
    b.read(None, "Music/x")
    assert len(p.calls) == 2  # root + Music, each listed once per session


def test_clib_children_reads_next_before_freeing_each_file() -> None:
    """c.next aliases the struct; destroy_file_t must not leave the walk reading freed memory."""
    import ctypes

    from syncmusic.devices.Android import _libmtp

    nodes = [_libmtp._File() for _ in range(3)]
    for i, n in enumerate(nodes):
        n.item_id, n.filetype, n.filename = 10 + i, 0 if i == 0 else 1, f"f{i}".encode()
        if i:
            nodes[i - 1].next = ctypes.pointer(n)
    freed: list[int] = []

    class Lib:
        def LIBMTP_Get_Files_And_Folders(self, dev: object, sid: int, parent: int) -> object:
            return ctypes.pointer(nodes[0])

        def LIBMTP_destroy_file_t(self, f: object) -> None:
            freed.append(ctypes.addressof(f.contents))  # type: ignore[attr-defined]
            ctypes.memset(ctypes.addressof(f.contents), 0, ctypes.sizeof(_libmtp._File))  # type: ignore[attr-defined]

        def LIBMTP_Clear_Errorstack(self, dev: object) -> None:
            pass

    clib = object.__new__(_libmtp.CLib)
    clib._lib = Lib()  # type: ignore[assignment]
    objs = clib.children(None, 1, _libmtp.ROOT)
    assert [(o.id, o.is_dir) for o in objs] == [(10, True), (11, False), (12, False)]
    assert freed == [ctypes.addressof(n) for n in nodes]  # each freed exactly once, in order
