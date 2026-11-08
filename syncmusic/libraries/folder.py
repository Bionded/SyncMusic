# SPDX-License-Identifier: GPL-3.0-or-later
"""Folder library: a local directory tree scanned with mutagen - or a folder on an FTP server (`source: ftp`), read through a local mirror of sparse tag copies (`libraries/remote.py`).

IDs are sha1 of the path relative to the root; per-file tags are cached by (size, mtime) so a rescan only
re-reads changed files. Covers come from embedded pictures or cover/folder/front images next to the file,
lyrics from a `.lrc` sidecar or embedded tags. `source_state` covers the media file and those sidecars.
Not a FeedbackSink in v1.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import posixpath
import re
from collections import defaultdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

import mutagen
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from syncmusic.base.capabilities import LibraryCapabilities
from syncmusic.base.library import Library
from syncmusic.core.models import (
    HealthStatus,
    ItemRef,
    LibrarySnapshot,
    Lyrics,
    MediaKind,
    Playlist,
    Track,
)
from syncmusic.core.thumbs import embedded_cover
from syncmusic.infra import ffmpeg
from syncmusic.libraries.remote import FtpSource, MirrorSource, RemoteError, SmbSource

VIDEO_EXTS = frozenset({".m4v", ".mp4", ".mov", ".mkv", ".avi", ".webm"})
MEDIA_EXTS = VIDEO_EXTS | frozenset(
    {
        ".mp3",
        ".flac",
        ".m4a",
        ".m4b",
        ".mp4",
        ".aac",
        ".ogg",
        ".oga",
        ".opus",
        ".wav",
        ".aif",
        ".aiff",
        ".wma",
        ".wv",
        ".ape",
    }
)
COVER_NAMES = ("cover", "folder", "front")
COVER_EXTS = (".jpg", ".jpeg", ".png")
PLAYLIST_EXTS = (".m3u", ".m3u8")
SIDE_EXTS = frozenset(
    {".lrc", *COVER_EXTS, *PLAYLIST_EXTS}
)  # what a server folder's mirror holds besides media

log = logging.getLogger(__name__)

# (size, mtime_ns) of one file
FileStat = tuple[int, int]


class FolderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # where the folder is; local: `root`; ftp / smb: server, port (empty: 21 / 445), login (empty
    # user: anonymous / guest), the folder on the server (smb: "/<share>/<folder>"), explicit FTPS
    source: Literal["local", "ftp", "smb"] = "local"
    root: Path | None = None
    host: str = ""
    port: int | None = Field(None, ge=1, le=65535)
    username: str = ""
    password: SecretStr = SecretStr("")
    remote_path: str = "/"
    tls: bool = False
    default_kind: MediaKind = MediaKind.MUSIC
    # relative subfolder → kind, e.g. {"Podcasts": "podcast"}; the longest matching prefix wins
    subfolder_kinds: dict[str, MediaKind] = {}
    genre_separators: str = ";/"
    # a local folder: react to changes within ~30 s instead of the periodic refresh; ftp/smb poll
    watch: bool = True

    @field_validator("root", mode="before")
    @classmethod
    def _absolute_root(cls, v: Any) -> Any:
        """'' would be Path('.'): the daemon's working directory."""
        if v is None:
            return v
        if isinstance(v, str | Path) and not str(v).strip():
            raise ValueError("must not be empty")
        if isinstance(v, str | Path) and not Path(v).is_absolute():
            raise ValueError("must be an absolute path")
        return v

    @model_validator(mode="after")
    def _where(self) -> FolderSettings:
        if self.source == "local" and self.root is None:
            raise ValueError("root: the folder is required")
        if self.source != "local":
            host = self.host.strip()
            if not host or "/" in host or " " in host:
                raise ValueError("host: a host name or IP address (no scheme, no path)")
            self.host = host
        if self.source == "smb" and not [p for p in self.remote_path.split("/") if p]:
            raise ValueError("remote_path: /<share>/<folder> (at least the share)")
        return self


MOUNTINFO = Path("/proc/self/mountinfo")
BY_UUID = Path("/dev/disk/by-uuid")


