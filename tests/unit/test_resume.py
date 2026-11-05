"""An interrupted sync is announced by its plan and cleared by the next finished sync."""

# ruff: noqa: F811 - fixtures imported from test_position_roundtrip

from __future__ import annotations

from syncmusic.core import manifest as manifest_io
from syncmusic.core.models import Interruption
from tests.unit.test_api_sync import _apply
from tests.unit.test_position_roundtrip import env, server  # noqa: F401 - fixtures


async def test_the_plan_says_it_resumes_and_the_finished_sync_clears_it(env):
    app, c, mount = env
    m = manifest_io.read(mount)
    m.header.interrupted = Interruption(plan_id="old", done=2, total=5)
    manifest_io.write(mount, m)

    plan = (await c.post("/api/v1/devices/ipod/plan", json={})).json()
    assert (plan["resuming"]["done"], plan["resuming"]["total"]) == (2, 5)
    await _apply(app, c, plan["id"])
    assert manifest_io.read(mount).header.interrupted is None
    assert (await c.post("/api/v1/devices/ipod/plan", json={})).json()["resuming"] is None
