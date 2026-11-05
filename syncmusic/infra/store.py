# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent registrations and history in SQLite (tables from migration 001): libraries, devices, templates,
sync reports, and the reverse sync's bookkeeping (submitted plays, pending feedback, local feedback). Rows hold
the pydantic model as JSON; `type`/`name` columns are copies for listing."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from syncmusic.core.models import (
    AppSettings,
    DeviceConfig,
    LibraryConfig,
    PlayEvent,
    ProcessorTemplate,
    RuleTemplate,
    SyncReport,
    utcnow,
)
from syncmusic.core.proc_templates import builtin_processor_templates
from syncmusic.core.rules import defaults
from syncmusic.infra.db import Database
from syncmusic.infra.jobs import HISTORY_LIMIT, JobInfo

log = logging.getLogger(__name__)

NOTIFICATIONS_LIMIT = 500  # the newest are kept


_warned_update_url = False


def _warn_update_url() -> None:
    global _warned_update_url
    _warned_update_url = True
    log.info(
        "the stored update repository (update_url) is ignored: updates come from the GitHub releases"
    )


@dataclass
class PendingRow:
    """A pending_feedback row: listening data a sink did not take yet."""

    id: int
    kind: str  # play | rating | position
    data: dict[str, Any]
    attempts: int
    last_error: str | None
    inflight: int | None = (
        None  # the sink's play count before this play was sent (send interrupted)
    )
    created_at: str | None = None  # captured from the device
    tried_at: str | None = None  # the last failed delivery
    tries: int = 0  # failed deliveries, refused or not reachable


DROPPED_KEEP = 200  # dropped_feedback rows kept per device


