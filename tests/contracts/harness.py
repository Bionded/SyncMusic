"""Per-implementation fixture factories for the contract suites (level 2).

Every type in `syncmusic.core.registry` must have an entry here; the suites fail for a registered type
without one. Adding an implementation = register it + add its harness here + make the suite pass.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from syncmusic.base.device import Device
from syncmusic.base.library import Library
from syncmusic.base.processor import Processor
from syncmusic.core.models import (
    DeviceConfig,
    ItemRef,
    LibraryConfig,
    Manifest,
    PlanItem,
    PlannedFile,
    Track,
    WorkItem,
)
from tests.fakes import MANIFEST, MARKER, FakeDevice, FakeLibrary, FakeTrailer


def work_item(
    tmp: Path, title: str, *, artist: str = "Artist", fmt: str = "mp3", size: int = 50_000
) -> WorkItem:
    """A prepared item backed by a file of `size` bytes in `tmp` (content is opaque to most devices)."""
    work = tmp / "work"
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"{abs(hash((title, artist)))}.{fmt}"
    path.write_bytes(os.urandom(size))
    track = Track(
        ref=ItemRef(library_id="lib", item_id=f"{artist}/{title}"),
        title=title,
        artist=artist,
        format=fmt,
        size_bytes=size,
        duration_s=180.0,
    )
    plan = PlanItem(track=track, file=PlannedFile(format=fmt, ext=fmt, size=size))
    return WorkItem(plan=plan, path=path, work_dir=work)


@dataclass
class LibraryHarness:
    make: Callable[[Path], Library]  # tmp dir -> library with at least two items
    mutate: Callable[[Library], None]  # change content so the state token must change
    bad_credentials: Callable[[Path], Library] | None = None


@dataclass
class FeedbackHarness:
    """A library that is a FeedbackSink; what it stored is read back through its own snapshot()."""

    make: Callable[[Path], Library]  # library with at least two items the sink accepts data for
    down: Callable[[Path], Library]  # the same library with its server unreachable


@dataclass
class DeviceHarness:
    make: Callable[[Path], Device]  # tmp dir -> device on a fresh virtual disk / tree
    make_item: Callable[[Path, str, str], WorkItem] = lambda tmp, title, artist: work_item(
        tmp, title, artist=artist
    )
    # write synthetic plays for the manifest's items onto the device (None = no feedback support)
    inject_feedback: Callable[[Device, Manifest], Awaitable[None]] | None = None
    can_format: bool = True
    # manifest file relative to the mount; must be the newest file after commit (None = not file-based)
    manifest_path: Path | None = None


@dataclass
class ProcessorHarness:
    make: Callable[[dict[str, Any]], Processor]
    supported: Callable[[Path], WorkItem]  # an input the processor changes
    unsupported: Callable[[Path], WorkItem] | None = None  # an input it must pass through unchanged
    alt_settings: dict[str, Any] = field(default_factory=dict)  # must change the cache key
    size_tolerance: float = 0.0  # allowed |predicted - actual| / actual


# --- fakes -----------------------------------------------------------------------------------------


def _fake_library(tmp: Path, password: str = "secret") -> Library:
    root = tmp / "fake-lib"
    root.mkdir(exist_ok=True)
    for name in ("Один.mp3", "Two.mp3"):
        if not (root / name).exists():
            (root / name).write_bytes(os.urandom(10_000))
    cfg = LibraryConfig(
        id="lib1", type="fake", name="Fake", settings={"root": str(root), "password": password}
    )
    return FakeLibrary(cfg, None)


def _fake_library_mutate(lib: Library) -> None:
    root = Path(lib.settings.root)  # type: ignore[attr-defined]
    (root / "Three.mp3").write_bytes(os.urandom(5_000))


def _fake_device(tmp: Path) -> Device:
    mount = tmp / "fake-dev"
    mount.mkdir(exist_ok=True)
    (mount / MARKER).touch()
    return FakeDevice(DeviceConfig(id="dev1", type="fake", name="Fake"), mount, None)


def _flash_device(tmp: Path) -> Device:
    from syncmusic.devices.flashdrive import FlashDrive

    mount = tmp / "flash"
    (mount / ".syncmusic").mkdir(parents=True, exist_ok=True)
    cfg = DeviceConfig(id="flash1", type="flashdrive", name="Flash")  # path rules of the tmp fs
    return FlashDrive(cfg, mount, None)


def _folder_library(tmp: Path) -> Library:
    from syncmusic.libraries.folder import FolderLibrary
    from tests.media import make_track

    root = tmp / "folder-lib"
    if not root.exists():
        make_track(
            root / "Скрябін" / "01 - Група крові.mp3",
            title="Група крові",
            artist="Скрябін",
            album="Група крові",
            tracknumber="1",
        )
        make_track(
            root / "Скрябін" / "02 - Зачини за мною двері.flac",
            title="Зачини за мною двері",
            artist="Скрябін",
            album="Група крові",
            tracknumber="2",
        )
        make_track(root / "Podcasts" / "Show" / "ep1.m4a", title="Episode 1", album="Show")
    cfg = LibraryConfig(
        id="folder1",
        type="folder",
        name="Folder",
        settings={"root": str(root), "subfolder_kinds": {"Podcasts": "podcast"}},
    )
    return FolderLibrary(cfg, None)


def _folder_library_mutate(lib: Library) -> None:
    from tests.media import make_track

    make_track(Path(lib.settings.root) / "Скрябін" / "03 - Нова.mp3", title="Нова")  # type: ignore[attr-defined]


def _navidrome_library(tmp: Path, password: str = "pw") -> Library:
    from syncmusic.libraries.subsonic import SubsonicLibrary
    from tests.fake_navidrome import FakeNavidrome, song

    server = FakeNavidrome()
    server.add(song("s1", "Група крові"))
    server.add(song("s2", "Зірка на ймення Сонце", track=2))
    lib = SubsonicLibrary(
        LibraryConfig(
            id="nd1",
            type="subsonic",
            name="ND",
            settings={"url": "http://nd.test", "username": "admin", "password": password},
        ),
        None,
    )
    lib.transport = server
    return lib


def _navidrome_mutate(lib: Library) -> None:
    from tests.fake_navidrome import song

    lib.transport.add(song("s3", "Зозуля"))  # type: ignore[attr-defined]


def _abs_library(tmp: Path, token: str = "tok") -> Library:
    from syncmusic.libraries.audiobookshelf import AudiobookshelfLibrary
    from tests.fake_abs import FakeABS

    server = FakeABS()
    server.add_book(
        "b1", "Добрі передвісники", [("01. Початок.mp3", 60.0), ("02. Кінець.mp3", 30.0)]
    )
    server.add_podcast("p1", "Подкаст", [("e1", "Епізод 1")])
    lib = AudiobookshelfLibrary(
        LibraryConfig(
            id="abs1",
            type="audiobookshelf",
            name="ABS",
            settings={"url": "http://abs.test", "api_token": token},
        ),
        None,
    )
    lib.transport = server
    return lib


def _abs_mutate(lib: Library) -> None:
    lib.transport.add_book("b2", "Воно", [("Епілог.mp3", 10.0)])  # type: ignore[attr-defined]


def _jellyfin_library(tmp: Path, api_key: str = "key") -> Library:
    from syncmusic.libraries.jellyfin import JellyfinLibrary
    from tests.fake_jellyfin import FakeJellyfin, video

    server = FakeJellyfin()
    server.add(video("j1", "Захар Беркут", size=1200))
    server.add(video("j2", "Пілот", series="Серіал", size=900))
    lib = JellyfinLibrary(
        LibraryConfig(
            id="jf1",
            type="jellyfin",
            name="Jellyfin",
            settings={"url": "http://jf.test", "username": "admin", "api_key": api_key},
        ),
        None,
    )
    lib.transport = server
    return lib


def _jellyfin_mutate(lib: Library) -> None:
    from tests.fake_jellyfin import video

    lib.transport.add(video("j3", "Другий", series="Серіал", episode=2))  # type: ignore[attr-defined]


class _Down(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


def _unreachable(make: Callable[[Path], Library]) -> Callable[[Path], Library]:
    def down(tmp: Path) -> Library:
        lib = make(tmp)
        lib.transport = _Down()  # type: ignore[attr-defined]
        return lib

    return down


FEEDBACK: dict[str, FeedbackHarness] = {
    "audiobookshelf": FeedbackHarness(make=_abs_library, down=_unreachable(_abs_library)),
    "subsonic": FeedbackHarness(make=_navidrome_library, down=_unreachable(_navidrome_library)),
}

LIBRARIES: dict[str, LibraryHarness] = {
    "audiobookshelf": LibraryHarness(
        make=_abs_library,
        mutate=_abs_mutate,
        bad_credentials=lambda tmp: _abs_library(tmp, token="wrong"),
    ),
    "subsonic": LibraryHarness(
        make=_navidrome_library,
        mutate=_navidrome_mutate,
        bad_credentials=lambda tmp: _navidrome_library(tmp, password="wrong"),
    ),
    "jellyfin": LibraryHarness(
        make=_jellyfin_library,
        mutate=_jellyfin_mutate,
        bad_credentials=lambda tmp: _jellyfin_library(tmp, api_key="wrong"),
    ),
    "folder": LibraryHarness(make=_folder_library, mutate=_folder_library_mutate),
    "fake": LibraryHarness(
        make=_fake_library,
        mutate=_fake_library_mutate,
        bad_credentials=lambda tmp: _fake_library(tmp, password="wrong"),
    ),
}

CLASSIC_7G_SYSINFO = b"ModelNumStr: xC297\nFirewireGuid: 0x000A11223344556F\n"


def ipod_tree(mount: Path, name: str = "Test iPod") -> Path:
    """A virtual iPod Classic 7G: iPod_Control tree + empty database (pygpod), SysInfo with its GUID."""
    from pygpod import init_ipod

    mount.mkdir(parents=True, exist_ok=True)
    init_ipod(str(mount), name=name, sysinfo_files={"SysInfo": CLASSIC_7G_SYSINFO})
    return mount


def _ipod_device(tmp: Path) -> Device:
    from syncmusic.devices.iPod.ipod import IPod

    mount = ipod_tree(tmp / "ipod")
    return IPod(DeviceConfig(id="ipod1", type="ipod", name="iPod"), mount, None)


def _ipod_item(tmp: Path, title: str, artist: str) -> WorkItem:
    """A real (tiny) MP3: pygpod reads duration/bitrate from the file."""
    from tests.media import make_music, media_work_item

    path = make_music(tmp / "src" / f"{abs(hash((title, artist)))}.mp3", title=title, artist=artist)
    item = media_work_item(path, tmp / "work")
    track = item.track.model_copy(
        update={
            "ref": ItemRef(library_id="lib", item_id=f"{artist}/{title}"),
            "title": title,
            "artist": artist,
        }
    )
    return item.model_copy(update={"plan": item.plan.model_copy(update={"track": track})})


async def _ipod_feedback(dev: Device, manifest: Manifest) -> None:
    """Write a Play Counts file as the iPod does: 2 plays + 4 stars for every track (database order)."""
    import struct
    import time

    from pygpod import Database

    n = len(Database(str(dev.mount), apply_play_counts=False).tracks)
    mac_now = int(time.time()) + 2082844800
    entries = b""
    for _ in range(n):
        entry = bytearray(28)
        struct.pack_into("<IIIIIII", entry, 0, 2, mac_now, 0, 80, 0, 0, 0)
        entries += bytes(entry)
    header = b"mhdp" + struct.pack("<III", 96, 28, n) + b"\x00" * 80
    (dev.mount / "iPod_Control" / "iTunes" / "Play Counts").write_bytes(header + entries)


# rockbox-info.txt as a Rockbox build on an iPod mini 2G writes it
ROCKBOX_INFO = (
    "Target: ipodmini2g\nTarget id: 21\nVersion: 4.0\n"
    "Features: albumart:dircache:rtc:swcodec:tagcache:tc_ramcache\n"
)


def rockbox_tree(mount: Path) -> Path:
    """A virtual Rockbox player: /.rockbox with the mini 2G's rockbox-info.txt."""
    rb = mount / ".rockbox"
    rb.mkdir(parents=True, exist_ok=True)
    (rb / "rockbox-info.txt").write_text(ROCKBOX_INFO)
    return mount


