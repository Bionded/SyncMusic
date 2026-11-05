"""Read-only folder picker."""

from pathlib import Path

import pytest

from syncmusic.libraries import browse


def test_local_listing_counts_the_first_two_levels(tmp_path: Path) -> None:
    (tmp_path / "A" / "Album1").mkdir(parents=True)
    (tmp_path / "A" / "Album1" / "deep.mp3").write_bytes(b"x")  # third level: not counted
    (tmp_path / "A" / "one.flac").write_bytes(b"x")
    (tmp_path / "B").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "top.mp3").write_bytes(b"x")
    (tmp_path / "cover.jpg").write_bytes(b"x")

    lst = browse.browse_local(str(tmp_path))
    assert lst.folders == ["A", "B"]
    assert (lst.files, lst.media_files) == (2, 1)
    assert (lst.preview_folders, lst.preview_media) == (3, 2)
    assert lst.parent == str(tmp_path.parent)
    assert not lst.partial and not lst.shares


def test_preview_budget_marks_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(5):
        (tmp_path / f"d{i}").mkdir()
    monkeypatch.setattr(browse, "PREVIEW_MAX_DIRS", 2)
    assert browse.browse_local(str(tmp_path)).partial


def test_norm_rejects_parent_and_strips_slashes() -> None:
    assert browse.norm("//a\\b/./") == "/a/b"
    assert browse.norm("") == "/"
    with pytest.raises(ValueError):
        browse.norm("/a/../etc")


def test_root_has_no_parent() -> None:
    assert browse.browse_local("/").parent is None
