"""FTP device specifics against the in-process test server of the contract harness: anonymous
login, an interrupted session cleaned up by the next one, a dropped connection opened again, the password masked."""

from __future__ import annotations

import ftplib
import io

import pytest
from pydantic import ValidationError

from syncmusic.core.models import DeviceConfig
from syncmusic.devices.ftp import FtpDevice, FtpSettings
from tests.contracts.harness import _ftp_device, _ftp_server, work_item


def _anonymous(tmp_path) -> FtpDevice:
    root, port = _ftp_server()
    base = f"anon-{tmp_path.name}"
    (root / base).mkdir()
    settings = {"host": "127.0.0.1", "port": port, "base_path": f"/{base}"}
    return FtpDevice(
        DeviceConfig(id="a", type="ftp", name="A", settings=settings), root / base, None
    )


async def _put(dev, tmp_path, title):
    s = await dev.begin()
    ref = await dev.put_track(s, work_item(tmp_path, title, artist="Скрябін"))
    return s, ref


async def test_anonymous_upload(tmp_path):
    dev = _anonymous(tmp_path)
    await dev.check()
    s, ref = await _put(dev, tmp_path, "Група крові")
    await dev.abort(s)
    assert not (dev.mount / ref.path).exists()
    assert await dev.capacity() == await dev.free_space()  # no quota: space unknown, not limited


async def test_interrupted_session_cleaned_up_by_the_next(tmp_path):
    dev = _ftp_device(tmp_path)
    _s, ref = await _put(dev, tmp_path, "Зозуля")  # never committed: the process "died"
    assert (dev.mount / ref.path).is_file()
    s = await dev.begin()
    assert s.data["inflight_removed"] == [ref.path]
    assert not (dev.mount / ref.path).exists()
    assert not list(dev.mount.rglob(".sdtmp-*"))


async def test_dropped_connection_is_opened_again(tmp_path):
    dev = _ftp_device(tmp_path)
    await dev.check()
    dev._conn.ftp.sock.close()  # the server or the network dropped it
    assert await dev.read_manifest() is None


def test_settings_validated_and_password_masked():
    s = FtpSettings(host=" ftp.local ", password="secret", base_path="music//sd/")
    assert s.host == "ftp.local" and s.base_path == "/music/sd"
    assert "secret" not in repr(s) and "secret" not in s.model_dump_json()
    for bad in (
        {"host": "ftp://x"},
        {"host": "x", "base_path": "/a/../b"},
        {"host": "x", "port": 0},
    ):
        with pytest.raises(ValidationError):
            FtpSettings(**bad)


async def test_list_fallback_when_the_server_has_no_mlsd(tmp_path):
    """vsftpd refuses MLSD ("501 Option not understood" / "500 Unknown command"): LIST is parsed instead."""
    dev = _ftp_device(tmp_path)
    s, ref = await _put(dev, tmp_path, "Зірка на ймення Сонце")
    with_mlsd = await dev.read_contents()
    dev._conn.mlsd = False
    without = await dev.read_contents()
    assert [(i.path, i.size) for i in without.items] == [(i.path, i.size) for i in with_mlsd.items]
    assert ref.path in {i.path for i in without.items}
    await dev.abort(s)


async def test_one_session_per_device_whatever_the_instances(tmp_path):
    """The sync service opens a device instance per request: they share one FTP session (servers limit them)."""
    first = _ftp_device(tmp_path)
    await first.check()
    others = [FtpDevice(first.config, first.mount, None) for _ in range(5)]
    for dev in others:
        assert await dev.read_manifest() is None
    assert {id(d._conn) for d in [first, *others]} == {id(first._conn)}
    assert first._conn.ftp is others[-1]._conn.ftp


class _FakeFtp:
    """Files in a dict; renames onto an existing name and (optionally) every rename of the upload refused."""

    def __init__(self, refuse_upload_rename: bool):
        self.files: dict[str, bytes] = {}
        self.refuse = refuse_upload_rename
        self.closed = 0

    def mkd(self, path):
        raise ftplib.error_perm("550 exists")

    def storbinary(self, cmd, data):
        self.files[cmd.removeprefix("STOR ")] = data.read()

    def rename(self, src, dst):
        if dst in self.files or (self.refuse and ".sdtmp-" in src and "old-" not in src):
            raise ftplib.error_perm("553 refused")
        self.files[dst] = self.files.pop(src)

    def delete(self, path):
        if self.files.pop(path, None) is None:
            raise ftplib.error_perm("550 no such file")

    def close(self):
        self.closed += 1


def test_put_replaces_a_file_on_a_server_refusing_rename_onto_it(tmp_path):
    dev = _anonymous(tmp_path)
    ftp = _FakeFtp(refuse_upload_rename=False)
    target = dev._abs("manifest.json")
    ftp.files[target] = b"old"
    dev._put(ftp, "manifest.json", io.BytesIO(b"new"))
    assert ftp.files == {target: b"new"}


def test_put_keeps_the_old_file_when_the_rename_fails(tmp_path):
    dev = _anonymous(tmp_path)
    ftp = _FakeFtp(refuse_upload_rename=True)
    target = dev._abs("manifest.json")
    ftp.files[target] = b"old"
    with pytest.raises(ftplib.error_perm):
        dev._put(ftp, "manifest.json", io.BytesIO(b"new"))
    assert ftp.files[target] == b"old"


def test_refused_login_closes_the_connection(tmp_path, monkeypatch):
    made: list[_FakeFtp] = []

    class Refusing(_FakeFtp):
        def __init__(self, timeout):
            super().__init__(False)
            made.append(self)

        def connect(self, host, port):
            pass

        def login(self, user, password):
            raise ftplib.error_perm("530 Login incorrect")

    monkeypatch.setattr(ftplib, "FTP", Refusing)
    dev = _anonymous(tmp_path)
    for _ in range(3):
        with pytest.raises(ftplib.error_perm):
            dev._connect()
    assert [f.closed for f in made] == [1, 1, 1]