def _rockbox_device(tmp: Path) -> Device:
    from syncmusic.devices.Rockbox.rockbox import Rockbox

    mount = rockbox_tree(tmp / "rockbox")
    return Rockbox(DeviceConfig(id="rb1", type="rockbox", name="Rockbox"), mount, None)


async def _rockbox_feedback(dev: Device, manifest: Manifest) -> None:
    """A playback.log as Rockbox writes it: one full play of every managed track (no RTC stamps)."""
    lines = ["# Started Ver. 4.0"]
    lines += [
        f"{60_000 * (i + 1)}:200000:200000:/{e.path}" for i, e in enumerate(manifest.items.values())
    ]
    (dev.mount / ".rockbox" / "playback.log").write_text("\n".join(lines) + "\n")


# the SonixOS tables the HiBy device reads (schema as on a HiBy R1)
HIBY_SCHEMA = {
    "library.db": (
        "CREATE TABLE PLAY_COUNTS(path TEXT PRIMARY KEY, plays INT NOT NULL, last_played INT);"
        "CREATE TABLE FAVOURITES(path TEXT PRIMARY KEY, name TEXT, artist TEXT, added_at INT);"
        "CREATE TABLE PLAY_HISTORY(id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, name TEXT,"
        " artist TEXT, played_at INT);"
    ),
    "audiobooks.db": (
        "CREATE TABLE AUDIOBOOK_TABLE(path TEXT PRIMARY KEY, name TEXT COLLATE NOCASE, size INT, mtime INT,"
        "last_played INT DEFAULT 0, resume_file TEXT, resume_pos REAL,author TEXT DEFAULT '', series TEXT DEFAULT '',"
        " series_part REAL, added INT DEFAULT 0,folder INT DEFAULT 0, summary TEXT);"
        "CREATE TABLE AUDIOBOOK_FINISHED(path TEXT PRIMARY KEY, finished_at INT DEFAULT 0);"
        "CREATE TABLE AUDIOBOOK_PARTS(path TEXT PRIMARY KEY, book TEXT, idx INT, title TEXT);"
        "CREATE INDEX AUDIOBOOK_PARTS_BOOK ON AUDIOBOOK_PARTS(book, idx);"
    ),
    "podcast.db": (
        "CREATE TABLE PODCAST_EPISODES(key TEXT PRIMARY KEY, path TEXT, title TEXT, show TEXT, image TEXT,"
        "feed_id INTEGER DEFAULT 0, episode_id INTEGER DEFAULT 0,position REAL DEFAULT 0, duration REAL DEFAULT 0,"
        " played INTEGER DEFAULT 0,touched INTEGER DEFAULT 0);"
    ),
}


