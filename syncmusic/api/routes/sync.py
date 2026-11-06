# SPDX-License-Identifier: GPL-3.0-or-later
"""REST routes for libraries (registration), devices, plans, jobs and sync reports."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from syncmusic.api.routes import thumb_response
from syncmusic.core.fit import FitTarget
from syncmusic.core.manifest import ManifestError, ManifestTooNew
from syncmusic.core.models import LibraryConfig
from syncmusic.core.rules import RuleError
from syncmusic.core.rules.templates import IdField
from syncmusic.core.sync import FirstSync, RegisterRequest, SyncError, fs_damage
from syncmusic.facade import AppFacade
from syncmusic.infra.blockdev.guard import GuardError
from syncmusic.infra.blockdev.mount import BlockdevError

STATUS = {
    "not_found": 404,
    "invalid": 400,
    "conflict": 409,
    "busy": 409,
    "not_connected": 409,
    "unmountable": 409,
    "confirmation_required": 409,
    "does_not_fit": 409,
    "library_unavailable": 503,
    "cancelled": 409,
    "unreachable": 502,
    "fs_damaged": 409,
}


class SuggestRequest(BaseModel):
    """What Register knows so far: the type, the chosen templates / libraries, the target."""

    model_config = ConfigDict(extra="forbid")

    type: str = ""  # may be empty with `device_id`
    template_ids: list[str] = []
    library_ids: list[str] = []
    disk: str | None = None
    mount_path: str | None = None
    name: str = ""
    # a registered device: type, libraries, templates and target come from its config
    device_id: str | None = None
    # the plan review: judged on the plan's items and the space they may use
    plan_id: str | None = None


class IgnoreDiskRequest(BaseModel):
    disk: str
    ignore: bool = True


class BindDiskRequest(BaseModel):
    disk: str | None = None  # /dev path; default: the disk the device's directory is on


class FormatRequest(BaseModel):
    confirm: str
    fs: str | None = None


class LibraryCheckRequest(BaseModel):
    settings: dict[str, Any] | None = None
    account: str | None = None  # one of the library's accounts


class CheckDeviceRequest(BaseModel):
    settings: dict[str, Any] = {}


class ChainPreviewRequest(BaseModel):
    library_id: str
    item_id: str
    processors: list[dict[str, Any]] | None = (
        None  # the chain on screen (unsaved); default: the device's
    )


class ChainLintRequest(BaseModel):
    processors: list[dict[str, Any]]


class RenameRequest(BaseModel):
    name: str
    preview: bool = False


class PlayLogRequest(BaseModel):
    on: bool


class ManualSelectionRequest(BaseModel):
    library_id: str
    ids: list[str]
    field: IdField = "track_id"
    remove: bool = False  # take them out again (Undo)


class RemoveContentsRequest(BaseModel):
    keys: list[str]  # ItemRef keys from GET /devices/{id}/contents
    exclude: bool = True  # the own template excludes them: the next sync does not put them back


class PlanRequest(BaseModel):
    force: bool = False


class SyncAllRequest(BaseModel):
    mode: Literal["full", "update"] = "full"


class FitRequest(BaseModel):
    to: FitTarget = "mp3"
    bitrate_kbps: int = 320


class ApplyRequest(BaseModel):
    confirm: bool = False
    first_sync: FirstSync | None = None
    confirm_db_limit: bool = False  # a plan over the device database limit
    # the review's choice per playlist conflict, by playlist name
    playlist_choices: dict[str, Literal["merge", "replace", "ignore", "rename"]] = {}


MEDIA_CHUNK = 1024 * 1024


class ChunkedMediaResponse(FileResponse):
    """A file for a media element, answered in short ranges: a browser asks for `bytes=N-` and keeps
    reading the rest only while it plays, so a long answer holds one of its few connections to the
    server even after the element is gone, and later requests of the page stall. A capped 206 ends
    at once; the browser asks for the next part when it needs it."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        headers = []
        for name, value in scope.get("headers", []):
            if name == b"range":
                m = re.fullmatch(rb"bytes=(\d+)-", value.strip())
                if m:
                    start = int(m.group(1))
                    value = b"bytes=%d-%d" % (start, start + MEDIA_CHUNK - 1)
            headers.append((name, value))
        await super().__call__({**scope, "headers": headers}, receive, send)


