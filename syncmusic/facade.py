# SPDX-License-Identifier: GPL-3.0-or-later
"""AppFacade: the single API used by the REST server and (via REST) the web UI and the CLI."""

from __future__ import annotations

import asyncio
import gc
import getpass
import json
import logging
import os
import platform
import re
import shutil
import socket
import threading
import time
import uuid
from collections import Counter, deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from pydantic import ValidationError

from syncmusic import __version__
from syncmusic.app import Application
from syncmusic.base.device import Device
from syncmusic.core import (
    backup,
    chain_lint,
    chain_preview,
    integrity_jobs,
    plan_view,
    preview,
    quality,
    registry,
    rule_test,
    thumbs,
    transfer,
    update_status,
)
from syncmusic.core import profile as pf
from syncmusic.core.activity import report_activity
from syncmusic.core.fit import FitTarget
from syncmusic.core.models import (
    AppSettings,
    DeviceConfig,
    LibraryConfig,
    LibrarySnapshot,
    MediaKind,
    ProcessorSlot,
    ProcessorTemplate,
    ReverseSyncSettings,
    Rule,
    RuleSet,
    RuleTemplate,
    SyncReport,
    SyncSettings,
    Track,
    utcnow,
)
from syncmusic.core.playlist_reverse import Policy
from syncmusic.core.prepare import Chain
from syncmusic.core.proc_templates import (
    DEFAULT_PROCESSOR_TEMPLATES,
    linked_devices,
    same_chain,
    with_tag_step,
)
from syncmusic.core.rules import RuleError, ruleset_from_text, validate_ruleset
from syncmusic.core.rules.defaults import PREFIX as DEFAULT_PREFIX
from syncmusic.core.rules.evaluator import resolve_rules
from syncmusic.core.rules.fields import ALL_FIELDS, FIELDS
from syncmusic.core.rules.presets import PRESETS, available, follow_name
from syncmusic.core.rules.templates import (
    IdField,
    add_to_manual_selection,
    as_rules,
    manual_ids,
    own_template_id,
    remove_from_manual_selection,
    template_usage,
)
from syncmusic.core.rules.text import pydantic_message, ruleset_to_yaml
from syncmusic.core.suggest import suggest
from syncmusic.core.sync import (
    FirstSync,
    RegisterRequest,
    SyncError,
    plan_summary,
    suggested_processors,
)
from syncmusic.core.thumbs import file_cover
from syncmusic.infra import (
    bundle,
    dbbackup,
    diagnostics,
    discovery,
    doctor,
    ffmpeg,
    self_update,
    storage,
)
from syncmusic.infra.blockdev.format import FS_LABELS, filesystem_support
from syncmusic.infra.blockdev.guard import GuardError
from syncmusic.infra.blockdev.mount import privilege_route
from syncmusic.infra.discovery import Kind
from syncmusic.infra.events import Event
from syncmusic.infra.ffmpeg import FfmpegError
from syncmusic.infra.housekeeping import disk_free
from syncmusic.infra.log import LEVELS, matches, parse_log
from syncmusic.infra.notify import WebhookNotifier
from syncmusic.libraries import browse

log = logging.getLogger(__name__)

PAGE_SIZE_MAX = 500
SEARCH_LIMIT = 5  # hits per group of the global search
# secrets are write-only in responses: a set secret is returned as this mask, and a PATCH that sends the
# mask back (or leaves the key out) keeps the stored value
SECRET_MASK = "********"
MIN_SUGGEST_CHARS = 3  # autocomplete starts after more than 2 typed characters
# autocomplete waits at most this long for a library's snapshot; a slower load goes on in the background and the
# library's last known snapshot (if any) is used meanwhile; a failed load is not retried for SUGGEST_RETRY_S
SUGGEST_WAIT_S = 1.0
SUGGEST_RETRY_S = 30.0
# the system cache in Settings; the media cache is infra/cache.py's `entries`
SYSTEM_CACHE_DIRS = ("thumbs", "preview", chain_preview.FOLDER)
WARM_PAUSE_S = 0.02  # between two rows of a thumbnail warm-up


def _slug(name: str) -> str:
    """Template id from its name: words (any script) joined by '-'."""
    return re.sub(r"[\W_]+", "-", name.casefold()).strip("-")[:32] or "template"


def _schema(cls: Any) -> dict[str, Any]:
    schema: dict[str, Any] = cls.Settings.model_json_schema()
    return schema


def _secret_keys(library_type: str) -> set[str]:
    """Settings keys of a library type that hold secrets (`SecretStr`: writeOnly in its JSON schema)."""
    try:
        props = _schema(registry.library_class(library_type)).get("properties", {})
    except KeyError:
        return set()
    return {k for k, v in props.items() if v.get("writeOnly")}


def _device_secret_keys(device_type: str) -> set[str]:
    """Settings keys of a device type that hold secrets (the FTP password)."""
    try:
        props = _schema(registry.device_class(device_type)).get("properties", {})
    except KeyError:
        return set()
    return {k for k, v in props.items() if v.get("writeOnly")}


def _plain(criteria: Any) -> Any:
    """Criteria without one-item `all` wrappers, which the rule builder adds around a single condition."""
    if isinstance(criteria, dict):
        if (
            set(criteria) == {"all"}
            and isinstance(criteria["all"], list)
            and len(criteria["all"]) == 1
        ):
            return _plain(criteria["all"][0])
        return {k: _plain(v) for k, v in criteria.items()}
    if isinstance(criteria, list):
        return [_plain(x) for x in criteria]
    return criteria


def _same_rules(a: RuleTemplate, b: RuleTemplate) -> bool:
    """Same name, libraries, rules and excludes (criteria compared without the builder's wrappers)."""

    def content(t: RuleTemplate) -> Any:
        data = t.model_dump(mode="json", include={"name", "library_ids", "rules", "excludes"})
        for r in (*data["rules"], *data["excludes"]):
            r["criteria"] = _plain(r["criteria"])
        return data

    return bool(content(a) == content(b))


def _secret_value(v: Any) -> str:
    """The clear text of a stored secret (a `SecretStr` or a plain string; empty when unset)."""
    if v is None:
        return ""
    return str(v.get_secret_value() if hasattr(v, "get_secret_value") else v)


def _type_unavailable(device_type: str) -> str | None:
    try:
        return registry.device_class(device_type).unavailable_reason()
    except Exception:  # noqa: BLE001 - an unknown type is reported by the register itself
        return None


def _public_device(cfg: DeviceConfig) -> dict[str, Any]:
    """A device config as the API returns it: secrets as the mask (like libraries)."""
    out = cfg.model_dump(mode="json")
    for key in _device_secret_keys(cfg.type) & set(out["settings"]):
        if out["settings"][key]:
            out["settings"][key] = SECRET_MASK
    return out


# sync settings only the stock iPod uses (devices/iPod/ipod.py backup_db)
IPOD_ONLY_SYNC_FIELDS = ("backup_device_db", "backup_keep")


def _general_schema() -> dict[str, Any]:
    full = DeviceConfig.model_json_schema()
    keys = (
        "update_sync",
        "update_every_min",
        "update_removals",
        "update_remove_played",
        "on_connect",
        "sync_at",
        "delete_threshold_pct",
        "remove_deleted",
    )
    return {"properties": {k: full["properties"][k] for k in keys}, "$defs": full.get("$defs", {})}


# gentle write mode: disk devices only, not network ones such as FTP
GENTLE_SYNC_FIELDS = ("gentle_write", "gentle_batch_files", "gentle_pause_s", "gentle_rate_mb_s")


def _sync_schema(device_type: str) -> dict[str, Any]:
    """SyncSettings' schema without the fields the device type does not use."""
    full = SyncSettings.model_json_schema()
    drop = set() if device_type == "ipod" else set(IPOD_ONLY_SYNC_FIELDS)
    if registry.device_class(device_type).network:
        drop |= set(GENTLE_SYNC_FIELDS)
    if not _edits_playlists(device_type):
        drop.add("playlist_conflicts")
    props = {k: v for k, v in full["properties"].items() if k not in drop}
    return {**full, "properties": props}


def _edits_playlists(device_type: str) -> bool:
    """Its playlists can be edited on the player and go back (Rockbox, HiBy)."""
    return registry.device_class(device_type).read_playlists is not Device.read_playlists


def _reverse_sync_schema(device_type: str) -> dict[str, Any]:
    full = ReverseSyncSettings.model_json_schema()
    if _edits_playlists(device_type):
        return full
    props = {k: v for k, v in full["properties"].items() if k != "playlists"}
    return {**full, "properties": props}


def _mask_url(v: str) -> str:
    """A URL without the credentials, query and fragment it carries: a library saved may still hold
    `user:pass@` or `?token=` in its url; it is never shown in clear. Other values pass unchanged."""
    try:
        parts = urlsplit(v)
        hostinfo = parts.netloc.rpartition("@")[2]
        if parts.scheme not in ("http", "https") or not hostinfo:
            return v
    except ValueError:
        return v
    if "@" not in parts.netloc and not parts.query and not parts.fragment:
        return v
    netloc = f"{SECRET_MASK}@{hostinfo}" if "@" in parts.netloc else hostinfo
    out = f"{parts.scheme}://{netloc}{parts.path}"
    out += f"?{SECRET_MASK}" if parts.query else ""
    return out + (f"#{SECRET_MASK}" if parts.fragment else "")


def _public_library(cfg: LibraryConfig) -> dict[str, Any]:
    out = cfg.model_dump(mode="json")
    for key in _secret_keys(cfg.type) & set(out["settings"]):
        if out["settings"][key]:
            out["settings"][key] = SECRET_MASK
    for key, value in out["settings"].items():
        if isinstance(value, str) and key not in _secret_keys(cfg.type):
            out["settings"][key] = _mask_url(value)
    for account in out["accounts"].values():  # its extra accounts
        for key in _secret_keys(cfg.type) & set(account["settings"]):
            if account["settings"][key]:
                account["settings"][key] = SECRET_MASK
    return out


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip("\x00\n ") or None
    except OSError:
        return None


def _uptime_s() -> int | None:
    text = _read_text(Path("/proc/uptime"))
    try:
        return int(float(text.split()[0])) if text else None
    except ValueError:
        return None


FACTORY_RESET_PHRASE = "reset syncmusic"


def _remove_app_data(config: Any) -> None:
    """The database (with its WAL files), the device state of network devices and the cache's folders."""
    for f in config.data_dir.glob("syncmusic.sqlite3*"):
        f.unlink(missing_ok=True)
    shutil.rmtree(config.data_dir / "network", ignore_errors=True)
    for sub in ("entries", "work", "thumbs", "preview"):
        shutil.rmtree(config.cache_dir / sub, ignore_errors=True)


def _public_settings(settings: AppSettings) -> dict[str, Any]:
    out = settings.model_dump(mode="json")
    if out["webhook_url"]:
        out["webhook_url"] = SECRET_MASK  # may carry a token
    return out