def hiby_tree(mount: Path) -> Path:
    """A virtual HiBy R1 (SonixOS): empty databases in /.local, an empty Music folder."""
    import sqlite3

    (mount / ".local").mkdir(parents=True, exist_ok=True)
    (mount / "Music").mkdir(exist_ok=True)
    for name, sql in HIBY_SCHEMA.items():
        con = sqlite3.connect(mount / ".local" / name)
        con.executescript(sql)
        con.close()
    return mount


def hiby_sql(mount: Path, db: str, sql: str, *args: Any) -> None:
    import sqlite3

    con = sqlite3.connect(mount / ".local" / db)
    con.execute(sql, args)
    con.commit()
    con.close()


def compas_tree(mount: Path, plugins: tuple[str, ...] = ("Audiobooks", "Podcasts")) -> Path:
    """A virtual HiBy R1 card written by the Compás firmware (device.json, plugin assets)."""
    import json

    (mount / ".compas").mkdir(parents=True, exist_ok=True)
    (mount / ".compas" / "device.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "model": "r1",
                "player_version": "v9.9.9",
                "plugin_api": 19,
                "database_format": 8,
            }
        )
    )
    for name in plugins:
        (mount / ".plugin-assets" / name).mkdir(parents=True, exist_ok=True)
    return mount