def _error(status: int, code: str, message: str, headers: Any = None, **extra: Any) -> JSONResponse:
    return JSONResponse(
        {"error": code, "message": message, **extra}, status_code=status, headers=headers
    )


def _damaged(request: Request, e: BaseException) -> JSONResponse:
    """409 fs_damaged; the device in the path is marked damaged."""
    device_id = request.path_params.get("device_id")
    request.app.state.facade.app.sync.mark_damaged(device_id, e)
    return _error(
        409,
        "fs_damaged",
        f"{device_id or 'the device'}: the file system reports an I/O error ({e}); check and repair it, or wipe it",
    )


# HTTP errors raised by the framework itself (unknown route, wrong method)
HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


def validation_message(errors: Any) -> str:
    """One line '<loc>: <msg>; ...' for pydantic / FastAPI validation errors (no URLs, no newlines)."""
    parts = []
    for err in errors:
        if err.get("type") == "json_invalid":  # loc is ('body', <offset>), not a field
            pos = err.get("loc", ())[-1]
            ctx = err.get("ctx") or {}
            detail = f": {ctx['error']}" if ctx.get("error") else ""
            parts.append(f"malformed JSON body at position {pos}{detail}")
            continue
        if err.get("type") == "missing" and tuple(err.get("loc", ())) == ("body",):
            parts.append("a JSON body is required")  # no body at all
            continue
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
        msg = str(err.get("msg", "invalid")).replace("\n", " ").removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def install_error_handlers(api: Any) -> None:
    @api.exception_handler(SyncError)
    async def sync_error(_: Request, e: SyncError) -> JSONResponse:
        return _error(STATUS.get(e.code, 400), e.code, str(e))

    @api.exception_handler(ManifestTooNew)
    async def too_new(_: Request, e: ManifestTooNew) -> JSONResponse:
        return _error(409, "manifest_too_new", str(e), choices=list(e.choices))

    @api.exception_handler(ManifestError)
    async def bad_manifest(request: Request, e: ManifestError) -> JSONResponse:
        if fs_damage(e) is not None:
            return _damaged(request, e)
        return _error(409, "manifest_unreadable", str(e))

    @api.exception_handler(OSError)
    async def os_error(request: Request, e: OSError) -> JSONResponse:
        """A device read or write failed: a JSON error, no traceback; EIO & co. mark the device damaged."""
        if fs_damage(e) is not None:
            return _damaged(request, e)
        # e.g. a Remove job deleted the file a player or cover asks for
        if isinstance(e, FileNotFoundError):
            return _error(404, "not_found", str(e))
        return _error(500, "io_error", str(e))

    @api.exception_handler(GuardError)
    async def guard(_: Request, e: GuardError) -> JSONResponse:
        return _error(403, "guard_refused", str(e))

    @api.exception_handler(BlockdevError)
    async def blockdev(_: Request, e: BlockdevError) -> JSONResponse:
        return _error(500, "blockdev_failed", str(e))

    @api.exception_handler(RuleError)
    async def bad_rule(_: Request, e: RuleError) -> JSONResponse:
        return _error(400, "invalid", f"invalid rules: {e}")

    @api.exception_handler(RequestValidationError)
    async def bad_request(_: Request, e: RequestValidationError) -> JSONResponse:
        """FastAPI's own body/query validation: 400 {error, message} like every other error."""
        return _error(400, "invalid", "invalid request: " + validation_message(e.errors()))

    @api.exception_handler(ValidationError)
    async def validation(_: Request, e: ValidationError) -> JSONResponse:
        """A config the service validated (device PATCH/register, library settings)."""
        # "invalid settings: root: must not be empty", not the model's class name
        what = "settings" if e.title.endswith("Settings") else e.title
        return _error(400, "invalid", f"invalid {what}: " + validation_message(e.errors()))

    @api.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, e: StarletteHTTPException) -> JSONResponse:
        return _error(
            e.status_code,
            HTTP_CODES.get(e.status_code, "invalid"),
            str(e.detail),
            headers=e.headers,
        )