class AppFacade:
    def __init__(self, app: Application) -> None:
        self.app = app
        self._suggest_loads: dict[str, asyncio.Task[LibrarySnapshot | None]] = {}
        self._suggest_failed: dict[
            str, float
        ] = {}  # library id → monotonic time of the last failed load
        # device id → (contents ETag, the background task making its rows' thumbnails)
        self._thumb_warm: dict[str, tuple[str, asyncio.Task[None]]] = {}
        self._thumb_warm_lock = asyncio.Lock()  # one device's warm-up at a time
        self._player_locks: dict[str, threading.Lock] = {}  # USB serial → one MTP session at a time

    async def health(self) -> dict[str, Any]:
        db_ok = True
        try:
            await self.app.db.fetchone("SELECT 1")
        except Exception:
            db_ok = False
        return {"status": "ok" if db_ok else "degraded", "db": db_ok, "mode": self.app.config.mode}

    def version(self) -> dict[str, Any]:
        """For Settings → About: SyncMusic, pygpod and Python versions, the host's name, its
        hardware model (device tree, None elsewhere) and uptime in seconds (None when unknown)."""
        try:
            pygpod = package_version("pygpod")
        except PackageNotFoundError:
            pygpod = "not installed"
        return {
            "version": __version__,
            "pygpod": pygpod,
            "python": platform.python_version(),
            "host": socket.gethostname(),
            "instance": {"id": self.app.instance_id, "name": self.app.instance_name},
            "model": _read_text(Path("/proc/device-tree/model")),
            "uptime_s": _uptime_s(),
        }

    # --- events (the UI's live updates; REST clients use GET /events) ----------------------------------

    def subscribe(self, queue_size: int) -> asyncio.Queue[Event]:
        return self.app.bus.subscribe(queue_size)

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self.app.bus.unsubscribe(queue)

    # --- libraries (registration; full management in M8) ----------------------------------------

    async def libraries(self) -> list[dict[str, Any]]:
        return [self._with_updated(_public_library(c)) for c in await self.app.store.libraries()]

    def _with_updated(self, lib: dict[str, Any]) -> dict[str, Any]:
        """+ `updated_at`: when the library last confirmed the browsing cache (null: nothing loaded);
        + `counts`: tracks / books / episodes / playlists of the loaded snapshot (null: not loaded)."""
        at = self.app.sync.snapshots.checked_at(lib["id"])
        counts = self.app.sync.snapshots.counts(lib["id"])
        return {**lib, "updated_at": at.isoformat() if at else None, "counts": counts}

    async def _library(self, library_id: str) -> LibraryConfig:
        cfg = await self.app.store.library(library_id)
        if cfg is None:
            raise SyncError(f"unknown library {library_id!r}", "not_found")
        return cfg

    async def library(self, library_id: str) -> dict[str, Any]:
        return self._with_updated(_public_library(await self._library(library_id)))

    async def add_library(self, cfg: LibraryConfig) -> dict[str, Any]:
        cfg = await self._validate_library(
            cfg.model_copy(update={"source_id": None, "source_id_now": None})
        )
        await self.app.store.put_library(cfg)
        self.app.sync.forget_library(cfg.id)
        self._forget_suggest(cfg.id)
        await self.app.sync.sync_watchers()  # a local folder is watched from now on
        # a temporary instance, not cached: nothing outlives the add
        lib = registry.library_class(cfg.type)(cfg, self.app.ctx)
        try:
            sid = await self.app.sync.refresh_source_id(lib, timeout_s=5.0)
        finally:
            await lib.close()
        return _public_library(cfg.model_copy(update={"source_id": sid}))

    async def _validate_library(self, cfg: LibraryConfig) -> LibraryConfig:
        """The config as stored: settings checked, a server URL in its final form (`http://` added)."""
        if cfg.type in registry.RETIRED_LIBRARIES:
            raise SyncError(registry.RETIRED_LIBRARIES[cfg.type], "invalid")
        try:
            cls = registry.library_class(cfg.type)
        except KeyError as e:
            raise SyncError(str(e.args[0]), "invalid") from None
        checked = cls.Settings.model_validate(cfg.settings)
        if isinstance(cfg.settings.get("url"), str) and isinstance(
            getattr(checked, "url", None), str
        ):
            cfg = cfg.model_copy(update={"settings": {**cfg.settings, "url": checked.url}})  # type: ignore[attr-defined]
        root = cfg.settings.get("root")
        if isinstance(root, str) and root:
            await self.app.sync.check_library_root(root)
        return cfg

    async def update_library(self, library_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """PATCH: top-level fields replace, `settings` keys merge (a secret left out or sent back as the mask stays
        as it is)."""
        old = await self._library(library_id)
        # read from the source, never edited
        changes = {k: v for k, v in changes.items() if k not in ("source_id", "source_id_now")}
        unknown = sorted(set(changes) - set(LibraryConfig.model_fields))
        if unknown:
            raise SyncError(f"unknown library fields: {', '.join(unknown)}", "invalid")
        for key in ("id", "type"):
            if changes.get(key, getattr(old, key)) != getattr(old, key):
                raise SyncError(f"the library {key} cannot be changed", "invalid")
        raw = old.model_dump(mode="python")
        new = {k: v for k, v in (changes.get("settings") or {}).items() if v != SECRET_MASK}
        settings = {**old.settings, **new}
        if isinstance(changes.get("accounts"), dict):
            changes = {
                **changes,
                "accounts": await self._library_accounts(old, changes["accounts"]),
            }
        cfg = LibraryConfig.model_validate({**raw, **changes, "settings": settings})
        cfg = await self._validate_library(cfg)
        await self.app.store.put_library(cfg)
        await self.app.sync.library_changed(library_id)
        self._forget_suggest(library_id)
        return _public_library(cfg)

    async def _library_accounts(
        self, old: LibraryConfig, accounts: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH `accounts` replaces them: a secret sent back as the mask keeps the account's
        stored value; each must make valid settings over the library's; one a device signs in as cannot go."""
        secrets = _secret_keys(old.type)
        out: dict[str, Any] = {}
        for aid, acc in accounts.items():
            if not isinstance(acc, dict):
                raise SyncError(f"account {aid!r} must be an object", "invalid")
            stored = old.accounts[aid].settings if aid in old.accounts else {}
            given = acc.get("settings") or {}
            settings = {
                k: stored.get(k) if k in secrets and v == SECRET_MASK else v
                for k, v in given.items()
            }
            try:
                registry.library_class(old.type).Settings.model_validate(
                    {**old.settings, **settings}
                )
            except ValidationError as e:
                raise SyncError(
                    f"account {acc.get('name') or aid!r}: {pydantic_message(e)}", "invalid"
                ) from None
            out[aid] = {**acc, "settings": settings}
        gone = set(old.accounts) - set(out)
        users = [
            f"{d.name} ({old.accounts[d.library_accounts[old.id]].name})"
            for d in await self.app.store.devices()
            if d.library_accounts.get(old.id) in gone
        ]
        if users:
            raise SyncError(
                f"these devices sign in as an account to remove: {', '.join(users)} - "
                "pick another account there first",
                "conflict",
            )
        return out

    async def delete_library(
        self, library_id: str, edited_defaults: Literal["keep", "delete"] | None = None
    ) -> None:
        """Its untouched default templates go with it; its edited ones need `edited_defaults`: `keep`
        makes them ordinary templates of every library, `delete` removes them (and unlinks them from devices)."""
        await self.library(library_id)
        all_tpls = (await self.app.store.templates()).values()
        users = [
            t.name
            for t in all_tpls
            if t.default_of != library_id
            and any(library_id in (r.libraries or []) for r in [*t.rules, *t.excludes])
        ]
        if users:  # a rule names it (a device's picks); template libraries are checked below
            raise SyncError(
                f"library {library_id!r} is used by the rules of {', '.join(users)}: "
                "change those rules first",
                "conflict",
            )
        # its untouched default templates go with it; edited ones as the caller chose
        edited = [t for t in all_tpls if t.default_of == library_id and t.edited]
        if edited and edited_defaults is None:
            raise SyncError(
                f"library {library_id!r} has edited default templates "
                f"({', '.join(t.name for t in edited)}): keep or delete them",
                "conflict",
            )
        tpls = [
            t.name for t in all_tpls if library_id in t.library_ids and t.default_of != library_id
        ]
        if tpls:
            raise SyncError(
                f"library {library_id!r} is used by templates ({', '.join(tpls)}): "
                "change their libraries or delete them first",
                "conflict",
            )
        for t in edited:
            if edited_defaults == "delete":
                await self.delete_template(t.id, detach=True)
            else:
                await self._keep_default(t)
        await self.app.store.delete_library(library_id)
        await self.app.sync.library_changed(library_id)
        self._forget_suggest(library_id)

    async def _keep_default(self, t: RuleTemplate) -> None:
        """An edited default kept on library removal becomes an ordinary template of every library under a fresh id
        from its name  (under its reserved default.<lib>.<key> it hid the defaults of a library added again with the same id); devices that linked it link the new id, rule ids (and so priorities) stay."""
        existing = await self.app.store.templates()
        base = _slug(t.name)
        new_id, n = base, 2
        while new_id in existing or new_id.startswith(DEFAULT_PREFIX):
            new_id, n = f"{base}-{n}", n + 1
        kept = t.model_copy(
            update={"id": new_id, "default_of": None, "edited": False, "library_ids": []}
        )
        await self.app.store.put_template(kept)
        for d in template_usage(t.id, await self.app.store.devices()):
            ids = [new_id if i == t.id else i for i in d.rule_set.template_ids]
            rs = d.rule_set.model_copy(update={"template_ids": ids})
            await self.app.sync.update_device(d.model_copy(update={"rule_set": rs}))
        await self.app.store.delete_template(t.id)

    async def check_library(
        self,
        library_id: str,
        settings: dict[str, Any] | None = None,
        account: str | None = None,
    ) -> dict[str, Any]:
        """Health of the stored library, or of these unsaved `settings` merged over it (Test connection in the
         editor, a secret left as the mask keeps the stored value; nothing is stored). With `account`
        : signed in as that account, `settings` over the account's stored ones."""
        if settings is None:
            lib = await self.app.sync.library_as(library_id, account)
            return (await lib.check()).model_dump(mode="json")
        old = await self._library(library_id)
        if account and account in old.accounts:
            old = old.model_copy(
                update={"settings": {**old.settings, **old.accounts[account].settings}}
            )
        new = {k: v for k, v in settings.items() if v != SECRET_MASK}
        try:
            cfg = LibraryConfig.model_validate(
                {**old.model_dump(mode="python"), "settings": {**old.settings, **new}}
            )
            registry.library_class(cfg.type).Settings.model_validate(cfg.settings)
        except ValidationError as e:
            return {"ok": False, "message": pydantic_message(e), "details": {}}
        return (await self.app.sync.check_unsaved_library(cfg)).model_dump(mode="json")

    async def refresh_library(self, library_id: str) -> dict[str, Any]:
        """Reload the library's snapshot as a job (a big library takes a while): 202 + job, named after the
        library's name, not its id."""
        lib = await self.app.sync.library(library_id)
        name = (await self._library(library_id)).name

        async def job(_: Any) -> dict[str, Any]:
            try:
                snap = await self.app.sync.snapshots.get(lib, force=True)
            except Exception as e:  # noqa: BLE001 - the job error names the reason
                raise SyncError(
                    f"library {library_id!r} is not available: {lib.failure_reason(e)}",
                    "library_unavailable",
                ) from e
            await self.app.sync.mark_libraries_changed([library_id])  # update checks
            return {"tracks": len(snap.tracks), "playlists": len(snap.playlists)}

        return self.app.jobs.submit(
            "refresh", f"refresh {name}", job, library_id=library_id
        ).model_dump(mode="json")

    async def _holders(self, library_id: str) -> dict[str, list[str]]:
        """Item key of the library → names of the registered devices that held it when their manifest was last
        read (the browser's "On devices" chips)."""
        names = {d.id: d.name for d in await self.app.store.devices()}
        prefix = f"{library_id}/"
        out: dict[str, list[str]] = {}
        for device_id, keys in (await self.app.sync.device_items()).items():
            if device_id not in names:
                continue
            for k in keys:
                if k.startswith(prefix):
                    out.setdefault(k, []).append(names[device_id])
        for v in out.values():
            v.sort(key=str.casefold)
        return out

    async def _library_snapshot(
        self, library_id: str, account: str | None = None
    ) -> LibrarySnapshot:
        return (await self._browse_snapshot(library_id, account))[0]

    async def _browse_snapshot(
        self, library_id: str, account: str | None = None
    ) -> tuple[LibrarySnapshot, datetime]:
        """The snapshot the library browser shows, and when the library last confirmed it: from memory at once,
        refreshed in the background when stale; only a cold cache waits. `account`: browsed as that
        library account."""
        lib = await self.app.sync.library_as(library_id, account)
        try:
            return await self.app.sync.snapshots.browse(lib)
        except Exception as e:  # noqa: BLE001 - unreachable server, broken folder
            raise SyncError(
                f"library {library_id!r} is not available: {lib.failure_reason(e)}",
                "library_unavailable",
            ) from e

    async def library_items(
        self,
        library_id: str,
        query: str = "",
        kind: MediaKind | None = None,
        page: int = 0,
        page_size: int = 50,
        group: Literal["album", "artist"] | None = None,
        album_id: str | None = None,
        artist_id: str | None = None,
        series: str | None = None,
        playlist_id: str | None = None,
        sort: BrowseSort = "artist",
        account: str | None = None,
    ) -> dict[str, Any]:
        """Tracks of the library's (cached) snapshot: `query` matches title/artist/album/series (case-insensitive,
        every word), sorted by artist, album, disc, track (`sort`: then newest added / best rated / newest year first); `album_id` / `artist_id` / `series` / `playlist_id` keep
        one album, artist, book or podcast series, or playlist (in its order). `group=album|artist` answers one row
        per album (a book / show by its series when it has no album) or artist instead."""
        if page < 0 or not 1 <= page_size <= PAGE_SIZE_MAX:
            raise SyncError(f"page must be >= 0 and page_size 1..{PAGE_SIZE_MAX}", "invalid")
        snap, checked = await self._browse_snapshot(library_id, account)
        words = query.casefold().split()
        order: dict[str, int] | None = None
        if playlist_id is not None:
            pl = snap.playlists.get(playlist_id)
            if pl is None:
                raise SyncError(f"unknown playlist {playlist_id!r}", "not_found")
            order = {r.item_id: i for i, r in enumerate(pl.track_refs)}
        rows = []
        for t in snap.tracks.values():
            if kind is not None and t.kind is not kind:
                continue
            if album_id is not None and t.album_id != album_id:
                continue
            if artist_id is not None and t.artist_id != artist_id:
                continue
            if series is not None and t.series != series:
                continue
            if order is not None and t.ref.item_id not in order:
                continue
            text = " ".join(x for x in (t.title, t.artist, t.album, t.series) if x).casefold()
            if all(w in text for w in words):
                rows.append(t)
        rows.sort(
            key=lambda t: (
                (t.artist or "").casefold(),
                (t.album or t.series or "").casefold(),
                t.disc_no or 0,
                t.chapter_index or t.track_no or 0,
                t.title.casefold(),
            )
        )
        if order is not None:
            rows.sort(key=lambda t: order[t.ref.item_id])
        elif sort in _ASC_SORTS:  # A–Z orders (a video tab sorts by show or title)
            rows.sort(key=_ASC_SORTS[sort])
        elif sort != "artist":
            rows.sort(key=_SORTS[sort], reverse=True)  # stable: equal ones stay by artist
        holders = await self._holders(library_id)
        if group is not None:
            groups = _group_tracks(rows, group, holders)
            lib = await self.app.sync.library(library_id)
            if group == "artist" and lib.capabilities.group_covers:
                for g in groups:  # a Subsonic server has artist pictures
                    g["cover_item"] = g["id"]
            chunk_g = groups[page * page_size : (page + 1) * page_size]
            return {
                "total": len(groups),
                "page": page,
                "page_size": page_size,
                "items": chunk_g,
                "updated_at": checked.isoformat(),
                "tab_counts": _tab_counts(snap),
            }
        chunk = rows[page * page_size : (page + 1) * page_size]
        return {
            "total": len(rows),
            "page": page,
            "page_size": page_size,
            "updated_at": checked.isoformat(),
            "tab_counts": _tab_counts(snap),
            "items": [
                {
                    "ref": t.ref.key(),
                    "item_id": t.ref.item_id,
                    "kind": t.kind.value,
                    "title": t.title,
                    "artist": t.artist,
                    "album": t.album,
                    "series": t.series,
                    "season": t.extra.get("season"),
                    "episode": t.extra.get("episode"),
                    "duration_s": t.duration_s,
                    "size_bytes": t.size_bytes,
                    "album_id": t.album_id,
                    "artist_id": t.artist_id,
                    "cover_item": t.ref.item_id if t.has_cover else None,
                    "year": t.year,
                    "rating": t.rating,
                    "format": t.format,
                    "position_s": t.position_s,
                    "finished": t.finished,
                    "on_devices": holders.get(t.ref.key(), []),
                }
                for t in chunk
            ],
        }

    async def library_playlists(
        self, library_id: str, query: str = "", account: str | None = None
    ) -> list[dict[str, Any]]:
        """The library's playlists (name, tracks it has, their size), by name; as `account`."""
        snap = await self._library_snapshot(library_id, account)
        group_covers = (await self.app.sync.library(library_id)).capabilities.group_covers
        holders = await self._holders(library_id)
        words = query.casefold().split()
        out = []
        for pid, pl in snap.playlists.items():
            if not all(w in pl.name.casefold() for w in words):
                continue
            tracks = [snap.tracks[r.item_id] for r in pl.track_refs if r.item_id in snap.tracks]
            out.append(
                {
                    "id": pid,
                    "name": pl.name,
                    "items": len(tracks),
                    "size_bytes": sum(t.size_bytes for t in tracks),
                    "smart": pl.smart,
                    "on_devices": _held_by(tracks, holders),
                    "on_devices_partly": _partly_held(tracks, holders),
                    # Subsonic servers make playlist pictures; elsewhere the first track with a cover
                    "cover_item": pid
                    if group_covers
                    else next((t.ref.item_id for t in tracks if t.has_cover), None),
                }
            )
        out.sort(key=lambda p: str(p["name"]).casefold())
        return out

    async def search(self, query: str, limit: int = SEARCH_LIMIT) -> dict[str, Any]:
        """One search box over everything: artists, albums and tracks of every library's cached
        snapshot (never waits for a server: a library without a loaded cache is listed in `not_loaded`), devices,
        templates and jobs. Every word must match (case-insensitive); at most `limit` hits per group."""
        words = query.casefold().split()
        out: dict[str, Any] = {
            k: [] for k in ("artists", "albums", "tracks", "devices", "templates", "jobs")
        }
        out["not_loaded"] = []
        if not words or not 1 <= limit <= PAGE_SIZE_MAX:
            return out

        def hit(*texts: str | None) -> bool:
            text = " ".join(x for x in texts if x).casefold()
            return all(w in text for w in words)

        for cfg in await self.app.store.libraries():
            snap = await self.app.sync.snapshots.cached(cfg.id)
            if snap is None:
                out["not_loaded"].append({"id": cfg.id, "name": cfg.name})
                continue
            lib = {"library_id": cfg.id, "library": cfg.name}
            tracks = sorted(snap.tracks.values(), key=lambda t: t.title.casefold())
            for g in _group_tracks([t for t in tracks if hit(t.artist)], "artist"):
                if hit(g["name"]):
                    out["artists"].append({**lib, "name": g["name"], "kind": g["kind"]})
            for g in _group_tracks([t for t in tracks if hit(t.album, t.series)], "album"):
                if hit(g["name"]):
                    out["albums"].append(
                        {**lib, "name": g["name"], "artist": g["artist"], "kind": g["kind"]}
                    )
            for t in tracks:
                if hit(t.title, t.artist, t.album, t.series):
                    out["tracks"].append(
                        {
                            **lib,
                            "title": t.title,
                            "artist": t.artist,
                            "album": t.album or t.series,
                            "kind": t.kind.value,
                        }
                    )
        out["devices"] = [
            {"id": d.id, "name": d.name, "type": d.type}
            for d in sorted(await self.app.store.devices(), key=lambda d: d.name.casefold())
            if hit(d.name, d.type)
        ]
        out["templates"] = [
            {"id": t.id, "name": t.name}
            for t in sorted(
                (await self.app.store.templates()).values(), key=lambda t: t.name.casefold()
            )
            if hit(t.name)
        ]
        out["jobs"] = [
            {
                "id": j.id,
                "name": j.name,
                "status": j.status.value,
                "created_at": j.created_at.isoformat(),
            }
            for j in sorted(self.app.jobs.jobs(), key=lambda j: j.created_at, reverse=True)
            if hit(j.name, j.kind)
        ]
        for k in ("artists", "albums", "tracks", "devices", "templates", "jobs"):
            out[k] = out[k][:limit]
        return out

    # --- types -----------------------------------------------------------------------------------

    def library_types(self) -> list[dict[str, Any]]:
        return [
            {
                "type": c.type_name,
                "display_name": c.display_name,
                "display_hint": c.display_hint,
                "beta": c.beta,  # a Beta badge in the UI
                "settings_schema": _schema(c),
                "capabilities": c.capabilities.model_dump(mode="json"),
                "account_fields": list(c.account_fields),  # a device's own account
            }
            for c in registry.library_classes()
        ]

    def device_types(self) -> list[dict[str, Any]]:
        support = filesystem_support()
        out = []
        for c in registry.device_classes():
            if c.unavailable_reason():
                continue
            out.append(
                {
                    "type": c.type_name,
                    "display_name": c.display_name,
                    # registered with its settings, no disk or folder (FTP)
                    "network": c.network,
                    # an MTP player: also detected on USB like a disk
                    "usb_mtp": c.usb_mtp,
                    "beta": c.beta,  # a Beta badge in the UI
                    "music_only": c.music_only,
                    "settings_schema": _schema(c),
                    "capabilities": c.capabilities.model_dump(mode="json"),
                    "suggested_processors": [
                        s.model_dump(mode="json") for s in suggested_processors(c.capabilities)
                    ],
                    # preselected on Register
                    "default_processor_template_id": DEFAULT_PROCESSOR_TEMPLATES.get(c.type_name),
                    # forms of the device page's Sync settings tab (web UI)
                    "general_schema": _general_schema(),
                    "sync_schema": _sync_schema(c.type_name),
                    "reverse_sync_schema": _reverse_sync_schema(c.type_name),
                    # Format in Device settings: each file system and whether this host can make it
                    "format_filesystems": [
                        {
                            "fs": fs,
                            "label": FS_LABELS.get(fs, fs),
                            "available": support.get(fs) is None,
                            "reason": support.get(fs),
                        }
                        for fs in c.format_filesystems
                    ],
                }
            )
        return out

    def processor_types(self) -> list[dict[str, Any]]:
        return [
            {
                "type": c.type_name,
                "display_name": c.display_name,
                "stage": c.stage,
                "settings_schema": _schema(c),
            }
            for c in registry.processor_classes()
        ]

    # --- rules and templates ---------------------------------------------------------------------

    async def validate_rules(
        self,
        rule_set: RuleSet | dict[str, Any] | None = None,
        rule: Rule | dict[str, Any] | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        """Exactly one of a rule set, a rule or text (JSON/YAML/.nsp). Errors are a result, not an HTTP error."""
        if sum(x is not None for x in (rule_set, rule, text)) != 1:
            raise SyncError("pass exactly one of rule_set, rule, text", "invalid")
        try:
            if text is not None:
                rs = ruleset_from_text(text)
            elif rule_set is not None:
                rs = RuleSet.model_validate(rule_set)
            else:
                rs = RuleSet(rules=[Rule.model_validate(rule)])
        except RuleError as e:
            return {"ok": False, "errors": [str(e)], "warnings": [], "rule_set": None}
        except ValidationError as e:
            return {"ok": False, "errors": [pydantic_message(e)], "warnings": [], "rule_set": None}
        linked, _, _ = resolve_rules(
            RuleSet(template_ids=rs.template_ids), await self.app.store.templates()
        )
        res = validate_ruleset(rs, {r.id for r in linked})  # the priority may list them
        return {
            "ok": res.ok,
            "errors": res.errors,
            "warnings": res.warnings,
            "rule_set": rs.model_dump(mode="json") if text is not None else None,
        }

    async def test_rules(self, device_id: str, rule_set: RuleSet | None = None) -> dict[str, Any]:
        return await rule_test.test_rules(self.app.sync, device_id, rule_set)

    async def preview_rules(self, device_id: str, rule_set: RuleSet) -> dict[str, Any]:
        return await rule_test.preview(self.app.sync, device_id, rule_set)

    # --- rule builder (web UI) ---------------------------------------------------------------

    async def rule_fields(self, library_ids: list[str] | None = None) -> dict[str, Any]:
        """Fields with their type (nsp: also known to Navidrome) and the operators the builder offers per type.
        `available`: the field exists in at least one of `library_ids` (None = every field)."""
        available: frozenset[str] | set[str] = ALL_FIELDS
        podcasts = True
        if library_ids is not None:
            configs = [c for c in await self.app.store.libraries() if c.id in library_ids]
            available = set().union(*(registry.library_class(c.type).rule_fields for c in configs))
            podcasts = any(
                MediaKind.PODCAST in registry.library_class(c.type).capabilities.kinds
                for c in configs
            )
        return {
            "fields": [
                {"name": s.name, "type": s.type, "nsp": s.nsp, "available": s.name in available}
                for s in FIELDS.values()
                if s.name
                not in ("track_id", "album_id", "artist_id")  # the manual selection's own fields
            ]
            # latestEpisodes as a builder row: offered when a library has podcasts
            + [
                {"name": "latest_episodes", "type": "episodes", "nsp": False, "available": podcasts}
            ],
            "operators": OPERATORS_BY_TYPE,
        }

    async def rule_presets(self, library_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """The rule presets; `available`: one of `library_ids` (None = every library) serves the
        preset's media kind and fields."""
        configs = [
            c
            for c in await self.app.store.libraries()
            if library_ids is None or c.id in library_ids
        ]
        offers = [
            (
                registry.library_class(c.type).capabilities.kinds,
                registry.library_class(c.type).rule_fields,
            )
            for c in configs
        ]
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "kind": p.kind.value,
                "pick": p.pick,
                "params": [vars(x) for x in p.params],
                "available": available(p, offers),
            }
            for p in PRESETS.values()
        ]

    def rule_preset(self, preset_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """The rule preset `preset_id` builds with these parameters (defaults for the missing ones)."""
        preset = PRESETS.get(preset_id)
        if preset is None:
            raise SyncError(f"unknown rule preset {preset_id!r}", "not_found")
        try:
            rule = preset.rule(params)
        except ValueError as exc:
            raise SyncError(str(exc), "invalid") from exc
        return rule.model_dump(mode="json")

    def rules_text(self, rule_set: RuleSet, fmt: str = "yaml") -> dict[str, str]:
        """A rule set as YAML or JSON text (the builder's text mode; `POST /rules/validate` parses it back)."""
        if fmt == "json":
            data = rule_set.model_dump(mode="json", exclude_defaults=True)
            return {"text": json.dumps(data, ensure_ascii=False, indent=2)}
        if fmt != "yaml":
            raise SyncError("format must be yaml or json", "invalid")
        return {"text": ruleset_to_yaml(rule_set)}

    async def test_rules_on(
        self,
        rule_set: RuleSet,
        library_ids: list[str] | None,
        accounts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Rule test without a device (templates): against these libraries, default every enabled one; `accounts`:
        library id → the account to read it as."""
        if not library_ids:  # None or [] = every enabled library, like a template's library_ids
            library_ids = [c.id for c in await self.app.store.libraries() if c.enabled]
        if not library_ids:
            raise SyncError("no libraries to test against: add a library first", "invalid")
        snaps = await self.app.sync.library_snapshots(library_ids, accounts=accounts or {})
        return await rule_test.test_selection(self.app.sync, rule_set, snaps)

    async def test_rules_csv(
        self,
        rule_set: RuleSet,
        library_ids: list[str] | None,
        accounts: dict[str, str] | None = None,
    ) -> str:
        """Every match of a rule test as CSV; `accounts` as in `test_rules_on`."""
        if not library_ids:
            library_ids = [c.id for c in await self.app.store.libraries() if c.enabled]
        snaps = await self.app.sync.library_snapshots(library_ids, accounts=accounts or {})
        return await rule_test.matches_csv(self.app.sync, rule_set, snaps)

    async def rule_values(
        self,
        field: str,
        query: str = "",
        library_ids: list[str] | None = None,
        limit: int = 20,
        *,
        kind: MediaKind | None = None,
        accounts: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Autocomplete: distinct values of a text field in the libraries' indexes (most frequent first) whose
        text contains `query` (case-insensitive, anywhere); `playlist` lists playlist names, `library_id` the
        libraries. A query of 1-2 characters suggests nothing: suggestions start after more than 2. `kind`:
        only items of that kind (the picker's Audiobooks / Podcasts / Episodes of an Audiobookshelf library)."""
        if not 1 <= limit <= 200:
            raise SyncError("limit must be 1..200", "invalid")
        if 0 < len(query.strip()) < MIN_SUGGEST_CHARS:
            return []
        libs = [c for c in await self.app.store.libraries() if c.enabled]
        if library_ids:  # [] = all, as above
            libs = [c for c in libs if c.id in library_ids]
        if field == "library_id":
            values = {c.id: 1 for c in libs}
            return _top_values(values, query, limit, labels={c.id: c.name for c in libs})
        spec = FIELDS.get(field)
        if field != "playlist" and (spec is None or spec.type not in ("string", "list")):
            raise SyncError(f"no value suggestions for {field!r}", "invalid")
        counts: dict[str, int] = {}
        # an album is named by its album artist too (the picker): the most frequent one per album name
        album_artists: dict[str, Counter[str]] = {}
        accounts = accounts or {}  # library id → the account to read it as
        snaps = await asyncio.gather(
            *(self._suggest_snapshot(c.id, accounts.get(c.id)) for c in libs)
        )
        for snap in snaps:
            if snap is None:  # an unreachable library just has no suggestions
                continue
            if field == "playlist":
                for pl in snap.playlists.values():
                    counts[pl.name] = counts.get(pl.name, 0) + len(pl.track_refs)
                continue
            assert spec is not None
            for t in snap.tracks.values():
                if kind is not None and t.kind is not kind:
                    continue
                v = spec.get(t)
                for x in v if isinstance(v, list) else [v]:
                    if x:
                        counts[str(x)] = counts.get(str(x), 0) + 1
                if field == "album" and v and (who := t.album_artist or t.artist):
                    album_artists.setdefault(str(v), Counter())[who] += 1
        labels = {a: c.most_common(1)[0][0] for a, c in album_artists.items()} or None
        return _top_values(counts, query, limit, labels=labels)

    async def _suggest_snapshot(
        self, library_id: str, account: str | None = None
    ) -> LibrarySnapshot | None:
        """A library's snapshot for autocomplete without waiting for a slow or dead server: the loads of
        all libraries run in parallel, each waited for at most SUGGEST_WAIT_S; one that takes longer continues in
        the background (later requests reuse it) and the last known snapshot stands in. `account`: as that library
        account."""
        key = f"{library_id}@{account}" if account else library_id
        task = self._suggest_loads.get(key)
        if task is None or task.done():
            failed = self._suggest_failed.get(key)
            if failed is not None and time.monotonic() - failed < SUGGEST_RETRY_S:
                return await self.app.sync.snapshots.cached(key)
            task = asyncio.create_task(self._load_suggest_snapshot(library_id, account))
            self._suggest_loads[key] = task
        done, _ = await asyncio.wait({task}, timeout=SUGGEST_WAIT_S)
        snap = task.result() if task in done else None
        return snap or await self.app.sync.snapshots.cached(key)

    async def _load_suggest_snapshot(
        self, library_id: str, account: str | None = None
    ) -> LibrarySnapshot | None:
        key = f"{library_id}@{account}" if account else library_id
        try:
            snap = await self.app.sync.snapshots.get(
                await self.app.sync.library_as(library_id, account)
            )
        except Exception:  # noqa: BLE001 - unreachable: no suggestions (or the last known ones)
            if (
                self._suggest_loads.get(key) is asyncio.current_task()
            ):  # not a load with old settings
                self._suggest_failed[key] = time.monotonic()
            return None
        self._suggest_failed.pop(key, None)
        return snap

    def _forget_suggest(self, library_id: str) -> None:
        """New library settings: the next autocomplete request loads the library again at once instead of
        skipping it after a failed load or reusing a load still running with the old settings (its accounts' too)."""
        for cache in (self._suggest_failed, self._suggest_loads):
            for key in [k for k in cache if k == library_id or k.startswith(library_id + "@")]:
                cache.pop(key)

    async def templates(self) -> list[dict[str, Any]]:
        return [t.model_dump(mode="json") for t in (await self.app.store.templates()).values()]

    async def template(self, template_id: str) -> dict[str, Any]:
        return (await self._template(template_id)).model_dump(mode="json")

    async def _template(self, template_id: str) -> RuleTemplate:
        tpl = await self.app.store.template(template_id)
        if tpl is None:
            raise SyncError(f"unknown template {template_id!r}", "not_found")
        return tpl

    @staticmethod
    def _follow_names(tpl: RuleTemplate) -> RuleTemplate:
        """Generated preset names follow their number ("The latest 5 episodes…" after 3 → 5)."""
        rules = [follow_name(r) for r in tpl.rules]
        excludes = [follow_name(r) for r in tpl.excludes]
        if rules == tpl.rules and excludes == tpl.excludes:
            return tpl
        return tpl.model_copy(update={"rules": rules, "excludes": excludes})

    async def _check_template(self, tpl: RuleTemplate) -> None:
        res = validate_ruleset(RuleSet(rules=tpl.rules, excludes=tpl.excludes))
        if not res.ok:
            raise SyncError("invalid rules: " + "; ".join(res.errors), "invalid")
        if tpl.library_ids:
            known = [c.id for c in await self.app.store.libraries()]
            for lid in tpl.library_ids:
                if lid not in known:
                    raise SyncError(
                        f"unknown library {lid!r} (known: {', '.join(known) or 'none'})",
                        "invalid",
                    )

    async def add_template(self, data: dict[str, Any]) -> dict[str, Any]:
        existing = await self.app.store.templates()
        if "id" not in data or not data["id"]:
            base = _slug(str(data.get("name") or ""))
            data = {**data, "id": base}
            n = 2
            while data["id"] in existing:
                data["id"], n = f"{base}-{n}", n + 1
        tpl = RuleTemplate.model_validate(
            {**data, "builtin": False, "default_of": None, "edited": False, "updated_at": utcnow()}
        )
        if tpl.id.startswith(DEFAULT_PREFIX):
            raise SyncError(
                f"template ids starting with {DEFAULT_PREFIX!r} are reserved", "invalid"
            )
        tpl = self._follow_names(tpl)
        if tpl.id in existing:
            raise SyncError(f"template {tpl.id!r} already exists", "conflict")
        await self._check_template(tpl)
        await self.app.store.put_template(tpl)
        return tpl.model_dump(mode="json")

    @staticmethod
    def _writable(tpl: RuleTemplate) -> None:
        if tpl.builtin:
            raise SyncError(
                f"{tpl.name!r} is a built-in template and cannot be changed: copy it first",
                "conflict",
            )

    async def copy_template(self, template_id: str, name: str | None = None) -> dict[str, Any]:
        """An unsaved draft of any template, built-in ones included: its rules with fresh ids (a
        copy and its original may be linked together), name "Copy of …"; nothing is stored until it is POSTed to
        /templates, which gives it an id from its final name."""
        tpl = await self._template(template_id)
        data = tpl.model_dump(
            mode="json", exclude={"id", "builtin", "default_of", "edited", "updated_at"}
        )
        suffix = uuid.uuid4().hex[:4]
        for r in (*data["rules"], *data["excludes"]):
            r["id"] = f"{r['id']}-{suffix}"
        return {**data, "name": name or f"Copy of {tpl.name}"}

    async def update_template(self, template_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        old = await self._template(template_id)
        self._writable(old)
        unknown = sorted(set(changes) - set(RuleTemplate.model_fields) - {"library_id"})
        if unknown:
            raise SyncError(f"unknown template fields: {', '.join(unknown)}", "invalid")
        if changes.get("id", template_id) != template_id:
            raise SyncError("the template id cannot be changed", "invalid")
        data = old.model_dump(mode="json")
        if "library_id" in changes:  # the one-library form (before) replaces the list
            data.pop("library_ids")
        # a library's default stays its default when edited: the store marks it edited
        keep = {"default_of": old.default_of, "edited": bool(old.default_of), "builtin": False}
        tpl = RuleTemplate.model_validate({**data, **changes, **keep, "updated_at": utcnow()})
        tpl = self._follow_names(tpl)
        await self._check_template(tpl)
        if old.default_of and not old.edited and _same_rules(old, tpl):
            # an unchanged save keeps a default generated
            return old.model_dump(mode="json")
        generated = await self.app.store.generated_template(template_id) if old.edited else None
        if generated is not None and _same_rules(generated, tpl):
            # edited back to what is generated (e.g. the Undo of "Add to a template"): a plain default again
            await self.app.store.reset_template(template_id)
            await self._template_changed(template_id)
            return generated.model_dump(mode="json")
        await self.app.store.put_template(tpl)
        await self._template_changed(template_id)
        return tpl.model_dump(mode="json")

    async def delete_template(self, template_id: str, detach: bool = False) -> None:
        """A template in use is refused (409) unless `detach`: it is then unlinked from every device that links it
        (a device has templates only, - its items leave a mirror device at the next sync)."""
        tpl = await self._template(template_id)
        self._writable(tpl)
        users = template_usage(template_id, await self.app.store.devices())
        rule_ids = {r.id for r in tpl.rules}
        if users and not detach:
            names = ", ".join(d.name for d in users)
            raise SyncError(
                f"template {template_id!r} is used by {names}: unlink it or pass detach=true",
                "conflict",
            )
        for d in users:
            ids = [t for t in d.rule_set.template_ids if t != template_id]
            rs = d.rule_set.model_copy(
                update={
                    "template_ids": ids,
                    "priority": [p for p in d.rule_set.priority if p not in rule_ids],
                }
            )
            await self.app.sync.update_device(d.model_copy(update={"rule_set": rs}))
        await self.app.store.delete_template(template_id)

    async def reset_template(self, template_id: str) -> dict[str, Any]:
        """An edited default template back to the generated one; devices that link it re-plan."""
        tpl = await self._template(template_id)
        if not (tpl.default_of and tpl.edited):
            raise SyncError(f"template {template_id!r} is not an edited default", "invalid")
        await self.app.store.reset_template(template_id)
        await self._template_changed(template_id)
        return (await self._template(template_id)).model_dump(mode="json")

    async def template_usage(self, template_id: str) -> list[dict[str, Any]]:
        await self._template(template_id)
        return [
            {"id": d.id, "name": d.name}
            for d in template_usage(template_id, await self.app.store.devices())
        ]

    # --- processor templates ------------------------------------------------------------

    async def processor_templates(self) -> list[dict[str, Any]]:
        devices = await self.app.store.devices()
        return [
            {
                **t.model_dump(mode="json"),
                "used_by": [{"id": d.id, "name": d.name} for d in linked_devices(t.id, devices)],
            }
            for t in (await self.app.store.processor_templates()).values()
        ]

    async def processor_template(self, template_id: str) -> dict[str, Any]:
        tpl = await self._processor_template(template_id)
        users = linked_devices(template_id, await self.app.store.devices())
        return {
            **tpl.model_dump(mode="json"),
            "used_by": [{"id": d.id, "name": d.name} for d in users],
        }

    async def _processor_template(self, template_id: str) -> ProcessorTemplate:
        tpl = await self.app.store.processor_template(template_id)
        if tpl is None:
            raise SyncError(f"unknown processor template {template_id!r}", "not_found")
        return tpl

    def _check_processor_template(self, tpl: ProcessorTemplate) -> None:
        if not tpl.name.strip():
            raise SyncError("a processor template needs a name", "invalid")
        if tpl.device_type is not None:
            try:
                registry.device_class(tpl.device_type)
            except KeyError:
                raise SyncError(f"unknown device type {tpl.device_type!r}", "invalid") from None
        for slot in tpl.processors:
            try:
                registry.processor_class(slot.type).Settings.model_validate(slot.settings)
            except KeyError as e:
                raise SyncError(str(e.args[0]), "invalid") from None

    async def add_processor_template(self, data: dict[str, Any]) -> dict[str, Any]:
        existing = await self.app.store.processor_templates()
        if not data.get("id"):
            base = _slug(str(data.get("name") or "")) or "chain"
            data, n = {**data, "id": base}, 2
            while data["id"] in existing:
                data["id"], n = f"{base}-{n}", n + 1
        tpl = ProcessorTemplate.model_validate({**data, "builtin": False, "updated_at": utcnow()})
        tpl.processors = with_tag_step(tpl.processors)  # every chain has the tag step
        if tpl.id in existing:
            raise SyncError(f"processor template {tpl.id!r} already exists", "conflict")
        self._check_processor_template(tpl)
        await self.app.store.put_processor_template(tpl)
        return await self.processor_template(tpl.id)

    async def copy_processor_template(
        self, template_id: str, name: str | None = None
    ) -> dict[str, Any]:
        """An unsaved draft: stored only when POSTed to /processor-templates."""
        tpl = await self._processor_template(template_id)
        data = tpl.model_dump(mode="json", exclude={"id", "builtin", "updated_at"})
        return {**data, "name": name or f"Copy of {tpl.name}"}

    async def update_processor_template(
        self, template_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Saves the template and renews the chain of every linked device (their plans are dropped)."""
        old = await self._processor_template(template_id)
        if old.builtin:
            raise SyncError(
                f"{old.name!r} is a built-in processor template and cannot be changed: copy it first",
                "conflict",
            )
        unknown = sorted(set(changes) - set(ProcessorTemplate.model_fields) - {"used_by"})
        if unknown:
            raise SyncError(f"unknown processor template fields: {', '.join(unknown)}", "invalid")
        if changes.get("id", template_id) != template_id:
            raise SyncError("the template id cannot be changed", "invalid")
        changes = {k: v for k, v in changes.items() if k != "used_by"}
        tpl = ProcessorTemplate.model_validate(
            {**old.model_dump(mode="json"), **changes, "builtin": False, "updated_at": utcnow()}
        )
        tpl.processors = with_tag_step(tpl.processors)
        self._check_processor_template(tpl)
        await self.app.store.put_processor_template(tpl)
        for d in linked_devices(template_id, await self.app.store.devices()):
            await self.app.sync.update_device(d.model_copy(update={"processors": tpl.processors}))
        return await self.processor_template(template_id)

    async def delete_processor_template(self, template_id: str, detach: bool = False) -> None:
        """A template linked to devices is refused (409) unless `detach`: they keep its chain as their own."""
        tpl = await self._processor_template(template_id)
        if tpl.builtin:
            raise SyncError(f"{tpl.name!r} is a built-in processor template", "conflict")
        users = linked_devices(template_id, await self.app.store.devices())
        if users and not detach:
            names = ", ".join(d.name for d in users)
            raise SyncError(
                f"processor template {template_id!r} is used by {names}: pass detach=true",
                "conflict",
            )
        for d in users:
            await self.app.sync.update_device(d.model_copy(update={"processor_template_id": None}))
        await self.app.store.delete_processor_template(template_id)

    async def _template_changed(self, template_id: str) -> None:
        for d in template_usage(template_id, await self.app.store.devices()):
            self.app.sync.drop_plans(d.id)

    # --- logs ------------------------------------------------------------------------------------

    def _log_path(self) -> Path:
        return self.app.config.logs / "syncmusic.log"

    def _filtered_log(
        self,
        level: str | None,
        device: str | None,
        job: str | None,
        query: str,
        step: str | None = None,
    ) -> list[dict[str, Any]]:
        if level and level.upper() not in LEVELS:
            raise SyncError(f"level must be one of {', '.join(LEVELS)}", "invalid")
        try:
            with open(self._log_path(), encoding="utf-8", errors="replace") as f:
                entries = parse_log(f)
        except FileNotFoundError:
            return []
        return [e for e in entries if matches(e, level, device, job, query, step)]

    async def log_entries(
        self,
        level: str | None = None,
        device: str | None = None,
        job: str | None = None,
        query: str = "",
        limit: int = 500,
        step: str | None = None,
    ) -> dict[str, Any]:
        """Entries of the application log, oldest first: the last `limit` that match the minimum
        `level`, the `device` / `job` they were logged for and every word of `query`. Secrets are redacted when
        logging (infra/log.py)."""
        if not 1 <= limit <= 5000:
            raise SyncError("limit must be 1..5000", "invalid")
        found = await asyncio.to_thread(self._filtered_log, level, device, job, query, step)
        return {
            "total": len(found),
            "entries": [{k: v for k, v in e.items() if k != "raw"} for e in found[-limit:]],
        }

    async def log_text(
        self,
        level: str | None = None,
        device: str | None = None,
        job: str | None = None,
        query: str = "",
        step: str | None = None,
    ) -> str:
        """The matching log lines as they are in the file (download)."""
        found = await asyncio.to_thread(self._filtered_log, level, device, job, query, step)
        return "".join(e["raw"] + "\n" for e in found)

    def logs(self, lines: int = 200) -> list[str]:
        """The last `lines` lines of the application log file (empty when logging to stdout only)."""
        if not 1 <= lines <= 10_000:
            raise SyncError("lines must be 1..10000", "invalid")
        try:
            with open(self._log_path(), encoding="utf-8", errors="replace") as f:
                return [line.rstrip("\n") for line in deque(f, maxlen=lines)]
        except FileNotFoundError:
            return []

    async def diagnostics_bundle(self, reports: int = 20) -> bytes:
        """The diagnostics zip: versions, OS, config, settings, libraries, devices, checks, the
        last `reports` sync reports with their job logs and the log tail; secrets masked, then scrubbed again."""
        cfgs = await self.app.store.devices()
        libs = await self.app.store.libraries()
        secrets = [(await self.app.store.app_settings()).webhook_url or ""]
        for lib in libs:
            secrets += [_secret_value(lib.settings.get(k)) for k in _secret_keys(lib.type)]
        for dev in cfgs:
            secrets += [_secret_value(dev.settings.get(k)) for k in _device_secret_keys(dev.type)]
        recent = (await self.sync_reports())[:reports]
        system = {
            **self.version(),
            "os": platform.platform(),
            "arch": platform.machine(),
            "ffmpeg": await asyncio.to_thread(ffmpeg.check),
        }

        def dump(x: Any) -> str:
            return json.dumps(x, indent=2, default=str)

        files = {
            "system.json": dump(system),
            "config.json": dump(self.app.config.model_dump(mode="json")),
            "settings.json": dump(await self.settings()),
            "libraries.json": dump(await self.libraries()),
            "devices.json": dump(
                {"configs": [_public_device(c) for c in cfgs], "live": await self.devices()}
            ),
            "checks.json": dump(await self.diagnostics()),
            "storage.json": dump(await self.storage()),
            "resources.json": dump(await self.resources()),
            "reports.json": dump(recent),
            "log-tail.txt": "\n".join(self.logs(2000)) + "\n",
        }
        for job in {r["job_id"] for r in recent if r.get("job_id")}:
            files[f"jobs/{job}.log"] = await self.log_text(job=job)
        return await asyncio.to_thread(bundle.make_zip, files, [s for s in secrets if s])

    # --- notifications centre -------------------------------------------------------------

    async def notifications(self, limit: int = 200) -> dict[str, Any]:
        store = self.app.store
        return {
            "items": await store.notifications(max(1, min(limit, 500))),
            "unread": await store.unread_notifications(),
            # for the centre's "kept N days"; None = until the count limit
            "keep_days": (await store.app_settings()).notification_days,
        }

    async def mark_notifications_read(self, ids: list[int] | None = None) -> dict[str, Any]:
        await self.app.store.mark_notifications_read(ids)
        self.app.bus.publish("notification.read")
        return {"unread": await self.app.store.unread_notifications()}

    async def clear_notifications(self, ids: list[int] | None = None) -> dict[str, Any]:
        await self.app.store.clear_notifications(ids)
        self.app.bus.publish("notification.cleared")
        return {"unread": await self.app.store.unread_notifications()}

    # --- settings --------------------------------------------------------------------------------

    async def settings(self) -> dict[str, Any]:
        return _public_settings(await self.app.store.app_settings())

    async def test_webhook(self, url: str | None = None) -> dict[str, Any]:
        """Send a test notification to `url` (or the stored webhook URL when it is empty or the mask).
        Returns {ok, message}; a failure is a result, not an error."""
        if not url or url == SECRET_MASK:
            url = (await self.app.store.app_settings()).webhook_url
        if not url:
            raise SyncError("no webhook URL: enter one first", "invalid")
        payload = {
            "event": "test",
            "ts": datetime.now(UTC).isoformat(),
            "device": {"id": None, "name": None},
            "summary": "SyncMusic test notification",
            "report_url": None,
        }
        try:
            await WebhookNotifier(url).send(payload)
        except Exception as e:  # noqa: BLE001 - reported to the user
            host = urlsplit(url).hostname or "?"
            message = str(e).replace(url, f"<{host}>")  # the URL may carry a token (it is a secret)
            return {"ok": False, "message": f"{type(e).__name__}: {message}"}
        return {"ok": True, "message": "sent"}

    def settings_schema(self) -> dict[str, Any]:
        return {"schema": AppSettings.model_json_schema(), "paths": self.paths()}

    def paths(self) -> dict[str, str]:
        """Where the data lives (bootstrap config, read-only at runtime; shown in the Settings view)."""
        c = self.app.config
        return {
            "data_dir": str(c.data_dir),
            "cache_dir": str(c.cache_dir),
            "mount_root": str(c.mount_root),
        }

    def _updates(self) -> dict[str, Any]:
        u = self.app.updates
        repo = self.app.config.update_repo.strip()
        return {
            "version": __version__,
            "running": u.running,
            "commit": u.commit,
            "install_type": u.install_type,
            "channel": u.channel,
            "latest": u.latest,
            "latest_date": u.latest_date,
            "url": u.url,
            "asset": u.asset,
            "available": u.available,
            "notes": u.notes,
            "error": u.error,
            "checked": u.checked,
            "enabled": bool(repo) and repo.lower() != "none",
            # installing from the app: the single-file binaries (Docker and source installs: the notice only)
            "installable": self.app.can_self_update(),
            "pending": self.app.update_pending,
            "jobs": sum(1 for j in self.app.jobs.jobs() if j.finished_at is None),
        }

    async def updates(self) -> dict[str, Any]:
        """Settings → Updates: the running version and the last check's result."""
        return self._updates()

    async def check_updates(self) -> dict[str, Any]:
        await self.app.check_updates()
        return await self.updates()

    async def device_allowlist(self, device_ids: list[str] | None = None) -> str:
        """tests/hardware.toml for the registered devices `device_ids` (all when None); unknown ids → 404."""
        from syncmusic.infra import allowlist_toml

        devices = await self.app.store.devices()
        if device_ids:
            known = {d.id for d in devices}
            missing = [i for i in device_ids if i not in known]
            if missing:
                raise SyncError(f"unknown device {missing[0]!r}", "not_found")
            devices = [d for d in devices if d.id in device_ids]
        return allowlist_toml.render(devices)

    async def install_update(self, when: str = "after_syncs") -> dict[str, Any]:
        """Settings → Updates → Update now: install the newer binary the last check found and restart into it.
        `after_syncs` with jobs running waits for them (the scheduler installs it, `waiting`); `now` interrupts
        them - they resume after the restart. Refused (409) without a found update or when it cannot be installed."""
        u = self.app.updates
        if not u.available or not u.latest:
            raise SyncError(
                "there is no newer version to install: check for updates first", "conflict"
            )
        if not self._updates()["installable"]:
            raise SyncError(
                "this install does not update itself: see Settings → Updates", "conflict"
            )
        if when == "after_syncs" and any(j.finished_at is None for j in self.app.jobs.jobs()):
            self.app.update_pending = True
            return {"waiting": True, "version": u.latest}
        try:
            version = await self.app.install_update()
        except self_update.Refused as e:
            log.warning("update to %s not installed: %s", u.latest, e)
            raise SyncError(f"the update cannot be installed: {e}", "conflict") from None
        return {"installing": version}

    async def cache_info(self) -> dict[str, Any]:
        """Settings → Cache & space: the conversion cache's size and limit, and the disk holding it."""
        total, free = await asyncio.to_thread(disk_free, self.app.config.cache_dir)
        return {
            "used": await self.app.cache.total_size(),
            "max": self.app.cache.max_bytes,
            "pinned": await self.app.cache.pinned_size(),
            "system": await asyncio.to_thread(_tree_size, self._system_cache_dirs()),
            "disk_total": total,
            "disk_free": free,
        }

    def _system_cache_dirs(self) -> list[Path]:
        """The system cache: thumbnails and previews - small, derived, rebuilt on demand."""
        return [self.app.config.cache_dir / d for d in SYSTEM_CACHE_DIRS]

    async def clear_cache(self, kind: str = "media") -> dict[str, Any]:
        """Empty the conversion (`media`) cache, refused while a job runs (it may be reading or filling it), or the
        `system` cache (thumbnails, previews: safe at any time)."""
        if kind == "system":
            dirs = self._system_cache_dirs()
            before = await asyncio.to_thread(_tree_size, dirs)
            for d in dirs:
                await asyncio.to_thread(shutil.rmtree, d, True)
            log.info("system cache cleared: %d bytes", before)
            return {"freed": before, "entries": None, **await self.cache_info()}
        if kind != "media":
            raise SyncError(f"unknown cache {kind!r}", "invalid")
        if any(j.finished_at is None for j in self.app.jobs.jobs()):
            raise SyncError("a job is running: clear the cache when it has finished", "busy")
        before = await self.app.cache.total_size()
        removed = await self.app.cache.evict(0)
        after = await self.app.cache.total_size()
        log.info("cache cleared: %d entries, %d bytes", len(removed), before - after)
        return {"freed": before - after, "entries": len(removed), **await self.cache_info()}

    # --- Settings → System → Storage & resources --------------------------------------------------

    def _busy(self) -> bool:
        return any(j.finished_at is None for j in self.app.jobs.jobs())

    async def storage(self) -> dict[str, Any]:
        """What the app keeps on disk, part by part, with the file systems holding it."""
        cfg = self.app.config
        settings = await self.app.store.app_settings()
        db = cfg.data_dir / "syncmusic.sqlite3"
        logs = cfg.logs
        backups = self.app.backup_dir(settings)
        rows = await self.app.db.fetchall(
            "SELECT library_id, length(data) AS size FROM snapshots ORDER BY size DESC"
        )
        runtime = self_update.SERVICE_RUNTIME

        def build() -> list[storage.Part]:
            parts = [
                storage.Part(
                    "database",
                    "Database",
                    "settings, devices, history and job logs; never deleted here",
                    [db, db.with_name(db.name + "-wal")],
                    action="Compact",
                ),
                storage.Part(
                    "db_backups",
                    "Database backups",
                    "automatic and manual backups of the database; the newest one is always kept",
                    [backups],
                    action="Delete old",
                ),
                storage.Part(
                    "logs",
                    "Logs",
                    "the current log and the rotated older ones",
                    [logs],
                    action="Clear old",
                ),
                storage.Part(
                    "media_cache",
                    "Media cache",
                    "converted and downloaded files kept for the next sync",
                    [cfg.cache_dir / "entries"],
                    action="Clear",
                ),
                storage.Part(
                    "system_cache",
                    "System cache",
                    "thumbnails and previews; rebuilt when needed",
                    self._system_cache_dirs(),
                    action="Clear",
                ),
                storage.Part(
                    "device_backups",
                    "Device database backups",
                    "copies of players' databases taken before a sync: restore points",
                    [cfg.cache_dir / "device-backups"],
                    action="Delete",
                ),
                storage.Part(
                    "device_tags",
                    "Player contents tags",
                    "tags read from players' files, so Player contents opens fast",
                    [cfg.cache_dir / "device-tags"],
                    action="Clear",
                ),
                storage.Part(
                    "library_mirrors",
                    "Server folder copies",
                    "local copies of FTP / SMB folder libraries; downloaded again when needed",
                    [cfg.cache_dir / "library-mirror"],
                    action="Delete",
                ),
                storage.Part(
                    "temp",
                    "Temporary files",
                    "work files of conversions; left over only after a crash",
                    [cfg.cache_dir / "work"],
                    action="Clear",
                ),
            ]
            if runtime.is_dir():
                parts.append(
                    storage.Part(
                        "runtime",
                        "Unpacked program",
                        "the program files of the installed version (and the previous one)",
                        [runtime],
                    )
                )
            for part in parts:
                part.size, part.files = storage.tree_size(part.paths)
            by_id = {p.id: p for p in parts}
            by_id["device_backups"].items = storage.subfolders(cfg.cache_dir / "device-backups")
            by_id["device_tags"].items = (
                [
                    {"id": f.stem, "label": f.stem, "size": f.stat().st_size}
                    for f in sorted((cfg.cache_dir / "device-tags").glob("*.json"))
                ]
                if (cfg.cache_dir / "device-tags").is_dir()
                else []
            )
            by_id["library_mirrors"].items = storage.subfolders(cfg.cache_dir / "library-mirror")
            found = dbbackup.backups(backups)
            by_id["db_backups"].detail = f"{len(found)} backups" + (
                f", newest {found[0][1].isoformat(timespec='minutes')}" if found else ""
            )
            return parts

        parts = await asyncio.to_thread(build)
        snap_size = sum(int(r["size"]) for r in rows)
        parts.insert(
            3,
            storage.Part(
                "snapshots",
                "Library snapshots",
                "the libraries' contents as last read, inside the database; reloaded when dropped",
                [db],
                size=snap_size,
                files=len(rows),
                action="Drop",
                items=[
                    {"id": r["library_id"], "label": r["library_id"], "size": int(r["size"])}
                    for r in rows
                ],
            ),
        )
        paths = [cfg.data_dir, cfg.cache_dir, logs, backups]
        out = [p.as_dict() for p in parts]
        return {
            "parts": out,
            "total": sum(
                p.size for p in parts if p.id != "snapshots"
            ),  # snapshots are in the database
            "filesystems": await asyncio.to_thread(storage.filesystems, paths),
            "busy": self._busy(),
        }

    async def storage_clear(
        self, part: str, item: str | None = None, keep: int = 1
    ) -> dict[str, Any]:
        """Clear one part (or one device's / library's item of it); returns the bytes freed and the new report.
        Refused (busy) while a job runs, except for the parts no job uses (system cache, old logs and backups)."""
        cfg = self.app.config
        safe_any_time = {"system_cache", "logs", "db_backups"}
        if part not in safe_any_time and self._busy():
            raise SyncError("a job is running: clear this when it has finished", "busy")
        before = {p["id"]: p for p in (await self.storage())["parts"]}
        if part not in before or before[part]["action"] is None:
            raise SyncError(f"{part!r} cannot be cleared", "invalid")
        names = {i["id"] for i in before[part]["items"]}
        if item is not None and item not in names:
            raise SyncError(f"{part!r} has no {item!r}", "not_found")

        def rm(path: Path) -> None:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

        if part == "media_cache":
            await self.clear_cache("media")
        elif part == "system_cache":
            await self.clear_cache("system")
        elif part == "database":
            await self.app.db.execute("VACUUM")
        elif part == "db_backups":
            await asyncio.to_thread(
                dbbackup.prune,
                self.app.backup_dir(await self.app.store.app_settings()),
                max(1, keep),
            )
        elif part == "logs":
            for f in await asyncio.to_thread(lambda: list(cfg.logs.glob("syncmusic.log.*"))):
                await asyncio.to_thread(rm, f)
        elif part == "snapshots":
            ids = [item] if item else sorted(names)
            for lid in ids:
                await self.app.sync.snapshots.invalidate(lid)
        else:
            folder = {
                "device_backups": cfg.cache_dir / "device-backups",
                "device_tags": cfg.cache_dir / "device-tags",
                "library_mirrors": cfg.cache_dir / "library-mirror",
                "temp": cfg.cache_dir / "work",
            }[part]
            target = (
                folder / (f"{item}.json" if part == "device_tags" else item) if item else folder
            )
            await asyncio.to_thread(rm, target)
        after = await self.storage()
        now = next(p for p in after["parts"] if p["id"] == part)
        freed = max(0, before[part]["size"] - now["size"])
        log.info("storage: %s%s cleared, %d bytes freed", part, f" ({item})" if item else "", freed)
        return {"freed": freed, **after}

    async def resources(self) -> dict[str, Any]:
        """Memory and CPU of the process now, the event-loop lag, and the in-memory caches."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.sleep(0)
        lag = loop.time() - t0
        return {
            **await asyncio.to_thread(storage.process_memory),
            **self.app.cpu.read(),
            "loop_lag_ms": round(lag * 1000, 2),
            "caches": self.app.sync.memory_caches(),
            "busy": self._busy(),
        }

    async def free_memory(self) -> dict[str, Any]:
        """Drop the in-memory caches that are rebuilt on demand (refused while a job runs)."""
        if self._busy():
            raise SyncError("a job is running: free memory when it has finished", "busy")
        before = (await asyncio.to_thread(storage.process_memory))["rss"]
        self.app.sync.drop_memory_caches()
        await asyncio.to_thread(gc.collect)
        await asyncio.to_thread(storage.trim_memory)
        after = await self.resources()
        return {"freed": max(0, before - after["rss"]), **after}

    async def ffmpeg_check(self, path: str | None = None) -> dict[str, Any]:
        """Settings → Conversion: version and encoders of the ffmpeg at `path` (default: the one in use)."""
        return await asyncio.to_thread(ffmpeg.check, path or None)

    async def diagnostics(self) -> list[dict[str, Any]]:
        """Settings → Maintenance → System check: the install's checks (doctor: versions,
        ffmpeg, space, folders, device access, udev, clock, Docker), library connections, USB devices, the Pi's
        temperature and throttling; each {id, label, detail, state: ok / warning / error / unknown, fix}."""
        settings = await self.app.store.app_settings()
        checks = await asyncio.to_thread(
            doctor.local_checks, self.app.config, settings.disk_warn_gb
        )

        async def one(cfg: LibraryConfig) -> tuple[str, bool]:
            try:
                lib = await self.app.sync.library(cfg.id)
                health = await asyncio.wait_for(lib.check(), 20)
            except Exception as e:  # noqa: BLE001 - a result, not an error
                return f"{cfg.name}: {type(e).__name__}", False
            return (f"{cfg.name} OK" if health.ok else f"{cfg.name}: {health.message}"), health.ok

        libs = [c for c in await self.app.store.libraries() if c.enabled]
        results = await asyncio.gather(*(one(c) for c in libs))
        bad = sum(not ok for _, ok in results)
        checks.append(
            doctor.item(
                "libraries",
                "Library connections",
                " · ".join(text for text, _ in results) or "no libraries",
                "warning" if bad else "ok",
                "check the address and login under Libraries → the library → Settings",
            )
        )
        listed = await self.app.sync.list_devices()
        connected = sum(1 for d in listed["registered"] if d["connected"] and d["kind"] == "disk")
        unknown = len(listed["unknown"])
        usb = (
            f"{connected} registered connected" if connected else "no registered device connected"
        ) + (f" · {unknown} not registered" if unknown else "")
        checks.append(diagnostics.check("usb", "USB devices", usb, "ok"))
        checks.append(await asyncio.to_thread(diagnostics.temperature))
        checks.append(await asyncio.to_thread(diagnostics.throttling))
        for c in checks:
            c.setdefault("fix", "")
        return checks

    async def _restart(self, between: Callable[[Application], Any] | None = None) -> None:
        """Stop the application, run `between(old app)` while it is stopped, start a fresh one in its place - every
        in-memory state (libraries, devices, plans, snapshots) is built again from the database."""
        old = self.app
        await old.stop()
        if between is not None:
            await between(old)
        new = Application(old.config)
        await new.start()
        self.app = new
        self._suggest_loads.clear()

    # --- backup ------------------------------------------------------------------

    async def backup(self, secrets: bool = False) -> dict[str, Any]:
        """Settings → Backup → Download backup: everything a restore replaces, as one JSON document."""
        store = self.app.store
        data = backup.Backup(
            app_version=__version__,
            secrets=True,
            settings=(await store.app_settings()).model_dump(mode="json"),
            libraries=await store.libraries(),
            devices=await store.devices(),
            templates=await store.stored_templates(),
            processor_templates=await store.stored_processor_templates(),
            deleted_default_templates=await store.deleted_default_templates(),
            ignored_disks=await store.ignored_disks(),
        )
        if not secrets:
            data = backup.strip_secrets(data, _secret_keys, _device_secret_keys)
        return data.model_dump(mode="json")

    def _parse_backup(self, data: dict[str, Any]) -> backup.Backup:
        if data.get("format") != backup.FORMAT:
            raise SyncError("this is not a SyncMusic backup", "invalid")
        try:
            return backup.Backup.model_validate(data)
        except ValidationError as e:
            raise SyncError(f"the backup cannot be read: {pydantic_message(e)}", "invalid") from e

    async def restore_preview(self, data: dict[str, Any]) -> dict[str, Any]:
        """What Restore replaces (counts), what it removes (names of what is here now and not in the file), whether it
        has the passwords, and what will not work after it."""
        parsed = self._parse_backup(data)
        try:
            out = backup.summary(
                parsed,
                {c.type_name for c in registry.library_classes()},
                {c.type_name for c in registry.device_classes()},
            )
        except ValidationError as e:
            raise SyncError(
                f"the backup's settings are invalid: {pydantic_message(e)}", "invalid"
            ) from e
        # a restore replaces everything: what exists now and is not in the file goes
        store = self.app.store
        out["removes"] = backup.removed(
            parsed,
            await store.libraries(),
            await store.devices(),
            await store.stored_templates(),
            await store.stored_processor_templates(),
        )
        return out

    async def restore(self, data: dict[str, Any]) -> dict[str, Any]:
        """Replace libraries, devices, templates and settings by the backup's (refused while a job runs); history,
        listening data and the cache stay. The application restarts in place."""
        parsed = self._parse_backup(data)
        preview = await self.restore_preview(data)
        if any(j.finished_at is None for j in self.app.jobs.jobs()):
            raise SyncError("a job is running: restore when it has finished", "busy")
        store = self.app.store
        parsed = backup.keep_current_secrets(
            parsed,
            {c.id: c for c in await store.libraries()},
            {c.id: c for c in await store.devices()},
            (await store.app_settings()).webhook_url,
            _secret_keys,
            _device_secret_keys,
        )

        async def replace(app: Application) -> None:
            await app.db.open()
            try:
                await app.store.replace_config(
                    AppSettings.model_validate(parsed.settings),
                    parsed.libraries,
                    parsed.devices,
                    parsed.templates,
                    parsed.processor_templates,
                    parsed.deleted_default_templates,
                    parsed.ignored_disks,
                )
            finally:
                await app.db.close()

        log.warning("restoring a backup from %s", parsed.created_at.isoformat())
        await self._restart(replace)
        return preview

    # --- database backups ------------------------------------------------------

    async def db_backups(self) -> list[dict[str, Any]]:
        """Settings → Backup: the automatic database backups, newest first."""
        folder = self.app.backup_dir(await self.app.store.app_settings())
        found = await asyncio.to_thread(dbbackup.backups, folder)
        return [
            {
                "name": p.name,
                "created_at": at.isoformat(),
                "reason": reason,
                "size": p.stat().st_size,
            }
            for p, at, reason in found
        ]

    async def make_db_backup(self) -> dict[str, Any]:
        """A database backup now ("manual"); the oldest beyond the setting go."""
        path = await self.app.backup_db("manual")
        return next(b for b in await self.db_backups() if b["name"] == path.name)

    async def db_backup_file(self, name: str) -> Path:
        folder = self.app.backup_dir(await self.app.store.app_settings())
        path = dbbackup.find(folder, name)
        if path is None:
            raise SyncError(f"unknown backup {name!r}", "not_found")
        return path

    async def restore_db_backup(self, name: str) -> dict[str, Any]:
        """Replace the whole database (settings, libraries, devices, templates, history, listening data) by a
        backup; the current one is backed up first ("before-restore"). The application restarts in place."""
        src = await self.db_backup_file(name)
        if any(j.finished_at is None for j in self.app.jobs.jobs()):
            raise SyncError("a job is running: restore when it has finished", "busy")
        data_dir = self.app.config.data_dir
        staged = data_dir / "restore.sqlite3.part"
        try:
            await asyncio.to_thread(dbbackup.unpack, src, staged)
        except ValueError as e:
            raise SyncError(str(e), "invalid") from e
        await self.app.backup_db("before-restore")
        log.warning("restoring the database backup %s", name)

        async def replace(app: Application) -> None:
            def swap() -> None:
                for f in data_dir.glob("syncmusic.sqlite3*"):
                    f.unlink(missing_ok=True)
                staged.replace(data_dir / "syncmusic.sqlite3")

            await asyncio.to_thread(swap)

        await self._restart(replace)
        return {"ok": True, "restored": name}

    # --- import / export ----------------------------------------------------------

    async def export(self, what: Literal["templates", "devices"]) -> dict[str, Any]:
        """Settings → Import / export: the user's rule and processor templates, or every device's settings with
        the templates they use; no secrets."""
        store = self.app.store
        templates = await store.stored_templates()
        procs = await store.stored_processor_templates()
        devices: list[dict[str, Any]] = []
        if what == "devices":
            cfgs = await store.devices()
            used = {t for c in cfgs for t in c.rule_set.template_ids}
            templates = [t for t in templates if t.id in used]
            procs = [p for p in procs if p.id in {c.processor_template_id for c in cfgs}]
            devices = [transfer.device_part(c, _device_secret_keys(c.type)) for c in cfgs]
        libs = [
            transfer.LibraryRef(id=c.id, name=c.name, type=c.type) for c in await store.libraries()
        ]
        data = transfer.Export(
            app_version=__version__,
            libraries=libs,
            templates=templates,
            processor_templates=procs,
            devices=devices,
        )
        return data.model_dump(mode="json")

    def _parse_export(self, data: dict[str, Any]) -> transfer.Export:
        if data.get("format") != transfer.FORMAT:
            raise SyncError(
                "this is not a SyncMusic export (a backup is restored under Backup)", "invalid"
            )
        try:
            return transfer.Export.model_validate(data)
        except ValidationError as e:
            raise SyncError(f"the export cannot be read: {pydantic_message(e)}", "invalid") from e

    async def import_preview(self, data: dict[str, Any]) -> dict[str, Any]:
        store = self.app.store
        return transfer.plan(
            self._parse_export(data),
            await store.libraries(),
            await store.templates(),
            await store.processor_templates(),
            await store.devices(),
        )

    async def import_apply(self, data: dict[str, Any]) -> dict[str, Any]:
        """Add / replace the templates and apply device settings as the preview says (refused while a job runs)."""
        parsed = self._parse_export(data)
        result = await self.import_preview(data)
        if any(j.finished_at is None for j in self.app.jobs.jobs()):
            raise SyncError("a job is running: import when it has finished", "busy")
        store = self.app.store
        libs = transfer.library_map(parsed.libraries, await store.libraries())
        todo = {(c["kind"], c["name"]): c for c in result["changes"] if c["action"] != "skip"}
        for tpl in parsed.templates:
            if ("template", tpl.name) in todo:
                await store.put_template(transfer.mapped_template(tpl, libs))
        for ptpl in parsed.processor_templates:
            if ("processor_template", ptpl.name) in todo:
                await store.put_processor_template(ptpl.model_copy(update={"updated_at": utcnow()}))
        for part in parsed.devices:
            change = todo.get(("device", part.get("name")))
            if change is None:
                continue
            cfg = await self.app.sync.config(change["id"])
            mapped = transfer.mapped_device(part, libs)
            secrets = _device_secret_keys(cfg.type)
            mapped["settings"] = {
                **mapped.get("settings", {}),
                **{k: v for k, v in cfg.settings.items() if k in secrets},
            }
            merged = DeviceConfig.model_validate({**cfg.model_dump(mode="json"), **mapped})
            await self.app.sync.update_device(merged)
        return result

    async def factory_reset(self, confirm: str) -> dict[str, Any]:
        """Settings → Maintenance → Factory reset: every library, device, template, the history,
        notifications and settings are deleted (the database and the cache start empty); files on devices and in
        libraries are not touched. The application is stopped, its data removed and a fresh one started in place."""
        if confirm != FACTORY_RESET_PHRASE:
            raise SyncError(f"type {FACTORY_RESET_PHRASE!r} to confirm", "confirmation_required")
        if any(j.finished_at is None for j in self.app.jobs.jobs()):
            raise SyncError("a job is running: reset when it has finished", "busy")
        await self.app.backup_db("reset")  # the old state can still be restored
        log.warning("factory reset: deleting the database and the cache")

        async def remove(app: Application) -> None:
            await asyncio.to_thread(_remove_app_data, app.config)

        await self._restart(remove)
        return {"ok": True}

    async def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        if changes.get("webhook_url") == SECRET_MASK:
            changes = {k: v for k, v in changes.items() if k != "webhook_url"}
        merged = {**(await self.app.store.app_settings()).model_dump(mode="json"), **changes}
        settings = AppSettings.model_validate(merged)
        if changes.get("ffmpeg_path"):  # a path that does not run is refused, not saved silently
            found = await self.ffmpeg_check(settings.ffmpeg_path)
            if found["error"]:
                raise SyncError(found["error"], "invalid")
        await self.app.store.put_app_settings(settings)
        self.app.apply_settings(settings)
        return _public_settings(settings)

    # --- devices ---------------------------------------------------------------------------------

    async def devices(self) -> dict[str, Any]:
        return await self.app.sync.list_devices()

    async def ignore_disk(self, disk: str, ignore: bool = True) -> dict[str, Any]:
        await self.app.sync.ignore_disk(disk, ignore)
        return await self.app.sync.list_devices()

    async def device_diagnostics(self) -> dict[str, Any]:
        """Why a plugged disk is (not) listed: privileges, udev monitor, scan errors and every disk
        the system sees with what SyncMusic made of it."""
        watcher = self.app.watcher
        euid = os.geteuid()
        route = await asyncio.to_thread(privilege_route)
        if euid == 0:
            privileges = "root"
        elif len(route) > 2:  # sudo -n <helper>: the root helper of
            privileges = "helper"
        else:
            privileges = "sudo" if await _sudo_works() else "none"
        connected = self.app.sync.detector.connected
        disks: list[dict[str, Any]] = []
        scan_error = watcher.status["scan_error"]
        try:
            seen = await asyncio.to_thread(watcher.scan)
        except Exception as e:  # noqa: BLE001 - reported, not raised
            seen, scan_error = [], str(e)
        for d in seen:
            cd = connected.get(d.name)
            if cd is not None and cd.device_id:
                state, reason = "registered", f"matched device {cd.device_id}"
            elif cd is not None:
                state, reason = "unknown", "listed as an unknown device (register it)"
            elif not watcher.wanted(d) and d.type == "loop":
                state, reason = "ignored", "loop device (loop devices are off)"
            elif not watcher.wanted(d):
                state, reason = "ignored", f"not a USB disk (transport {d.tran or 'none'})"
            else:
                state, reason = (
                    "pending",
                    "USB disk not processed yet (detection off or still settling)",
                )
            disks.append(
                {
                    "disk": d.path,
                    "model": " ".join(x for x in (d.vendor, d.model) if x) or None,
                    "size": d.size,
                    "tran": d.tran,
                    "removable": d.rm,
                    "serial": d.serial,
                    "state": state,
                    "reason": reason,
                }
            )
        hints = []
        if not self.app.config.detect_devices:
            hints.append("device detection is off (detect_devices = false)")
        if scan_error:
            hints.append(f"the block device scan fails: {scan_error}")
        if watcher.status["udev"] != "running":
            hints.append(
                f"hot plug is not followed (udev {watcher.status['udev']}): restart after plugging"
            )
        if privileges == "none":
            hints.append(
                "not root, no passwordless sudo and no syncmusic-priv helper: devices are listed but cannot "
                "be mounted, synced or formatted (run the service, or install with --user, see README 'Running "
                "as a normal user')"
            )
        return {
            "user": getpass.getuser(),
            "euid": euid,
            "privileges": privileges,
            "detect_devices": self.app.config.detect_devices,
            "udev": watcher.status["udev"],
            "last_event": watcher.status["last_event"],
            "scan_error": scan_error,
            "disks": disks,
            "hints": hints,
        }

    async def device(self, device_id: str) -> dict[str, Any]:
        cfg = await self.app.sync.config(device_id)
        reports = await self.app.sync.reports(device_id)
        full = [r for r in reports if r.kind == "full"]
        update = next((r for r in reports if r.kind == "update"), None)
        nxt = update_status.next_update_check(self.app.sync, cfg)
        last = full[0].model_dump(mode="json") if full else None
        if last:  # the Overview's "Full report" opens the job that wrote it
            last["job_id"] = (await self.app.store.report_jobs([full[0].id])).get(full[0].id)
        try:
            cls = registry.device_class(cfg.type)
        except KeyError:
            # a type this version does not know (renamed or removed): the page still
            # loads, says so and offers Remove
            return {
                "config": _public_device(cfg),
                "unknown_type": True,
                "libraries": [],
                "network": False,
                "usb_mtp": False,
                "last_report": last,
                "last_update": update.model_dump(mode="json") if update else None,
                "erased": None,
                "health_warning": None,
                "next_update_check": None,
                "update": None,
            }
        return {
            "unknown_type": False,
            "config": _public_device(cfg),
            # the libraries its templates pull from
            "libraries": await self.app.sync.libraries_of(cfg),
            # a server: nothing to eject; an MTP player can be ejected
            "network": cls.network and not cls.usb_mtp,
            "usb_mtp": cls.usb_mtp,
            # the last full sync; update syncs have their own line
            "last_report": last,
            "last_update": update.model_dump(mode="json") if update else None,
            # a wipe / format since the last full sync
            "erased": await self.app.sync.erased(cfg),
            # "Storage may be failing": the plan review warns before writing
            "health_warning": None
            if cls.network
            else (await self.app.sync.health(cfg.id))["warning"],
            "next_update_check": nxt.isoformat() if nxt else None,
            "update": await update_status.update_state(self.app.sync, cfg),  # the v2 update line
        }

    async def suggest_processor_template(self, req: dict[str, Any]) -> dict[str, Any]:
        """Score every offered template on the items
        of the chosen libraries against the target's capabilities and free space. With `device_id` the
        registered device's config fills in the rest; its target is the connected disk or its mount path."""
        sync = self.app.sync
        device: DeviceConfig | None = None
        plan = sync.get_plan(req["plan_id"]).plan if req.get("plan_id") else None
        if plan is not None:
            req = {**req, "device_id": plan.device_id}
        if req.get("device_id"):
            device = await sync.config(req["device_id"])
            cd = next(
                (c for c in sync.detector.connected.values() if c.device_id == device.id), None
            )
            req = {
                **req,
                "type": device.type,
                "name": device.name,
                "library_ids": await sync.libraries_of(device),
                "template_ids": [],
                "disk": cd.disk.path if cd is not None else None,
                "mount_path": device.mount_path,
            }
        try:
            cls = registry.device_class(req["type"])
        except KeyError as e:
            raise SyncError(f"unknown device type {req['type']!r}", "invalid") from e
        all_libs = [lib.id for lib in await self.app.store.libraries()]
        lib_ids = set(req["library_ids"])
        for tid in req["template_ids"]:
            tpl = await self.app.store.template(tid)
            if tpl is not None:
                lib_ids |= set(tpl.library_ids or all_libs)
        tracks: list[Track] = []
        # a registered device without libraries has nothing to judge; Register falls back to every library
        libs = sorted(lib_ids & set(all_libs) if lib_ids or device else all_libs)
        if plan is not None:  # what the rules select for this device, not the whole libraries
            tracks = [a.item.track for a in plan.actions if a.item is not None]
            libs = []
        for lid in libs:
            try:
                snap = await sync.snapshots.get(await sync.library(lid))
            except Exception as e:  # noqa: BLE001 - an unreachable library counts as empty
                log.info("suggest: library %s not read: %s", lid, e)
                continue
            tracks += snap.tracks.values()
        caps, free = cls.capabilities, None
        path = Path(req["mount_path"]) if req["mount_path"] else None
        cd = (
            next((c for c in sync.detector.connected.values() if c.disk.path == req["disk"]), None)
            if req["disk"]
            else None
        )
        if cd is not None:
            path = cd.mountpoint
            free = cd.disk.size or None
            model = next((p.model for p in cd.probes if p.type_name == cls.type_name), None)
            caps = pf.apply_profile(caps, pf.resolve_profile(cls.type_name, model, caps))
        if path is not None and path.is_dir():
            cfg = device or DeviceConfig(
                id="suggest", type=cls.type_name, name=req["name"] or "suggest"
            )
            caps = cls(cfg, path, None).effective_capabilities()
            free = shutil.disk_usage(path).free
        if plan is not None:
            # the space the plan's items may use: free now, plus what every file the plan replaces, keeps or
            # removes takes, minus the plan's reserve
            sp = plan.space
            free = sp.free + sum(a.entry.size for a in plan.actions if a.entry) - sp.reserve
        offered = [
            t
            for t in (await self.app.store.processor_templates()).values()
            if t.device_type in (None, cls.type_name)
        ]
        s = await asyncio.to_thread(
            suggest,
            offered,
            tracks,
            caps,
            free,
            default_id=DEFAULT_PROCESSOR_TEMPLATES.get(cls.type_name),
            device_type=cls.type_name,
            device_name=req["name"],
        )
        # the suggestion is what the device already runs (its own chain equals the template's after defaults):
        # nothing to suggest
        same = bool(
            device is not None
            and s.template_id
            and (tpl := next((t for t in offered if t.id == s.template_id), None)) is not None
            and (
                device.processor_template_id == tpl.id
                or same_chain(device.processors, tpl.processors)
            )
        )
        return {
            "template_id": s.template_id,
            "same_as_current": same,
            "draft": s.draft.model_dump(mode="json", exclude={"id"}) if s.draft else None,
            "reason": s.reason,
            "candidates": [
                {
                    "id": c.id,
                    "name": c.name,
                    "needed": c.needed,
                    "fits": c.fits(free),
                    "unplayable": sum(c.unplayable.values()),
                    "converted": c.converted,
                }
                for c in s.candidates
            ],
            # generated chains to choose from, the best one preselected
            "variants": [
                {
                    "key": v.key,
                    "to": v.to,
                    "quality": v.quality,
                    "processors": [p.model_dump(mode="json") for p in v.processors],
                    "needed": v.score.needed,
                    "fits": v.score.fits(free),
                    "converted": v.score.converted,
                }
                for v in s.variants
            ],
            "best_variant": s.best_variant,
            "free": free,
        }

    async def register_device(self, req: RegisterRequest) -> dict[str, Any]:
        reason = _type_unavailable(req.type) if req.type else None
        if reason:
            raise SyncError(reason, "invalid")
        return _public_device(await self.app.sync.register(req))

    async def update_device(self, device_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        cfg = await self.app.sync.config(device_id)
        # `erased` is set by Wipe / Format only
        unknown = sorted(
            set(changes) - set(DeviceConfig.model_fields) | ({"erased"} & set(changes))
        )
        if unknown:
            raise SyncError(f"unknown device fields: {', '.join(unknown)}", "invalid")
        if changes.get("id", device_id) != device_id:
            raise SyncError("the device id cannot be changed", "invalid")
        changes = await self._processor_link(cfg, changes)
        if isinstance(
            changes.get("settings"), dict
        ):  # a secret sent back as the mask keeps the stored value
            secrets = _device_secret_keys(cfg.type)
            changes = {
                **changes,
                "settings": {
                    k: cfg.settings.get(k) if k in secrets and v == SECRET_MASK else v
                    for k, v in changes["settings"].items()
                },
            }
        if isinstance(changes.get("library_accounts"), dict):
            for lid, aid in changes["library_accounts"].items():
                lib = await self.app.store.library(lid)
                if aid and (lib is None or aid not in lib.accounts):
                    raise SyncError(f"library {lid!r} has no account {aid!r}", "invalid")
            changes = {
                **changes,
                "library_accounts": {k: v for k, v in changes["library_accounts"].items() if v},
            }
        merged = {**cfg.model_dump(mode="json"), **changes}
        saved = await self.app.sync.update_device(DeviceConfig.model_validate(merged))
        return _public_device(saved)

    async def check_device(self, device_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Test a server device's connection with changed settings before they are saved; a secret
        sent back as the mask is the stored one."""
        cfg = await self.app.sync.config(device_id)
        cls = registry.device_class(cfg.type)
        if not cls.network:
            raise SyncError(f"{cfg.name} is not a server: nothing to test", "invalid")
        secrets = _device_secret_keys(cfg.type)
        merged = {
            **cfg.settings,
            **{
                k: cfg.settings.get(k) if k in secrets and v == SECRET_MASK else v
                for k, v in settings.items()
            },
        }
        cls.Settings.model_validate(merged)
        await self.app.sync.check_network(cfg.model_copy(update={"settings": merged}))
        return {"ok": True}

    async def quality_profiles(self, device_id: str) -> list[dict[str, Any]]:
        """The device's quality profiles: id, name and the chain each makes for this device."""
        caps, suggested = await self._device_caps(device_id)
        return [
            {
                "id": p,
                "name": quality.NAMES[p],
                "processors": [
                    s.model_dump(mode="json") for s in quality.chain(p, caps, suggested)
                ],
            }
            for p in quality.PROFILES
        ]

    async def chain_lint(
        self, device_id: str, processors: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Warnings about the chain on screen for this device, each with the fixed chain."""
        try:
            slots = [ProcessorSlot.model_validate(p) for p in processors]
        except ValidationError as e:
            raise SyncError(f"invalid processors: {pydantic_message(e)}", "invalid") from e
        caps, _ = await self._device_caps(device_id)
        return chain_lint.lint(slots, caps)

    async def chain_preview_tracks(self, device_id: str, q: str = "") -> list[dict[str, Any]]:
        """Tracks to try the device's chain on: one per format first, from its templates' libraries;
        with `q` the items matching it instead."""
        cfg = await self.app.sync.config(device_id)
        snaps = await self.app.sync.library_snapshots(await self.app.sync.libraries_of(cfg), cfg)
        tracks = (
            chain_preview.search_tracks(snaps, q)
            if q.strip()
            else chain_preview.sample_tracks(snaps)
        )
        return [
            {
                "library_id": t.ref.library_id,
                "item_id": t.ref.item_id,
                "label": chain_preview.label(t),
            }
            for t in tracks
        ]

    async def chain_preview(
        self,
        device_id: str,
        library_id: str,
        item_id: str,
        processors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run the chain (the one on screen, else the device's) on one item: the before / after
        table and the result to download."""
        cfg = await self.app.sync.config(device_id)
        try:
            slots = (
                [ProcessorSlot.model_validate(p) for p in processors]
                if processors is not None
                else cfg.processors
            )
            chain = Chain.from_slots(slots, self.app.ctx)
        except ValidationError as e:
            raise SyncError(f"invalid processors: {pydantic_message(e)}", "invalid") from e
        snap = (await self.app.sync.library_snapshots([library_id]))[library_id]
        track = snap.tracks.get(item_id)
        if track is None:
            raise SyncError(f"no item {item_id!r} in {library_id!r}", "not_found")
        caps, _ = await self._device_caps(device_id)
        try:
            out = await chain_preview.run(
                self.app.sync.preparer,
                await self.app.sync.library(library_id),
                track,
                chain,
                caps,
                self.app.config.cache_dir,
            )
        except FfmpegError as e:
            raise SyncError(f"the chain failed on {track.title}: {e}", "invalid") from e
        out["download"] = f"/api/v1/chain-preview/{out['token']}/{quote(out['file'])}"
        return out

    def chain_preview_file(self, token: str, name: str) -> Path:
        path = chain_preview.result_file(self.app.config.cache_dir, token, name)
        if path is None:
            raise SyncError("this preview is gone: run the chain again", "not_found")
        return path

    async def _device_caps(self, device_id: str) -> tuple[Any, list[Any]]:
        cfg = await self.app.sync.config(device_id)
        cls = registry.device_class(cfg.type)
        device = cls(cfg, Path("/nonexistent"), self.app.ctx)
        caps = device.effective_capabilities()
        return caps, suggested_processors(caps)

    async def _processor_link(self, cfg: DeviceConfig, changes: dict[str, Any]) -> dict[str, Any]:
        """Linking a processor template copies its chain into the device; changing the chain of a linked device
        by hand detaches it. Choosing a quality profile makes its chain and detaches a
        template; a chain edited by hand is Custom again."""
        if changes.get("quality_profile"):
            caps, suggested = await self._device_caps(cfg.id)
            slots = quality.chain(changes["quality_profile"], caps, suggested)
            changes = {
                **changes,
                "processors": [s.model_dump(mode="json") for s in slots],
                "processor_template_id": None,
            }
        elif "processors" in changes and "quality_profile" not in changes:
            changes = {**changes, "quality_profile": None}
        if changes.get("processor_template_id"):
            changes = {**changes, "quality_profile": None}
        if changes.get("processor_template_id"):
            tpl = await self._processor_template(str(changes["processor_template_id"]))
            if "processors" not in changes:
                changes = {**changes, "processors": tpl.model_dump(mode="json")["processors"]}
        elif "processors" in changes and "processor_template_id" not in changes:
            if cfg.processor_template_id:
                changes = {**changes, "processor_template_id": None}
        return changes

    async def device_profile(self, device_id: str) -> dict[str, Any]:
        return await self.app.sync.profile(device_id)

    async def device_space(self, device_id: str) -> dict[str, Any]:
        return await self.app.sync.space(device_id)

    async def device_positions(self, device_id: str) -> list[dict[str, Any]]:
        return await self.app.sync.positions(device_id)

    async def device_feedback(self, device_id: str) -> dict[str, Any]:
        return await self.app.sync.feedback(device_id)

    async def clear_dropped_feedback(self, device_id: str) -> None:
        await self.app.sync.clear_dropped_feedback(device_id)

    async def device_contents(self, device_id: str, refresh: bool = False) -> list[dict[str, Any]]:
        rows = await self.app.sync.contents(device_id, refresh)
        etag = await self.app.sync.contents_etag(device_id)
        warm = self._thumb_warm.get(device_id)
        if etag is not None and (warm is None or warm[0] != etag):
            if warm is not None:
                warm[1].cancel()
            paths = [r["path"] for r in rows if not r.get("missing") and r.get("path")]
            task = asyncio.create_task(
                self._warm_thumbs(device_id, paths), name=f"thumbs-{device_id}"
            )
            self._thumb_warm[device_id] = (etag, task)
        return rows

    async def _warm_thumbs(self, device_id: str, rels: list[str]) -> None:
        """Make the thumbnails of every Player contents row in the background, one at a time:
        scrolling then serves them from the cache instead of reading each cover from the device on demand. The
        paths come from the contents rows: no manifest read per row  (thousands of full parses of a 13.8k-item manifest kept live at ~85 % CPU); one device at a time, with a pause between rows."""
        async with self._thumb_warm_lock:
            try:
                device = await self.app.sync.open_device(device_id)
                root = await asyncio.to_thread(device.mount.resolve)
            except Exception:  # noqa: BLE001 - unplugged meanwhile: the next contents read starts over
                return
            for rel in rels:
                try:
                    path = await asyncio.to_thread((root / rel).resolve)
                    if not path.is_relative_to(root):
                        continue
                    await self._path_thumb(device_id, path)
                except asyncio.CancelledError:
                    raise
                except FileNotFoundError:
                    continue
                except Exception:  # noqa: BLE001 - unplugged or changed meanwhile: the next contents read starts over
                    return
                await asyncio.sleep(WARM_PAUSE_S)

    async def device_contents_etag(self, device_id: str) -> str | None:
        return await self.app.sync.contents_etag(device_id)

    async def library_thumb(
        self, library_id: str, item: str, large: bool = False, account: str | None = None
    ) -> Path | None:
        """Small cover of a library row (`cover_item` of a track / album / artist / playlist row);
        `large`: for the browser's artwork grid; `account`: a playlist only that user has."""
        lib = await self.app.sync.library_as(library_id, account)
        size = thumbs.LARGE if large else thumbs.SIZE
        return await thumbs.thumbnail(
            self.app.config.cache_dir,
            f"lib:{lib.cache_key}",
            item,
            lambda: lib.open_thumb(item, size * 2),
            size,
        )

    async def device_thumb(self, device_id: str, key: str) -> Path | None:
        """Small cover of a Player contents row: the picture embedded in the device file, else the folder's
        cover.jpg / folder.jpg."""
        return await self._path_thumb(device_id, await self.app.sync.content_file(device_id, key))

    async def _path_thumb(self, device_id: str, path: Path) -> Path | None:
        st = await asyncio.to_thread(path.stat)
        return await thumbs.thumbnail(
            self.app.config.cache_dir,
            f"dev:{device_id}",
            f"{path}\0{st.st_size}\0{st.st_mtime_ns}",
            lambda: asyncio.to_thread(file_cover, path),
        )

    async def device_content_media(self, device_id: str, key: str) -> tuple[Path, str]:
        """(file, Content-Type) a browser can play: the device file itself, or an MP3 of it for codecs
        a browser may not decode (ALAC in Firefox)."""
        path = await self.app.sync.content_file(device_id, key)
        try:
            return await preview.browser_file(path, self.app.config.cache_dir)
        except FfmpegError as e:
            raise SyncError(f"cannot play {path.name}: {e}", "invalid") from e

    async def remove_from_device(
        self, device_id: str, keys: list[str], exclude: bool = True
    ) -> dict[str, Any]:
        job = await self.app.sync.remove_from_device(device_id, keys, exclude)
        return job.model_dump(mode="json")

    async def device_orphans(self, device_id: str) -> list[dict[str, Any]]:
        return await self.app.sync.orphans(device_id)

    async def remove_orphans(self, device_id: str) -> dict[str, Any]:
        return (await self.app.sync.remove_orphans(device_id)).model_dump(mode="json")

    async def add_to_device(
        self,
        device_id: str,
        library_id: str,
        ids: list[str],
        field: IdField = "track_id",
        remove: bool = False,
    ) -> dict[str, Any]:
        """ "Add to device…" from a library browser: append ids to the `Manual selection` rule of the device's own
        template, which draws from that library too. Blank ids and ids the library
        does not know are refused: they would pile up in the template and never match. `remove`: take
        them out again (the toast's Undo). + `added`: the ids that were not in the rule before."""
        cfg = await self.app.sync.config(device_id)
        ids = await self._selection_ids(library_id, ids, field, check=not remove)
        own = as_rules(await self.app.store.template(own_template_id(device_id)))
        rules, added = _changed_selection(own, field, ids, library_id, remove)
        return {**_public_device(await self.app.sync.set_own_rules(cfg, rules)), "added": added}

    async def add_to_template(
        self,
        template_id: str,
        library_id: str,
        ids: list[str],
        field: IdField = "track_id",
        remove: bool = False,
    ) -> dict[str, Any]:
        """The same for a template (every device linking it): its `Manual selection` rule. A template
        that draws from other libraries only is refused (its rules would never see these items)."""
        tpl = await self._template(template_id)
        self._writable(tpl)
        if tpl.library_ids and library_id not in tpl.library_ids:
            raise SyncError(f"{tpl.name!r} does not draw from {library_id!r}", "invalid")
        ids = await self._selection_ids(library_id, ids, field, check=not remove)
        rules, added = _changed_selection(as_rules(tpl), field, ids, library_id, remove)
        changes = {
            "rules": [r.model_dump(mode="json") for r in rules.rules],
            "excludes": [r.model_dump(mode="json") for r in rules.excludes],
        }
        return {**await self.update_template(template_id, changes), "added": added}

    async def _selection_ids(
        self, library_id: str, ids: list[str], field: IdField, check: bool = True
    ) -> list[str]:
        await self._library(library_id)
        ids = [i.strip() for i in ids]
        if not ids:
            raise SyncError("nothing to add", "invalid")
        if not all(ids):
            raise SyncError("an empty id cannot be added", "invalid")
        if not check:  # removing: ids the library lost meanwhile may go too
            return ids
        snap = await self._library_snapshot(library_id)  # the browser's cached snapshot
        tracks = snap.tracks.values()
        known = (
            set(snap.tracks)
            if field == "track_id"
            else {t.album_id for t in tracks}
            if field == "album_id"
            else {t.artist_id for t in tracks}
        )
        if unknown := [i for i in dict.fromkeys(ids) if i not in known]:
            shown = ", ".join(repr(i) for i in unknown[:5]) + (" …" if len(unknown) > 5 else "")
            raise SyncError(f"{library_id!r} has no {field.removesuffix('_id')} {shown}", "invalid")
        return ids

    async def delete_device(self, device_id: str) -> None:
        await self.app.sync.delete_device(device_id)

    async def eject_device(self, device_id: str) -> None:
        await self.app.sync.eject(device_id)

    async def bind_device_disk(self, device_id: str, disk: str | None = None) -> dict[str, Any]:
        return _public_device(await self.app.sync.bind_disk(device_id, disk))

    async def format_device(
        self, device_id: str, confirm: str, fs: str | None = None
    ) -> dict[str, Any]:
        cfg = await self.app.sync.config(device_id)
        cls = registry.device_class(cfg.type)
        if fs is not None and fs not in cls.format_filesystems:
            choices = ", ".join(FS_LABELS.get(f, f) for f in cls.format_filesystems)
            raise SyncError(f"{cls.display_name} can only be formatted as {choices}", "invalid")
        chosen = fs or cls.format_filesystems[0]
        if reason := filesystem_support().get(chosen):
            raise SyncError(
                f"cannot format as {FS_LABELS.get(chosen, chosen)}: {reason}", "invalid"
            )
        return _public_device(await self.app.sync.format(device_id, confirm, fs))

    async def wipe_device(self, device_id: str, confirm: str) -> dict[str, Any]:
        return (await self.app.sync.wipe(device_id, confirm)).model_dump(mode="json")

    async def peek_manifest(self, disk: str | None, mount_path: str | None) -> dict[str, Any]:
        return await self.app.sync.peek_manifest(disk, mount_path)

    async def device_home(self, device_id: str) -> dict[str, Any]:
        return await self.app.sync.home_info(device_id)

    async def make_home(self, device_id: str) -> dict[str, Any]:
        await self.app.sync.make_home(device_id)
        return await self.app.sync.home_info(device_id)

    async def device_health(self, device_id: str) -> dict[str, Any]:
        await self.app.sync.config(device_id)
        return await self.app.sync.health(device_id)

    async def acknowledge_health(self, device_id: str, undo: bool = False) -> dict[str, Any]:
        await self.app.sync.config(device_id)
        return await self.app.sync.acknowledge_health(device_id, undo)

    async def clear_health_errors(self, device_id: str) -> dict[str, Any]:
        await self.app.sync.config(device_id)
        return await self.app.sync.clear_health_errors(device_id)

    async def integrity_check(self, device_id: str, deep: bool = False) -> dict[str, Any]:
        return (await integrity_jobs.integrity_check(self.app.sync, device_id, deep)).model_dump(
            mode="json"
        )

    async def last_integrity(self, device_id: str) -> dict[str, Any] | None:
        await self.app.sync.config(device_id)
        return await integrity_jobs.last_integrity(self.app.sync, device_id)

    async def device_templates(self, device_id: str) -> list[dict[str, Any]]:
        return await self.app.sync.device_templates(device_id)

    async def import_device_templates(self, device_id: str, uids: list[str]) -> dict[str, Any]:
        return {"linked": await self.app.sync.import_device_templates(device_id, uids)}

    async def update_template_copy(self, device_id: str, template_id: str) -> dict[str, Any]:
        tpl = await self.app.sync.update_template_copy(device_id, template_id)
        return tpl.model_dump(mode="json")

    async def device_mounts(self, device_id: str) -> list[dict[str, Any]]:
        return await self.app.sync.mounts(device_id)

    async def unmount_everywhere(self, device_id: str, lazy: bool = False) -> list[dict[str, Any]]:
        return await self.app.sync.unmount_everywhere(device_id, lazy=lazy)

    async def repair_device(self, device_id: str) -> dict[str, Any]:
        return (await self.app.sync.repair(device_id)).model_dump(mode="json")

    async def integrity_fix(self, device_id: str) -> dict[str, Any]:
        return (await integrity_jobs.integrity_fix(self.app.sync, device_id)).model_dump(
            mode="json"
        )

    async def rename_device(
        self, device_id: str, name: str, preview: bool = False
    ) -> dict[str, Any]:
        """Rename on the device itself; `preview`: only what would be written."""
        if preview:
            return await self.app.sync.rename_preview(device_id, name)
        cfg, label = await self.app.sync.rename(device_id, name)
        return {"config": _public_device(cfg), "name": cfg.name, "label": label}

    async def play_log(self, device_id: str) -> dict[str, Any]:
        return await self.app.sync.play_log(device_id)

    async def set_play_log(self, device_id: str, on: bool) -> dict[str, Any]:
        return await self.app.sync.set_play_log(device_id, on)

    # --- sync ------------------------------------------------------------------------------------

    async def plan(self, device_id: str, force: bool = False) -> dict[str, Any]:
        """A plan for the UI/CLI: tracked with progress, joined by a second request for the device."""
        sync = self.app.sync
        try:
            pc = await sync.planning.run(
                device_id, force, lambda: sync.plan(device_id, force=force)
            )
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if me is not None and me.cancelling():
                raise  # this request itself went away
            raise SyncError("planning was cancelled", "cancelled") from None
        return plan_summary(pc)

    async def discover_servers(self, kinds: list[Kind], timeout_s: float) -> list[dict[str, Any]]:
        """Network discovery on demand."""
        return [s.model_dump() for s in await discovery.find_servers(kinds, timeout_s)]

    async def discover_shares(
        self, host: str, port: int, username: str, password: str
    ) -> list[dict[str, Any]]:
        try:
            shares = await discovery.list_shares(host, port, username, password)
        except discovery.DiscoveryError as e:
            raise SyncError(str(e), "unreachable") from e
        return [s.model_dump() for s in shares]

    def _found_player(self, disk: str) -> tuple[Any, str]:
        """A connected, not registered MTP player of the found devices: (its class, USB serial)."""
        from syncmusic.infra.blockdev.mtp import is_mtp

        cd = next(
            (c for c in self.app.sync.detector.connected.values() if c.disk.path == disk), None
        )
        if cd is None or not is_mtp(cd.disk) or not cd.probes:
            raise SyncError(f"no connected MTP player {disk}", "not_found")
        cls = registry.device_class(cd.probes[0].type_name)
        serial = cd.probes[0].identity.usb_serial
        if not getattr(cls, "usb_mtp", False) or not serial:
            raise SyncError(f"no connected MTP player {disk}", "not_found")
        return cls, serial

    async def _on_player(self, disk: str, fn: Any) -> Any:
        """Run `fn(backend)` in a thread on a short session to a found player; closed afterwards."""
        cls, serial = self._found_player(disk)
        allowlist = self.app.guard.allowlist

        # libmtp allows one session per device: overlapping requests queue here instead of failing with
        # "another MTP client holds it"
        lock = self._player_locks.setdefault(serial, threading.Lock())

        def work() -> Any:
            with lock:
                b = cls.open_player(serial, allowlist.serials if allowlist is not None else None)
                try:
                    return fn(b)
                finally:
                    b.close()

        try:
            return await asyncio.to_thread(work)
        except GuardError:
            raise  # 403 by the API's handler
        except (OSError, RuntimeError) as e:
            raise SyncError(str(e) or type(e).__name__, "unreachable") from e

    async def player_storages(self, disk: str) -> list[dict[str, Any]]:
        """The storages of a found MTP player (name, capacity, free) for Register's Storage list."""
        found = await self._on_player(disk, lambda b: b.storages())
        return [{"name": st.name, "capacity": st.capacity, "free": st.free} for st in found]

    async def browse_path(
        self,
        source: browse.Source,
        path: str,
        host: str = "",
        port: int | None = None,
        username: str = "",
        password: str = "",
        tls: bool = False,
        disk: str = "",
        storage: str | None = None,
    ) -> dict[str, Any]:
        """One folder of a local path / FTP server / SMB share / found MTP player's storage with a preview (read-only)."""
        try:
            path = browse.norm(path)
            if source == "mtp":

                def listing(b: Any) -> dict[str, Any]:
                    def list_dir(p: str) -> list[Any]:
                        return [
                            (e.name, e.is_dir, e.size, 0) for e in b.list(storage, p.lstrip("/"))
                        ]

                    return browse.browse_with(path, list_dir).model_dump()

                try:
                    out: dict[str, Any] = await self._on_player(disk, listing)
                except SyncError as e:
                    if isinstance(e.__cause__, FileNotFoundError):
                        raise SyncError(
                            f"{path} does not exist on the player", "not_found"
                        ) from None
                    raise
                return out
            if source == "local":
                try:
                    return (await asyncio.to_thread(browse.browse_local, path)).model_dump()
                except FileNotFoundError:
                    raise SyncError(f"{path} does not exist", "not_found") from None
                except OSError as e:
                    raise SyncError(f"{path}: {e.strerror or e}", "invalid") from None
            if not host:
                raise SyncError("a server address is needed", "invalid")
            if source == "smb" and path == "/":
                shares = await discovery.list_shares(host, port or 445, username, password)
                return browse.shares_listing([s.name for s in shares]).model_dump()
            listing = await asyncio.to_thread(
                browse.browse_remote,
                source,
                host=host,
                port=port,
                username=username,
                password=password,
                path=path,
                tls=tls,
            )
            return listing.model_dump()
        except ValueError as e:
            raise SyncError(str(e), "invalid") from e
        except (discovery.DiscoveryError, OSError) as e:
            raise SyncError(str(e) or type(e).__name__, "unreachable") from e

    def planning(self) -> list[dict[str, Any]]:
        """The plans running now with their progress."""
        return self.app.sync.planning.running()

    def cancel_plan(self, device_id: str) -> bool:
        return self.app.sync.planning.cancel(device_id)

    async def fit_estimate(self, device_id: str, to: FitTarget, kbps: int) -> dict[str, Any]:
        return await self.app.sync.fit_estimate(device_id, to, kbps)

    async def fit_apply(self, device_id: str, to: FitTarget, kbps: int) -> dict[str, Any]:
        await self.app.sync.fit_apply(device_id, to, kbps)
        return plan_summary(await self.app.sync.plan(device_id))

    async def get_plan(self, plan_id: str) -> dict[str, Any]:
        return plan_summary(self.app.sync.get_plan(plan_id))

    def get_plan_device(self, plan_id: str) -> str:
        return self.app.sync.get_plan(plan_id).plan.device_id

    def plan_items(
        self,
        plan_id: str,
        fmt: str,
        group: str | None,
        q: str,
        offset: int,
        limit: int,
        field: str = "",
        direction: str = "",
        library: str = "",
        kind: str = "",
        rule: str = "",
    ) -> Any:
        """The plan's full list: a page (json + offset/limit/group/q), or everything as
        csv / a json list for the download."""
        plan = self.app.sync.get_plan(plan_id).plan
        if group is not None and group not in (*plan_view.GROUPS, plan_view.META):
            raise SyncError(f"unknown group {group!r}", "invalid")
        if fmt == "csv":
            return plan_view.items_csv(plan)
        if fmt == "all":
            return plan_view.items_json(plan)
        return plan_view.items_page(
            plan, group, q, max(0, offset), limit, field, direction, library, kind, rule
        )

    async def apply_plan(
        self,
        plan_id: str,
        confirm: bool = False,
        first_sync: FirstSync | None = None,
        confirm_db_limit: bool = False,
        playlist_choices: Mapping[str, Policy] | None = None,
    ) -> dict[str, Any]:
        job = await self.app.sync.apply(
            plan_id,
            confirm=confirm,
            first_sync=first_sync,
            confirm_db_limit=confirm_db_limit,
            playlist_choices=playlist_choices,
        )
        return job.model_dump(mode="json")

    async def sync_all(self, mode: Literal["full", "update"] = "full") -> dict[str, Any]:
        return await self.app.sync.sync_all(mode)

    async def update_sync(self, device_id: str) -> dict[str, Any]:
        job = await self.app.sync.update_sync(device_id)
        return job.model_dump(mode="json")

    def jobs(self) -> list[dict[str, Any]]:
        return [j.model_dump(mode="json") for j in self.app.jobs.jobs()]

    def job(self, job_id: str) -> dict[str, Any]:
        info = self.app.jobs.get(job_id)
        if info is None:
            raise SyncError(f"unknown job {job_id!r}", "not_found")
        out = info.model_dump(mode="json")
        result = out.get("result")
        if isinstance(result, dict) and "device_id" in result and "counts" in result:
            # a sync report: its action log in words for the report view
            try:
                acts = report_activity(SyncReport.model_validate(result))
                result["activity"] = [a.model_dump(mode="json") for a in acts]
            except ValidationError:
                pass
        return out

    async def wait_job(self, job_id: str) -> dict[str, Any]:
        """The job's info once it has finished (the UI follows a refresh / format this way)."""
        self.job(job_id)
        return (await self.app.jobs.wait(job_id)).model_dump(mode="json")

    def cancel_job(self, job_id: str) -> bool:
        self.job(job_id)
        return self.app.jobs.cancel(job_id)

    async def _activity_targets(self) -> dict[str, tuple[str, str]]:
        """Device id -> (its name, its libraries' names): where the action log's entries went."""
        libs = {c.id: c.name for c in await self.app.store.libraries()}
        return {
            d.id: (d.name, ", ".join(libs[i] for i in d.library_ids if i in libs))
            for d in await self.app.store.devices()
        }

    async def sync_reports(self, device_id: str | None = None) -> list[dict[str, Any]]:
        """Reports, newest first, each with the `job_id` that wrote it (its log)."""
        reports = await self.app.sync.reports(device_id)
        jobs = await self.app.store.report_jobs([r.id for r in reports])
        targets = await self._activity_targets()
        return [
            {
                **r.model_dump(mode="json"),
                "job_id": jobs.get(r.id),
                "activity": [
                    a.model_dump(mode="json")
                    for a in report_activity(r, *targets.get(r.device_id, ("", "")))
                ],
            }
            for r in reports
        ]

    async def activity(
        self, device_id: str | None = None, kind: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """The action log across devices, newest first, from the stored reports."""
        out = []
        targets = await self._activity_targets()
        for r in await self.app.sync.reports(device_id):
            acts = report_activity(r, *targets.get(r.device_id, ("", "")))
            out += [a for a in reversed(acts) if kind is None or a.kind == kind]
        # reports come by start time; an entry's `at` is its report's end, so sort (stable keeps report order)
        out = sorted(out, key=lambda a: a.at, reverse=True)[:limit]
        jobs = await self.app.store.report_jobs(list({a.report_id for a in out}))
        return [{**a.model_dump(mode="json"), "job_id": jobs.get(a.report_id)} for a in out]


async def _sudo_works() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            "true",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await asyncio.wait_for(proc.wait(), 10) == 0
    except (OSError, TimeoutError):
        return False


# builder operators per field type; inPlaylist / notInPlaylist use the pseudo field `playlist`,
# latestEpisodes the pseudo field `latest_episodes` (its value: the episodes per podcast)
OPERATORS_BY_TYPE: dict[str, list[str]] = {
    "string": ["contains", "notContains", "is", "isNot", "in", "notIn", "startsWith", "endsWith"],
    "list": ["contains", "notContains", "is", "isNot", "in", "notIn"],
    "number": ["gt", "lt", "is", "isNot", "inTheRange"],
    "bool": ["is", "isNot"],
    "date": ["inTheLast", "notInTheLast", "before", "after", "inTheRange"],
    "playlist": ["inPlaylist", "notInPlaylist"],
    "episodes": ["latestEpisodes"],
}


def _changed_selection(
    rules: RuleSet, field: IdField, ids: list[str], library_id: str, remove: bool
) -> tuple[RuleSet, list[str]]:
    """`rules` with `ids` added to (or removed from) the Manual selection rule, and the ids that were new."""
    if remove:
        return remove_from_manual_selection(rules, field, ids), []
    before = set(manual_ids(rules, field))
    added = [i for i in dict.fromkeys(ids) if i not in before]
    return add_to_manual_selection(rules, field, ids, library_id), added


BrowseSort = Literal["artist", "added", "rating", "year", "title", "show"]


def _tree_size(dirs: list[Path]) -> int:
    """Bytes under these folders, a hard-linked file once (the shared thumbnails)."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for d in dirs:
        for root, _dirs, files in os.walk(d):
            for name in files:
                try:
                    st = os.lstat(os.path.join(root, name))
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) not in seen:
                    seen.add((st.st_dev, st.st_ino))
                    total += st.st_size
    return total


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SORTS: dict[str, Callable[[Track], Any]] = {
    "added": lambda t: t.date_added or t.published or _EPOCH,
    "rating": lambda t: t.rating or 0,
    "year": lambda t: t.year or 0,
}
_ASC_SORTS: dict[str, Callable[[Track], Any]] = {
    "title": lambda t: t.title.casefold(),
    # a show's episodes by season and episode; movies (no show) after the shows
    "show": lambda t: (
        t.series is None,
        (t.series or "").casefold(),
        t.extra.get("season") or 0,
        t.extra.get("episode") or 0,
        t.title.casefold(),
    ),
}


def _tab_counts(snap: LibrarySnapshot) -> dict[str, int]:
    """What each browser tab lists: music tracks, albums and artists, playlists, books, podcast
    episodes, videos - the totals of an unfiltered tab."""
    music = [t for t in snap.tracks.values() if t.kind is MediaKind.MUSIC]
    books = [t for t in snap.tracks.values() if t.kind is MediaKind.AUDIOBOOK]
    return {
        "songs": len(music),
        "albums": len(_group_tracks(music, "album")),
        "artists": len(_group_tracks(music, "artist")),
        "playlists": len(snap.playlists),
        "books": len(_group_tracks(books, "album")),
        "podcasts": sum(1 for t in snap.tracks.values() if t.kind is MediaKind.PODCAST),
        "videos": sum(1 for t in snap.tracks.values() if t.kind is MediaKind.VIDEO),
    }


def _held_by(tracks: list[Track], holders: dict[str, list[str]]) -> list[str]:
    """The devices that hold every one of these tracks (a row's "On devices" chips)."""
    if not tracks:
        return []
    common = set(holders.get(tracks[0].ref.key(), []))
    for t in tracks[1:]:
        if not common:
            break
        common &= set(holders.get(t.ref.key(), []))
    return sorted(common, key=str.casefold)


def _partly_held(tracks: list[Track], holders: dict[str, list[str]]) -> list[dict[str, Any]]:
    """The devices that hold some but not all of these tracks, with how many (a "7/8" chip)."""
    held: dict[str, int] = {}
    for t in tracks:
        for name in holders.get(t.ref.key(), []):
            held[name] = held.get(name, 0) + 1
    return [
        {"name": name, "items": n}
        for name, n in sorted(held.items(), key=lambda x: x[0].casefold())
        if n < len(tracks)
    ]


def _group_tracks(
    rows: list[Track],
    group: Literal["album", "artist"],
    holders: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """One row per album or artist of these tracks (in their order). An album row is an album (`field`
    album_id); a book or podcast without an album tag is grouped by its series and is sent as its tracks
    (`field` track_id, `id` "series:<name>"). Tracks without either are left out (they are in the track list).
    With `holders`: `on_devices`, the devices that hold all of the row's tracks, and `on_devices_partly`, those
    that hold some of them ({name, items})."""
    out: dict[str, dict[str, Any]] = {}
    members: dict[str, list[Track]] = {}
    for t in rows:
        if group == "artist":
            if not t.artist_id:
                continue
            key, field, name, artist = t.artist_id, "artist_id", t.artist or "", None
        elif t.album_id:
            key, field, name, artist = (
                t.album_id,
                "album_id",
                t.album or "",
                t.album_artist or t.artist,
            )
        elif t.series:
            key, field, name, artist = (
                f"series:{t.series}",
                "track_id",
                t.series,
                t.author or t.artist,
            )
        else:
            continue
        row = out.get(key)
        if row is None:
            row = out[key] = {
                "id": key,
                "field": field,
                "name": name,
                "artist": artist,
                "kind": t.kind.value,
                "items": 0,
                "size_bytes": 0,
                "has_cover": False,
                "cover_item": None,  # a track whose cover stands for the row (thumbnail)
            }
        members.setdefault(key, []).append(t)
        row["items"] += 1
        row["size_bytes"] += t.size_bytes
        row["has_cover"] = row["has_cover"] or t.has_cover
        if row["cover_item"] is None and t.has_cover:
            row["cover_item"] = t.ref.item_id
        if t.year and not row.get("year"):
            row["year"] = t.year
    if holders is not None:
        for key, row in out.items():
            row["on_devices"] = _held_by(members[key], holders)
            row["on_devices_partly"] = _partly_held(members[key], holders)
    return list(out.values())


def _top_values(
    counts: dict[str, int], query: str, limit: int, labels: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    q = query.strip().casefold()
    hits = [
        (v, n)
        for v, n in counts.items()
        if q in v.casefold() or (labels is not None and q in labels.get(v, "").casefold())
    ]
    hits.sort(key=lambda x: (-x[1], x[0].casefold()))
    return [
        {"value": v, "count": n, **({"label": labels[v]} if labels and v in labels else {})}
        for v, n in hits[:limit]
    ]