def _compas_device(tmp: Path) -> Device:
    from syncmusic.devices.HibyR1.compas import HibyCompas

    mount = compas_tree(tmp / "compas")
    return HibyCompas(DeviceConfig(id="compas1", type="hiby_compas", name="Compás"), mount, None)


def _hiby_device(tmp: Path) -> Device:
    from syncmusic.devices.HibyR1.sonixos import Hiby

    mount = hiby_tree(tmp / "hiby")
    return Hiby(DeviceConfig(id="hiby1", type="hiby", name="HiBy"), mount, None)


async def _hiby_feedback(dev: Device, manifest: Manifest) -> None:
    """Two plays of every managed track in PLAY_COUNTS, the first one a favourite."""
    for i, e in enumerate(manifest.items.values()):
        hiby_sql(
            dev.mount,
            "library.db",
            "INSERT INTO PLAY_COUNTS VALUES (?, 2, ?)",
            f"/mnt/sd_0/{e.path}",
            1_793_101_600 + i,
        )
        if i == 0:
            hiby_sql(
                dev.mount,
                "library.db",
                "INSERT INTO FAVOURITES VALUES (?, '', '', 0)",
                f"/mnt/sd_0/{e.path}",
            )


_FTP: dict[
    str, Any
] = {}  # one in-process FTP server (pyftpdlib) for the whole run: root folder + port


