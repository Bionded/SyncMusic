import json
from pathlib import Path

import pytest

from syncmusic.infra.blockdev.guard import Allowlist, GuardError, check_target
from syncmusic.infra.blockdev.identify import parse_lsblk

DATA = Path(__file__).parent / "data" / "lsblk_pi5.json"
HARDWARE = Path(__file__).parent / "data" / "allowlist_pi5.toml"


@pytest.fixture
def disks():
    disks = parse_lsblk(DATA.read_text())
    for d in disks:  # what scan_block_devices adds from udev
        if d.name == "sdb":
            d.usb_vendor_id = "05ac"
    return disks


@pytest.fixture
def allow():
    return Allowlist.load(HARDWARE)


@pytest.mark.parametrize("target", ["/dev/nvme0n1", "/dev/nvme0n1p1", "/dev/nvme0n1p2"])
def test_refuses_nvme(disks, allow, target):
    with pytest.raises(GuardError, match="NVMe"):
        check_target(target, disks, allowlist=allow)


def test_refuses_system_mountpoint_even_on_usb(disks):
    disk = next(d for d in disks if d.name == "sda")
    disk.children[0].mountpoints = ["/boot/firmware"]
    with pytest.raises(GuardError, match="system mountpoint"):
        check_target("/dev/sda1", disks)


def test_refuses_swap_and_loop(disks):
    with pytest.raises(GuardError, match="swap"):
        check_target("/dev/loop0", disks, allow_loop=True)
    with pytest.raises(GuardError, match="zram|transport|unknown|swap"):
        check_target("/dev/zram0", disks)


def test_refuses_loop_unless_allowed(disks):
    disks.append(
        parse_lsblk({"blockdevices": [{"name": "loop7", "path": "/dev/loop7", "type": "loop"}]})[0]
    )
    with pytest.raises(GuardError, match="loop devices not allowed"):
        check_target("/dev/loop7", disks)
    # allowed loop devices must still be named by the (emulated-device) allowlist
    with pytest.raises(GuardError, match="emulated-device allowlist"):
        check_target("/dev/loop7", disks, allow_loop=True)
    emulated = Allowlist(serials=frozenset({"EMU0000000FLASH1"}))
    with pytest.raises(GuardError, match="emulated-device allowlist"):
        check_target("/dev/loop7", disks, allow_loop=True, allowlist=emulated)
    disks[-1].serial = "EMU0000000FLASH1"  # what the image's identity sidecar gives it
    check_target("/dev/loop7", disks, allow_loop=True, allowlist=emulated)
    with pytest.raises(GuardError, match="loop devices not allowed"):
        check_target("/dev/loop7", disks, allowlist=emulated)


def test_refuses_unknown(disks):
    with pytest.raises(GuardError, match="unknown"):
        check_target("/dev/sdz", disks)


def test_accepts_allowlisted_usb(disks, allow):
    disk, node = check_target("/dev/sda", disks, allowlist=allow)
    assert disk.name == "sda" and node is disk
    disk, node = check_target("/dev/sdb1", disks, allowlist=allow)
    assert disk.name == "sdb" and node.name == "sdb1"


def test_refuses_usb_not_in_allowlist(disks):
    with pytest.raises(GuardError, match="allowlist"):
        check_target("/dev/sda1", disks, allowlist=Allowlist(frozenset({"OTHER"})))


def test_refuses_non_removable_non_ipod(disks):
    disk = next(d for d in disks if d.name == "sda")
    disk.rm = False
    with pytest.raises(GuardError, match="not removable"):
        check_target("/dev/sda", disks)
    ipod = next(d for d in disks if d.name == "sdb")
    ipod.rm = False
    check_target("/dev/sdb1", disks)  # Apple vendor id → still accepted


def test_registered_identities(disks):
    with pytest.raises(GuardError, match="registered"):
        check_target("/dev/sda", disks, registered=["nope"])
    check_target("/dev/sda", disks, registered=["F00DFEED0123456789ABCDEF"])


def test_partitions_inherit_transport(disks):
    sda = next(d for d in disks if d.name == "sda")
    assert all(p.tran == "usb" for p in sda.partitions())