def _unescape_mount(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _fs_uuid(dev: int) -> str | None:
    """The UUID of the filesystem on device number `dev` (udev's /dev/disk/by-uuid links)."""
    try:
        for link in BY_UUID.iterdir():
            if os.stat(link).st_rdev == dev:
                return link.name
    except OSError:
        pass
    return None


def fs_location(path: Path) -> tuple[str, str] | None:
    """(filesystem UUID, the path inside that filesystem) of a local folder: the same for every mount
    point or bind mount of one filesystem. None when the filesystem has no UUID (tmpfs, overlay, ...)."""
    try:
        path = path.resolve()
        dev = os.stat(path).st_dev
        lines = MOUNTINFO.read_text().splitlines()
    except OSError:
        return None
    best: tuple[str, str] | None = None  # (mount point, root of the mount inside the fs)
    for line in lines:
        f = line.split()
        if len(f) < 5 or f[2] != f"{os.major(dev)}:{os.minor(dev)}":
            continue
        point, root = _unescape_mount(f[4]), _unescape_mount(f[3])
        if path.is_relative_to(point) and (best is None or len(point) > len(best[0])):
            best = (point, root)
    if best is None:
        return None
    uuid = _fs_uuid(dev)
    if uuid is None:
        return None
    inside = posixpath.normpath(posixpath.join(best[1], str(path.relative_to(best[0]))))
    return uuid, inside


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def _undecodable(name: str) -> bool:
    """True for a name with bytes that are not UTF-8 (os.fsdecode keeps them as lone surrogates)."""
    try:
        name.encode()
    except UnicodeEncodeError:
        return True
    return False


def _natural_key(name: str) -> list[Any]:
    return [
        (0, int(p), "") if p.isdigit() else (1, 0, p.casefold())
        for p in re.split(r"(\d+)", name)
        if p
    ]


def _first_int(value: str | None) -> int | None:
    m = re.match(r"\s*(\d+)", value or "")
    return int(m.group(1)) or None if m else None


def _disc_total(discnumber: str | None, total: str | None) -> int | None:
    """From "2/3" or a separate disc-total tag."""
    m = re.match(r"\s*\d+\s*/\s*(\d+)", discnumber or "")
    return int(m.group(1)) or None if m else _first_int(total)


def _year(value: str | None) -> int | None:
    m = re.search(r"\b(\d{4})\b", value or "")
    return int(m.group(1)) if m else None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.strip())
    except ValueError:
        try:
            d = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _format_of(audio: Any, ext: str) -> str:
    name = type(audio).__name__
    if name == "MP3":
        return "mp3"
    if name == "FLAC":
        return "flac"
    if name == "MP4":
        return "alac" if str(getattr(audio.info, "codec", "")).startswith("alac") else "aac"
    if name == "OggOpus":
        return "opus"
    if name == "OggVorbis":
        return "ogg"
    if name == "WAVE":
        return "wav"
    if name == "AIFF":
        return "aiff"
    if name == "ASF":
        return "wma"
    return ext.lstrip(".")


def _embedded_lyrics(audio: Any) -> Lyrics | None:
    tags = audio.tags
    if tags is None:
        return None
    if hasattr(tags, "getall"):  # ID3
        for frame in tags.getall("SYLT"):
            lines = [(ms / 1000, text) for text, ms in frame.text]
            if lines:
                return Lyrics(synced=True, lines=lines, language=frame.lang or None)
        for frame in tags.getall("USLT"):
            if frame.text.strip():
                return Lyrics.from_text(frame.text)
        return None
    for key in ("©lyr", "lyrics", "unsyncedlyrics", "LYRICS", "UNSYNCEDLYRICS"):
        try:
            value = tags.get(key)
        except (KeyError, ValueError):
            continue
        if value:
            text = str(value[0]) if isinstance(value, list) else str(value)
            if text.strip():
                return Lyrics.from_text(text)
    return None


