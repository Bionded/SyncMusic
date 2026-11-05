"""Big plans on the plan page: the summary carries totals and a diverse sample of at most 100
items, the full list is paged / downloaded from GET /plans/{id}/items, apply still uses the whole plan."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

import csv
import time

from syncmusic.core import plan_view
from syncmusic.core.models import (
    ActionKind,
    ItemRef,
    LeftOut,
    LimitReport,
    PlanAction,
    PlanItem,
    PlannedFile,
    SyncPlan,
    Track,
)
from tests.unit.test_api_sync import _register, env, music  # noqa: F401 - fixtures


def _action(
    i: int, kind: ActionKind = ActionKind.ADD, artist: str = "A", album: str = "X"
) -> PlanAction:
    ref = ItemRef(library_id="lib", item_id=f"t{i}")
    tr = Track(ref=ref, title=f"Track {i}", artist=artist, album=album)
    item = PlanItem(track=tr, file=PlannedFile(format="mp3", ext=".mp3", size=1000))
    return PlanAction(kind=kind, ref=ref, title=tr.title, item=item, size_delta=1000, reason="rule")


def _big_plan() -> SyncPlan:
    """12 000 adds over 40 artists × 3 albums (in album order), 600 removes, 50 left out by the limit."""
    adds = [
        _action(i, artist=f"Artist {i // 300}", album=f"Album {i // 100}") for i in range(12_000)
    ]
    removes = [_action(20_000 + i, ActionKind.REMOVE) for i in range(600)]
    left = [LeftOut(key=f"lib/x{i}", title=f"Left {i}", size=5) for i in range(50)]
    return SyncPlan(
        id="p",
        device_id="d",
        actions=adds + removes,
        limit=LimitReport(left_out=left, left_out_bytes=250),
    )


def test_sample_is_small_diverse_and_counts_everything():
    plan = _big_plan()
    s = plan_view.sample(plan)
    assert s["sampled"] is True
    assert s["totals"] == {
        "add": 12_000,
        "replace": 0,
        "update_meta": 0,
        "remove": 600,
        "keep": 0,
        "left": 50,
    }
    assert s["bytes"]["add"] == 12_000_000 and s["bytes"]["left"] == 250
    n = len(s["actions"]) + len(s["left_out"])
    assert n == plan_view.SAMPLE_MAX
    kinds = {a["kind"] for a in s["actions"]}
    assert kinds == {"add", "remove"} and s["left_out"]  # every group is in it
    adds = [a for a in s["actions"] if a["kind"] == "add"]
    albums = {a["item"]["track"]["album"] for a in adds}
    assert len(albums) == len(adds)  # one per album first, not the first album's tracks
    assert plan_view.sample(plan) == s  # stable


def test_small_plan_keeps_every_item():
    plan = SyncPlan(id="p", device_id="d", actions=[_action(i) for i in range(5)])
    s = plan_view.sample(plan)
    assert not s["sampled"] and len(s["actions"]) == 5


def test_items_page_filter_and_downloads():
    plan = _big_plan()
    page = plan_view.items_page(plan, "add", "", 150, 100)
    assert page["total"] == 12_000 and [a["title"] for a in page["actions"]][:1] == ["Track 150"]
    assert plan_view.items_page(plan, None, "", 12_590, 20)["left"][0]["title"] == "Left 0"
    hit = plan_view.items_page(plan, "add", "album 119", 0, 1000)
    assert hit["total"] == 100  # matched on the album
    rows = list(csv.DictReader(plan_view.items_csv(plan).splitlines()))
    assert len(rows) == 12_650 and rows[-1]["group"] == "left" and rows[-1]["item_id"] == "x49"
    assert len(plan_view.items_json(plan)) == 12_650


def test_items_page_library_kind_rule_filters():
    """the list's filters, and the facets that fill them."""
    from syncmusic.core.models import MediaKind

    plan = _big_plan()
    for i, a in enumerate(plan.actions[:10]):
        a.rules = ["fav"] if i % 2 else ["all"]
    pod = _action(30_000)
    pod.ref = ItemRef(library_id="abs", item_id="p1")
    assert pod.item is not None
    pod.item.track.kind = MediaKind.PODCAST
    plan.actions.append(pod)
    f = plan_view.facets(plan)
    assert f["library"] == {"lib": 12_600, "abs": 1} and f["kind"]["podcast"] == 1
    assert f["rule"] == {"all": 5, "fav": 5}
    assert plan_view.items_page(plan, "add", "", 0, 50, rule="fav")["total"] == 5
    assert plan_view.items_page(plan, "add", "", 0, 50, kind="podcast")["total"] == 1
    assert plan_view.items_page(plan, None, "", 0, 50, library="abs")["total"] == 1
    # left-out items: only the library filter applies (their key carries it)
    assert plan_view.items_page(plan, "left", "", 0, 50, library="lib")["total"] == 50
    assert plan_view.items_page(plan, "left", "", 0, 50, kind="music")["total"] == 0


