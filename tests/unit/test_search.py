"""Global search: artists, albums and tracks from the cached library snapshots, devices,
templates and jobs; never waits for a library that has no cache yet."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from tests.unit.test_api_sync import env, music  # noqa: F401 - fixtures


async def _search(c, q, **params):
    r = await c.get("/api/v1/search", params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_search_groups(env, music):
    app, c = env
    await c.get("/api/v1/libraries/music/items")  # loads the snapshot cache
    res = await _search(c, "СКРЯБІН")
    assert [a["name"] for a in res["artists"]] == ["Скрябін"]
    assert [(a["name"], a["artist"], a["library_id"]) for a in res["albums"]] == []
    assert sorted(t["title"] for t in res["tracks"]) == ["Група крові", "Зачини за мною двері"]
    # the overlay picks the hit icon by kind
    assert {t["kind"] for t in res["tracks"]} == {"music"}
    assert res["not_loaded"] == []
    res = await _search(c, "група")
    assert [(a["name"], a["artist"]) for a in res["albums"]] == [("Група крові", "Скрябін")]
    assert len(res["tracks"]) == 2  # a track matches by its album too, like the browser
    assert (await _search(c, "крові скрябін", limit=1))["tracks"][0]["album"] == "Група крові"


async def test_search_devices_templates_jobs(env, music):
    app, c = env
    tpl = (await c.get("/api/v1/templates")).json()[0]
    word = tpl["name"].split()[0]
    assert tpl["id"] in [t["id"] for t in (await _search(c, word))["templates"]]
    r = await c.post("/api/v1/libraries/music/refresh")
    job = r.json()
    await app.jobs.wait(job["job_id"] if "job_id" in job else job["id"])
    name = app.jobs.jobs()[0].name
    assert [j["name"] for j in (await _search(c, name))["jobs"]][:1] == [name]


async def test_empty_query_and_uncached_library(env, music):
    app, c = env
    res = await _search(c, "  ")
    assert all(v == [] for v in res.values())
    res = await _search(c, "скрябін")
    assert res["artists"] == [] and [x["id"] for x in res["not_loaded"]] == ["music"]