def test_partitions_in_number_order_whatever_lsblk_lists_first():
    """Right after a partition re-read lsblk may list p2 before p1."""

    def part(name: str) -> dict[str, str]:
        return {"name": name, "path": f"/dev/{name}", "type": "part"}

    disk = parse_lsblk(
        {
            "blockdevices": [
                {
                    "name": "sdb",
                    "path": "/dev/sdb",
                    "type": "disk",
                    "children": [part("sdb10"), part("sdb2"), part("sdb1")],
                },
                {
                    "name": "loop0",
                    "path": "/dev/loop0",
                    "type": "loop",
                    "children": [part("loop0p2"), part("loop0p1")],
                },
            ]
        }
    )
    assert [[p.name for p in d.partitions()] for d in disk] == [
        ["sdb1", "sdb2", "sdb10"],
        ["loop0p1", "loop0p2"],
    ]


def test_fixture_is_real_lsblk_output():
    assert "nvme0n1" in json.dumps(json.loads(DATA.read_text()))


def test_refuses_ipod_firmware_and_whole_disk_format(disks, allow):
    for d in disks:
        if d.name == "sdc":
            d.usb_vendor_id = "05ac"  # iPod mini 2G (Rockbox)
    for target, op in [
        ("/dev/sdc1", "format"),  # firmware partition
        ("/dev/sdc", "format"),  # whole disk → partition table
        ("/dev/sdc", "partition"),
        ("/dev/sdb", "format"),  # iPod Classic whole disk
        ("/dev/sdc1", "label"),  # Rename: never the firmware partition
        ("/dev/sdb", "label"),
        ("/dev/sdc1", "repair"),  # fsck: the data partition only
        ("/dev/sdb", "repair"),
    ]:
        with pytest.raises(GuardError):
            check_target(target, disks, allowlist=allow, operation=op)
    # the data partitions stay usable, and flash drives may be repartitioned
    check_target("/dev/sdc2", disks, allowlist=allow, operation="format")
    check_target("/dev/sdb1", disks, allowlist=allow, operation="format")
    check_target("/dev/sdc2", disks, allowlist=allow, operation="label")
    check_target("/dev/sdc2", disks, allowlist=allow, operation="repair")
    check_target("/dev/sda", disks, allowlist=allow, operation="partition")


def test_ipod_read_mount_eject_unaffected_by_destructive_rules(disks, allow):
    # non-destructive operations on the whole iPod disk remain allowed (e.g. reading the partition table)
    check_target("/dev/sdb", disks, allowlist=allow, operation="read")
    check_target("/dev/sdb", disks, allowlist=allow, operation="eject")
    check_target("/dev/sdb1", disks, allowlist=allow, operation="mount")
    check_target("/dev/sdb1", disks, allowlist=allow, operation="write")


def test_ipod_raw_write_on_whole_disk_refused(disks, allow):
    with pytest.raises(GuardError, match="partition table"):
        check_target("/dev/sdb", disks, allowlist=allow, operation="write")


def test_refuses_ipod_partition_of_unknown_type(disks, allow):
    sdb = next(d for d in disks if d.name == "sdb")
    sdb.children[0].fstype = None
    sdb.children[0].parttype = "0x83"
    with pytest.raises(GuardError, match="not an iPod data partition"):
        check_target("/dev/sdb1", disks, allowlist=allow, operation="mkfs")


def test_unknown_operation_names_refused(disks, allow):
    # A typo or a new destructive verb must not silently skip the iPod rules (fail closed).
    for d in disks:
        if d.name == "sdc":
            d.usb_vendor_id = "05ac"
    for op in ("Format", "wipe", "erase", ""):
        with pytest.raises(GuardError):
            check_target("/dev/sdc", disks, allowlist=allow, operation=op)


def test_time_sync_only_on_ipod_whole_disk(disks, allow):
    check_target("/dev/sdb", disks, allowlist=allow, operation="time_sync")
    for target in ("/dev/sdb1", "/dev/sda", "/dev/nvme0n1"):  # partition, flash drive, system disk
        with pytest.raises(GuardError):
            check_target(target, disks, allowlist=allow, operation="time_sync")
    with pytest.raises(GuardError, match="registered"):
        check_target("/dev/sdb", disks, registered=["other"], operation="time_sync")


