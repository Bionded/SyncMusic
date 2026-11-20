# SPDX-License-Identifier: GPL-3.0-or-later
"""The mirror of a remote folder library when the connection drops: what was fetched stays indexed, and a failed
whole-file download leaves no partial file behind."""

from __future__ import annotations

import contextlib
import json

import pytest

from syncmusic.libraries.remote import INDEX, MirrorSource, RemoteError


class Flaky(MirrorSource):
    def __init__(self, mirror, files, fail_on=(), error=OSError):
        self.mirror, self.files, self.fail_on, self.error = mirror, files, set(fail_on), error

    def describe(self):
        return "test server"

    def session(self):
        return contextlib.nullcontext(None)

    def fetch(self, h, rel, write):
        data = self.files[rel]
        write(data[: len(data) // 2])
        if rel in self.fail_on:
            raise self.error("connection dropped")
        write(data[len(data) // 2 :])


def test_index_keeps_the_files_fetched_before_a_dropped_connection(tmp_path):
    files = {f"{n}.txt": f"text {n}".encode() for n in range(4)}
    src = Flaky(tmp_path / "m", files, fail_on={"2.txt"})
    listing = {rel: (len(data), 10**9) for rel, data in files.items()}
    with pytest.raises(OSError):
        src.sync_mirror(listing, lambda rel: True)
    index = json.loads((tmp_path / "m" / INDEX).read_text())
    assert sorted(index) == ["0.txt", "1.txt"]


def test_failed_full_download_leaves_no_part_file(tmp_path):
    src = Flaky(tmp_path / "m", {"a.flac": b"x" * 100}, fail_on={"a.flac"}, error=RemoteError)
    with pytest.raises(RemoteError):
        src.full("a.flac")
    assert not [p for p in (tmp_path / "m").rglob("*") if p.is_file()]
