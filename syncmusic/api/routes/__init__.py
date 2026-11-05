# SPDX-License-Identifier: GPL-3.0-or-later
"""REST routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse, Response

THUMB_MAX_AGE_S = 7 * 24 * 3600


def thumb_response(path: Path | None) -> FileResponse | Response:
    """A cached thumbnail, or 204 when there is no cover: the image fails quietly (a 404 would be a console
    error for every row without a cover) and the UI keeps its placeholder. Both may be cached for a while."""
    if path is None:
        return Response(status_code=204, headers={"Cache-Control": "max-age=3600"})
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": f"max-age={THUMB_MAX_AGE_S}"}
    )
