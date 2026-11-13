"""The client-protocol conformance checks pass against an in-process server, skip what needs items when the client
selects nothing, and the CLI exits non-zero on a failure."""

from __future__ import annotations

from syncmusic import client_conformance
from syncmusic.client_conformance import run
from syncmusic.reference_client import ReferenceClient
from tests.unit.test_clients import new_code
from tests.unit.test_reference_client import server  # noqa: F401 - fixture


async def test_all_checks_pass(server, tmp_path):  # noqa: F811
    _, paired, _ = server
    rc = await paired(tmp_path / "phone")  # paired + rules; the suite reuses that pairing
    report = await run(rc, None)
    assert report.ok, report.results
    assert set(report.by_name().values()) == {"pass"}


async def test_without_rules_item_checks_skip(server, tmp_path):  # noqa: F811
    app, paired, _ = server
    probe = await paired(tmp_path / "probe")
    rc = ReferenceClient(tmp_path / "fresh", http=probe.http)
    rc.state["server"] = "http://t/"
    report = await run(rc, (await new_code(probe.http))["code"])
    statuses = report.by_name()
    assert report.ok, report.results
    assert statuses["pair"] == "pass" and statuses["file_download"] == "skip"


async def test_wrong_saved_token_fails(server, tmp_path):  # noqa: F811
    _, paired, _ = server
    rc = await paired(tmp_path / "phone")
    rc.state["token"] = "stale"
    report = await run(rc, None)
    assert not report.ok and report.by_name()["me"] == "fail"


def test_cli_reports_failure(tmp_path, capsys):
    # nothing listens there: hello fails, the exit code says so
    assert client_conformance.main(["http://127.0.0.1:9", "-", str(tmp_path), "--json"]) == 1
