# ruff: noqa: F811 - fixtures imported from test_api_sync
"""the suggested processor template - copy when everything plays and fits, a converting template
when it does not fit, a generated chain for formats nobody plays, video counted but never converted."""

from __future__ import annotations

from syncmusic.core import registry
from syncmusic.core.models import ItemRef, MediaKind, Track
from syncmusic.core.proc_templates import builtin_processor_templates
from syncmusic.core.suggest import suggest
from tests.unit.test_api_sync import _as_template, env, music  # noqa: F401 - fixtures

ROCKBOX = registry.device_class("rockbox")
IPOD = registry.device_class("ipod")


def _tracks(n: int, fmt: str = "flac", size: int = 30_000_000, kind=MediaKind.MUSIC) -> list[Track]:
    return [
        Track(
            ref=ItemRef(library_id="l", item_id=f"{fmt}{i}"),
            title=f"t{i}",
            format=fmt,
            size_bytes=size,
            duration_s=240,
            sample_rate=44100,
            bit_depth=16,
            bitrate_kbps=1000 if fmt == "flac" else 256,
            kind=kind,
        )
        for i in range(n)
    ]


def _offered(device_type: str):
    return [t for t in builtin_processor_templates().values() if t.device_type == device_type]


def test_copy_is_suggested_when_everything_plays_and_fits():
    s = suggest(
        _offered("rockbox"),
        _tracks(10),
        ROCKBOX.capabilities,
        10**10,
        default_id="builtin-rockbox-copy",
        device_type="rockbox",
    )
    assert s.template_id == "builtin-rockbox-copy" and s.draft is None
    assert s.reason["key"] == "copy" and s.reason["files"] == 10 and s.reason["fits"]


def test_mp3_320_when_the_originals_do_not_fit():
    s = suggest(
        _offered("rockbox"),
        _tracks(10),
        ROCKBOX.capabilities,
        150_000_000,  # 300 MB as they are, ~100 MB as MP3 320
        default_id="builtin-rockbox-copy",
    )
    assert s.template_id == "builtin-rockbox" and s.reason["key"] == "convert"
    assert s.reason["converted"] == 10 and s.reason["as_is"] > 150_000_000


def test_unknown_space_never_rules_out_a_template():
    s = suggest(
        _offered("rockbox"),
        _tracks(3),
        ROCKBOX.capabilities,
        None,
        default_id="builtin-rockbox-copy",
    )
    assert s.template_id == "builtin-rockbox-copy" and s.reason["free"] is None


def _copy_only():
    return [builtin_processor_templates()["builtin-rockbox-copy"]]


def test_copy_converts_what_does_not_play_so_it_still_wins():
    """Every chain converts unplayable formats, so "Rockbox: copy" covers the Opus files of
    a player without the Opus codec - no generated chain, nothing left off."""
    caps = ROCKBOX.capabilities.model_copy(update={"formats": {"mp3", "flac"}})
    tracks = [*_tracks(3), *_tracks(2, "opus", 5_000_000)]
    for offered in (_offered("rockbox"), _copy_only()):
        s = suggest(offered, tracks, caps, 10**10, default_id="builtin-rockbox-copy")
        assert s.template_id == "builtin-rockbox-copy" and s.draft is None


def test_generated_chain_adds_convert_to_fit_when_needed():
    caps = ROCKBOX.capabilities.model_copy(update={"formats": {"mp3", "flac"}})
    s = suggest(
        _copy_only(),
        [*_tracks(10), *_tracks(1, "opus", 5_000_000)],
        caps,
        150_000_000,
        default_id="builtin-rockbox-copy",
    )
    rules = next(p for p in s.draft.processors if p.type == "transcode").settings["rules"]
    assert rules[0]["above_target_only"] and rules[0]["to"] == "mp3"
    assert s.reason["fits"]


def test_stock_ipod_gets_alac_and_rockbox_only_music():
    s = suggest(_offered("ipod"), _tracks(2), IPOD.capabilities, 10**10, default_id=None)
    assert s.template_id == "builtin-ipod"  # FLAC does not play: the ALAC/AAC template
    books = _tracks(5, "mp3", kind=MediaKind.AUDIOBOOK)
    s = suggest(
        _offered("rockbox"),
        [*_tracks(2), *books],
        ROCKBOX.capabilities,
        10**10,
        default_id="builtin-rockbox-copy",
    )
    assert s.reason["files"] == 2  # Rockbox takes music only


def test_video_is_counted_but_not_converted():
    caps = registry.device_class("flashdrive").capabilities.model_copy(
        update={"supports_video": True, "formats": {"mp3", "flac"}}
    )
    s = suggest(_offered("flashdrive"), _tracks(1, "mkv", kind=MediaKind.VIDEO), caps, 10**10)
    assert s.draft is not None and s.reason["formats"] == []