class FolderLibrary(Library):
    type_name = "folder"
    display_name = "Folder"
    Settings: ClassVar[type[BaseModel]] = FolderSettings
    capabilities = LibraryCapabilities(
        kinds={MediaKind.MUSIC, MediaKind.PODCAST, MediaKind.AUDIOBOOK}
    )

    def __init__(self, config: Any, ctx: Any) -> None:
        super().__init__(config, ctx)
        self._tag_cache: dict[str, tuple[FileStat, Track]] = {}
        self._paths: dict[str, str] = {}  # item_id -> relative path
        s: FolderSettings = self.settings  # type: ignore[assignment]
        self.remote: MirrorSource | None = None
        cache = Path(ctx.cache_dir) if ctx is not None else Path("/var/tmp/syncmusic-mirror")
        mirror = cache / "library-mirror" / self.id
        user, password = s.username, s.password.get_secret_value()
        if s.source == "ftp":
            self.remote = FtpSource(
                host=s.host,
                port=s.port or 21,
                username=user,
                password=password,
                path=s.remote_path,
                tls=s.tls,
                mirror=mirror,
            )
        elif s.source == "smb":
            self.remote = SmbSource(
                host=s.host,
                port=s.port or 445,
                username=user,
                password=password,
                path=s.remote_path,
                mirror=mirror,
            )

    async def source_id(self) -> str | None:
        """`folder:<fs uuid>:<path inside the fs>` for a local folder, `folder:<smb|ftp>://host[:port]/path`
        (no credentials) for a server folder."""
        s: FolderSettings = self.settings  # type: ignore[assignment]
        if s.source != "local":
            port = f":{s.port}" if s.port else ""
            path = "/" + "/".join(p for p in s.remote_path.split("/") if p)
            return f"folder:{s.source}://{s.host.lower()}{port}{path}"
        assert s.root is not None
        where = await asyncio.to_thread(fs_location, s.root)
        return f"folder:{where[0]}:{where[1]}" if where else None

    @property
    def root(self) -> Path:
        """The folder scanned: the local folder, or the server folder's mirror in the cache."""
        if self.remote is not None:
            return self.remote.mirror
        root: Path = self.settings.root  # type: ignore[attr-defined]
        return root

    # --- walking ------------------------------------------------------------------------------

    def _walk(self) -> dict[str, FileStat]:
        """All non-hidden files below the root: relative posix path → (size, mtime_ns).

        Raises FileNotFoundError when the root is missing (e.g. an unmounted disk): an empty result
        would make the planner delete everything from the device. A server folder is listed on the server
        (RemoteError when it cannot be read, for the same reason).
        """
        if self.remote is not None:
            return self.remote.walk()
        if not self.root.is_dir():
            raise FileNotFoundError(f"library folder {self.root} does not exist")
        out: dict[str, FileStat] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            rel_dir = os.path.relpath(dirpath, self.root)
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                st = os.stat(os.path.join(dirpath, name))
                rel = name if rel_dir == "." else f"{Path(rel_dir).as_posix()}/{name}"
                out[rel] = (st.st_size, st.st_mtime_ns)
        return out

    @staticmethod
    def _token(files: dict[str, FileStat]) -> str:
        h = hashlib.sha1()
        for rel, (size, mtime) in sorted(files.items()):
            h.update(f"{rel}\0{size}\0{mtime}\n".encode(errors="surrogateescape"))
        return h.hexdigest()

    def _kind_for(self, rel: str) -> MediaKind:
        best, best_len = self.settings.default_kind, -1  # type: ignore[attr-defined]
        for prefix, kind in self.settings.subfolder_kinds.items():  # type: ignore[attr-defined]
            p = prefix.strip("/")
            if (rel == p or rel.startswith(p + "/")) and len(p) > best_len:
                best, best_len = kind, len(p)
        return best

    def _sidecars(self, rel: str, names: list[str] | None = None) -> tuple[str | None, str | None]:
        """(lyrics .lrc, folder cover image) relative paths for a media file, if present.

        `names` are the file names in the file's folder (listed from disk when omitted).
        """
        parent, _, name = rel.rpartition("/")
        if names is None:
            names = sorted(os.listdir(self.root / parent))
        stem = os.path.splitext(name)[0]
        lrc = stem + ".lrc" if stem + ".lrc" in names else None
        cover = next(
            (
                n
                for n in names
                if os.path.splitext(n)[0].casefold() in COVER_NAMES
                and os.path.splitext(n)[1].casefold() in COVER_EXTS
            ),
            None,
        )
        join = (lambda n: f"{parent}/{n}") if parent else (lambda n: n)
        return (join(lrc) if lrc else None), (join(cover) if cover else None)

    # --- tags ----------------------------------------------------------------------------------

    def _read_video(
        self, rel: str, stat: FileStat, sidecars: tuple[str | None, str | None]
    ) -> Track | None:
        """A video file from ffprobe: title, show / season / episode from its tags (MP4 `tvsh` /
        `tvsn` / `tves`) or its folders ("Show/Season 2/S02E05 Title.mkv"), size, codec, frame size. None: the file
        has no video stream (an audio-only .mp4 is read as audio)."""
        info = ffmpeg.probe_json(self.root / rel)
        streams = info.get("streams") or []
        video = next(
            (
                s
                for s in streams
                if s.get("codec_type") == "video"
                and not (s.get("disposition") or {}).get("attached_pic")
            ),
            None,
        )
        if video is None:
            return None
        fmt = info.get("format") or {}
        tags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
        parts = rel.split("/")
        name = os.path.splitext(parts[-1])[0]
        se = re.search(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})", name)
        season_dir = next(
            (
                i
                for i, p in enumerate(parts[:-1])
                if re.fullmatch(r"(?i)(season|сезон|staffel)\s*\d+", p)
            ),
            None,
        )
        season = _first_int(tags.get("season_number")) or (int(se.group(1)) if se else None)
        if season is None and season_dir is not None:
            season = _first_int(re.sub(r"\D+", " ", parts[season_dir]))
        episode = _first_int(tags.get("episode_sort")) or (int(se.group(2)) if se else None)
        show = tags.get("show") or (
            parts[season_dir - 1] if season_dir else (parts[-2] if se and len(parts) > 1 else None)
        )
        title = (
            tags.get("title")
            or (re.sub(r"^[ ._-]+|[ ._-]+$", "", name[se.end() :]) if se else "")
            or name
        )
        size, mtime_ns = stat
        mtime = datetime.fromtimestamp(mtime_ns / 1e9, UTC)
        duration = fmt.get("duration")
        extra: dict[str, Any] = {
            k: v
            for k, v in {
                "show": show,
                "season": season,
                "episode": episode,
                "width": video.get("width"),
                "height": video.get("height"),
            }.items()
            if v is not None
        }
        return Track(
            ref=ItemRef(library_id=self.id, item_id=_sha1(rel)),
            kind=MediaKind.VIDEO,
            title=title,
            artist=tags.get("artist"),
            album=tags.get("album"),
            year=_year(tags.get("date")),
            genres=[g for g in [tags.get("genre")] if g],
            duration_s=float(duration) if duration else None,
            size_bytes=size,
            format=os.path.splitext(rel)[1].lower().lstrip("."),
            codec=video.get("codec_name"),
            bitrate_kbps=int(fmt["bit_rate"]) // 1000
            if str(fmt.get("bit_rate", "")).isdigit()
            else None,
            date_added=mtime,
            date_modified=mtime,
            path=rel,
            series=show,
            has_cover=sidecars[1] is not None,
            source_state=f"{size}:{mtime_ns}",
            extra=extra,
        )

    def _read_track(
        self, rel: str, stat: FileStat, sidecars: tuple[str | None, str | None]
    ) -> Track:
        path = self.root / rel
        ext = path.suffix.lower()
        if ext in VIDEO_EXTS:
            video = self._read_video(rel, stat, sidecars)
            if video is not None:
                return video
        kind = self._kind_for(rel)
        if kind is MediaKind.VIDEO:  # a folder set to video: its audio files are still music
            kind = MediaKind.MUSIC
        item_id = _sha1(rel)
        audio = mutagen.File(path)
        easy = mutagen.File(path, easy=True)
        tags = easy.tags if easy is not None and easy.tags is not None else {}

        def tag(key: str) -> str | None:
            try:
                values = tags.get(key)
            except (KeyError, ValueError):
                return None
            return str(values[0]).strip() or None if values else None

        seps = self.settings.genre_separators  # type: ignore[attr-defined]
        genres: list[str] = []
        for g in tags.get("genre", []) if tags else []:
            for part in re.split(f"[{re.escape(seps)}]", str(g)) if seps else [str(g)]:
                if part.strip() and part.strip() not in genres:
                    genres.append(part.strip())

        lrc, cover = sidecars
        info = getattr(audio, "info", None)
        title = tag("title") or path.stem
        artist = tag("artist")
        album_artist = tag("albumartist")
        album = tag("album")
        size, mtime_ns = stat
        mtime = datetime.fromtimestamp(mtime_ns / 1e9, UTC)
        track = Track(
            ref=ItemRef(library_id=self.id, item_id=item_id),
            kind=kind,
            title=title,
            artist=artist,
            album_artist=album_artist,
            album=album,
            album_id=_sha1(f"{album_artist or artist or ''}|{album}") if album else None,
            artist_id=_sha1(artist) if artist else None,
            track_no=_first_int(tag("tracknumber")),
            disc_no=_first_int(tag("discnumber")),
            disc_total=_disc_total(tag("discnumber"), tag("disctotal") or tag("totaldiscs")),
            year=_year(tag("date") or tag("originaldate")),
            genres=genres,
            duration_s=float(info.length) if info is not None and info.length else None,
            size_bytes=size,
            format=_format_of(audio, ext) if audio is not None else ext.lstrip("."),
            codec=str(getattr(info, "codec", "")) or None if info is not None else None,
            bitrate_kbps=int(info.bitrate) // 1000 if getattr(info, "bitrate", 0) else None,
            sample_rate=getattr(info, "sample_rate", None) or None,
            bit_depth=getattr(info, "bits_per_sample", None) or None,
            channels=getattr(info, "channels", None) or None,
            date_added=mtime,
            date_modified=mtime,
            compilation=(tag("compilation") or "0") not in ("0", "false", ""),
            comment=tag("comment") if kind is MediaKind.MUSIC else None,
            path=rel,
            has_cover=cover is not None
            or (audio is not None and embedded_cover(audio) is not None),
            has_lyrics=lrc is not None
            or (audio is not None and _embedded_lyrics(audio) is not None),
            source_state=f"{size}:{mtime_ns}",
        )
        if kind is not MediaKind.MUSIC:
            folder = os.path.basename(os.path.dirname(rel)) or None
            track.series = album or folder
            track.author = tag("composer") or album_artist or artist
            if kind is MediaKind.PODCAST:
                track.published = _parse_date(tag("date"))
        return track

    def _scan(self) -> LibrarySnapshot:
        files = self._walk()
        if self.remote is not None:  # what mutagen and the sidecar lookups read
            self.remote.sync_mirror(
                files, lambda rel: os.path.splitext(rel)[1].lower() in MEDIA_EXTS | SIDE_EXTS
            )
        by_dir: dict[str, list[str]] = defaultdict(list)
        for rel in files:
            parent, _, name = rel.rpartition("/")
            by_dir[parent].append(name)
        tracks: dict[str, Track] = {}
        cache: dict[str, tuple[Any, Track]] = {}
        for rel, stat in files.items():
            if os.path.splitext(rel)[1].lower() not in MEDIA_EXTS:
                continue
            if _undecodable(rel):
                log.warning("skipping %r: file name is not valid UTF-8", rel)
                continue
            sidecars = self._sidecars(rel, by_dir[rel.rpartition("/")[0]])
            key = (stat, sidecars)  # a new/removed .lrc or cover changes has_lyrics/has_cover
            cached = self._tag_cache.get(rel)
            if cached is not None and cached[0] == key:
                track = cached[1].model_copy(deep=True)
            else:
                try:
                    track = self._read_track(rel, stat, sidecars)
                except (mutagen.MutagenError, OSError, ffmpeg.FfmpegError, ValueError):
                    continue  # unreadable file: skip, like Navidrome does
            cache[rel] = (key, track)
            track.source_state = self._source_state(stat, sidecars, files)
            tracks[track.ref.item_id] = track
        self._assign_chapters(tracks)
        self._tag_cache = cache
        self._paths = {t.ref.item_id: t.path or "" for t in tracks.values()}
        playlists = {
            pl.ref.item_id: pl
            for rel in files
            if os.path.splitext(rel)[1].lower() in PLAYLIST_EXTS and not _undecodable(rel)
            if (pl := self._read_playlist(rel, tracks)) is not None
        }
        return LibrarySnapshot(
            library_id=self.id, state_token=self._token(files), tracks=tracks, playlists=playlists
        )

    def _read_playlist(self, rel: str, tracks: dict[str, Track]) -> Playlist | None:
        """An .m3u / .m3u8 file below the root: entries are paths relative to the playlist's folder or
        absolute paths inside the root; URLs, entries outside the root and files the library does not have are
        left out (one warning per playlist). Name: `#PLAYLIST:` or the file name."""
        try:
            raw = (self.root / rel).read_bytes()
        except OSError:
            return None
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")  # old .m3u files are not always UTF-8
        name = os.path.splitext(rel.rpartition("/")[2])[0]
        parent = rel.rpartition("/")[0]
        root = self.root.as_posix().rstrip("/")
        refs: list[ItemRef] = []
        dropped: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("#PLAYLIST:") and line[10:].strip():
                name = line[10:].strip()
            if not line or line.startswith("#") or "://" in line:
                continue
            entry = line.replace("\\", "/")
            if entry.startswith("/"):
                if not entry.startswith(root + "/"):
                    dropped.append(f"{entry} (outside the library)")
                    continue
                entry = entry[len(root) + 1 :]
            else:
                entry = posixpath.join(parent, entry) if parent else entry
            entry = posixpath.normpath(entry)
            item_id = _sha1(entry)
            if item_id in tracks:
                refs.append(tracks[item_id].ref)
            else:
                dropped.append(entry)
        if dropped:
            # one line per playlist and snapshot, like the On-The-Go rule of
            log.warning(
                "playlist %s (%s): %d entries left out, no such file in the library: %s",
                name,
                rel,
                len(dropped),
                ", ".join(dropped[:10]) + (", ..." if len(dropped) > 10 else ""),
            )
        return Playlist(
            ref=ItemRef(library_id=self.id, item_id=_sha1(rel)), name=name, track_refs=refs
        )

    @staticmethod
    def _source_state(
        stat: FileStat, sidecars: tuple[str | None, str | None], files: dict[str, FileStat]
    ) -> str:
        """size:mtime of the media file plus the sidecars the library serves (.lrc, folder cover), so
        editing, adding or removing one of them replaces the device copy."""
        state = f"{stat[0]}:{stat[1]}"
        for label, rel in zip(("lrc", "cover"), sidecars, strict=True):
            if rel is not None:
                size, mtime = files[rel]
                state += f"|{label}={rel.rpartition('/')[2]}:{size}:{mtime}"
        return state

    @staticmethod
    def _assign_chapters(tracks: dict[str, Track]) -> None:
        """Audiobook chapter order per folder: disc, track number, then natural file-name order."""
        books: dict[str, list[Track]] = defaultdict(list)
        for t in tracks.values():
            if t.kind is MediaKind.AUDIOBOOK:
                books[os.path.dirname(t.path or "")].append(t)
        for chapters in books.values():
            chapters.sort(
                key=lambda t: (
                    t.disc_no or 0,
                    t.track_no if t.track_no is not None else 1 << 30,
                    _natural_key(os.path.basename(t.path or "")),
                )
            )
            for i, t in enumerate(chapters, 1):
                t.chapter_index = i

    # --- Library API ----------------------------------------------------------------------------

    async def check(self) -> HealthStatus:
        if self.remote is not None:
            try:
                await asyncio.to_thread(self.remote.check)
            except RemoteError as e:
                return HealthStatus(ok=False, message=str(e))
            return HealthStatus(ok=True)
        if not self.root.is_dir():
            return HealthStatus(ok=False, message=f"folder {self.root} does not exist")
        if not os.access(self.root, os.R_OK | os.X_OK):
            return HealthStatus(ok=False, message=f"folder {self.root} is not readable")
        return HealthStatus(ok=True)

    async def state_token(self) -> str:
        return self._token(await asyncio.to_thread(self._walk))

    async def snapshot(self) -> LibrarySnapshot:
        return await asyncio.to_thread(self._scan)

    def _path_of(self, item_id: str) -> Path:
        rel = self._paths.get(item_id)
        if rel is None:
            raise KeyError(f"unknown item {item_id!r} in library {self.id!r} (snapshot first)")
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise KeyError(f"item {item_id!r} resolves outside the library root")
        return path

    async def open_media(self, item_id: str) -> Path:
        if not self._paths:
            await self.snapshot()
        if (
            self.remote is not None
        ):  # the whole file, downloaded now (the mirror has a tag copy only)
            self._path_of(item_id)
            return await asyncio.to_thread(self.remote.full, self._paths[item_id])
        return self._path_of(item_id)

    async def _tag_path(self, item_id: str) -> Path:
        """A file to read tags from: the media file, or a server file's tag copy in the mirror."""
        if not self._paths:
            await self.snapshot()
        return self._path_of(item_id)

    async def open_cover(self, item_id: str) -> bytes | None:
        path = await self._tag_path(item_id)

        def read() -> bytes | None:
            audio = mutagen.File(path)
            data = embedded_cover(audio) if audio is not None else None
            if data:
                return data
            _, cover = self._sidecars(self._paths[item_id])
            return (self.root / cover).read_bytes() if cover else None

        return await asyncio.to_thread(read)

    async def get_lyrics(self, item_id: str) -> Lyrics | None:
        path = await self._tag_path(item_id)

        def read() -> Lyrics | None:
            lrc, _ = self._sidecars(self._paths[item_id])
            if lrc:
                return Lyrics.from_text(
                    (self.root / lrc).read_text(encoding="utf-8-sig", errors="replace")
                )
            audio = mutagen.File(path)
            return _embedded_lyrics(audio) if audio is not None else None

        return await asyncio.to_thread(read)
