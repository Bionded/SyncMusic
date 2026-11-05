"""An in-process stand-in for `syncmusic.devices.Android._libmtp.CLib`: the same methods over local folders, one
folder per storage, so `LibMtpBackend` (path resolution, caching, busy and serial handling) runs against it and tests
can look at what arrived. Players are (serial, {storage name: folder}); `busy` serials cannot be opened."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from syncmusic.devices.Android._libmtp import ROOT, Obj, Store


@dataclass
class FakePlayer:
    serial: str
    storages: dict[str, Path]
    capacity: int = 1000**3
    busy: bool = False
    open: bool = False
    address: tuple[int, int] = (1, 1)  # (bus, device number) on USB
    calls: list[str] = field(default_factory=list)


class FakeLibMtp:
    def __init__(self, *players: FakePlayer) -> None:
        self.players = list(players)
        self._ids: dict[Path, int] = {}
        self._paths: dict[int, Path] = {}
        self._next = 0

    def _id(self, p: Path) -> int:
        if p not in self._ids:
            self._next += 1  # ids are never reused, like MTP object handles
            self._ids[p] = self._next
            self._paths[self._ids[p]] = p
        return self._ids[p]

    def _root(self, dev: FakePlayer, storage_id: int) -> Path:
        return list(dev.storages.values())[storage_id - 0x10001]

    def _dir(self, dev: FakePlayer, storage_id: int, parent: int) -> Path:
        return self._root(dev, storage_id) if parent in (ROOT, 0) else self._paths[parent]

    def detect(self) -> list[FakePlayer]:
        return list(self.players)

    def open(self, raw: FakePlayer) -> FakePlayer | None:
        if raw.busy:
            return None
        raw.open = True
        return raw

    def location(self, raw: FakePlayer) -> tuple[int, int]:
        return raw.address

    def release(self, dev: FakePlayer) -> None:
        dev.open = False

    def serial(self, dev: FakePlayer) -> str:
        return dev.serial

    def storages(self, dev: FakePlayer) -> list[Store]:
        out = []
        for i, (name, root) in enumerate(dev.storages.items()):
            used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            out.append(Store(0x10001 + i, name, dev.capacity, max(0, dev.capacity - used)))
        return out

    def children(self, dev: FakePlayer, storage_id: int, parent_id: int) -> list[Obj]:
        dev.calls.append(f"children {parent_id}")
        folder = self._dir(dev, storage_id, parent_id)
        return [
            Obj(self._id(p), p.name, p.is_dir(), 0 if p.is_dir() else p.stat().st_size)
            for p in sorted(folder.iterdir())
        ]

    def get_to_fd(self, dev: FakePlayer, obj_id: int, fd: int) -> None:
        os.write(fd, self._paths[obj_id].read_bytes())

    def send_from_fd(
        self, dev: FakePlayer, fd: int, name: str, size: int, parent_id: int, storage_id: int
    ) -> int:
        dest = self._dir(dev, storage_id, parent_id) / name
        assert not dest.exists(), dest  # MTP: no overwrite
        with os.fdopen(os.dup(fd), "rb") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        assert dest.stat().st_size == size
        return self._id(dest)

    def delete(self, dev: FakePlayer, obj_id: int) -> bool:
        p = self._paths.pop(obj_id)
        del self._ids[p]
        if p.is_dir():
            p.rmdir()
        else:
            p.unlink()
        return True

    def mkdir(self, dev: FakePlayer, name: str, parent_id: int, storage_id: int) -> int:
        p = self._dir(dev, storage_id, parent_id) / name
        p.mkdir()
        return self._id(p)