def test_summary_of_a_12k_plan_is_fast():
    """Target < 1 s for the plan page once the plan is ready: the summary is built and sent quickly."""
    from syncmusic.core.sync import PlanContext, plan_summary

    pc = PlanContext(plan=_big_plan(), matches=[])
    t0 = time.perf_counter()
    data = plan_summary(pc)
    assert time.perf_counter() - t0 < 1.0
    assert len(data["actions"]) < 100 and data["counts"]["add"] == 12_000
    assert data["limit"]["left_out_count"] == 50 and len(data["limit"]["left_out"]) < 50


async def test_items_endpoint(env, tmp_path):
    app, c = env
    dev = await _register(c, tmp_path / "stick", name="Stick", processors=[])
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["totals"]["add"] == len(plan["actions"]) and plan["sampled"] is False
    url = f"/api/v1/plans/{plan['id']}/items"
    page = (await c.get(url, params={"group": "add", "limit": 2})).json()
    assert page["total"] == plan["totals"]["add"] and len(page["actions"]) <= 2
    r = await c.get(url, params={"format": "csv"})
    assert (
        r.headers["content-type"].startswith("text/csv")
        and "attachment" in r.headers["content-disposition"]
    )
    assert r.text.startswith("group,title,artist,album,reason")
    assert (await c.get(url, params={"group": "nope"})).status_code == 400
    assert (await c.get("/api/v1/plans/unknown/items")).status_code == 404


def test_metadata_view_page_and_download():
    """the summary's `meta` counts the changes per direction:field, the `meta` group pages the
    items with changes filtered by field/direction, the download has one row per change."""
    from syncmusic.core.models import MetaChange

    acts = [_action(i, ActionKind.KEEP) for i in range(5)]
    acts[0].changes = [MetaChange(field="title", old="A", new="B")]
    acts[1].changes = [
        MetaChange(field="position", old="10", new="20", direction="to_library", conflict="x")
    ]
    plan = SyncPlan(id="p", device_id="dev", actions=acts)
    view = plan_view.meta_view(plan)
    assert view["total"] == 2 and view["conflicts"] == 1
    assert view["changes"] == {"to_device:title": 1, "to_library:position": 1}
    page = plan_view.items_page(plan, "meta", "", 0, 50, direction="to_library")
    assert page["total"] == 1 and page["actions"][0]["changes"][0]["new"] == "20"
    assert plan_view.items_page(plan, "meta", "", 0, 50, field="title")["total"] == 1
    rows = list(csv.DictReader(plan_view.items_csv(plan).splitlines()))
    meta = [r for r in rows if r["group"] == "meta"]
    assert [(r["field"], r["old"], r["new"], r["direction"]) for r in meta] == [
        ("title", "A", "B", "to_device"),
        ("position", "10", "20", "to_library"),
    ]
    assert len(rows) == 7