def _ftp_server() -> tuple[Path, int]:
    """Serve a local folder over FTP (user `sd`/`secret` and anonymous, both writable) in a daemon thread."""
    if not _FTP:
        import tempfile
        import threading

        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer

        root = Path(tempfile.mkdtemp(prefix="sd-ftp-contract-", dir="/var/tmp"))
        auth = DummyAuthorizer()
        auth.add_user("sd", "secret", str(root), perm="elradfmwMT")
        auth.add_anonymous(str(root), perm="elradfmwMT")
        handler = type("Handler", (FTPHandler,), {"authorizer": auth, "banner": "sd test"})
        server = FTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True, name="ftp-contract").start()
        _FTP.update(root=root, port=server.socket.getsockname()[1], server=server)
    return _FTP["root"], _FTP["port"]


def stop_ftp_server() -> None:
    """Stop the FTP server and remove its folder (from pytest_sessionfinish: xdist workers run no atexit handlers)."""
    import shutil

    if _FTP:
        _FTP["server"].close_all()
        shutil.rmtree(_FTP["root"], ignore_errors=True)
        _FTP.clear()


def _ftp_device(tmp: Path) -> Device:
    """An FTP device on its own base folder of the test server; `mount` is that folder on the local disk, so the
    contract can look at what arrived on the server."""
    from syncmusic.devices.ftp import FtpDevice

    root, port = _ftp_server()
    base = f"dev-{tmp.name}-{os.getpid()}"
    (root / base).mkdir()
    settings = {
        "host": "127.0.0.1",
        "port": port,
        "username": "sd",
        "password": "secret",
        "base_path": f"/{base}",
        "quota_gb": 1,
    }
    return FtpDevice(
        DeviceConfig(id="ftp1", type="ftp", name="FTP", settings=settings), root / base, None
    )


def _client_device(tmp: Path) -> Device:
    """A Wi-Fi client: `mount` is its outbox on the server; it reported 1 GB of storage."""
    from syncmusic.devices.client import ClientDevice

    dev = ClientDevice(
        DeviceConfig(id="client1", type="client", name="Phone"), tmp / "outbox", None
    )
    dev.report({"formats": ["mp3", "flac"], "storage": {"total": 10**9, "free": 10**9}}, {})
    return dev


def _smb_device(tmp: Path) -> Device:
    """An SMB device on its own folder of the compose samba's writable [devices] share; `mount` is
    that folder on the host (bind-mounted into the container), so the contract can look at what arrived."""

    from syncmusic.devices.smb import SmbDevice
    from tests.integration import services

    if not services.docker():
        pytest.skip("Docker not available for the compose samba ")
    # counted like the services fixture: a bare up()/down() here raced the xdist workers' stack; released at
    # the end of the session (tests/conftest.py pytest_sessionfinish)
    services.acquire()
    folder = f"dev-{tmp.name}-{os.getpid()}"
    settings = {
        "host": services.ENV["SMB_HOST"],
        "port": int(services.ENV["SMB_PORT"]),
        "username": services.ENV["SMB_USER"],
        "password": services.ENV["SMB_PASSWORD"],
        "remote_path": f"/devices/{folder}",
        "quota_gb": 1,
    }
    device = SmbDevice(
        DeviceConfig(id=f"smb-{folder}", type="smb", name="SMB", settings=settings),
        services.SMB_DEVICES / folder,
        None,
    )
    device._call(
        lambda kw: device._makedirs(kw, "")
    )  # creates the folder (make runs inside the event loop)
    return device


