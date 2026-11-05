"""Minimal in-test implementations of Library, Device and Processor.

They exist so the contract suites in tests/contracts/ are exercised before real implementations land, and as
the smallest example of "one class per type". They are not registered in syncmusic.core.registry.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel

from syncmusic.base.capabilities import DeviceCapabilities, LibraryCapabilities, PathRules
from syncmusic.base.device import Device, DeviceSession, ProbeResult
from syncmusic.base.library import Library
from syncmusic.base.processor import Processor
from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import (
    DeviceContents,
    DeviceItem,
    DeviceTrackRef,
    HealthStatus,
    ItemRef,
    LibrarySnapshot,
    Manifest,
    MediaKind,
    PlanItem,
    PreparedItem,
    Track,
    WorkItem,
)
from syncmusic.infra.blockdev.identify import BlockDeviceInfo

# ---------------------------------------------------------------------------------------------
# library: a folder of *.mp3 files, metadata from the file name
# ---------------------------------------------------------------------------------------------


class FakeLibrarySettings(BaseModel):
    root: Path
    password: str = "secret"


class FakeLibrary(Library):
    type_name = "fake"
    display_name = "Fake library"
    Settings: ClassVar[type[BaseModel]] = FakeLibrarySettings
    capabilities = LibraryCapabilities(kinds={MediaKind.MUSIC})

    @property
    def root(self) -> Path:
        return Path(self.settings.root)  # type: ignore[attr-defined]

    async def check(self) -> HealthStatus:
        ok = self.root.is_dir() and self.settings.password == "secret"  # type: ignore[attr-defined]
        return HealthStatus(ok=ok, message="" if ok else "bad root or credentials")

    def _files(self) -> list[Path]:
        return sorted(self.root.glob("*.mp3"))

    async def state_token(self) -> str:
        h = hashlib.sha1()
        for f in self._files():
            st = f.stat()
            h.update(f"{f.name}|{st.st_size}|{st.st_mtime_ns}".encode())
        return h.hexdigest()

    async def snapshot(self) -> LibrarySnapshot:
        tracks: dict[str, Track] = {}
        for f in self._files():
            item_id = hashlib.sha1(f.name.encode()).hexdigest()[:12]
            st = f.stat()
            tracks[item_id] = Track(
                ref=ItemRef(library_id=self.id, item_id=item_id),
                title=f.stem,
                size_bytes=st.st_size,
                format="mp3",
                path=f.name,
                source_state=f"{st.st_size}:{st.st_mtime_ns}",
            )
        return LibrarySnapshot(
            library_id=self.id, state_token=await self.state_token(), tracks=tracks
        )

    async def open_media(self, item_id: str) -> Path:
        for f in self._files():
            if hashlib.sha1(f.name.encode()).hexdigest()[:12] == item_id:
                return f
        raise KeyError(item_id)


# ---------------------------------------------------------------------------------------------
# device: a directory with a marker file and the manifest; a fixed byte quota as capacity
# ---------------------------------------------------------------------------------------------

MARKER = ".fake-device"
MANIFEST = manifest_io.MANIFEST_PATH


class FakeDeviceSettings(BaseModel):
    capacity: int = 100 * 1000**2


class FakeDevice(Device):
    type_name = "fake"
    display_name = "Fake device"
    Settings: ClassVar[type[BaseModel]] = FakeDeviceSettings
    capabilities = DeviceCapabilities(
        formats={"mp3"}, supports_podcasts=True, path_rules=PathRules.for_filesystem("vfat")
    )

    @classmethod
    def probe(cls, blk: BlockDeviceInfo | None, mount: Path | None) -> ProbeResult | None:
        if mount is None or not (mount / MARKER).is_file():
            return None
        return ProbeResult(type_name=cls.type_name, suggested_name="Fake")

    def _used(self) -> int:
        return sum(f.stat().st_size for f in (self.mount / "Music").rglob("*") if f.is_file())

    async def capacity(self) -> int:
        return int(self.settings.capacity)  # type: ignore[attr-defined]

    async def free_space(self) -> int:
        return await self.capacity() - self._used()

    async def read_manifest(self) -> Manifest | None:
        return manifest_io.read(self.mount)

    async def read_contents(self) -> DeviceContents:
        root = self.mount / "Music"
        files = sorted(f for f in root.rglob("*") if f.is_file()) if root.is_dir() else []
        return DeviceContents(
            items=[
                DeviceItem(
                    device_ref=str(f.relative_to(self.mount)),
                    path=str(f.relative_to(self.mount)),
                    size=f.stat().st_size,
                    title=f.stem,
                )
                for f in files
            ]
        )

    async def begin(self) -> DeviceSession:
        return DeviceSession(await self.read_manifest())

    def _safe(self, name: str) -> str:
        rules = self.effective_capabilities().path_rules
        out = "".join("_" if c in rules.forbidden_chars or ord(c) < 32 else c for c in name)
        # 100 chars keeps Cyrillic names under ext4's 255-byte limit on the host tmp dir
        return out[: min(100, rules.max_name_len - 8)].rstrip(rules.strip_trailing) or "_"

    async def put_track(self, s: DeviceSession, item: PreparedItem) -> DeviceTrackRef:
        t = item.track
        rel = (
            Path("Music")
            / self._safe(t.artist or "Unknown")
            / f"{self._safe(t.title)}.{item.plan.file.ext}"
        )
        dest = self.mount / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item.path, dest)
        s.inflight.append(str(rel))
        return DeviceTrackRef(device_ref=str(rel), path=str(rel), size=dest.stat().st_size)

    async def remove_track(self, s: DeviceSession, ref: DeviceTrackRef) -> None:
        (self.mount / ref.path).unlink(missing_ok=True)

    async def commit(self, s: DeviceSession, manifest: Manifest) -> None:
        manifest_io.write(self.mount, manifest)
        s.inflight.clear()

    async def format(self, name: str, fs: str | None = None) -> None:
        for child in self.mount.iterdir():
            if child.name != MARKER:
                shutil.rmtree(child) if child.is_dir() else child.unlink()


# ---------------------------------------------------------------------------------------------
# processor: appends an ID3v1-like 128-byte trailer to mp3 files, leaves everything else alone
# ---------------------------------------------------------------------------------------------


class FakeTrailerSettings(BaseModel):
    marker: str = "TAG"


class FakeTrailer(Processor):
    type_name = "fake"
    display_name = "Fake trailer"
    Settings: ClassVar[type[BaseModel]] = FakeTrailerSettings
    stage: ClassVar[Literal["audio", "tags", "cover", "sidecar"]] = "tags"
    TRAILER = 128

    def plan(self, item: PlanItem, caps: DeviceCapabilities) -> PlanItem:
        if item.file.format != "mp3":
            return item
        file = item.file.model_copy(update={"size": item.file.size + self.TRAILER})
        return item.model_copy(update={"file": file, "steps": [*item.steps, self.type_name]})

    async def run(self, item: WorkItem, caps: DeviceCapabilities) -> WorkItem:
        if item.plan.file.format != "mp3":
            return item
        marker = self.settings.marker.encode()  # type: ignore[attr-defined]
        with item.path.open("ab") as f:
            f.write(marker.ljust(self.TRAILER, b"\0"))
        return item.model_copy(update={"steps_done": [*item.steps_done, self.type_name]})