def router(facade: AppFacade) -> APIRouter:
    r = APIRouter()

    @r.get("/libraries")
    async def libraries() -> list[dict[str, Any]]:
        return await facade.libraries()

    @r.post("/libraries", status_code=201)
    async def add_library(cfg: LibraryConfig) -> dict[str, Any]:
        return await facade.add_library(cfg)

    @r.get("/libraries/{library_id}")
    async def library(library_id: str) -> dict[str, Any]:
        return await facade.library(library_id)

    @r.post("/libraries/{library_id}/check")
    async def check_library(
        library_id: str, req: LibraryCheckRequest | None = None
    ) -> dict[str, Any]:
        """Health of the library; with `settings`: of those unsaved values merged over it."""
        return await facade.check_library(
            library_id, req.settings if req else None, req.account if req else None
        )

    @r.get("/devices/allowlist.toml")
    async def devices_allowlist(device: list[str] | None = Query(None)) -> Response:
        """The hardware test suite's allowlist (tests/hardware.toml) for the given registered devices (all without
        `device`)."""
        return Response(
            await facade.device_allowlist(device),
            media_type="application/toml",
            headers={"Content-Disposition": 'attachment; filename="hardware.toml"'},
        )

    @r.get("/devices")
    async def devices() -> dict[str, Any]:
        return await facade.devices()

    @r.post("/devices/ignore")
    async def ignore_disk(req: IgnoreDiskRequest) -> dict[str, Any]:
        """Ignore a plugged, unregistered disk, or stop ignoring it (`ignore: false`) → the device list."""
        return await facade.ignore_disk(req.disk, req.ignore)

    @r.get("/devices/diagnostics")
    async def device_diagnostics() -> dict[str, Any]:
        return await facade.device_diagnostics()

    @r.post("/devices/suggest-processor-template")
    async def suggest_processor_template(req: SuggestRequest) -> dict[str, Any]:
        """The processor template that fits the libraries and the device best, or a generated draft."""
        return await facade.suggest_processor_template(req.model_dump())

    @r.post("/devices/register", status_code=201)
    async def register(req: RegisterRequest) -> dict[str, Any]:
        return await facade.register_device(req)

    @r.get("/devices/{device_id}")
    async def device(device_id: str) -> dict[str, Any]:
        return await facade.device(device_id)

    @r.patch("/devices/{device_id}")
    async def update_device(device_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return await facade.update_device(device_id, changes)

    @r.delete("/devices/{device_id}", status_code=204)
    async def delete_device(device_id: str) -> None:
        await facade.delete_device(device_id)

    @r.get("/devices/{device_id}/profile")
    async def profile(device_id: str) -> dict[str, Any]:
        """The device profile: each field's value and source (default, known_model, detected, setting,
        override); detection only while the device is connected."""
        return await facade.device_profile(device_id)

    @r.get("/devices/{device_id}/space")
    async def space(device_id: str) -> dict[str, Any]:
        return await facade.device_space(device_id)

    @r.get("/devices/{device_id}/positions")
    async def positions(device_id: str) -> list[dict[str, Any]]:
        """Audiobooks in progress on the device and who is ahead, the device or the library."""
        return await facade.device_positions(device_id)

    @r.get("/devices/{device_id}/feedback")
    async def feedback(device_id: str) -> dict[str, Any]:
        """Listening data not delivered to its library yet (`pending`) and given up on (`dropped`)."""
        return await facade.device_feedback(device_id)

    @r.delete("/devices/{device_id}/feedback/dropped", status_code=204)
    async def clear_dropped(device_id: str) -> None:
        await facade.clear_dropped_feedback(device_id)

    @r.get("/devices/{device_id}/contents")
    async def contents(request: Request, device_id: str, refresh: bool = False) -> Response:
        """Player contents: everything on the device; cached until its manifest/database changes
        , `refresh` reads it again (files another tool changed while it is mounted). With an ETag:
         a tab switch revalidates and gets a 304 instead of thousands of rows again."""
        etag = None if refresh else await facade.device_contents_etag(device_id)
        if etag is not None and request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        rows = await facade.device_contents(device_id, refresh)
        etag = await facade.device_contents_etag(device_id)
        headers = {"Cache-Control": "no-cache", **({"ETag": etag} if etag else {})}
        return JSONResponse(rows, headers=headers)

    @r.get("/devices/{device_id}/contents/file")
    async def content_file(device_id: str, key: str) -> FileResponse:
        """The item's file for the browser player; codecs a browser may not play come as MP3."""
        path, media_type = await facade.device_content_media(device_id, key)
        # stat here: a file removed meanwhile is a 404, not FileResponse's RuntimeError
        return ChunkedMediaResponse(
            path, media_type=media_type, stat_result=await asyncio.to_thread(os.stat, path)
        )

    @r.get("/devices/{device_id}/contents/thumb", response_model=None)
    async def content_thumb(device_id: str, key: str) -> FileResponse | Response:
        """Small square JPEG of the picture in the item's file; 204 when there is none."""
        return thumb_response(await facade.device_thumb(device_id, key))

    @r.post("/devices/{device_id}/contents/remove", status_code=202)
    async def remove_contents(device_id: str, req: RemoveContentsRequest) -> dict[str, Any]:
        """Remove these items from the device (a sync job); they do not come back with the next sync."""
        return await facade.remove_from_device(device_id, req.keys, req.exclude)

    @r.post("/devices/{device_id}/orphans/remove", status_code=202)
    async def remove_orphans(device_id: str) -> dict[str, Any]:
        """Remove every orphan (gone from its library) from the device (a sync job)."""
        return await facade.remove_orphans(device_id)

    @r.post("/devices/{device_id}/manual-selection")
    async def manual_selection(device_id: str, req: ManualSelectionRequest) -> dict[str, Any]:
        return await facade.add_to_device(device_id, req.library_id, req.ids, req.field, req.remove)

    @r.post("/templates/{template_id}/manual-selection")
    async def template_selection(template_id: str, req: ManualSelectionRequest) -> dict[str, Any]:
        """Add library items to a template's Manual selection rule (library browser)."""
        return await facade.add_to_template(
            template_id, req.library_id, req.ids, req.field, req.remove
        )

    @r.post("/devices/{device_id}/eject", status_code=204)
    async def eject(device_id: str) -> None:
        await facade.eject_device(device_id)

    @r.post("/devices/{device_id}/bind-disk")
    async def bind_disk(device_id: str, req: BindDiskRequest | None = None) -> dict[str, Any]:
        """A directory-bound device becomes a disk device (USB serial / fs UUID), so it can be formatted."""
        return await facade.bind_device_disk(device_id, req.disk if req else None)

    @r.post("/devices/{device_id}/format")
    async def format_device(device_id: str, req: FormatRequest) -> dict[str, Any]:
        return await facade.format_device(device_id, req.confirm, req.fs)

    @r.post("/devices/{device_id}/check")
    async def check_device(device_id: str, req: CheckDeviceRequest) -> dict[str, Any]:
        """A server device (FTP) logs in with these settings, not saved (Device settings' Test and save); the server's reason comes back as the error."""
        return await facade.check_device(device_id, req.settings)

    @r.post("/devices/{device_id}/wipe", status_code=202)
    async def wipe_device(device_id: str, req: FormatRequest) -> dict[str, Any]:
        """Wipe a Rockbox player (`confirm` = its name): every file except `/.rockbox`, as a job."""
        return await facade.wipe_device(device_id, req.confirm)

    @r.get("/devices/{device_id}/quality-profiles")
    async def quality_profiles(device_id: str) -> list[dict[str, Any]]:
        """Maximum quality / Save space / For a weak player with the chain each makes for this device;
        `PATCH /devices/{id}` `{"quality_profile": …}` applies one."""
        return await facade.quality_profiles(device_id)

    @r.post("/devices/{device_id}/chain-lint")
    async def chain_lint(device_id: str, req: ChainLintRequest) -> list[dict[str, Any]]:
        """Warnings about a chain for this device: [{id, step, message, fix, processors}]."""
        return await facade.chain_lint(device_id, req.processors)

    @r.get("/devices/{device_id}/chain-preview/tracks")
    async def chain_preview_tracks(device_id: str, q: str = "") -> list[dict[str, Any]]:
        """Tracks to try the chain on: one per format first; `q` searches title, artist, album,
        author and series of every item."""
        return await facade.chain_preview_tracks(device_id, q)

    @r.post("/devices/{device_id}/chain-preview")
    async def chain_preview(device_id: str, req: ChainPreviewRequest) -> dict[str, Any]:
        """Run the chain on one item: {rows: [{key, before, after, changed}], steps, file, size, download}."""
        return await facade.chain_preview(device_id, req.library_id, req.item_id, req.processors)

    @r.get("/chain-preview/{token}/{name}")
    async def chain_preview_file(token: str, name: str) -> FileResponse:
        """Download result: the processed file of a recent preview."""
        path = facade.chain_preview_file(token, name)
        return FileResponse(path, filename=name, content_disposition_type="attachment")

    @r.get("/devices/{device_id}/health")
    async def device_health(device_id: str) -> dict[str, Any]:
        """Storage health: I/O errors in 30 days, write speeds of the last syncs, the warning."""
        return await facade.device_health(device_id)

    @r.post("/devices/{device_id}/integrity", status_code=202)
    async def integrity_check(device_id: str, deep: bool = False) -> dict[str, Any]:
        """Check the device against its manifest, every file read back (`deep`: decoded too), as a job."""
        return await facade.integrity_check(device_id, deep)

    @r.get("/devices/{device_id}/integrity")
    async def integrity_last(device_id: str) -> dict[str, Any] | None:
        """The newest integrity check: {job_id, finished_at, status, error, report} or null."""
        return await facade.last_integrity(device_id)

    @r.post("/devices/{device_id}/repair", status_code=202)
    async def repair_device(device_id: str) -> dict[str, Any]:
        """Check and repair the data partition's file system (fsck); clears 'file system damaged'."""
        return await facade.repair_device(device_id)

    @r.post("/devices/{device_id}/integrity/fix", status_code=202)
    async def integrity_fix(device_id: str) -> dict[str, Any]:
        """Delete the files the last check found broken; the next sync writes them again."""
        return await facade.integrity_fix(device_id)

    @r.post("/devices/{device_id}/rename")
    async def rename_device(device_id: str, req: RenameRequest) -> dict[str, Any]:
        """Rename on the device itself (iPod name / file system label); not a sync."""
        return await facade.rename_device(device_id, req.name, req.preview)

    @r.get("/devices/{device_id}/play-log")
    async def play_log(device_id: str) -> dict[str, Any]:
        """Rockbox play log: `{setting, state}`, state on|off|unset from config.cfg (null: not connected)."""
        return await facade.play_log(device_id)

    @r.post("/devices/{device_id}/play-log")
    async def set_play_log(device_id: str, req: PlayLogRequest) -> dict[str, Any]:
        """Switch it on/off: the setting, and a connected player's config.cfg at once (not a sync)."""
        return await facade.set_play_log(device_id, req.on)

    @r.post("/devices/{device_id}/plan")
    async def plan(device_id: str, req: PlanRequest | None = None) -> dict[str, Any]:
        return await facade.plan(device_id, force=(req or PlanRequest()).force)

    @r.get("/planning")
    async def planning() -> list[dict[str, Any]]:
        """The plans running now: device, step, fraction (null: unknown), items, started_at."""
        return facade.planning()

    @r.delete("/devices/{device_id}/plan")
    async def cancel_plan(device_id: str) -> dict[str, Any]:
        """Cancel the device's running plan; the request waiting for it answers 409 `cancelled`."""
        return {"cancelled": facade.cancel_plan(device_id)}

    @r.post("/devices/{device_id}/fit/estimate")
    async def fit_estimate(device_id: str, req: FitRequest) -> dict[str, Any]:
        """Transcode to fit: the space the plan needs with this target; nothing is stored."""
        return await facade.fit_estimate(device_id, req.to, req.bitrate_kbps)

    @r.post("/devices/{device_id}/fit")
    async def fit_apply(device_id: str, req: FitRequest) -> dict[str, Any]:
        """Accept transcode to fit: set the device's transcode rule and answer the new plan."""
        return await facade.fit_apply(device_id, req.to, req.bitrate_kbps)

    @r.get("/plans/{plan_id}")
    async def get_plan(plan_id: str) -> dict[str, Any]:
        return await facade.get_plan(plan_id)

    @r.get("/plans/{plan_id}/items", response_model=None)
    async def plan_items(
        plan_id: str,
        format: Literal["json", "csv", "all"] = "json",
        group: str | None = None,
        q: str = "",
        offset: int = 0,
        limit: int = 200,
        field: str = "",
        direction: Literal["", "to_device", "to_library"] = "",
        library: str = "",
        kind: str = "",
        rule: str = "",
    ) -> Any:
        """The plan's full list: `json` one page (?group&q&offset&limit), `csv` / `all` (JSON)
         every item as a download; the plan itself carries only a sample. Group `meta`: the
         items with metadata changes, `field` / `direction` filter them; `library` / `kind` / `rule` narrow the list
        ."""
        data = facade.plan_items(
            plan_id, format, group, q, offset, limit, field, direction, library, kind, rule
        )
        name = f"plan-{facade.get_plan_device(plan_id)}"
        if format == "csv":
            return Response(
                data,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
            )
        if format == "all":
            return JSONResponse(
                data, headers={"Content-Disposition": f'attachment; filename="{name}.json"'}
            )
        return data

    @r.post("/devices/sync-all", status_code=202)
    async def sync_all(req: SyncAllRequest | None = None) -> dict[str, Any]:
        """'Sync all connected': {devices, skipped: [{device_id, name, reason}]}; plans that need
        a confirmation wait for it; `sync_all.finished` {mode, done, waiting, failed} when every one has ended."""
        return await facade.sync_all((req or SyncAllRequest()).mode)

    @r.post("/devices/{device_id}/update", status_code=202)
    async def update_sync(device_id: str) -> dict[str, Any]:
        """An update sync: metadata, ratings, positions, playlists - no new files."""
        return await facade.update_sync(device_id)

    @r.post("/plans/{plan_id}/apply", status_code=202)
    async def apply(plan_id: str, req: ApplyRequest | None = None) -> dict[str, Any]:
        req = req or ApplyRequest()
        return await facade.apply_plan(
            plan_id,
            confirm=req.confirm,
            first_sync=req.first_sync,
            confirm_db_limit=req.confirm_db_limit,
            playlist_choices=req.playlist_choices,
        )

    @r.get("/search")
    async def search(q: str = "", limit: int = 5) -> dict[str, Any]:
        """The global search: artists, albums, tracks, devices, templates, jobs."""
        return await facade.search(q, limit)

    @r.get("/jobs")
    async def jobs() -> list[dict[str, Any]]:
        return facade.jobs()

    @r.get("/jobs/{job_id}")
    async def job(job_id: str) -> dict[str, Any]:
        return facade.job(job_id)

    @r.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str) -> dict[str, bool]:
        return {"cancelled": facade.cancel_job(job_id)}

    @r.get("/sync-reports")
    async def reports(device: str | None = None) -> list[dict[str, Any]]:
        return await facade.sync_reports(device)

    @r.get("/activity")
    async def activity(
        device: str | None = None, kind: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """What SyncMusic did, newest first: sent listening data, playlists, files."""
        return await facade.activity(device, kind, max(1, min(limit, 1000)))

    return r