def _android_device(tmp: Path) -> Device:
    """An Android (MTP) device over the in-process fake backend; `mount` is its base folder on the
    fake's storage, so the contract can look at what arrived."""
    from syncmusic.devices.Android.mtp import AndroidMtp
    from tests.fake_mtp import FakeMtpBackend

    storage = tmp / "mtp-storage"
    storage.mkdir()
    backend = FakeMtpBackend(storage)
    device = AndroidMtp(
        DeviceConfig(
            id="android1",
            type="android",
            name="Phone",
            settings={"usb_serial": "FAKE123", "base_path": "Music/SyncMusic"},
        ),
        storage / "Music" / "SyncMusic",
        None,
    )
    device.backend_factory = lambda settings, *_: backend  # type: ignore[method-assign]
    return device


def _android_libmtp_device(tmp: Path) -> Device:
    """The Android device over the real libmtp backend logic with a fake libmtp underneath."""
    from syncmusic.devices.Android._libmtp import LibMtpBackend
    from syncmusic.devices.Android.mtp import AndroidMtp
    from tests.fake_libmtp import FakeLibMtp, FakePlayer

    storage = tmp / "mtp-storage"
    storage.mkdir()
    lib = FakeLibMtp(FakePlayer("FAKE123", {"Internal shared storage": storage}))
    device = AndroidMtp(
        DeviceConfig(
            id="android1",
            type="android",
            name="Phone",
            settings={"usb_serial": "FAKE123", "base_path": "Music/SyncMusic"},
        ),
        storage / "Music" / "SyncMusic",
        None,
    )
    device.backend_factory = lambda settings, *_: LibMtpBackend(settings.usb_serial, lib)  # type: ignore[method-assign]
    return device


DEVICES: dict[str, DeviceHarness] = {
    "ipod": DeviceHarness(
        make=_ipod_device,
        make_item=_ipod_item,
        inject_feedback=_ipod_feedback,
        manifest_path=MANIFEST,
        can_format=False,  # needs the data partition of a block device (hardware test)
    ),
    "fake": DeviceHarness(make=_fake_device, manifest_path=MANIFEST),
    # format needs a block device: loop image test and hardware test
    "rockbox": DeviceHarness(
        make=_rockbox_device,
        inject_feedback=_rockbox_feedback,
        manifest_path=MANIFEST,
        can_format=False,
    ),
    "hiby": DeviceHarness(
        make=_hiby_device, inject_feedback=_hiby_feedback, manifest_path=MANIFEST, can_format=False
    ),
    "hiby_compas": DeviceHarness(make=_compas_device, manifest_path=MANIFEST, can_format=False),
    # format needs a block device: covered on loop images by the mount/format tests
    "flashdrive": DeviceHarness(make=_flash_device, manifest_path=MANIFEST, can_format=False),
    # a network device: nothing to format
    "ftp": DeviceHarness(make=_ftp_device, manifest_path=MANIFEST, can_format=False),
    "client": DeviceHarness(make=_client_device, manifest_path=MANIFEST, can_format=False),
    "smb": DeviceHarness(make=_smb_device, manifest_path=MANIFEST, can_format=False),
    # not a block device: nothing to format (fake MTP backend)
    "android": DeviceHarness(make=_android_device, manifest_path=MANIFEST, can_format=False),
    "android-libmtp": DeviceHarness(
        make=_android_libmtp_device, manifest_path=MANIFEST, can_format=False
    ),
}


def _transcode(settings: dict[str, Any]) -> Processor:
    from syncmusic.processors.transcode import Transcode

    return Transcode(settings, None)


