# SPDX-License-Identifier: GPL-3.0-or-later
"""REST routes for library management, types, rules, templates and settings."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from syncmusic.api.routes import thumb_response
from syncmusic.core.models import MediaKind, RuleSet
from syncmusic.facade import AppFacade, BrowseSort


class UpdateInstall(BaseModel):
    when: Literal["after_syncs", "now"] = "after_syncs"


class CacheClear(BaseModel):
    kind: Literal["media", "system"] = "media"


class StorageClear(BaseModel):
    item: str | None = None  # one device's / library's share of the part; None = all of it
    keep: int = 1  # database backups: how many of the newest stay


class ValidateRequest(BaseModel):
    """Exactly one of them: a rule set, a single rule, or text (JSON / YAML / .nsp)."""

    # raw objects: an invalid rule (unknown key, wrong type) is a validation result, not a 400
    rule_set: dict[str, Any] | None = None
    rule: dict[str, Any] | None = None
    text: str | None = None


class TestRequest(BaseModel):
    rule_set: RuleSet | None = None  # None: the device's own rule set


class PresetRequest(BaseModel):
    params: dict[str, Any] = {}


class PreviewRequest(BaseModel):
    rule_set: RuleSet


class RulesTestRequest(BaseModel):
    rule_set: RuleSet
    library_ids: list[str] | None = None  # None: every enabled library
    accounts: dict[str, str] = {}  # library id → the library account to read it as


def _accounts(text: str | None) -> dict[str, str]:
    """`lid:account,lid2:account` (a query parameter) → {library id: account id}."""
    pairs = (x.split(":", 1) for x in (text or "").split(",") if ":" in x)
    return {lid: aid for lid, aid in pairs if lid and aid}


class RulesTextRequest(BaseModel):
    rule_set: RuleSet
    format: str = "yaml"


class DiscoverRequest(BaseModel):
    """Network discovery on demand."""

    kinds: list[Literal["smb", "ftp"]] = ["smb", "ftp"]
    timeout_s: float = Field(3.0, ge=0.5, le=15)


class SharesRequest(BaseModel):
    host: str = Field(min_length=1)
    port: int = Field(445, ge=1, le=65535)
    username: str = ""
    password: str = ""


class BrowseRequest(BaseModel):
    """Read-only folder picker; server fields are ignored for `local`."""

    # disk: a connected USB disk (`disk`), paths from its root; mounted read-only for this when it is not mounted
    source: Literal["local", "ftp", "smb", "mtp", "disk"] = "local"
    path: str = "/"
    host: str = ""
    port: int | None = Field(None, ge=1, le=65535)
    username: str = ""
    password: str = ""
    tls: bool = False
    disk: str = ""  # mtp: the found player; disk: the /dev path
    storage: str | None = None  # mtp: the storage's name (None: the first)


class ReadRequest(BaseModel):
    ids: list[int] | None = None  # None: all


def router(facade: AppFacade) -> APIRouter:
    r = APIRouter()

    # --- network discovery (only when the user asks) -------------------------------

    @r.post("/discovery/servers")
    async def discover_servers(req: DiscoverRequest) -> list[dict[str, Any]]:
        """SMB / FTP servers on the LAN (mDNS, WS-Discovery, NetBIOS); waits `timeout_s`."""
        return await facade.discover_servers(req.kinds, req.timeout_s)

    @r.post("/discovery/shares")
    async def discover_shares(req: SharesRequest) -> list[dict[str, Any]]:
        """The disk shares of one SMB server (empty user: guest); 502 `unreachable` with the reason."""
        return await facade.discover_shares(req.host, req.port, req.username, req.password)

    @r.post("/browse")
    async def browse_path(req: BrowseRequest) -> dict[str, Any]:
        """One folder with its subfolders and a preview (folders / media files in the first levels); SMB at `/`:
        the shares. Never writes. 502 `unreachable` with the reason."""
        return await facade.browse_path(
            req.source,
            req.path,
            req.host,
            req.port,
            req.username,
            req.password,
            req.tls,
            req.disk,
            req.storage,
        )

    @r.get("/discovery/mtp-storages")
    async def mtp_storages(disk: str) -> list[dict[str, Any]]:
        """The storages of a found, not registered MTP player; 502 `unreachable` with the reason."""
        return await facade.player_storages(disk)

    # --- libraries ---------------------------------------------------------------------------------

    @r.patch("/libraries/{library_id}")
    async def update_library(library_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return await facade.update_library(library_id, changes)

    @r.delete("/libraries/{library_id}", status_code=204)
    async def delete_library(
        library_id: str, edited_defaults: Literal["keep", "delete"] | None = None
    ) -> None:
        """`edited_defaults`: what happens to its edited default templates (409 without it)."""
        await facade.delete_library(library_id, edited_defaults)

    @r.post("/libraries/{library_id}/refresh", status_code=202)
    async def refresh_library(library_id: str) -> dict[str, Any]:
        return await facade.refresh_library(library_id)

    @r.get("/libraries/{library_id}/items")
    async def library_items(
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
        """`account`: browsed as that library account."""
        return await facade.library_items(
            library_id,
            query,
            kind,
            page,
            page_size,
            group=group,
            album_id=album_id,
            artist_id=artist_id,
            series=series,
            playlist_id=playlist_id,
            sort=sort,
            account=account or None,
        )

    @r.get("/libraries/{library_id}/thumb", response_model=None)
    async def library_thumb(
        library_id: str, item: str, large: bool = False, account: str | None = None
    ) -> FileResponse | Response:
        """Small square JPEG of a row's `cover_item` (`large`: the artwork grid's); 204 when
        there is none (the UI shows a placeholder)."""
        path = await facade.library_thumb(library_id, item, large, account or None)
        return thumb_response(path)

    @r.get("/libraries/{library_id}/playlists")
    async def library_playlists(
        library_id: str, query: str = "", account: str | None = None
    ) -> list[dict[str, Any]]:
        """The library's playlists with their track count and size; as `account`."""
        return await facade.library_playlists(library_id, query, account or None)

    # --- types ---------------------------------------------------------------------------------------

    @r.get("/library-types")
    async def library_types() -> list[dict[str, Any]]:
        return facade.library_types()

    @r.get("/device-types")
    async def device_types() -> list[dict[str, Any]]:
        return facade.device_types()

    @r.get("/processor-types")
    async def processor_types() -> list[dict[str, Any]]:
        return facade.processor_types()

    # --- rules and templates -------------------------------------------------------------------------

    @r.post("/rules/validate")
    async def validate_rules(req: ValidateRequest) -> dict[str, Any]:
        return await facade.validate_rules(req.rule_set, req.rule, req.text)

    @r.post("/devices/{device_id}/rules/test")
    async def test_rules(device_id: str, req: TestRequest | None = None) -> dict[str, Any]:
        return await facade.test_rules(device_id, (req or TestRequest()).rule_set)

    @r.post("/devices/{device_id}/rules/preview")
    async def preview_rules(device_id: str, req: PreviewRequest) -> dict[str, Any]:
        """Live preview of an unsaved rule set: the plan's numbers on the connected device."""
        return await facade.preview_rules(device_id, req.rule_set)

    @r.get("/rules/fields")
    async def rule_fields(libraries: str | None = None) -> dict[str, Any]:
        """`libraries` (comma-separated ids): mark the fields none of them has as not available."""
        ids = [x for x in libraries.split(",") if x] if libraries is not None else None
        return await facade.rule_fields(ids)

    @r.get("/rules/values")
    async def rule_values(
        field: str,
        q: str = "",
        libraries: str | None = None,
        limit: int = 20,
        kind: MediaKind | None = None,
        accounts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Autocomplete values of `field` from the library indexes (`libraries`: comma-separated ids, `kind`: only items of that kind, e.g. the audiobooks of an Audiobookshelf library, `accounts`: `lid:account, …` read those libraries as these accounts)."""
        ids = [x for x in libraries.split(",") if x] if libraries else None
        return await facade.rule_values(
            field, q, ids, limit, kind=kind, accounts=_accounts(accounts)
        )

    @r.get("/rule-presets")
    async def rule_presets(libraries: str | None = None) -> list[dict[str, Any]]:
        """Rule presets; `libraries` (comma-separated ids) marks those none of them can serve."""
        ids = [x for x in libraries.split(",") if x] if libraries is not None else None
        return await facade.rule_presets(ids)

    @r.post("/rule-presets/{preset_id}/rule")
    async def rule_preset(preset_id: str, req: PresetRequest | None = None) -> dict[str, Any]:
        """The rule a preset builds with these parameters, to be added to a rule set by the client."""
        return facade.rule_preset(preset_id, (req or PresetRequest()).params)

    @r.post("/rules/text")
    async def rules_text(req: RulesTextRequest) -> dict[str, str]:
        return facade.rules_text(req.rule_set, req.format)

    @r.post("/rules/test")
    async def rules_test(req: RulesTestRequest) -> dict[str, Any]:
        """What a rule set (a template) selects from libraries, without a device."""
        return await facade.test_rules_on(req.rule_set, req.library_ids, req.accounts)

    @r.post("/rules/test/csv", response_model=None)
    async def rules_test_csv(req: RulesTestRequest) -> Response:
        """Every item the rule set selects, with its rules, as a CSV download."""
        text = await facade.test_rules_csv(req.rule_set, req.library_ids, req.accounts)
        return Response(
            text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="matches.csv"'},
        )

    @r.get("/templates")
    async def templates() -> list[dict[str, Any]]:
        return await facade.templates()

    @r.post("/templates", status_code=201)
    async def add_template(data: dict[str, Any]) -> dict[str, Any]:
        return await facade.add_template(data)

    @r.get("/templates/{template_id}")
    async def template(template_id: str) -> dict[str, Any]:
        return await facade.template(template_id)

    @r.patch("/templates/{template_id}")
    async def update_template(template_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return await facade.update_template(template_id, changes)

    @r.delete("/templates/{template_id}", status_code=204)
    async def delete_template(template_id: str, detach: bool = False) -> None:
        await facade.delete_template(template_id, detach)

    @r.post("/templates/{template_id}/reset")
    async def reset_template(template_id: str) -> dict[str, Any]:
        """An edited default back to its generated rules; 400 for any other template."""
        return await facade.reset_template(template_id)

    @r.post("/templates/{template_id}/copy")
    async def copy_template(template_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """An unsaved draft to edit (built-in templates are read-only): POST it to /templates to create it; body
        `{"name": ...}` optional."""
        return await facade.copy_template(template_id, (data or {}).get("name"))

    # processor templates: reusable processor chains, applied to or linked with devices
    @r.get("/processor-templates")
    async def processor_templates() -> list[dict[str, Any]]:
        return await facade.processor_templates()

    @r.post("/processor-templates", status_code=201)
    async def add_processor_template(data: dict[str, Any]) -> dict[str, Any]:
        return await facade.add_processor_template(data)

    @r.get("/processor-templates/{template_id}")
    async def processor_template(template_id: str) -> dict[str, Any]:
        return await facade.processor_template(template_id)

    @r.patch("/processor-templates/{template_id}")
    async def update_processor_template(
        template_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Saves it; every linked device gets the new chain."""
        return await facade.update_processor_template(template_id, changes)

    @r.delete("/processor-templates/{template_id}", status_code=204)
    async def delete_processor_template(template_id: str, detach: bool = False) -> None:
        await facade.delete_processor_template(template_id, detach)

    @r.post("/processor-templates/{template_id}/copy")
    async def copy_processor_template(
        template_id: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await facade.copy_processor_template(template_id, (data or {}).get("name"))

    @r.get("/templates/{template_id}/usage")
    async def template_usage(template_id: str) -> list[dict[str, Any]]:
        return await facade.template_usage(template_id)

    # --- notifications centre ---------------------------------------------------------------

    @r.get("/notifications")
    async def notifications(limit: int = 200) -> dict[str, Any]:
        """The newest notifications (`items`, newest first) and how many are unread."""
        return await facade.notifications(limit)

    @r.post("/notifications/read")
    async def notifications_read(body: ReadRequest) -> dict[str, Any]:
        """Mark `ids` as read; without `ids` all of them."""
        return await facade.mark_notifications_read(body.ids)

    @r.delete("/notifications")
    async def notifications_clear() -> dict[str, Any]:
        """Delete every notification (Clear all); returns the unread count (0)."""
        return await facade.clear_notifications()

    @r.delete("/notifications/{notification_id}")
    async def notification_clear(notification_id: int) -> dict[str, Any]:
        """Delete one notification (its Clear button); returns the unread count."""
        return await facade.clear_notifications([notification_id])

    # --- settings --------------------------------------------------------------------------------------

    @r.get("/settings")
    async def settings() -> dict[str, Any]:
        return await facade.settings()

    @r.get("/settings/schema")
    async def settings_schema() -> dict[str, Any]:
        """The settings form (AppSettings JSON schema) and the read-only paths of the bootstrap config."""
        return facade.settings_schema()

    @r.get("/settings/cache")
    async def settings_cache() -> dict[str, Any]:
        """The conversion cache: used and max bytes, the free and total bytes of its disk."""
        return await facade.cache_info()

    @r.post("/settings/cache/clear")
    async def settings_cache_clear(body: CacheClear | None = None) -> dict[str, Any]:
        """Empty the conversion cache (`media`, the default; 409 while a job runs) or the `system` cache (thumbnails, previews); returns the bytes freed and the new state."""
        return await facade.clear_cache(body.kind if body else "media")

    @r.get("/system/storage")
    async def system_storage() -> dict[str, Any]:
        """What the app keeps on disk, part by part (size, paths, clear action, per-device/-library items) and the
        free space of the file systems involved."""
        return await facade.storage()

    @r.post("/system/storage/{part}/clear")
    async def system_storage_clear(part: str, body: StorageClear | None = None) -> dict[str, Any]:
        """Clear one part (or one item of it); 409 while a job needs that data. Returns the bytes freed and the
        new report."""
        b = body or StorageClear()
        return await facade.storage_clear(part, b.item, b.keep)

    @r.get("/system/resources")
    async def system_resources() -> dict[str, Any]:
        """Memory (RSS, children), CPU %, uptime, threads, event-loop lag and the in-memory caches."""
        return await facade.resources()

    @r.post("/system/resources/free")
    async def system_resources_free() -> dict[str, Any]:
        """Drop the in-memory caches rebuilt on demand (409 while a job runs)."""
        return await facade.free_memory()

    @r.get("/settings/ffmpeg")
    async def settings_ffmpeg(path: str | None = None) -> dict[str, Any]:
        """The ffmpeg at `path` (default: the one in use): version, encoders found, missing ones, error."""
        return await facade.ffmpeg_check(path)

    @r.get("/settings/compas-sdtool")
    async def settings_compas_sdtool(path: str | None = None) -> dict[str, Any]:
        """The compas-sdtool at `path` (default: the setting, else the usual places): found, version, build,
        database format, error, where it comes from and its licence."""
        return await facade.compas_sdtool(path)

    @r.post("/settings/compas-sdtool/download", status_code=202)
    async def settings_compas_sdtool_download() -> dict[str, Any]:
        """Download compas-sdtool for this computer from SyncMusic's release (checksum, architecture and build
        checked) into the data folder, as a job."""
        return await facade.compas_sdtool_download()

    @r.get("/settings/backup")
    async def settings_backup(secrets: bool = False) -> JSONResponse:
        """Download a backup; `secrets=true`: with passwords and the webhook URL, in plain text."""
        data = await facade.backup(secrets)
        name = f"syncmusic-backup-{data['created_at'][:10]}.json"
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @r.post("/settings/backup/preview")
    async def settings_backup_preview(body: dict[str, Any]) -> dict[str, Any]:
        """What restoring this backup replaces: counts, passwords included, warnings."""
        return await facade.restore_preview(body)

    @r.post("/settings/backup/restore")
    async def settings_backup_restore(body: dict[str, Any]) -> dict[str, Any]:
        """Replace libraries, devices, templates and settings by the backup's (409 while a job runs)."""
        return await facade.restore(body)

    @r.get("/settings/export")
    async def settings_export(what: Literal["templates", "devices"] = "templates") -> JSONResponse:
        """Export templates, or device settings with the templates they use; no secrets."""
        data = await facade.export(what)
        name = f"syncmusic-{what}-{data['created_at'][:10]}.json"
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @r.post("/settings/import/preview")
    async def settings_import_preview(body: dict[str, Any]) -> dict[str, Any]:
        """The library mapping and what importing this file adds, replaces or skips."""
        return await facade.import_preview(body)

    @r.post("/settings/import")
    async def settings_import(body: dict[str, Any]) -> dict[str, Any]:
        """Import as the preview says (409 while a job runs)."""
        return await facade.import_apply(body)

    @r.get("/updates")
    async def updates() -> dict[str, Any]:
        """The running version and commit, and the last update check."""
        return await facade.updates()

    @r.post("/updates/check")
    async def updates_check() -> dict[str, Any]:
        """Check the configured repository for a newer main now."""
        return await facade.check_updates()

    @r.post("/updates/install")
    async def updates_install(body: UpdateInstall | None = None) -> dict[str, Any]:
        """Install the newer version the last check found and restart (`when`: after_syncs waits for running
        jobs, now interrupts them; 409 when refused)."""
        return await facade.install_update((body or UpdateInstall()).when)

    @r.get("/diagnostics")
    async def diagnostics() -> list[dict[str, Any]]:
        """Settings → Maintenance: ffmpeg, library connections, USB devices, CPU temperature and throttling."""
        return await facade.diagnostics()

    @r.get("/diagnostics/bundle")
    async def diagnostics_bundle() -> Response:
        """The diagnostics zip for a bug report: secrets masked, no media or databases."""
        data = await facade.diagnostics_bundle()
        name = f"syncmusic-diagnostics-{datetime.now(UTC):%Y%m%d-%H%M%S}.zip"
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @r.get("/db-backups")
    async def db_backups() -> list[dict[str, Any]]:
        """Settings → Backup: the database backups (automatic, before an update/reset/restore, manual)."""
        return await facade.db_backups()

    @r.post("/db-backups", status_code=201)
    async def make_db_backup() -> dict[str, Any]:
        """A database backup now; the oldest beyond the setting are deleted."""
        return await facade.make_db_backup()

    @r.get("/db-backups/{name}", response_model=None)
    async def db_backup_download(name: str) -> FileResponse:
        path = await facade.db_backup_file(name)
        return FileResponse(path, media_type="application/gzip", filename=path.name)

    @r.post("/db-backups/{name}/restore")
    async def db_backup_restore(name: str) -> dict[str, Any]:
        """Replace the whole database by this backup and restart in place (409 while a job runs)."""
        return await facade.restore_db_backup(name)

    @r.post("/settings/factory-reset")
    async def factory_reset(body: dict[str, Any]) -> dict[str, Any]:
        """Delete everything SyncMusic stores (`{"confirm": "reset syncmusic"}`); 409 while a job runs."""
        return await facade.factory_reset(str(body.get("confirm", "")))

    @r.post("/settings/webhook-test")
    async def webhook_test(body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a test notification (`{"url": ...}`; empty = the stored webhook URL)."""
        return await facade.test_webhook((body or {}).get("url"))

    @r.patch("/settings")
    async def update_settings(changes: dict[str, Any]) -> dict[str, Any]:
        return await facade.update_settings(changes)

    @r.get("/logs")
    async def logs(lines: int = 200) -> list[str]:
        return facade.logs(lines)

    @r.get("/logs/entries")
    async def log_entries(
        level: str | None = None,
        device: str | None = None,
        job: str | None = None,
        query: str = "",
        limit: int = 500,
        step: str | None = None,
    ) -> dict[str, Any]:
        """`step`: the sync step the line was logged in (plan / reverse / copy / database / verify)."""
        return await facade.log_entries(level, device, job, query, limit, step)

    @r.get("/logs/download")
    async def log_download(
        level: str | None = None,
        device: str | None = None,
        job: str | None = None,
        query: str = "",
        step: str | None = None,
    ) -> PlainTextResponse:
        text = await facade.log_text(level, device, job, query, step)
        tag = re.sub(r"[^\w-]", "", job or "")  # header-safe
        name = f"syncmusic{'-' + tag if tag else ''}.log"
        return PlainTextResponse(
            text, headers={"Content-Disposition": f'attachment; filename="{name}"'}
        )

    return r
