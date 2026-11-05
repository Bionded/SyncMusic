"""Release files: GPL-3.0-or-later headers, the third-party list, docs links."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_source_file_has_the_spdx_header():
    missing = [
        str(p.relative_to(ROOT))
        for p in [*ROOT.glob("syncmusic/**/*.py"), *ROOT.glob("scripts/*.py")]
        if "SPDX-License-Identifier: GPL-3.0-or-later"
        not in "".join(p.read_text().splitlines(True)[:2])
    ]
    assert not missing


def test_third_party_lists_every_locked_runtime_dependency():
    req = subprocess.run(
        ["uv", "export", "--no-hashes", "--no-dev", "--no-emit-project", "--frozen"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    listed = (ROOT / "THIRD-PARTY.md").read_text()
    for line in req.splitlines():
        if "==" in line and "sys_platform == 'win32'" not in line:
            name, version = line.split(";")[0].strip().split("==")
            assert f"| {name} | {version} |" in listed, (
                f"{name} {version}: run scripts/gen_third_party.py"
            )
    assert (
        (ROOT / "LICENSE").read_text().startswith("                    GNU GENERAL PUBLIC LICENSE")
    )


def test_user_docs_links_resolve():
    """every relative link in docs/ points to an existing file."""
    import re

    for page in (ROOT / "docs").glob("*.md"):
        for target in re.findall(r"\]\(([^)#:]+)(?:#[^)]*)?\)", page.read_text()):
            assert (page.parent / target).exists(), f"{page.name}: {target}"


def test_settings_reference_lists_every_setting():
    """docs/05-settings.md names every application, sync and reverse-sync setting."""
    from syncmusic.core.models import AppSettings, ReverseSyncSettings, SyncSettings

    text = (ROOT / "docs" / "05-settings.md").read_text()
    for model in (AppSettings, SyncSettings, ReverseSyncSettings):
        for name in model.model_fields:
            assert f"`{name}`" in text, f"{model.__name__}.{name} missing in docs/05-settings.md"


def test_user_docs_images_are_used():
    """every screenshot in docs/img/ is referenced by a chapter (and the link test checks the
    reverse)."""
    text = "".join(p.read_text() for p in (ROOT / "docs").glob("*.md"))
    unused = [p.name for p in (ROOT / "docs" / "img").glob("*.png") if f"img/{p.name}" not in text]
    assert not unused