def _play_key(sink: str, p: PlayEvent) -> tuple[str, str, str, str, str]:
    return (sink, p.ref.library_id, p.ref.item_id, p.played_at.isoformat(), p.source_device)


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- libraries ---------------------------------------------------------------------------

    async def libraries(self) -> list[LibraryConfig]:
        rows = await self.db.fetchall("SELECT config FROM libraries ORDER BY created_at, id")
        return [LibraryConfig.model_validate_json(r["config"]) for r in rows]

    async def library(self, library_id: str) -> LibraryConfig | None:
        row = await self.db.fetchone("SELECT config FROM libraries WHERE id = ?", (library_id,))
        return LibraryConfig.model_validate_json(row["config"]) if row else None

    async def put_library(self, cfg: LibraryConfig) -> None:
        now = utcnow().isoformat()
        await self.db.execute(
            "INSERT INTO libraries (id, type, name, enabled, config, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET type = excluded.type, "
            "name = excluded.name, enabled = excluded.enabled, config = excluded.config, "
            "updated_at = excluded.updated_at",
            (cfg.id, cfg.type, cfg.name, int(cfg.enabled), cfg.model_dump_json(), now, now),
        )

    async def delete_library(self, library_id: str) -> None:
        """Its untouched default templates go with it (they are generated); edited ones are rows of `templates`."""
        await self.db.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
        await self.db.execute(
            "DELETE FROM deleted_default_templates WHERE library_id = ?", (library_id,)
        )

    # --- devices -----------------------------------------------------------------------------

    async def devices(self) -> list[DeviceConfig]:
        rows = await self.db.fetchall("SELECT config FROM devices ORDER BY created_at, id")
        return [DeviceConfig.model_validate_json(r["config"]) for r in rows]

    async def device(self, device_id: str) -> DeviceConfig | None:
        row = await self.db.fetchone("SELECT config FROM devices WHERE id = ?", (device_id,))
        return DeviceConfig.model_validate_json(row["config"]) if row else None

    async def put_device(self, cfg: DeviceConfig) -> None:
        now = utcnow().isoformat()
        await self.db.execute(
            "INSERT INTO devices (id, type, name, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET type = excluded.type, name = excluded.name, "
            "config = excluded.config, updated_at = excluded.updated_at",
            (cfg.id, cfg.type, cfg.name, cfg.model_dump_json(), now, now),
        )

    async def delete_device(self, device_id: str) -> None:
        await self.db.execute_all(
            [
                ("DELETE FROM devices WHERE id = ?", (device_id,)),
                ("DELETE FROM position_scopes WHERE device_id = ?", (device_id,)),
                ("DELETE FROM device_items WHERE device_id = ?", (device_id,)),
                ("DELETE FROM dropped_feedback WHERE device_id = ?", (device_id,)),
                ("DELETE FROM device_health WHERE device_id = ?", (device_id,)),
            ]
        )

    # --- templates ---------------------------------------------------------------------------

    # A library's default templates are generated for it (core/rules/defaults.py) unless the user
    # deleted one (remembered in deleted_default_templates) or edited it (then it is a row of `templates`).

    async def _deleted_defaults(self) -> set[str]:
        rows = await self.db.fetchall("SELECT id FROM deleted_default_templates")
        return {r["id"] for r in rows}

    async def _generated(self, libraries: list[LibraryConfig] | None = None) -> list[RuleTemplate]:
        deleted = await self._deleted_defaults()
        libs = await self.libraries() if libraries is None else libraries
        out: list[RuleTemplate] = []
        for lib in libs:
            try:
                out += [t for t in defaults.generated(lib) if t.id not in deleted]
            except Exception:  # one library's broken defaults must not take every page down
                log.exception("default templates of library %s could not be generated", lib.id)
        return out

    async def template(self, template_id: str) -> RuleTemplate | None:
        row = await self.db.fetchone("SELECT data FROM templates WHERE id = ?", (template_id,))
        if row:
            return RuleTemplate.model_validate_json(row["data"])
        return await self.generated_template(template_id)

    async def generated_template(self, template_id: str) -> RuleTemplate | None:
        """A library default as it is generated, whether or not an edited version is stored."""
        if template_id.startswith(defaults.PREFIX):
            for lib in await self.libraries():
                if template_id.startswith(f"{defaults.PREFIX}{lib.id}."):
                    for t in await self._generated([lib]):
                        if t.id == template_id:
                            return t
        return None

    async def delete_template(self, template_id: str) -> None:
        tpl = await self.template(template_id)
        await self.db.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        if tpl is not None and tpl.default_of:
            await self.db.execute(
                "INSERT OR IGNORE INTO deleted_default_templates (id, library_id) VALUES (?, ?)",
                (template_id, tpl.default_of),
            )

    async def reset_template(self, template_id: str) -> None:
        """An edited default back to its generated version: the stored row goes."""
        await self.db.execute("DELETE FROM templates WHERE id = ?", (template_id,))

    async def templates(self) -> dict[str, RuleTemplate]:
        """Every library's defaults (in library order; an edited one in its place) and then the user's templates
        by name."""
        rows = await self.db.fetchall("SELECT data FROM templates ORDER BY name")
        stored = {t.id: t for t in (RuleTemplate.model_validate_json(r["data"]) for r in rows)}
        out = {t.id: stored.pop(t.id, t) for t in await self._generated()}
        return {**out, **stored}

    async def put_template(self, tpl: RuleTemplate) -> None:
        if tpl.builtin:
            raise ValueError(f"built-in template {tpl.id!r} is read-only")
        if tpl.default_of:
            tpl = tpl.model_copy(
                update={"edited": True}
            )  # stored from now on: an upgrade no longer changes it
        await self.db.execute(
            "INSERT INTO templates (id, name, data, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE "
            "SET name = excluded.name, data = excluded.data, updated_at = excluded.updated_at",
            (tpl.id, tpl.name, tpl.model_dump_json(), tpl.updated_at.isoformat()),
        )

    # --- processor templates --------------------------------------------------------

    async def processor_template(self, template_id: str) -> ProcessorTemplate | None:
        builtins = builtin_processor_templates()
        if template_id in builtins:
            return builtins[template_id]
        row = await self.db.fetchone(
            "SELECT data FROM processor_templates WHERE id = ?", (template_id,)
        )
        return ProcessorTemplate.model_validate_json(row["data"]) if row else None

    async def processor_templates(self) -> dict[str, ProcessorTemplate]:
        rows = await self.db.fetchall("SELECT data FROM processor_templates ORDER BY name")
        out = [ProcessorTemplate.model_validate_json(r["data"]) for r in rows]
        builtins = builtin_processor_templates()
        return {**builtins, **{t.id: t for t in out if t.id not in builtins}}

    async def put_processor_template(self, tpl: ProcessorTemplate) -> None:
        if tpl.builtin or tpl.id in builtin_processor_templates():
            raise ValueError(f"built-in processor template {tpl.id!r} is read-only")
        await self.db.execute(
            "INSERT INTO processor_templates (id, name, data, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(id) "
            "DO UPDATE SET name = excluded.name, data = excluded.data, updated_at = excluded.updated_at",
            (tpl.id, tpl.name, tpl.model_dump_json(), tpl.updated_at.isoformat()),
        )

    async def delete_processor_template(self, template_id: str) -> None:
        await self.db.execute("DELETE FROM processor_templates WHERE id = ?", (template_id,))

    # --- settings ------------------------------------------------------------------------------

    async def app_settings(self) -> AppSettings:
        """Stored values over the defaults (one row per field, JSON values; unknown keys ignored)."""
        rows = await self.db.fetchall("SELECT key, value FROM settings")
        stored = {r["key"]: json.loads(r["value"]) for r in rows}
        values = {k: v for k, v in stored.items() if k in AppSettings.model_fields}
        if stored.get("update_url") and not _warned_update_url:
            _warn_update_url()
        try:
            return AppSettings.model_validate(values)
        except ValidationError as e:
            # a value stored before a stricter check: its default instead, never a failed start
            bad = {str(err["loc"][0]) for err in e.errors() if err["loc"]}
            log.warning("stored settings %s are invalid now, using their defaults", sorted(bad))
            return AppSettings.model_validate({k: v for k, v in values.items() if k not in bad})

    async def put_app_settings(self, settings: AppSettings) -> None:
        await self.db.execute_all(
            ("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
            for k, v in settings.model_dump(mode="json").items()
        )

    async def ignored_disks(self) -> list[str]:
        """Unknown disks the user chose to ignore: their keys, see `SyncService.disk_key`. A
        settings row of its own, not part of `AppSettings` (not a setting the Settings page edits)."""
        row = await self.db.fetchone("SELECT value FROM settings WHERE key = 'ignored_disks'")
        return list(json.loads(row["value"])) if row else []

    async def put_ignored_disks(self, keys: list[str]) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('ignored_disks', ?)",
            (json.dumps(keys),),
        )

    # --- storage health ------------------------------------------------------

    async def add_health(
        self, device_id: str, kind: str, bytes_: int = 0, seconds: float = 0.0, detail: str = ""
    ) -> None:
        await self.db.execute(
            "INSERT INTO device_health (device_id, at, kind, bytes, seconds, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, utcnow().isoformat(), kind, bytes_, seconds, detail),
        )

    async def health(self, device_id: str, since: datetime) -> list[dict[str, Any]]:
        """The device's health records since `since`, oldest first."""
        rows = await self.db.fetchall(
            "SELECT at, kind, bytes, seconds, detail FROM device_health WHERE device_id = ? AND at >= ? "
            "ORDER BY at, id",
            (device_id, since.isoformat()),
        )
        return [dict(r) for r in rows]

    # --- backup -------------------------------------------------------------

    async def stored_templates(self) -> list[RuleTemplate]:
        """Only the stored rule templates: the user's and edited library defaults (untouched defaults are generated
        from their library)."""
        rows = await self.db.fetchall("SELECT data FROM templates ORDER BY name")
        return [RuleTemplate.model_validate_json(r["data"]) for r in rows]

    async def stored_processor_templates(self) -> list[ProcessorTemplate]:
        rows = await self.db.fetchall("SELECT data FROM processor_templates ORDER BY name")
        return [ProcessorTemplate.model_validate_json(r["data"]) for r in rows]

    async def deleted_default_templates(self) -> list[tuple[str, str]]:
        rows = await self.db.fetchall("SELECT id, library_id FROM deleted_default_templates")
        return [(r["id"], r["library_id"]) for r in rows]

    async def replace_config(
        self,
        settings: AppSettings,
        libraries: list[LibraryConfig],
        devices: list[DeviceConfig],
        templates: list[RuleTemplate],
        processor_templates: list[ProcessorTemplate],
        deleted_defaults: list[tuple[str, str]],
        ignored_disks: list[str],
    ) -> None:
        """Replace everything a backup holds in one transaction (Restore); history, listening data and
        the cache stay."""
        now = utcnow().isoformat()
        stmts: list[tuple[str, Iterable[Any]]] = [
            (f"DELETE FROM {table}", ())  # noqa: S608 - fixed table names
            for table in (
                "libraries",
                "devices",
                "templates",
                "processor_templates",
                "deleted_default_templates",
            )
        ]
        # the login is not in a backup: a restore keeps it
        stmts.append(("DELETE FROM settings WHERE key != 'auth'", ()))
        stmts += [
            (
                "INSERT INTO libraries (id, type, name, enabled, config, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (c.id, c.type, c.name, int(c.enabled), c.model_dump_json(), now, now),
            )
            for c in libraries
        ]
        stmts += [
            (
                "INSERT INTO devices (id, type, name, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (c.id, c.type, c.name, c.model_dump_json(), now, now),
            )
            for c in devices
        ]
        stmts += [
            (
                "INSERT INTO templates (id, name, data, updated_at) VALUES (?, ?, ?, ?)",
                (t.id, t.name, t.model_dump_json(), t.updated_at.isoformat()),
            )
            for t in templates
        ]
        stmts += [
            (
                "INSERT INTO processor_templates (id, name, data, updated_at) VALUES (?, ?, ?, ?)",
                (t.id, t.name, t.model_dump_json(), t.updated_at.isoformat()),
            )
            for t in processor_templates
        ]
        stmts += [
            ("INSERT INTO deleted_default_templates (id, library_id) VALUES (?, ?)", pair)
            for pair in deleted_defaults
        ]
        stmts += [
            ("INSERT INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
            for k, v in settings.model_dump(mode="json").items()
        ]
        stmts.append(
            (
                "INSERT INTO settings (key, value) VALUES ('ignored_disks', ?)",
                (json.dumps(ignored_disks),),
            )
        )
        await self.db.execute_all(stmts)

    # --- sync reports ------------------------------------------------------------------------

    async def put_report(self, report: SyncReport, job_id: str | None = None) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO sync_reports (id, device_id, job_id, started_at, finished_at, status, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                report.id,
                report.device_id,
                job_id,
                report.started_at.isoformat(),
                report.finished_at.isoformat() if report.finished_at else None,
                report.status,
                report.model_dump_json(),
            ),
        )

    async def reports(self, device_id: str | None = None, limit: int = 50) -> list[SyncReport]:
        if device_id is None:
            rows = await self.db.fetchall(
                "SELECT data FROM sync_reports ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        else:
            rows = await self.db.fetchall(
                "SELECT data FROM sync_reports WHERE device_id = ? ORDER BY started_at DESC LIMIT ?",
                (device_id, limit),
            )
        return [SyncReport.model_validate_json(r["data"]) for r in rows]

    async def last_report(self, device_id: str, kind: str) -> SyncReport | None:
        """The device's newest report of a kind ("full" / "update")."""
        row = await self.db.fetchone(
            "SELECT data FROM sync_reports WHERE device_id = ? "
            "AND coalesce(json_extract(data, '$.kind'), 'full') = ? ORDER BY started_at DESC LIMIT 1",
            (device_id, kind),
        )
        return SyncReport.model_validate_json(row["data"]) if row else None

    async def report_jobs(self, report_ids: list[str]) -> dict[str, str]:
        """report id → id of the job that wrote it (for the "log" link of a report)."""
        if not report_ids:
            return {}
        marks = ",".join("?" * len(report_ids))
        rows = await self.db.fetchall(
            f"SELECT id, job_id FROM sync_reports WHERE id IN ({marks}) AND job_id IS NOT NULL",  # noqa: S608
            tuple(report_ids),
        )
        return {r["id"]: r["job_id"] for r in rows}

    # --- notifications centre ----------------------------------------------------------

    async def add_notification(
        self,
        event: str,
        level: str,
        data: dict[str, Any],
        device_id: str | None = None,
        device_name: str | None = None,
        link: str | None = None,
    ) -> dict[str, Any]:
        ts = utcnow().isoformat()
        payload = json.dumps(data, default=str)
        await self.db.execute_all(
            [
                (
                    "INSERT INTO notifications (ts, event, level, device_id, device_name, data, link) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ts, event, level, device_id, device_name, payload, link),
                ),
                (
                    "DELETE FROM notifications WHERE id NOT IN "
                    "(SELECT id FROM notifications ORDER BY id DESC LIMIT ?)",
                    (NOTIFICATIONS_LIMIT,),
                ),
            ]
        )
        row = await self.db.fetchone("SELECT * FROM notifications ORDER BY id DESC LIMIT 1")
        assert row is not None
        return _notification(row)

    async def notifications(self, limit: int = NOTIFICATIONS_LIMIT) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [_notification(r) for r in rows]

    async def unread_notifications(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM notifications WHERE read = 0")
        return int(row["n"]) if row else 0

    async def mark_notifications_read(
        self, ids: list[int] | None = None, read: bool = True
    ) -> None:
        """Mark these (None: all) as read - or as unread again (`read=False`)."""
        flag = 1 if read else 0
        if ids is None:
            await self.db.execute("UPDATE notifications SET read = ? WHERE read != ?", (flag, flag))
        elif ids:
            marks = ",".join("?" * len(ids))
            await self.db.execute(
                f"UPDATE notifications SET read = ? WHERE id IN ({marks})",  # noqa: S608
                (flag, *ids),
            )

    async def clear_notifications(self, ids: list[int] | None = None) -> None:
        """Delete every notification, or the ones in `ids` (Clear)."""
        if ids is None:
            await self.db.execute("DELETE FROM notifications")
        elif ids:
            marks = ",".join("?" * len(ids))
            await self.db.execute(
                f"DELETE FROM notifications WHERE id IN ({marks})",  # noqa: S608
                tuple(ids),
            )

    async def prune_notifications(self, before: datetime) -> int:
        """Delete notifications older than `before`, read or not. Returns how many."""
        cut = before.isoformat()  # ISO timestamps of one format compare as text
        row = await self.db.fetchone("SELECT count(*) AS n FROM notifications WHERE ts < ?", (cut,))
        await self.db.execute("DELETE FROM notifications WHERE ts < ?", (cut,))
        return int(row["n"]) if row else 0

    async def prune_history(self, before: datetime) -> int:
        """Delete sync reports and finished jobs started before `before` (Settings' Keep sync history);
        the newest full and update report of every device stay (the Overview's last sync). Returns the rows deleted."""
        cut = before.isoformat()
        keep = (
            "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY device_id, "
            "coalesce(json_extract(data, '$.kind'), 'full') ORDER BY started_at DESC) AS n FROM sync_reports) "
            "WHERE n = 1"
        )
        n_reports = await self.db.fetchone(
            f"SELECT count(*) FROM sync_reports WHERE started_at < ? AND id NOT IN ({keep})",  # noqa: S608
            (cut,),
        )
        n_jobs = await self.db.fetchone(
            "SELECT count(*) FROM jobs WHERE created_at < ? AND finished_at IS NOT NULL", (cut,)
        )
        await self.db.execute_all(
            [
                (f"DELETE FROM sync_reports WHERE started_at < ? AND id NOT IN ({keep})", (cut,)),  # noqa: S608
                ("DELETE FROM jobs WHERE created_at < ? AND finished_at IS NOT NULL", (cut,)),
            ]
        )
        return int(n_reports[0] if n_reports else 0) + int(n_jobs[0] if n_jobs else 0)

    # --- job history -----------------------------------------------------------------

    async def put_job(self, info: JobInfo) -> None:
        try:
            data = info.model_dump_json()
        except PydanticSerializationError:  # a result that is not JSON: keep the rest
            data = info.model_copy(update={"result": None}).model_dump_json()
        await self.db.execute_all(
            [
                (
                    "INSERT OR REPLACE INTO jobs (id, kind, device_id, status, created_at, finished_at, data) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        info.id,
                        info.kind,
                        info.device_id,
                        info.status.value,
                        info.created_at.isoformat(),
                        info.finished_at.isoformat() if info.finished_at else None,
                        data,
                    ),
                ),
                (
                    "DELETE FROM jobs WHERE id NOT IN "
                    "(SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
                    (HISTORY_LIMIT,),
                ),
            ]
        )

    async def recent_jobs(self, limit: int = HISTORY_LIMIT) -> list[JobInfo]:
        rows = await self.db.fetchall(
            "SELECT data FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [JobInfo.model_validate_json(r["data"]) for r in rows]

    # --- reverse sync ----------------------------------------------------------------

    async def submitted_plays(self, sink: str, plays: list[PlayEvent]) -> set[int]:
        """Indexes into `plays` of those already submitted to `sink` (dedup, never resubmitted)."""
        out: set[int] = set()
        for i, p in enumerate(plays):
            row = await self.db.fetchone(
                "SELECT 1 FROM submitted_plays WHERE sink = ? AND library_id = ? AND item_id = ? "
                "AND played_at = ? AND device_id = ?",
                _play_key(sink, p),
            )
            if row is not None:
                out.add(i)
        return out

    @staticmethod
    def _mark_sql(sink: str, plays: list[PlayEvent]) -> list[tuple[str, Iterable[Any]]]:
        now = utcnow().isoformat()
        return [
            (
                "INSERT OR IGNORE INTO submitted_plays "
                "(sink, library_id, item_id, played_at, device_id, submitted_at) VALUES (?, ?, ?, ?, ?, ?)",
                (*_play_key(sink, p), now),
            )
            for p in plays
        ]

    async def mark_submitted(self, sink: str, plays: list[PlayEvent]) -> None:
        await self.db.execute_all(self._mark_sql(sink, plays))

    async def pending_feedback(self, device_id: str) -> list[PendingRow]:
        rows = await self.db.fetchall(
            "SELECT id, kind, data, attempts, last_error, inflight, created_at, tried_at, tries FROM pending_feedback "
            "WHERE device_id = ? ORDER BY id",
            (device_id,),
        )
        return [
            PendingRow(
                r["id"],
                r["kind"],
                json.loads(r["data"]),
                r["attempts"],
                r["last_error"],
                r["inflight"],
                r["created_at"],
                r["tried_at"],
                r["tries"],
            )
            for r in rows
        ]

    async def feedback_counts(self) -> dict[str, tuple[int, int]]:
        """device id → (pending, dropped) listening data rows (the device list and dashboard)."""
        out: dict[str, tuple[int, int]] = {}
        for r in await self.db.fetchall(
            "SELECT device_id, COUNT(*) AS n FROM pending_feedback GROUP BY device_id"
        ):
            out[r["device_id"]] = (r["n"], 0)
        for r in await self.db.fetchall(
            "SELECT device_id, COUNT(*) AS n FROM dropped_feedback GROUP BY device_id"
        ):
            out[r["device_id"]] = (out.get(r["device_id"], (0, 0))[0], r["n"])
        return out

    async def drop_pending(self, row: PendingRow, device_id: str, error: str) -> None:
        """Give up on a pending row: it moves to dropped_feedback with the reason, in one transaction;
        the newest DROPPED_KEEP per device are kept."""
        await self.db.execute_all(
            [
                (
                    "INSERT INTO dropped_feedback (device_id, kind, data, attempts, error, dropped_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        device_id,
                        row.kind,
                        json.dumps(row.data),
                        row.attempts + 1,
                        error,
                        utcnow().isoformat(),
                    ),
                ),
                ("DELETE FROM pending_feedback WHERE id = ?", (row.id,)),
                (
                    "DELETE FROM dropped_feedback WHERE device_id = ? AND id NOT IN "
                    "(SELECT id FROM dropped_feedback WHERE device_id = ? ORDER BY id DESC LIMIT ?)",
                    (device_id, device_id, DROPPED_KEEP),
                ),
            ]
        )

    async def dropped_feedback(self, device_id: str) -> list[dict[str, Any]]:
        """Newest first: {id, kind, data, attempts, error, dropped_at}."""
        rows = await self.db.fetchall(
            "SELECT id, kind, data, attempts, error, dropped_at FROM dropped_feedback "
            "WHERE device_id = ? ORDER BY id DESC",
            (device_id,),
        )
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    async def clear_dropped_feedback(self, device_id: str) -> None:
        await self.db.execute("DELETE FROM dropped_feedback WHERE device_id = ?", (device_id,))

    async def capture_feedback(
        self,
        device_id: str,
        rows: list[tuple[str, dict[str, Any]]],
        superseded: list[int],
        play_counts: dict[str, int] | None,
    ) -> None:
        """New listening data read from a device, in one transaction: (kind, data) rows appended to
        pending_feedback, superseded pending rows deleted, and the device's absolute play counts merged into its
        journal (None: leave the journal alone)."""
        now = utcnow().isoformat()
        stmts: list[tuple[str, Iterable[Any]]] = [
            ("DELETE FROM pending_feedback WHERE id = ?", (i,)) for i in superseded
        ]
        stmts += [
            (
                "INSERT INTO pending_feedback (device_id, kind, data, created_at) VALUES (?, ?, ?, ?)",
                (device_id, kind, json.dumps(data), now),
            )
            for kind, data in rows
        ]
        if play_counts is not None:
            merged = {**(await self.feedback_journal(device_id) or {}), **play_counts}
            stmts.append(
                (
                    "INSERT OR REPLACE INTO feedback_journal (device_id, play_counts, created_at) "
                    "VALUES (?, ?, ?)",
                    (device_id, json.dumps(merged), now),
                )
            )
        await self.db.execute_all(stmts)

    async def feedback_journal(self, device_id: str) -> dict[str, int] | None:
        row = await self.db.fetchone(
            "SELECT play_counts FROM feedback_journal WHERE device_id = ?", (device_id,)
        )
        return {str(k): int(v) for k, v in json.loads(row["play_counts"]).items()} if row else None

    async def clear_feedback_journal(self, device_id: str) -> None:
        await self.db.execute("DELETE FROM feedback_journal WHERE device_id = ?", (device_id,))

    async def device_items(self) -> dict[str, set[str]]:
        """Per registered device the item keys its manifest held when SyncMusic last read it."""
        rows = await self.db.fetchall("SELECT device_id, keys FROM device_items")
        return {r["device_id"]: set(json.loads(r["keys"])) for r in rows}

    async def set_device_items(self, device_id: str, keys: set[str]) -> None:
        await self.db.execute(
            "INSERT INTO device_items (device_id, keys, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET keys = excluded.keys, updated_at = excluded.updated_at",
            (device_id, json.dumps(sorted(keys)), utcnow().isoformat()),
        )

    async def position_scopes(self, device_id: str) -> dict[str, float]:
        """The library positions per scope recorded after the device's last reverse sync."""
        rows = await self.db.fetchall(
            "SELECT scope, position_s FROM position_scopes WHERE device_id = ?", (device_id,)
        )
        return {r["scope"]: float(r["position_s"]) for r in rows}

    async def set_position_scopes(
        self, device_id: str, library_id: str, positions: dict[str, float]
    ) -> None:
        """Replace the device's recorded scopes of one library (`<library>/...`) with `positions`."""
        stmts: list[tuple[str, tuple[Any, ...]]] = [
            (
                "DELETE FROM position_scopes WHERE device_id = ? AND substr(scope, 1, ?) = ?",
                (device_id, len(library_id) + 1, f"{library_id}/"),
            )
        ]
        stmts += [
            (
                "INSERT INTO position_scopes (device_id, scope, position_s) VALUES (?, ?, ?)",
                (device_id, scope, pos),
            )
            for scope, pos in positions.items()
        ]
        await self.db.execute_all(stmts)

    async def set_inflight(self, row_id: int, count: int | None) -> None:
        await self.db.execute(
            "UPDATE pending_feedback SET inflight = ? WHERE id = ?", (count, row_id)
        )

    async def delivered(
        self, row_id: int, sink: str | None = None, plays: list[PlayEvent] | None = None
    ) -> None:
        """A pending row reached its library: deleted, and its play marked submitted, in one transaction."""
        stmts = self._mark_sql(sink, plays) if sink and plays else []
        stmts.append(("DELETE FROM pending_feedback WHERE id = ?", (row_id,)))
        await self.db.execute_all(stmts)

    async def retry_failed(self, row_id: int, error: str, count: bool = True) -> None:
        """Record a failed delivery; `count` = it was a permanent rejection (attempts + 1)."""
        await self.db.execute(
            "UPDATE pending_feedback SET attempts = attempts + ?, tries = tries + 1, last_error = ?, inflight = NULL, "
            "tried_at = ? WHERE id = ?",
            (int(count), error, utcnow().isoformat(), row_id),
        )

    async def delete_pending(self, row_ids: list[int]) -> None:
        await self.db.executemany(
            "DELETE FROM pending_feedback WHERE id = ?", [(i,) for i in row_ids]
        )

    async def add_local_feedback(
        self,
        library_id: str,
        item_id: str,
        *,
        plays: int = 0,
        last_played: datetime | None = None,
        rating: int | None = None,
        set_rating: bool = False,
        position_s: float | None = None,
        done_row: int | None = None,
    ) -> None:
        """Listening data for a library that cannot take it (Folder): kept in SyncMusic's DB.
        `done_row`: the pending_feedback row it came from, deleted in the same transaction."""
        row = await self.db.fetchone(
            "SELECT play_count, last_played, rating, position_s FROM local_feedback "
            "WHERE library_id = ? AND item_id = ?",
            (library_id, item_id),
        )
        count = (row["play_count"] if row else 0) + plays
        old_last = row["last_played"] if row else None
        new_last = last_played.isoformat() if last_played else None
        last = max(x for x in (old_last, new_last) if x) if (old_last or new_last) else None
        stmts: list[tuple[str, Iterable[Any]]] = [
            (
                "INSERT OR REPLACE INTO local_feedback "
                "(library_id, item_id, play_count, last_played, rating, position_s) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    library_id,
                    item_id,
                    count,
                    last,
                    rating if set_rating else (row["rating"] if row else None),
                    position_s if position_s is not None else (row["position_s"] if row else None),
                ),
            )
        ]
        if done_row is not None:
            stmts.append(("DELETE FROM pending_feedback WHERE id = ?", (done_row,)))
        await self.db.execute_all(stmts)

    async def local_feedback(self, library_id: str, item_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT play_count, last_played, rating, position_s FROM local_feedback "
            "WHERE library_id = ? AND item_id = ?",
            (library_id, item_id),
        )
        return dict(row) if row else None


def _notification(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "event": row["event"],
        "level": row["level"],
        "device_id": row["device_id"],
        "device_name": row["device_name"],
        "data": json.loads(row["data"]),
        "link": row["link"],
        "read": bool(row["read"]),
    }
