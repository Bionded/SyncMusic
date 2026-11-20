# SPDX-License-Identifier: GPL-3.0-or-later
"""Application: wires the services together and owns their start/stop lifecycle."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from syncmusic import __version__
from syncmusic.base.context import AppContext
from syncmusic.core import manifest
from syncmusic.core.client_sessions import ClientSessions
from syncmusic.core.models import AppSettings, LibraryConfig, ManifestLibrary
from syncmusic.core.prepare import Preparer
from syncmusic.core.snapshots import SnapshotCache
from syncmusic.core.sync import SCHEDULE_TICK_S, SyncService
from syncmusic.devices.HibyR1 import compas_sdtool
from syncmusic.infra import client_apps, dbbackup, ffmpeg, self_update, storage
from syncmusic.infra.announce import Announcer, txt_record
from syncmusic.infra.auth import Auth
from syncmusic.infra.blockdev.guard import Guard
from syncmusic.infra.blockdev.identify import BlockDeviceInfo
from syncmusic.infra.blockdev.watcher import BlockWatcher
from syncmusic.infra.cache import ProcessingCache
from syncmusic.infra.clients import Clients
from syncmusic.infra.config import BootstrapConfig
from syncmusic.infra.db import Database
from syncmusic.infra.events import EventBus
from syncmusic.infra.housekeeping import LowDisk, disk_free, prune_logs
from syncmusic.infra.jobs import JobInfo, JobRunner
from syncmusic.infra.log import BusLogHandler
from syncmusic.infra.notify import Notifications
from syncmusic.infra.store import Store
from syncmusic.infra.updates import Checker, UpdateState, running_version

log = logging.getLogger(__name__)

HOUSEKEEPING_EVERY_S = 3600  # hourly since notification expiry
UPDATE_CHECK_EVERY_S = 24 * 3600


class Application:
    def __init__(self, config: BootstrapConfig) -> None:
        self.config = config
        self.bus = EventBus()
        self.db = Database(
            config.data_dir / "syncmusic.sqlite3", backup_dir=config.data_dir / "backups"
        )
        self.jobs = JobRunner(self.bus)
        self.guard = Guard(config.device_allowlist, allow_loop=config.allow_loop_devices)
        self.ctx = AppContext(
            config=config, db=self.db, bus=self.bus, jobs=self.jobs, guard=self.guard
        )
        self.store = Store(self.db)
        self.instance_id = ""  # set at start (Store.instance_id)
        self.cpu = storage.CpuMeter()  # Storage & resources: CPU % since the last reading
        self.instance_name = socket.gethostname()
        self.auth = Auth(self.db)  # optional login
        self.clients = Clients(self.db)  # Wi-Fi clients: pairing codes and tokens
        self.announcer = Announcer(config.port)
        self.client_settings = AppSettings()  # the Clients part is read by the client API
        self.jobs.on_finished = self._job_finished
        self.jobs.on_update = self.store.put_job
        self.cache = ProcessingCache(self.db, config.cache_dir)
        self.sync = SyncService(
            self.ctx,
            self.store,
            Preparer(self.cache, config.cache_dir / "work"),
            SnapshotCache(self.db),
        )
        self.jobs.on_failed = self.sync.job_failed  # a damaged device file system
        self.client_sessions = ClientSessions(
            self.sync, self.clients
        )  # Wi-Fi clients' sync sessions
        host = socket.gethostname() if config.host in ("0.0.0.0", "::", "") else config.host
        self.notifications = Notifications(
            self.bus, self._device_name, f"http://{host}:{config.port}", inbox=self.store
        )
        self.watcher = BlockWatcher(
            self._disk_added, self.sync.on_disk_removed, include_loop=config.allow_loop_devices
        )
        self.started = False
        self._log_handler: BusLogHandler | None = None
        self._scheduler: asyncio.Task[None] | None = None
        self._low_disk = LowDisk()
        self._housekept = 0.0  # monotonic time of the last housekeeping run
        self._log_level: int | None = None
        # Settings → Updates: the last check, when it ran (monotonic), the version announced
        self.updates = UpdateState()
        self._update_checked: float | None = None
        self.update_checker = Checker(config.cache_dir / "update-releases.json")
        self.app_fetch = (
            client_apps.AutoFetch()
        )  # the Compás app builds of this release, fetched by themselves
        # installing an update: the runner stops the app through `on_restart`, then exits for the unit to restart it
        # ("exit") or starts the new binary in its place ("exec")
        self.restart: str | None = None
        self.on_restart: Callable[[], None] | None = None
        self.update_transport: httpx.AsyncBaseTransport | None = None  # tests
        self._trial: asyncio.Task[None] | None = None
        self._update_pending = False
        self._update_announced: str | None = None
        # the running install's step for Settings → Updates and every open page (`app.update_progress`)
        self.update_progress: dict[str, Any] | None = None
        self._progress_sent = 0.0
        # the last automatic compas-sdtool check (Settings → Devices), see compas_sdtool.check_update
        self.sdtool_update: dict[str, Any] | None = None

    async def _device_name(self, device_id: str) -> str | None:
        cfg = await self.store.device(device_id)
        return cfg.name if cfg else None

    async def _disk_added(self, disk: BlockDeviceInfo) -> None:
        cd = await self.sync.detector.disk_added(disk)
        # the start-up scan only re-discovers what is plugged in: no connect sync after a restart
        await self.sync.on_disk_added(cd, startup=not self.started)

    async def start(self) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        # the database holds library/device passwords and the logs paths: readable by the service user only
        self.config.data_dir.chmod(0o700)
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        await self.db.open()
        # log records → `log.entry` events (live tail of the Logs page)
        self._log_handler = BusLogHandler(self.bus, asyncio.get_running_loop())
        logging.getLogger().addHandler(self._log_handler)
        for info in self.jobs.restore(await self.store.recent_jobs()):
            await self.store.put_job(info)  # cut off by a crash: stored as failed
        self.instance_id = await self.store.instance_id()
        self.apply_settings(await self.store.app_settings(), starting=True)
        await self.announce()
        self.store.on_libraries_changed = self.update_manifest_context
        await self.update_manifest_context()
        await self.notifications.start()
        await self.sync.add_tag_steps()
        await self.sync.load()
        self.sync.wifi_ids = {p.device_id for p in await self.clients.list() if not p.revoked}
        self.sync.wifi_alive = self.clients.alive
        if self.config.detect_devices:
            await self.watcher.start()
        try:
            await self.housekeeping()
        except Exception:  # noqa: BLE001 - never a failed start
            log.exception("housekeeping")
        self._scheduler = asyncio.create_task(self._schedule_loop(), name="schedule")
        self._update_outcome()
        self.started = True
        log.info("SyncMusic %s started (mode=%s)", __version__, self.config.mode)
        self.bus.publish("app.started", version=__version__)

    async def announce(self) -> None:
        """The mDNS announcement for Wi-Fi clients after a settings change or a pairing code opened/closed."""
        s = self.client_settings
        on = s.clients_allowed and s.clients_mdns
        txt = txt_record(
            self.instance_id, self.instance_name, on and await self.clients.pairing_open()
        )
        await self.announcer.update(on, txt)

    async def _schedule_loop(self) -> None:
        """The daily device syncs: checked every SCHEDULE_TICK_S."""
        while True:
            await asyncio.sleep(SCHEDULE_TICK_S)
            try:
                await self.sync.scheduled_syncs()
            except Exception:  # noqa: BLE001 - logged; the next tick tries again
                log.exception("scheduled syncs")
            try:
                await self.sync.periodic_updates()
            except Exception:  # noqa: BLE001 - logged; the next tick tries again
                log.exception("update checks")
            try:
                await self.check_disk()
                await self.scheduled_backup()
                if time.monotonic() - self._housekept > HOUSEKEEPING_EVERY_S:
                    await self.housekeeping()
                if self.update_pending and not any(j.finished_at is None for j in self.jobs.jobs()):
                    await self._install_pending()
                if (self.sdtool_update or {}).get("status") == "waiting" and not any(
                    j.finished_at is None for j in self.jobs.jobs()
                ):
                    await self.check_sdtool()
                if (
                    self._update_checked is None
                    or time.monotonic() - self._update_checked > UPDATE_CHECK_EVERY_S
                ):
                    await self.check_updates()
            except Exception:  # noqa: BLE001 - logged; the next tick tries again
                log.exception("housekeeping")

    async def check_updates(self, install: bool = True) -> UpdateState:
        """Ask the releases of `update_repo` for the newest version of the update channel; a newer one than the
        running version is announced once (`app.update_available`). `install=False` (the user's Check) only
        checks: automatic updates are left to the scheduled checks."""
        self._update_checked = time.monotonic()
        settings = await self.store.app_settings()
        repo = self.config.update_repo.strip()
        if not repo or repo.lower() == "none":
            self.updates = UpdateState(channel=settings.update_channel)
            return self.updates
        self.updates = await self.update_checker.check(repo, settings.update_channel)
        if not self.updates.error:
            await self.check_sdtool()
        if self.updates.error:
            log.warning("update check: %s", self.updates.error)
        elif self.updates.available and self.updates.latest != self._update_announced:
            self._update_announced = self.updates.latest
            if install and settings.auto_update != "off" and self.can_self_update():
                log.info("automatic update to %s (%s)", self.updates.latest, settings.auto_update)
                if settings.auto_update == "now" or not any(
                    j.finished_at is None for j in self.jobs.jobs()
                ):
                    await self._install_pending()
                    return self.updates
                self.update_pending = True
            self.bus.publish(
                "app.update_available", latest=self.updates.latest, notes=len(self.updates.notes)
            )
        return self.updates

    async def check_sdtool(self) -> None:
        """Keep the downloaded compas-sdtool at the build SyncMusic's release offers (only while no job runs; a
        user-set tool is never replaced). Never raises: the result is shown in Settings → Devices."""
        repo = self.config.update_repo.strip()
        try:
            releases = await self.update_checker.releases(repo)
            settings = await self.store.app_settings()
            got = await compas_sdtool.check_update(
                releases,
                running_version(),
                self.config.data_dir,
                settings.compas_sdtool_path,
                idle=not any(j.finished_at is None for j in self.jobs.jobs()),
                transport=self.update_transport,
            )
        except Exception as e:  # noqa: BLE001 - offline or a broken release: shown, tried at the next check
            got = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        got["checked"] = datetime.now(UTC).isoformat(timespec="seconds")
        if got["status"] == "updated":
            log.info("compas-sdtool updated from %s to %s", got.get("previous"), got.get("held"))
        elif got["status"] == "error" and (self.sdtool_update or {}).get("error") != got.get(
            "error"
        ):
            log.warning("compas-sdtool update check: %s", got.get("error"))
        self.sdtool_update = got

    @property
    def update_pending(self) -> bool:
        """An update waits for the running jobs (installed by the scheduler when none runs); automatic syncs are
        held back meanwhile."""
        return self._update_pending

    @update_pending.setter
    def update_pending(self, value: bool) -> None:
        self._update_pending = value
        self.sync.hold_automatic = value

    def can_self_update(self) -> bool:
        return self.updates.install_type in ("service", "standalone", "desktop") and (
            self_update.running_binary() is not None
        )

    async def prune_runtimes(self) -> int:
        """Remove the unpacked runtimes of old versions next to the running one (any install prefix); the
        installed binary's and its rollback's stay. At start, hourly and after a confirmed update."""
        binary, current = self_update.running_binary(), self_update.running_runtime()
        if binary is None or current is None:
            return 0
        try:
            removed, freed = await asyncio.to_thread(
                self_update.prune_old_runtimes, binary, current
            )
        except Exception as e:  # noqa: BLE001 - only clean-up
            log.warning("old runtimes not removed: %s", e)
            return 0
        if removed:
            log.info(
                "removed %d old runtime(s) from %s (%s), %d MB freed",
                len(removed),
                current.parent,
                ", ".join(removed),
                freed // 1_000_000,
            )
        return freed

    @property
    def update_marker(self) -> Path:
        return self.config.data_dir / "update.json"

    def _update_outcome(self) -> None:
        """After an update's restart: the new version ends the trial once it ran HEALTHY_AFTER_S and says so;
        when the launcher went back to the previous binary, that is reported."""
        binary = self_update.running_binary()
        paths = self_update.Paths(binary) if binary else None
        result = self_update.outcome(self.update_marker, paths, self.updates.running)
        if result is None:
            return
        kind, info = result
        if kind == "rolled_back":
            log.error(
                "the update to %s did not start: back on %s", info["to"], self.updates.running
            )
            self.bus.publish("app.update_failed", version=info["to"], running=self.updates.running)
            return

        async def healthy() -> None:
            await asyncio.sleep(self_update.HEALTHY_AFTER_S)
            self_update.confirm(self.update_marker, paths)
            await self.prune_runtimes()
            log.info("updated from %s to %s", info["from"], info["to"])
            self.bus.publish("app.updated", version=info["to"], previous=info["from"])

        self._trial = asyncio.create_task(healthy(), name="update-trial")

    async def _install_pending(self) -> None:
        self.update_pending = False
        try:
            await self.install_update()
        except Exception as e:  # noqa: BLE001 - reported; the next check tries this version again
            self._update_announced = None
            if not isinstance(e, self_update.Refused):
                log.exception("update to %s failed", self.updates.latest)
            log.warning("update not installed: %s", e)
            self.bus.publish(
                "app.update_failed",
                version=self.updates.latest,
                running=self.updates.running,
                reason=str(e),
            )

    async def install_update(self) -> str:
        """Download, check and put in place the newer binary the last check found, then restart into it (the
        running jobs are interrupted like at any restart and resume afterwards). Raises self_update.Refused."""
        u = self.updates
        binary = self_update.running_binary()
        if u.install_type not in ("service", "standalone", "desktop") or binary is None:
            raise self_update.Refused("this install updates itself only as a single-file binary")
        if not u.available or not u.latest or not u.asset or not u.sums:
            raise self_update.Refused(
                "there is no newer version to install: check for updates first"
            )
        paths = self_update.Paths(binary)
        new = paths.download
        latest = u.latest
        try:
            self._progress("downloading", latest, done=0, total=0, force=True)
            await self_update.download(
                u.asset,
                u.sums,
                new,
                self.update_transport,
                lambda done, total: self._progress("downloading", latest, done=done, total=total),
            )
            try:
                self._progress("installing", u.latest, force=True)
                service = u.install_type == "service"
                runtime_id = await asyncio.to_thread(self_update.unpack, new, service)
                await self.backup_db("update")
                self_update.swap(paths, new)
            finally:
                new.unlink(missing_ok=True)
            if service:
                await asyncio.to_thread(
                    self_update.retarget_sudoers, self_update.service_helper(runtime_id)
                )
            self_update.write_marker(self.update_marker, u.running, u.latest)
        except BaseException as e:
            self._progress("failed", u.latest, error=str(e) or type(e).__name__, force=True)
            raise
        log.info("update %s installed; restarting", u.latest)
        self._progress("restarting", u.latest, previous=u.running, force=True)
        self.restart = "exit" if service else "exec"
        if self.on_restart is not None:
            self.on_restart()
        return u.latest

    def _progress(self, step: str, version: str, *, force: bool = False, **extra: Any) -> None:
        """Store the install step and announce it (download progress at most twice a second)."""
        self.update_progress = {"step": step, "version": version, **extra}
        now = time.monotonic()
        if force or now - self._progress_sent >= 0.5:
            self._progress_sent = now
            self.bus.publish("app.update_progress", **self.update_progress)

    async def _job_finished(self, job: JobInfo) -> None:
        """Store the finished job, freeze the survivors for the garbage collector when no other job runs, then hand the memory the
        job freed back to the system."""
        await self.store.put_job(job)
        if not any(j.finished_at is None for j in self.jobs.jobs()):
            storage.freeze_gc()
        await asyncio.to_thread(storage.trim_memory)

    def backup_dir(self, settings: AppSettings) -> Path:
        """Where the database backups go."""
        return (
            Path(settings.db_backup_dir)
            if settings.db_backup_dir
            else self.config.data_dir / "backups"
        )

    async def backup_db(self, reason: str) -> Path:
        """A database backup now (`reason`: auto, update, reset, manual), then the oldest beyond the setting go."""
        settings = await self.store.app_settings()
        folder = self.backup_dir(settings)
        path = await dbbackup.make(self.db, folder, reason)
        gone = await asyncio.to_thread(dbbackup.prune, folder, settings.db_backup_keep)
        log.info("database backup %s (%d old ones deleted)", path, gone)
        return path

    async def scheduled_backup(self) -> None:
        settings = await self.store.app_settings()
        folder = self.backup_dir(settings)
        if await asyncio.to_thread(
            dbbackup.due, folder, settings.db_backup_every, datetime.now(UTC)
        ):
            await self.backup_db("auto")

    async def housekeeping(self) -> None:
        """Delete sync history, rotated logs and notifications older than the Settings say;
        at start and hourly."""
        self._housekept = time.monotonic()
        if not any(j.finished_at is None for j in self.jobs.jobs()):
            storage.collect_frozen()  # the one full collection, between jobs
        settings = await self.store.app_settings()
        rows = await self.store.prune_history(
            datetime.now(UTC) - timedelta(days=settings.history_days)
        )
        files = await asyncio.to_thread(prune_logs, self.config.logs, settings.log_days)
        await self.sync.sweep_network_files()
        await self.prune_runtimes()
        notes = 0
        if settings.notification_days:
            notes = await self.store.prune_notifications(
                datetime.now(UTC) - timedelta(days=settings.notification_days)
            )
            if notes:
                self.bus.publish("notification.cleared")  # the bell and the centre reload
        if rows or files or notes:
            log.info(
                "housekeeping: %d history rows, %d old log files and %d notifications deleted",
                rows,
                files,
                notes,
            )

    async def check_disk(self) -> None:
        """`storage.low` once when the disk holding the cache has less free space than the setting."""
        limit_gb = (await self.store.app_settings()).disk_warn_gb
        _total, free = await asyncio.to_thread(disk_free, self.config.cache_dir)
        limit = int(limit_gb * 1000**3) if limit_gb else None
        if self._low_disk.crossed(free, limit):
            log.warning("the disk holding %s has %d bytes free", self.config.cache_dir, free)
            self.bus.publish("storage.low", free=free, limit=limit, path=str(self.config.cache_dir))

    async def update_manifest_context(self) -> None:
        """What device manifests need to map their libraries to ours (after a library or alias change)."""
        libraries = await self.store.libraries()
        manifest.set_context(
            manifest.Context(
                instance_id=self.instance_id,
                instance_name=self.instance_name,
                app_version=__version__,
                libraries={c.id: _manifest_library(c) for c in libraries},
                aliases=await self.store.library_aliases(),
            )
        )

    def apply_settings(self, settings: AppSettings, starting: bool = False) -> None:
        """Runtime settings. The number of processing threads changes only at start (no job runs)."""
        self.client_settings = settings
        self.cache.max_bytes = int(
            settings.cache_max_gb * 1000**3
        )  # decimal GB like every size in the UI
        self.notifications.configure(
            settings.webhook_url,
            settings.webhook_events,
            settings.inbox_events,
            settings.quiet_hours,
        )
        self.instance_name = settings.instance_name or socket.gethostname()
        ctx = manifest.context()
        if ctx.instance_id and ctx.instance_name != self.instance_name:
            manifest.set_context(dataclasses.replace(ctx, instance_name=self.instance_name))
        self.jobs.set_device_job_limit(settings.max_device_syncs)
        self.sync.preparer.cache_books = settings.cache_combined_books
        self.sync.snapshots.policy = settings.snapshot_refresh
        self.sync.snapshots.expired_gap = timedelta(minutes=settings.update_change_gap_min)
        root = logging.getLogger()
        if self._log_level is None:
            self._log_level = (
                root.level
            )  # restored at stop (tests start several apps in one process)
        root.setLevel(
            settings.log_level or self._log_level
        )  # None: as the configuration file set it
        ffmpeg.configure(settings.ffmpeg_path, settings.convert_nice)
        compas_sdtool.configure(settings.compas_sdtool_path)
        if starting and settings.concurrency and settings.concurrency != self.jobs.max_workers:
            self.jobs.resize(settings.concurrency)

    async def stop(self) -> None:
        if self._log_handler is not None:  # also after a failed start
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        if not self.started:
            return
        ffmpeg.configure(
            None, 0
        )  # module-level: the next Application (tests) starts from the defaults
        self.bus.publish("app.stopping")
        for task in (self._scheduler, self._trial):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._scheduler = self._trial = None
        await self.watcher.stop()
        await self.announcer.stop()
        await self.jobs.shutdown()
        await self.notifications.stop()
        await self.client_sessions.close()
        await self.sync.close()
        await self.db.close()
        self.started = False
        log.info("SyncMusic stopped")


def _manifest_library(cfg: LibraryConfig) -> ManifestLibrary:
    """How a device's manifest describes our library: no credentials, no account names."""
    hint = str(cfg.settings.get("url") or "")
    if hint:
        parts = urlsplit(hint)
        hint = (
            urlunsplit((parts.scheme, parts.hostname or "", "", "", "")) if parts.hostname else ""
        )
        if parts.port and hint:
            hint += f":{parts.port}"
    return ManifestLibrary(source_id=cfg.source_id, type=cfg.type, name=cfg.name, url_hint=hint)
