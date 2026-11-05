"""tests/hardware.toml generated from registered devices: the guard accepts exactly those devices."""

from __future__ import annotations

from syncmusic.core.models import DeviceConfig, DeviceIdentity
from syncmusic.infra import allowlist_toml
from syncmusic.infra.blockdev.guard import Allowlist
from syncmusic.infra.blockdev.identify import BlockDeviceInfo


def _dev(id_: str, type_: str, **ident) -> DeviceConfig:
    return DeviceConfig(id=id_, name=id_.title(), type=type_, identity=DeviceIdentity(**ident))


def _disk(serial: str | None, uuid: str | None = None) -> BlockDeviceInfo:
    return BlockDeviceInfo(name="sdx", path="/dev/sdx", serial=serial, uuid=uuid)


def test_render_loads_and_matches_only_the_listed(tmp_path):
    devices = [
        _dev("classic", "ipod", usb_serial="000A2700AAAA", fs_uuid="AAAA-0001"),
        _dev("mini", "rockbox", usb_serial="000A2700BBBB", usb_serial_aliases=["ALIAS1"]),
        _dev("r1", "hiby", usb_serial="HiBy_R1", fs_uuid="CCCC-0003"),  # every R1 says HiBy_R1
        _dev("phone", "android", usb_serial="MTP0001"),
        _dev("nas", "smb"),  # no identity: not listed
    ]
    text = allowlist_toml.render(devices)
    assert text.startswith("# Hardware allowlist") and "may FORMAT" in text
    assert "[nas]" not in text and "HiBy_R1" not in text and "# Classic (ipod)" in text
    path = tmp_path / "hardware.toml"
    path.write_text(text)
    allow = Allowlist.load(path)
    assert allow.serials == {"000A2700AAAA", "000A2700BBBB", "ALIAS1", "MTP0001"}
    assert allow.fs_uuids == {"AAAA-0001", "CCCC-0003"}
    node = _disk(None, "CCCC-0003")
    assert allow.allows(_disk("HiBy_R1"), node)  # the listed R1 by its file system
    assert not allow.allows(_disk("HiBy_R1"), _disk(None, "DDDD-0004"))  # another R1
    assert not allow.allows(_disk("OTHER"), _disk(None, "EEEE-0005"))


def test_duplicate_keys_get_a_suffix():
    text = allowlist_toml.render(
        [_dev("a-b", "ipod", usb_serial="S1"), _dev("a_b", "ipod", usb_serial="S2")]
    )
    assert "[a_b]" in text and "[a_b_2]" in text
