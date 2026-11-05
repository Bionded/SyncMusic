# SPDX-License-Identifier: GPL-3.0-or-later
"""SCSI commands sent to a disk node.

Setting a stock iPod's clock: iTunes (and libgpod's `tools/ipod-time-sync`, `ipod-scsi.c`) send a vendor-specific
WRITE BUFFER (opcode 0x3B, mode 0x01, buffer id 0, offset 0x0C0000) with a 12-byte payload to the iPod's disk:
`u16 BE year, u16 BE day of year (0-based), u8 UTC offset in 15-min units (east positive, two's complement),
u8 hour, u8 minute, u8 second, u8 dst (0/1), 3 zero bytes`, all local time. Sent with `sg_raw` (sg3-utils)
as root (the device node is root-owned), only after the guard accepted operation `time_sync`.

The iPod may react by writing its own file system (iPod_Control/Device/clock, Preferences, alarms, SysInfo, Calendars/, Contacts/ - seen on the freshly restored Classic for ~15 s after the command, on later time syncs it wrote nothing, so those files are no readback of the clock). So the command is refused
while any file system of the disk is mounted on the host, and the caller waits `SETTLE_S` before the host mounts
or powers the iPod off - not the full `SETTLE_S`: `wait_settled` polls every second and goes on
once the disk answers TEST UNIT READY and the start of its data partition (boot sector, FSInfo, the first FATs
entries), read past the host cache, stayed the same for `SETTLE_STABLE` reads in a row; at least `SETTLE_MIN_S`,
at most `SETTLE_S`. On the real Classic it settles after ~5 s  (the "lost FAT entries" seen after an early mount were the hardware tests' fsck racing an automatic update sync, not the iPod writing late).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import struct
import time
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

from syncmusic.infra.blockdev.guard import Guard
from syncmusic.infra.blockdev.mount import BlockdevError, run_cmd, run_cmd_raw

SETTLE_S = 30.0  # at most; the Classic wrote its Device files up to 14 s after the command
SETTLE_MIN_S = 3.0  # at least, so a write the iPod starts a moment after the command is seen
SETTLE_STABLE = 3  # identical readings in a row, one per SETTLE_POLL_S
SETTLE_POLL_S = 1.0
FINGERPRINT_MB = (
    16  # read of the data partition's start: boot sector, FSInfo, the first FAT entries
)

log = logging.getLogger(__name__)
TIME_CDB = ["3B", "01", "00", "0C", "00", "00", "00", "00", "0C", "00"]  # WRITE BUFFER, 12 bytes


class TimeSyncRejected(BlockdevError):
    """The device answered the command with an error (this model does not support it)."""


def ipod_time_payload(now: dt.datetime, zone: str | None = None) -> bytes:
    """The 12-byte payload for `now` (aware): local time in the IANA `zone`, or the host's local time
    exactly like libgpod's localtime() when `zone` is None."""
    if now.tzinfo is None:
        raise ValueError("an aware datetime is needed")
    if zone:
        local = now.astimezone(ZoneInfo(zone))
        tm = local.timetuple()
        offset = int((local.utcoffset() or dt.timedelta()).total_seconds())
        dst = 1 if local.dst() else 0
    else:
        tm = time.localtime(now.timestamp())
        offset = tm.tm_gmtoff
        dst = 1 if tm.tm_isdst > 0 else 0
    tz = (offset // 900) & 0xFF
    return struct.pack(
        ">HHBBBBB3x", tm.tm_year, tm.tm_yday - 1, tz, tm.tm_hour, tm.tm_min, tm.tm_sec, dst
    )


async def set_ipod_time(
    guard: Guard, disk_path: str, now: dt.datetime, zone: str | None = None
) -> None:
    """Send the time to the iPod's disk node. TimeSyncRejected when the device refuses the command."""
    disk, _ = guard.check(disk_path, "time_sync")
    if mounted := disk.all_mountpoints():
        raise BlockdevError(
            f"refusing time sync on {disk_path}: mounted at {', '.join(sorted(mounted))} "
            "(the iPod writes its file system when its clock is set)"
        )
    payload = ipod_time_payload(now, zone)
    try:
        await run_cmd(["sg_raw", "-s", str(len(payload)), disk_path, *TIME_CDB], input=payload)
    except BlockdevError as e:
        if "check condition" in str(e).casefold() or "illegal request" in str(e).casefold():
            raise TimeSyncRejected(str(e)) from e
        raise


async def _fingerprint(guard: Guard, disk_path: str, part_path: str | None) -> str | None:
    """A reading of the disk's state for `wait_settled`; None while the disk says it is not ready or cannot be
    read. A disk that does not take SCSI commands (a loop image) is judged by the partition read alone."""
    guard.check(disk_path, "read")
    try:
        await run_cmd(["sg_turs", disk_path], timeout_s=10)
    except BlockdevError as e:
        if "not ready" in str(e).casefold():
            return None
    if part_path is None:
        return "ready"
    guard.check(part_path, "read")
    try:
        data = await run_cmd_raw(
            [
                "dd",
                f"if={part_path}",
                "bs=1M",
                f"count={FINGERPRINT_MB}",
                "iflag=direct",
                "status=none",
            ],
            timeout_s=15,
        )
    except BlockdevError:
        return None
    return hashlib.sha1(data).hexdigest()


async def wait_settled(
    guard: Guard,
    disk_path: str,
    part_path: str | None,
    *,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> float:
    """After the clock was set: wait until the iPod is done writing its Device files. Returns the seconds
    waited (SETTLE_MIN_S..SETTLE_S)."""
    start = clock()
    last: str | None = None
    same = 0
    while (waited := clock() - start) < SETTLE_S:
        try:
            reading = await _fingerprint(guard, disk_path, part_path)
        except Exception as e:  # noqa: BLE001 - an unreadable state is "not settled yet"
            log.debug("%s: settle reading failed: %s", disk_path, e)
            reading = None
        same = same + 1 if reading is not None and reading == last else 0
        last = reading
        if same >= SETTLE_STABLE - 1 and waited >= SETTLE_MIN_S:
            log.info("%s settled %.1f s after the clock was set", disk_path, waited)
            return waited
        await sleep(SETTLE_POLL_S)
    log.info("%s: no stable state within %.0f s after the clock was set", disk_path, SETTLE_S)
    return SETTLE_S


# --- SysInfoExtended (libgpod tools/ipod-scsi.c read_sysinfo_extended) ------------------------------
# INQUIRY with EVPD page 0xC0 returns the list of pages holding the XML plist; each page's payload is
# `data[4 : 4 + data[3]]`. A stock restore leaves SysInfo empty, so this is how the model (serial number,
# DatabaseVersion, FireWire GUID) is known.
SYSINFO_INDEX_PAGE = 0xC0


def _vpd_payload(raw: bytes) -> bytes:
    return raw[4 : 4 + raw[3]] if len(raw) >= 4 else b""


async def read_sysinfo_extended(guard: Guard, disk_path: str) -> bytes | None:
    """The iPod's SysInfoExtended plist read over SCSI (guard operation `read`), None if not available."""
    guard.check(disk_path, "read")

    async def page(n: int) -> bytes:
        return _vpd_payload(await run_cmd_raw(["sg_inq", f"--page=0x{n:02x}", "--raw", disk_path]))

    try:
        pages = await page(SYSINFO_INDEX_PAGE)
        xml = b"".join([await page(p) for p in pages if p])
    except BlockdevError:
        return None
    xml = xml.rstrip(b"\0")
    return xml if b"<plist" in xml else None