def test_allowlist_and_registered_ids_accept_serial_aliases(disks, tmp_path):
    """the iPod mini reports one USB serial in Apple disk mode and another under Rockbox."""
    toml = tmp_path / "hw.toml"
    toml.write_text('[mini]\nusb_serial = "ROCKBOXMODE"\nusb_serial_aliases = ["DISKMODE"]\n')
    allow = Allowlist.load(toml)
    assert allow.serials == frozenset({"ROCKBOXMODE", "DISKMODE"})
    disk = next(d for d in disks if d.name == "sda")
    disk.serial = "DISKMODE"
    check_target("/dev/sda", disks, allowlist=allow)
    with pytest.raises(GuardError, match="allowlist"):
        check_target("/dev/sda", disks, allowlist=Allowlist(frozenset({"ROCKBOXMODE"})))

    from syncmusic.core.models import DeviceIdentity

    ident = DeviceIdentity(usb_serial="ROCKBOXMODE", usb_serial_aliases=["DISKMODE"], fs_uuid="U")
    assert ident.guard_ids() == ["ROCKBOXMODE", "DISKMODE", "U"]
    check_target("/dev/sda", disks, registered=ident.guard_ids())


# --- tests that kill surviving mutants (refusals must happen for the right reason) ---


def _disk(name: str, **kw: object) -> list:
    return parse_lsblk({"blockdevices": [{"name": name, "path": f"/dev/{name}", **kw}]})


def test_refuses_mmc_system_disk_even_on_usb_and_allowlisted():
    disks = _disk("mmcblk0", type="disk", tran="usb", rm=True, serial="SD1")
    with pytest.raises(GuardError, match="NVMe/MMC system disk"):
        check_target("/dev/mmcblk0", disks, allowlist=Allowlist(frozenset({"SD1"})))


def test_swap_refusal_names_the_swap_area():
    disks = _disk(
        "sdx",
        type="disk",
        tran="usb",
        rm=True,
        serial="S",
        children=[{"name": "sdx1", "path": "/dev/sdx1", "type": "part", "fstype": "swap"}],
    )
    with pytest.raises(GuardError, match="disk holds a swap area"):
        check_target("/dev/sdx", disks, allowlist=Allowlist(frozenset({"S"})))


@pytest.mark.parametrize(
    ("name", "kind"), [("loop9", "disk"), ("emu0", "loop")], ids=["loop-name", "loop-type"]
)
def test_loop_detected_by_name_or_type(name, kind):
    # either signal alone makes it a loop device: never treated as a USB disk
    disks = _disk(name, type=kind, tran="usb", rm=True, serial="EMU")
    with pytest.raises(GuardError, match="loop devices not allowed"):
        check_target(f"/dev/{name}", disks, allowlist=Allowlist(frozenset({"EMU"})))


def test_transport_refusal_names_the_transport(disks):
    disk = next(d for d in disks if d.name == "sda")
    disk.tran = "sata"
    with pytest.raises(GuardError, match="transport is 'sata', not usb"):
        check_target("/dev/sda", disks)


def test_allowlist_matches_by_fs_uuid_only(disks):
    by_uuid = Allowlist(fs_uuids=frozenset({"C5D6-E7F8"}))
    check_target("/dev/sdc2", disks, allowlist=by_uuid)
    with pytest.raises(GuardError, match="not in the hardware allowlist"):
        check_target("/dev/sdc1", disks, allowlist=by_uuid)  # sibling partition, no uuid
    assert Allowlist.load(HARDWARE).fs_uuids >= {"1A2B-3C4D", "2B3C-4D5E"}


def test_registered_identity_by_fs_uuid_and_mismatched_uuid(disks):
    check_target("/dev/sdb1", disks, registered=["B4C5-D6E7"])
    with pytest.raises(GuardError, match="not a registered or user-selected device"):
        check_target(
            "/dev/sdb1", disks, registered=["0000-0000"]
        )  # node has a uuid, but another one


def test_firmware_partition_refused_even_with_a_data_fstype(disks, allow):
    sdc = next(d for d in disks if d.name == "sdc")
    sdc.usb_vendor_id = "05ac"
    sdc.children[0].fstype = "vfat"  # lsblk misreading the firmware partition must not unlock it
    with pytest.raises(GuardError, match="iPod firmware partition"):
        check_target("/dev/sdc1", disks, allowlist=allow, operation="format")
    with pytest.raises(GuardError, match="whole iPod disk: the partition table"):
        check_target("/dev/sdc", disks, allowlist=allow, operation="format")