def _flac_item(tmp: Path) -> WorkItem:
    from tests.media import make_music, media_work_item

    return media_work_item(make_music(tmp / "src" / "Пісня.flac", title="Пісня"), tmp / "work")


def _mp3_item(tmp: Path) -> WorkItem:
    from tests.media import make_music, media_work_item

    return media_work_item(make_music(tmp / "src" / "Пісня.mp3", title="Пісня"), tmp / "work")


def _retag(settings: dict[str, Any]) -> Processor:
    from syncmusic.processors.retag import Retag

    return Retag(settings, None)


def _wav_item(tmp: Path) -> WorkItem:
    from tests.media import make_music, media_work_item

    return media_work_item(make_music(tmp / "src" / "Пісня.wav"), tmp / "work")


def _covers(settings: dict[str, Any]) -> Processor:
    from syncmusic.processors.covers import Covers

    return Covers(settings, None)


def _mp3_cover_item(tmp: Path) -> WorkItem:
    from tests.media import big_cover, embed_cover, make_music, media_work_item

    path = make_music(tmp / "src" / "Обкладинка.mp3", title="Обкладинка")
    embed_cover(path, big_cover())
    return media_work_item(path, tmp / "work", has_cover=True)


def _lyrics(settings: dict[str, Any]) -> Processor:
    from syncmusic.processors.lyrics import LyricsProcessor

    return LyricsProcessor(settings, None)


def _lyrics_item(tmp: Path) -> WorkItem:
    from syncmusic.core.models import Lyrics

    item = _mp3_item(tmp)
    track = item.track.model_copy(update={"has_lyrics": True})
    return item.model_copy(
        update={
            "plan": item.plan.model_copy(update={"track": track}),
            "lyrics": Lyrics.from_text("[00:01.00]Раз\n[00:02.50]Два\n"),
        }
    )


def _video(settings: dict[str, Any]) -> Processor:
    from syncmusic.processors.video import Video

    return Video(settings, None)


def _video_item(tmp: Path) -> WorkItem:
    from tests.media import make_video, video_work_item

    return video_work_item(
        make_video(tmp / "src" / "Мультик.mkv", seconds=4, noise=True), tmp / "work"
    )


PROCESSORS: dict[str, ProcessorHarness] = {
    "lyrics": ProcessorHarness(
        make=_lyrics,
        supported=_lyrics_item,
        unsupported=_mp3_item,  # no lyrics
        alt_settings={"synced": "embed"},
        size_tolerance=0.05,
    ),
    # the prepared cover's size depends on the image content: plan() can only estimate it
    "covers": ProcessorHarness(
        make=_covers,
        supported=_mp3_cover_item,
        unsupported=_wav_item,
        alt_settings={"max_size": 300},
        size_tolerance=0.5,
    ),
    "retag": ProcessorHarness(
        make=_retag,
        supported=_mp3_item,
        unsupported=_wav_item,
        alt_settings={"id3v23": True},
        size_tolerance=0.05,  # +2 KiB predicted for tags on a 75 KB test file
    ),
    # contract CAPS play mp3/aac/alac: FLAC is converted (fallback → ALAC), MP3 passes through.
    # Pink noise compresses differently from music (ALAC ≈ 0.97 × FLAC instead of 1.03), hence 8 %;
    # the ±5 % ALAC criterion is checked on the test media's long FLACs (tests/integration/test_processor_chain_flac.py).
    "transcode": ProcessorHarness(
        make=_transcode,
        supported=_flac_item,
        unsupported=_mp3_item,
        alt_settings={"rules": [{"from": ["flac"], "to": "aac"}]},
        size_tolerance=0.08,
    ),
    # a 640×480 video with film grain is encoded into the iPod profile at x264's average bitrate
    "video": ProcessorHarness(
        make=_video,
        supported=_video_item,
        unsupported=_mp3_item,
        alt_settings={"video_kbps": 400},
        size_tolerance=0.15,
    ),
    "fake": ProcessorHarness(
        make=lambda settings: FakeTrailer(settings, None),
        supported=lambda tmp: work_item(tmp, "song"),
        unsupported=lambda tmp: work_item(tmp, "song", fmt="flac"),
        alt_settings={"marker": "XYZ"},
    ),
}
