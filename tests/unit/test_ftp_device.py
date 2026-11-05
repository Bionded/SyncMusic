"""FTP device specifics against the in-process test server of the contract harness: anonymous
login, an interrupted session cleaned up by the next one, a dropped connection opened again, the password masked."""

from __future__ import annotations

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