def test_ipod_data_partition_accepted_by_fstype_or_parttype(disks, allow):
    part = next(d for d in disks if d.name == "sdb").children[0]
    part.parttype = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"  # GPT basic data: known fstype is enough
    check_target("/dev/sdb1", disks, allowlist=allow, operation="format")
    part.parttype, part.fstype = "0xb", None  # blank FAT32 partition: known parttype is enough
    check_target("/dev/sdb1", disks, allowlist=allow, operation="format")


def test_time_sync_refusal_reason(disks, allow):
    with pytest.raises(GuardError, match="time sync is sent to an iPod's whole disk only"):
        check_target("/dev/sdb1", disks, allowlist=allow, operation="time_sync")


def test_guard_class_passes_its_configuration(disks, monkeypatch):
    from syncmusic.infra.blockdev import guard as guard_mod

    for d in disks:
        if d.name == "sdc":
            d.usb_vendor_id = "05ac"
    g = guard_mod.Guard(HARDWARE)
    assert (
        g.check("/dev/sdb1", "mount", registered=["000A11223344556F"], disks=disks)[1].name
        == "sdb1"
    )
    with pytest.raises(
        GuardError, match="refusing format on /dev/sdc1 .*: iPod firmware partition"
    ):
        g.check("/dev/sdc1", "format", disks=disks)
    with pytest.raises(GuardError, match="not a registered"):
        g.check("/dev/sdb1", "mount", registered=["other"], disks=disks)
    loop = _disk("loop5", type="loop", serial="000A11223344556F")
    with pytest.raises(GuardError, match="loop devices not allowed"):
        g.check("/dev/loop5", "mount", disks=loop)
    assert guard_mod.Guard(HARDWARE, allow_loop=True).check("/dev/loop5", "mount", disks=loop)
    with pytest.raises(GuardError, match="emulated-device allowlist"):
        guard_mod.Guard(allow_loop=True).check("/dev/loop5", "mount", disks=loop)
    monkeypatch.setattr(guard_mod, "scan_block_devices", lambda: disks)
    with pytest.raises(GuardError, match="NVMe"):
        g.check("/dev/nvme0n1", "read")  # rescans when no disks are given


def test_refusal_message_names_model_or_disk(disks):
    sda = next(d for d in disks if d.name == "sda")
    sda.model, sda.rm = "FlashStick 3.0", False
    with pytest.raises(GuardError, match=r"\(FlashStick 3.0\): not removable and not an iPod"):
        check_target("/dev/sda", disks)
    sda.model = None
    with pytest.raises(GuardError, match=r"\(sda\): not removable"):
        check_target("/dev/sda", disks)


def test_allowlist_load_skips_top_level_scalars_and_names_serials(tmp_path):
    toml = tmp_path / "hw.toml"
    toml.write_text(
        'version = 1\n[mini]\nusb_serial = "A"\nusb_serial_aliases = ["B"]\n[flash]\nfs_uuid = "U"\n'
    )
    allow = Allowlist.load(toml)
    assert allow == Allowlist(frozenset({"A", "B"}), frozenset({"U"}), {"A": "mini", "B": "mini"})


def test_unknown_operation_message(disks):
    with pytest.raises(GuardError, match="'wipe' on /dev/sda: unknown operation"):
        check_target("/dev/sda", disks, operation="wipe")


def test_missing_allowlist_file_refuses_every_disk(disks, tmp_path):
    from types import SimpleNamespace

    from syncmusic.devices.Android.mtp import AndroidMtp
    from syncmusic.infra.blockdev.guard import Guard

    guard = Guard(tmp_path / "hardware.toml")
    assert guard.allowlist == Allowlist()
    with pytest.raises(GuardError, match="allowlist"):
        check_target("/dev/sdb1", disks, allowlist=guard.allowlist)
    player = SimpleNamespace(ctx=SimpleNamespace(guard=guard), s=SimpleNamespace(usb_serial="ANY"))
    with pytest.raises(GuardError, match="not in the device allowlist"):
        AndroidMtp._check_allowed(player)  # type: ignore[arg-type]
