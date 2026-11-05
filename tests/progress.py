# SPDX-License-Identifier: GPL-3.0-or-later
"""Live progress of a pytest run as a JSON file, for scripts/test.py (`-p tests.progress`).

Active only when `SM_TEST_STATUS` names the file; runs in the controlling process (under xdist the workers' events
reach it as hooks). The file holds the counts, the tests running right now (since when, on which worker, in which
xdist group) and the latest failures; it is rewritten at most every half second and once more at the end.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

RECENT_FAILURES = 8


class Progress:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.time()
        self.total: int | None = None
        self.counts = {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "error": 0,
            "xfailed": 0,
            "xpassed": 0,
        }
        self.running: dict[str, dict[str, Any]] = {}
        self.failures: list[str] = []
        self.finished = False
        self._written = 0.0

    def write(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._written < 0.5:
            return
        self._written = now
        state = {
            "started": self.started,
            "now": now,
            "total": self.total,
            "counts": self.counts,
            "running": self.running,
            "failures": self.failures[-RECENT_FAILURES:],
            "finished": self.finished,
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.path)

    # --- hooks -------------------------------------------------------------------------------
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        if not session.config.pluginmanager.has_plugin("dsession"):  # without xdist
            self.total = len(session.items)
            self.write(force=True)

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: Any, ids: list[str]) -> None:
        self.total = len(ids)  # every worker collects the same items
        self.write(force=True)

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        test, _, group = nodeid.partition("@")
        self.running[nodeid] = {"test": test, "group": group, "since": time.time(), "worker": ""}
        self.write()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        entry = self.running.get(report.nodeid)
        node = getattr(report, "node", None)
        if entry is not None and node is not None:
            entry["worker"] = node.gateway.id
        test = report.nodeid.partition("@")[0]
        if report.when == "call" or (report.when == "setup" and not report.passed):
            if hasattr(report, "wasxfail"):
                self.counts["xfailed" if report.skipped else "xpassed"] += 1
            elif report.passed:
                self.counts["passed"] += 1
            elif report.skipped:
                self.counts["skipped"] += 1
            elif report.when == "setup":
                self.counts["error"] += 1
                self.failures.append(f"ERROR {test}")
            else:
                self.counts["failed"] += 1
                self.failures.append(f"FAILED {test}")
        elif report.when == "teardown" and report.failed:
            self.counts["error"] += 1
            self.failures.append(f"ERROR (teardown) {test}")
        self.write()

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        self.running.pop(nodeid, None)
        self.write()

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        self.finished = True
        self.running.clear()
        self.write(force=True)


def pytest_configure(config: pytest.Config) -> None:
    path = os.environ.get("SM_TEST_STATUS")
    if path and not hasattr(config, "workerinput"):
        config.pluginmanager.register(Progress(Path(path)), "sm-progress")