async def test_api_suggests_rockbox_copy_for_a_folder_of_mp3s(env, tmp_path):
    from tests.contracts.harness import rockbox_tree

    app, c = env
    r = await c.post(
        "/api/v1/devices/suggest-processor-template",
        json={"type": "rockbox", "mount_path": str(rockbox_tree(tmp_path / "rb")), "name": "Mini"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["template_id"] == "builtin-rockbox-copy" and body["draft"] is None
    assert (
        body["reason"]["key"] == "copy"
        and body["reason"]["files"] >= 2
        and body["reason"]["free"] > 0
    )
    assert {x["id"] for x in body["candidates"]} >= {"builtin-rockbox", "builtin-rockbox-copy"}
    # generated variants with sizes, the copy preselected (MP3s: nothing to convert)
    vs = body["variants"]
    assert vs[body["best_variant"]]["key"] == "copy" and all(v["fits"] for v in vs)
    assert all(v["processors"] and v["needed"] > 0 for v in vs)
    r = await c.post("/api/v1/devices/suggest-processor-template", json={"type": "nope"})
    assert r.status_code == 400


async def test_api_suggests_for_a_registered_device(env, tmp_path):
    """the Processors tab asks with `device_id`; type, libraries and target come from the device."""
    from tests.contracts.harness import rockbox_tree

    app, c = env
    root = rockbox_tree(tmp_path / "rb")
    body = {"name": "Mini", "mount_path": str(root), "type": "rockbox"}
    r = await c.post("/api/v1/devices/register", json=body)
    assert r.status_code == 201, r.text
    dev = r.json()["id"]
    r = await c.post("/api/v1/devices/suggest-processor-template", json={"device_id": dev})
    assert r.status_code == 200, r.text
    assert (
        r.json()["reason"]["files"] == 0
    )  # no libraries yet: nothing to judge (not every library)
    rs = await _as_template(c, "Mini", {"rules": [{"id": "all"}]}, ["music"])
    r = await c.patch(f"/api/v1/devices/{dev}", json={"rule_set": rs})
    assert r.status_code == 200, r.text
    r = await c.post("/api/v1/devices/suggest-processor-template", json={"device_id": dev})
    body = r.json()
    assert body["template_id"] == "builtin-rockbox-copy" and body["reason"]["files"] >= 2
    assert body["reason"]["free"] > 0
    r = await c.post("/api/v1/devices/suggest-processor-template", json={"device_id": "gone"})
    assert r.status_code == 404


def _names(vs):
    return [(v.key, v.to, v.quality) for v in vs]


def test_variants_offer_only_targets_the_device_plays():
    """Several generated chains, each with its size; only codecs the device plays."""
    from syncmusic.core.suggest import variants

    copy = builtin_processor_templates()["builtin-rockbox-copy"].processors
    caps = ROCKBOX.capabilities.model_copy(update={"formats": {"mp3", "flac", "alac"}})
    vs = variants(copy, _tracks(10), caps, 10**10)
    assert _names(vs) == [
        ("copy", None, {}),
        ("lossless", "alac", {}),
        ("fit", "mp3", {"bitrate_kbps": 320}),
        ("lossy", "mp3", {"bitrate_kbps": 320}),
        ("lossy", "mp3", {"vbr": 0}),
    ]  # "lossless as FLAC" is the copy itself (all sources FLAC): dropped
    sizes = {(v.key, v.to): v.score.needed for v in vs}
    assert sizes[("copy", None)] == sizes[("fit", "mp3")] < 301_000_000  # fits: nothing converted
    assert sizes[("lossy", "mp3")] < 0.5 * sizes[("copy", None)]
    ipod = variants(
        builtin_processor_templates()["builtin-ipod"].processors,
        _tracks(4),
        IPOD.capabilities,
        None,
    )
    assert {v.to for v in ipod} <= {None, "alac", "aac", "mp3"} and ("lossy", "aac") in {
        (v.key, v.to) for v in ipod
    }


def test_best_variant_fits_with_least_loss():
    copy = builtin_processor_templates()["builtin-rockbox-copy"].processors
    caps = ROCKBOX.capabilities
    from syncmusic.core.suggest import best_variant, variants

    roomy = variants(copy, _tracks(10), caps, 10**10)
    assert roomy[best_variant(roomy, 10**10)].key == "copy"
    tight = variants(copy, _tracks(10), caps, 150_000_000)
    best = tight[best_variant(tight, 150_000_000)]
    # converts the lossless files only because they do not fit, sized as the converted chain
    assert best.key == "fit" and best.score.fits(150_000_000) and best.score.converted == 10
    tiny = variants(copy, _tracks(10), caps, 1_000)
    assert tiny[best_variant(tiny, 1_000)].score.needed == min(v.score.needed for v in tiny)
    s = suggest(
        _offered("rockbox"), _tracks(10), caps, 150_000_000, default_id="builtin-rockbox-copy"
    )
    assert s.variants and s.variants[s.best_variant].key == "fit"


async def test_api_suggests_from_a_plans_items(env, tmp_path):
    """With `plan_id` the suggestion is judged on what the rules select for the device (the
    plan's items), not on its whole libraries; the space is what the plan's items may use."""
    from tests.contracts.harness import rockbox_tree

    app, c = env
    body = {"name": "Mini", "mount_path": str(rockbox_tree(tmp_path / "rb")), "type": "rockbox"}
    dev = (await c.post("/api/v1/devices/register", json=body)).json()["id"]
    rule = {"id": "skryabin", "criteria": {"all": [{"is": {"artist": "Скрябін"}}]}}
    rs = await _as_template(c, "Mini", {"rules": [rule]}, ["music"])
    assert (await c.patch(f"/api/v1/devices/{dev}", json={"rule_set": rs})).status_code == 200
    whole = (
        await c.post("/api/v1/devices/suggest-processor-template", json={"device_id": dev})
    ).json()
    plan = (await c.post(f"/api/v1/devices/{dev}/plan", json={})).json()
    adds = [a for a in plan["actions"] if a["kind"] == "add"]
    r = await c.post("/api/v1/devices/suggest-processor-template", json={"plan_id": plan["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"]["files"] == len(adds) == 2 < whole["reason"]["files"]
    assert body["template_id"] == "builtin-rockbox-copy" and body["reason"]["fits"]
    assert body["free"] > 0 and body["variants"]
    r = await c.post("/api/v1/devices/suggest-processor-template", json={"plan_id": "gone"})
    assert r.status_code == 404
