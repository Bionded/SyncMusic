"""An in-process MTP backend over a local folder: the folder is the player's first storage, so tests
can look at what arrived. `busy = True` simulates a player held by another MTP client or unplugged."""

from __future__ import annotations

import shutil
from pathlib import Path

from syncmusic.devices.Android.mtp import MtpEntry, MtpStorage, MtpUnavailable


class FakeMtpBackend:
    def __init__(
        self, root: Path, *, capacity: int = 1000**3, name: str = "Internal shared storage"
    ) -> None:
        self.root = root
        self.capacity = capacity
        self.name = name
        self.busy = False
        self.closed = False

    def _path(self, storage: str | None, path: str) -> Path:
        self._check()
        if storage not in (None, self.name):
            raise MtpUnavailable(f"no storage {storage!r}")
        return self.root / path

    def _check(self) -> None:
        if self.busy:
            raise MtpUnavailable("the player is busy (another MTP client holds it)")

    def storages(self) -> list[MtpStorage]:
        self._check()
        used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        return [
            MtpStorage(name=self.name, capacity=self.capacity, free=max(0, self.capacity - used))
        ]

    def list(self, storage: str | None, path: str) -> list[MtpEntry]:
        folder = self._path(storage, path)
        if not folder.is_dir():
            raise FileNotFoundError(path)
        return [
            MtpEntry(p.name, p.is_dir(), 0 if p.is_dir() else p.stat().st_size)
            for p in sorted(folder.iterdir())
        ]

    def read(self, storage: str | None, path: str) -> bytes | None:
        p = self._path(storage, path)
        return p.read_bytes() if p.is_file() else None

    def send(self, storage: str | None, src: Path, path: str) -> None:
        dest = self._path(storage, path)
        assert dest.parent.is_dir() and not dest.exists(), (
            path
        )  # MTP: no overwrite, folder made first
        shutil.copyfile(src, dest)

    def send_bytes(self, storage: str | None, data: bytes, path: str) -> None:
        dest = self._path(storage, path)
        assert dest.parent.is_dir() and not dest.exists(), path
        dest.write_bytes(data)

    def delete(self, storage: str | None, path: str) -> None:
        p = self._path(storage, path)
        if p.is_dir():
            p.rmdir()  # OSError when not empty, like MTP
        elif p.exists():
            p.unlink()

    def mkdir(self, storage: str | None, path: str) -> None:
        self._path(storage, path).mkdir(exist_ok=True)

    def close(self) -> None:
        self.closed = True
