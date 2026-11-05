# SPDX-License-Identifier: GPL-3.0-or-later
"""The diagnostics bundle: one zip with what a bug report needs - versions, config, settings,
libraries, devices, checks, recent reports and their job logs, the log tail. Never media files or databases.

The parts come from the API views, which already mask secrets; as a second line of defence every known secret value
is replaced in every file before it is written."""

from __future__ import annotations

import io
import zipfile

MASK = "********"


def scrub(text: str, secrets: list[str]) -> str:
    """`text` with every secret value replaced by the mask (longest first, so a value inside another goes too)."""
    for s in sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True):
        text = text.replace(s, MASK)
    return text


def make_zip(files: dict[str, str], secrets: list[str]) -> bytes:
    """A zip of `files` (name → text), each scrubbed of `secrets`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, scrub(text, secrets))
    return buf.getvalue()
