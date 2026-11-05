# SPDX-License-Identifier: GPL-3.0-or-later
"""The libmtp backend of the Android (MTP) device (the developer notes proposal 2).

Two layers: `CLib` binds the few libmtp 1.1 calls we need through ctypes on `libmtp.so.9` (package `libmtp9t64`)
and returns plain Python values; `LibMtpBackend` implements `MtpBackend` on top of any object with `CLib`'s methods
(the tests use an in-process fake, tests/fake_libmtp.py, so everything except the ctypes layer is tested).

MTP addresses objects by id, not by path: the backend resolves a path folder by folder and caches each folder's
listing for the session. The player is opened by its USB serial (udev's or libmtp's); when libmtp sees an MTP device it cannot open, an
other MTP client (gvfs, kio, a file manager) holds it, which gets its own message ("busy").
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import posixpath
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from syncmusic.devices.Android.mtp import MtpEntry, MtpStorage, MtpUnavailable

ROOT = 0xFFFFFFFF  # LIBMTP_FILES_AND_FOLDERS_ROOT
FILETYPE_FOLDER = 0
FILETYPE_UNKNOWN = (
    44  # LIBMTP_FILETYPE_UNKNOWN: the player's media scanner decides by the extension
)
ERROR_NO_DEVICE_ATTACHED = 5


@dataclass
class Obj:
    id: int
    name: str
    is_dir: bool
    size: int = 0


@dataclass
class Store:
    id: int
    name: str
    capacity: int
    free: int


# --- ctypes layer (libmtp 1.1.x structs, only the leading fields we read) -----------------------------


class _DeviceEntry(ctypes.Structure):
    _fields_ = [
        ("vendor", ctypes.c_char_p),
        ("vendor_id", ctypes.c_uint16),
        ("product", ctypes.c_char_p),
        ("product_id", ctypes.c_uint16),
        ("device_flags", ctypes.c_uint32),
    ]


class _RawDevice(ctypes.Structure):
    _fields_ = [
        ("device_entry", _DeviceEntry),
        ("bus_location", ctypes.c_uint32),
        ("devnum", ctypes.c_uint8),
    ]


class _Storage(ctypes.Structure):
    pass


_Storage._fields_ = [
    ("id", ctypes.c_uint32),
    ("StorageType", ctypes.c_uint16),
    ("FilesystemType", ctypes.c_uint16),
    ("AccessCapability", ctypes.c_uint16),
    ("MaxCapacity", ctypes.c_uint64),
    ("FreeSpaceInBytes", ctypes.c_uint64),
    ("FreeSpaceInObjects", ctypes.c_uint64),
    ("StorageDescription", ctypes.c_char_p),
    ("VolumeIdentifier", ctypes.c_char_p),
    ("next", ctypes.POINTER(_Storage)),
    ("prev", ctypes.POINTER(_Storage)),
]


class _Device(ctypes.Structure):
    _fields_ = [
        ("object_bitsize", ctypes.c_uint8),
        ("params", ctypes.c_void_p),
        ("usbinfo", ctypes.c_void_p),
        ("storage", ctypes.POINTER(_Storage)),
    ]


class _File(ctypes.Structure):
    pass


_File._fields_ = [
    ("item_id", ctypes.c_uint32),
    ("parent_id", ctypes.c_uint32),
    ("storage_id", ctypes.c_uint32),
    ("filename", ctypes.c_char_p),
    ("filesize", ctypes.c_uint64),
    ("modificationdate", ctypes.c_long),
    ("filetype", ctypes.c_int),
    ("next", ctypes.POINTER(_File)),
]


def _text(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


class CLib:
    """libmtp through ctypes. Device handles are opaque pointers; every call is blocking."""

    def __init__(self) -> None:
        name = ctypes.util.find_library("mtp") or "libmtp.so.9"
        try:
            lib = ctypes.CDLL(name)
        except OSError as e:
            raise MtpUnavailable(
                "libmtp is not installed (Debian/Ubuntu: apt install libmtp9t64 or libmtp9)"
            ) from e
        self._libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        self._libc.free.argtypes = [ctypes.c_void_p]
        dev = ctypes.POINTER(_Device)
        sig: dict[str, tuple[Any, list[Any]]] = {
            "LIBMTP_Init": (None, []),
            "LIBMTP_Detect_Raw_Devices": (
                ctypes.c_int,
                [ctypes.POINTER(ctypes.POINTER(_RawDevice)), ctypes.POINTER(ctypes.c_int)],
            ),
            "LIBMTP_Open_Raw_Device_Uncached": (dev, [ctypes.POINTER(_RawDevice)]),
            "LIBMTP_Release_Device": (None, [dev]),
            "LIBMTP_Get_Serialnumber": (ctypes.c_void_p, [dev]),
            "LIBMTP_Get_Storage": (ctypes.c_int, [dev, ctypes.c_int]),
            "LIBMTP_Get_Files_And_Folders": (
                ctypes.POINTER(_File),
                [dev, ctypes.c_uint32, ctypes.c_uint32],
            ),
            "LIBMTP_destroy_file_t": (None, [ctypes.POINTER(_File)]),
            "LIBMTP_new_file_t": (ctypes.POINTER(_File), []),
            "LIBMTP_Get_File_To_File_Descriptor": (
                ctypes.c_int,
                [dev, ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p],
            ),
            "LIBMTP_Send_File_From_File_Descriptor": (
                ctypes.c_int,
                [dev, ctypes.c_int, ctypes.POINTER(_File), ctypes.c_void_p, ctypes.c_void_p],
            ),
            "LIBMTP_Delete_Object": (ctypes.c_int, [dev, ctypes.c_uint32]),
            "LIBMTP_Create_Folder": (
                ctypes.c_uint32,
                [dev, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32],
            ),
            "LIBMTP_Clear_Errorstack": (None, [dev]),
        }
        for fn, (res, args) in sig.items():
            f = getattr(lib, fn)
            f.restype, f.argtypes = res, args
        self._lib = lib
        self._raw: Any = None  # the raw device array (freed with libc free on the next detect)
        lib.LIBMTP_Init()

    def detect(self) -> list[Any]:
        if self._raw is not None:
            self._libc.free(self._raw)
            self._raw = None
        raw = ctypes.POINTER(_RawDevice)()
        count = ctypes.c_int(0)
        err = self._lib.LIBMTP_Detect_Raw_Devices(ctypes.byref(raw), ctypes.byref(count))
        if err == ERROR_NO_DEVICE_ATTACHED or count.value == 0:
            return []
        if err != 0:
            raise MtpUnavailable(f"libmtp could not scan USB for MTP devices (error {err})")
        self._raw = raw
        return [ctypes.pointer(raw[i]) for i in range(count.value)]

    def open(self, raw: Any) -> Any | None:
        dev = self._lib.LIBMTP_Open_Raw_Device_Uncached(raw)
        return dev if dev else None

    def location(self, raw: Any) -> tuple[int, int]:
        return int(raw.contents.bus_location), int(raw.contents.devnum)

    def release(self, dev: Any) -> None:
        self._lib.LIBMTP_Release_Device(dev)

    def serial(self, dev: Any) -> str:
        ptr = self._lib.LIBMTP_Get_Serialnumber(dev)
        if not ptr:
            return ""
        try:
            return _text(ctypes.string_at(ptr)).strip()
        finally:
            self._libc.free(ptr)

    def storages(self, dev: Any) -> list[Store]:
        if self._lib.LIBMTP_Get_Storage(dev, 0) != 0:
            self._lib.LIBMTP_Clear_Errorstack(dev)
            raise MtpUnavailable(
                "the player did not list its storage (unlock it and allow file transfer)"
            )
        out: list[Store] = []
        st = dev.contents.storage
        while st:
            s = st.contents
            name = _text(s.StorageDescription) or _text(s.VolumeIdentifier) or f"storage {s.id:#x}"
            out.append(Store(s.id, name, s.MaxCapacity, s.FreeSpaceInBytes))
            st = s.next
        return out

    def children(self, dev: Any, storage_id: int, parent_id: int) -> list[Obj]:
        out: list[Obj] = []
        f = self._lib.LIBMTP_Get_Files_And_Folders(dev, storage_id, parent_id)
        while f:
            c = f.contents
            out.append(Obj(c.item_id, _text(c.filename), c.filetype == FILETYPE_FOLDER, c.filesize))
            # c.next shares the struct's memory: copy the address before destroy_file_t frees it
            nxt = ctypes.cast(c.next, ctypes.c_void_p).value
            self._lib.LIBMTP_destroy_file_t(f)
            f = ctypes.cast(nxt, ctypes.POINTER(_File)) if nxt else None
        self._lib.LIBMTP_Clear_Errorstack(dev)  # an empty folder leaves an error on the stack
        return out

    def get_to_fd(self, dev: Any, obj_id: int, fd: int) -> None:
        if self._lib.LIBMTP_Get_File_To_File_Descriptor(dev, obj_id, fd, None, None) != 0:
            self._lib.LIBMTP_Clear_Errorstack(dev)
            raise OSError(f"MTP: reading object {obj_id} failed")

    def send_from_fd(
        self, dev: Any, fd: int, name: str, size: int, parent_id: int, storage_id: int
    ) -> int:
        f = self._lib.LIBMTP_new_file_t()
        c = f.contents
        c.filename = name.encode(
            "utf-8"
        )  # kept alive by the struct; cleared before destroy_file_t frees it
        c.filesize, c.parent_id, c.storage_id, c.filetype = (
            size,
            parent_id,
            storage_id,
            FILETYPE_UNKNOWN,
        )
        try:
            if self._lib.LIBMTP_Send_File_From_File_Descriptor(dev, fd, f, None, None) != 0:
                self._lib.LIBMTP_Clear_Errorstack(dev)
                raise OSError(f"MTP: sending {name!r} failed (storage full or the player locked?)")
            return int(c.item_id)
        finally:
            c.filename = None  # the Python-owned string must not be freed by libmtp
            self._lib.LIBMTP_destroy_file_t(f)

    def delete(self, dev: Any, obj_id: int) -> bool:
        if self._lib.LIBMTP_Delete_Object(dev, obj_id) != 0:
            self._lib.LIBMTP_Clear_Errorstack(dev)
            return False
        return True

    def mkdir(self, dev: Any, name: str, parent_id: int, storage_id: int) -> int:
        new = self._lib.LIBMTP_Create_Folder(dev, name.encode("utf-8"), parent_id, storage_id)
        if not new:
            self._lib.LIBMTP_Clear_Errorstack(dev)
            raise OSError(f"MTP: making folder {name!r} failed")
        return int(new)


# --- the backend ------------------------------------------------------------------------------------


class LibMtpBackend:
    """`MtpBackend` over libmtp: opens the player whose USB serial is `serial`, one session until `close`."""

    def __init__(
        self,
        serial: str,
        lib: Any | None = None,
        usb: dict[tuple[int, int], str] | None = None,
        allowed: frozenset[str] | None = None,
    ) -> None:
        self.lib = lib if lib is not None else CLib()
        self._dev = self._open(serial, usb, allowed)
        self._stores: list[Store] = []
        self._listing: dict[
            tuple[int, int], list[Obj]
        ] = {}  # (storage id, folder id) -> its objects

    def _open(
        self, serial: str, usb: dict[tuple[int, int], str] | None, allowed: frozenset[str] | None
    ) -> Any:
        """`usb`: udev's MTP devices, (bus, device number) → USB serial. The device whose udev
        serial is `serial` is opened alone; otherwise every device is opened and asked for its MTP serial - with
        `allowed` (the allowlist) only devices whose udev serial is in it, so no session is opened to another phone."""
        raws = self.lib.detect()
        if not raws:
            raise MtpUnavailable(
                "no MTP player found: connect it, unlock it and choose 'File transfer' in its USB menu"
            )
        exact = False  # udev's serial matched: the device is ours whatever MTP serial it reports
        if usb is not None:
            udev = {id(raw): usb.get(self.lib.location(raw)) for raw in raws}
            if any(udev[id(raw)] == serial for raw in raws):
                raws, exact = [raw for raw in raws if udev[id(raw)] == serial], True
            elif allowed is not None:
                raws = [raw for raw in raws if udev[id(raw)] in allowed]
        unopened = 0
        seen: list[str] = []
        for raw in raws:
            dev = self.lib.open(raw)
            if dev is None:
                unopened += 1
                continue
            found = self.lib.serial(dev)
            if exact or found == serial:
                return dev
            seen.append(found or "?")
            self.lib.release(dev)
        if unopened:
            raise MtpUnavailable(
                "the player is busy: another MTP client holds it (a file manager, gvfs or kio) - close it, "
                "or unplug and reconnect the player"
            )
        raise MtpUnavailable(f"no MTP player with serial {serial!r} (found: {', '.join(seen)})")

    # --- path resolution --------------------------------------------------------------------------

    def _store(self, storage: str | None) -> Store:
        if not self._stores:
            self._stores = self.lib.storages(self._dev)
        if not self._stores:
            raise MtpUnavailable("the player shows no storage (unlock it and allow file transfer)")
        if storage is None:
            return self._stores[0]
        for st in self._stores:
            if st.name == storage:
                return st
        raise MtpUnavailable(f"no storage {storage!r} on the player")

    def _children(self, sid: int, folder: int) -> list[Obj]:
        key = (sid, folder)
        if key not in self._listing:
            self._listing[key] = self.lib.children(self._dev, sid, folder)
        return self._listing[key]

    def _find(self, sid: int, folder: int, name: str) -> Obj | None:
        return next((o for o in self._children(sid, folder) if o.name == name), None)

    def _parts(self, path: str) -> list[str]:
        return [p for p in path.split("/") if p]

    def _folder(self, sid: int, path: str) -> int | None:
        """The id of the folder at `path` (ROOT for ""), None when it does not exist."""
        folder = ROOT
        for part in self._parts(path):
            obj = self._find(sid, folder, part)
            if obj is None or not obj.is_dir:
                return None
            folder = obj.id
        return folder

    def _lookup(self, sid: int, path: str) -> tuple[int, Obj | None]:
        """(parent folder id, the object at `path` or None); FileNotFoundError when the parent is missing."""
        parent = self._folder(sid, posixpath.dirname(path))
        if parent is None:
            raise FileNotFoundError(path)
        return parent, self._find(sid, parent, posixpath.basename(path))

    @staticmethod
    def _parent_arg(folder: int) -> int:
        return 0 if folder == ROOT else folder  # send/create take 0 for the storage's root

    # --- MtpBackend --------------------------------------------------------------------------------

    def storages(self) -> list[MtpStorage]:
        self._stores = self.lib.storages(self._dev)  # fresh: free space changes
        return [MtpStorage(name=s.name, capacity=s.capacity, free=s.free) for s in self._stores]

    def list(self, storage: str | None, path: str) -> list[MtpEntry]:
        sid = self._store(storage).id
        folder = self._folder(sid, path)
        if folder is None:
            raise FileNotFoundError(path)
        return [MtpEntry(o.name, o.is_dir, o.size) for o in self._children(sid, folder)]

    def read(self, storage: str | None, path: str) -> bytes | None:
        sid = self._store(storage).id
        try:
            _, obj = self._lookup(sid, path)
        except FileNotFoundError:
            return None
        if obj is None or obj.is_dir:
            return None
        with tempfile.TemporaryFile() as tmp:
            self.lib.get_to_fd(self._dev, obj.id, tmp.fileno())
            tmp.seek(0)
            return tmp.read()

    def _send(self, sid: int, path: str, fd: int, size: int) -> None:
        parent, existing = self._lookup(sid, path)
        if existing is not None:
            raise FileExistsError(path)
        name = posixpath.basename(path)
        new = self.lib.send_from_fd(self._dev, fd, name, size, self._parent_arg(parent), sid)
        self._children(sid, parent).append(Obj(new, name, False, size))

    def send(self, storage: str | None, src: Path, path: str) -> None:
        sid = self._store(storage).id
        with open(src, "rb") as f:
            self._send(sid, path, f.fileno(), os.fstat(f.fileno()).st_size)

    def send_bytes(self, storage: str | None, data: bytes, path: str) -> None:
        sid = self._store(storage).id
        with tempfile.TemporaryFile() as tmp:
            tmp.write(data)
            tmp.flush()
            tmp.seek(0)
            self._send(sid, path, tmp.fileno(), len(data))

    def delete(self, storage: str | None, path: str) -> None:
        sid = self._store(storage).id
        try:
            parent, obj = self._lookup(sid, path)
        except FileNotFoundError:
            return
        if obj is None:
            return
        if obj.is_dir and self._children(sid, obj.id):
            raise OSError(f"MTP: folder {path!r} is not empty")
        if not self.lib.delete(self._dev, obj.id):
            raise OSError(f"MTP: deleting {path!r} failed")
        self._listing[(sid, parent)] = [o for o in self._children(sid, parent) if o.id != obj.id]
        self._listing.pop((sid, obj.id), None)

    def mkdir(self, storage: str | None, path: str) -> None:
        sid = self._store(storage).id
        parent, obj = self._lookup(sid, path)
        if obj is not None:
            if not obj.is_dir:
                raise FileExistsError(path)
            return
        name = posixpath.basename(path)
        new = self.lib.mkdir(self._dev, name, self._parent_arg(parent), sid)
        self._children(sid, parent).append(Obj(new, name, True))
        self._listing[(sid, new)] = []

    def close(self) -> None:
        if self._dev is not None:
            self.lib.release(self._dev)
            self._dev = None
        self._listing.clear()
