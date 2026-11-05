# SPDX-License-Identifier: GPL-3.0-or-later
"""The test files a change can affect, for `scripts/test.py --changed`.

A changed test file runs itself; a changed Python module runs every test file that imports it, directly or through
other modules of the repository (the import graph of syncmusic/ and tests/, read with `ast`); a changed
`conftest.py` runs every test below its folder; a changed file of the web UI (syncmusic/webui/) runs the UI tests.
Anything else that tests depend on without importing it (pyproject.toml, uv.lock, compose.yaml, packaging, the
test media generator's inputs) runs everything: `affected()` returns None then.
"""

from __future__ import annotations

import ast
import subprocess
from collections import defaultdict
from pathlib import Path

# changes that cannot affect a test: nothing runs for them
IGNORED_SUFFIXES = {".md", ".png", ".jpg", ".svg", ".ico", ".txt"}
IGNORED_PREFIXES = ("docs/", "assets/", ".github/ISSUE_TEMPLATE/")
UI_PREFIX = "syncmusic/webui/"


def changed_files(root: Path) -> list[str]:
    """Paths (relative to `root`) changed against the last commit: modified, staged, deleted and untracked."""
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root, capture_output=True
    )
    if head.returncode != 0:
        raise RuntimeError(
            "no commit to compare against yet: --changed works after the first commit"
        )
    out = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(set(out) | set(untracked))


def _module(rel: str) -> str:
    """syncmusic/core/sync.py -> syncmusic.core.sync; a package's __init__.py -> the package."""
    parts = rel.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path, module: str) -> set[str]:
    """The absolute module names `path` imports (relative imports resolved; `from a import b` gives a and a.b)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                up = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*up, base] if base else up)
            names.add(base)
            names.update(f"{base}.{a.name}" for a in node.names)
    return names


def _graph(root: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    """module -> file, and module -> the repository modules that import it."""
    files: dict[str, str] = {}
    for top in ("syncmusic", "tests"):
        for p in (root / top).rglob("*.py"):
            rel = p.relative_to(root).as_posix()
            files[_module(rel)] = rel
    importers: dict[str, set[str]] = defaultdict(set)
    for module, rel in files.items():
        for name in _imports(root / rel, module):
            # `import a.b.c` also needs a and a.b; keep the repository modules only
            parts = name.split(".")
            for i in range(1, len(parts) + 1):
                dep = ".".join(parts[:i])
                if dep in files and dep != module:
                    importers[dep].add(module)
    return files, importers


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith("tests/") and name.startswith("test_") and name.endswith(".py")


def affected(root: Path, changed: list[str]) -> list[str] | None:
    """Test files (sorted) the `changed` paths can affect; None = everything (a change no rule covers)."""
    files, importers = _graph(root)
    by_file = {rel: module for module, rel in files.items()}
    tests: set[str] = set()
    start: list[str] = []
    for rel in changed:
        if rel.startswith(IGNORED_PREFIXES) or Path(rel).suffix in IGNORED_SUFFIXES:
            continue
        if rel.endswith("conftest.py") and rel.startswith("tests/"):
            folder = rel.rsplit("/", 1)[0]
            tests.update(f for f in by_file if _is_test_file(f) and f.startswith(folder + "/"))
        elif rel.endswith(".py") and rel in by_file:
            start.append(by_file[rel])
        elif (
            rel.endswith(".py")
            and not (root / rel).exists()
            and rel.startswith(("syncmusic/", "tests/"))
        ):
            continue  # a deleted module: its importers changed too (or fail at import, which the suite shows)
        elif rel.startswith(UI_PREFIX):
            tests.update(f for f in by_file if _is_test_file(f) and f.startswith("tests/webui/"))
        else:
            return None
    seen = set(start)
    queue = list(start)
    while queue:
        module = queue.pop()
        rel = files[module]
        if _is_test_file(rel):
            tests.add(rel)
        for importer in importers.get(module, ()):
            if importer not in seen:
                seen.add(importer)
                queue.append(importer)
    return sorted(tests)
