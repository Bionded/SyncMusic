"""a Rockbox player takes music only - podcasts and audiobooks are off whatever the capability
overrides say, and a template that draws only from podcast/audiobook libraries cannot be linked to it."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core.models import DeviceConfig, LibraryConfig, MediaKind, RuleTemplate
from syncmusic.devices.Rockbox.rockbox import Rockbox
from tests.unit.test_api_sync import env, music  # noqa: F401


def _rockbox_tree(path):
    (path / ".rockbox").mkdir(parents=True)
    return path


def test_overrides_cannot_turn_podcasts_or_audiobooks_on(tmp_path):
    cfg = DeviceConfig(
        id="rb",
        type="rockbox",
        name="RB",
        capability_overrides={"supports_podcasts": True, "supports_audiobooks": True},
    )
    caps = Rockbox(cfg, _rockbox_tree(tmp_path / "rb"), None).effective_capabilities()
    assert not caps.supports_kind(MediaKind.PODCAST) and not caps.supports_kind(MediaKind.AUDIOBOOK)
    assert caps.supports_kind(MediaKind.MUSIC)


async def _register(c, path, template_ids, **kw):
    body = {
        "name": "Mini",
        "mount_path": str(_rockbox_tree(path)),
        "type": "rockbox",
        "rule_set": {"template_ids": template_ids},
        **kw,
    }
    return await c.post("/api/v1/devices/register", json=body)


async def test_a_template_of_an_audiobook_library_is_refused(env, tmp_path):
    app, c = env
    await app.store.put_library(
        LibraryConfig(id="abs", type="audiobookshelf", name="Books", settings={"url": "http://x"})
    )
    await app.store.put_template(RuleTemplate(id="books", name="My books", library_ids=["abs"]))
    await app.store.put_template(
        RuleTemplate(id="mixed", name="Mixed", library_ids=["abs", "music"])
    )
    r = await _register(c, tmp_path / "rb", ["books"])
    assert r.status_code == 400
    assert "'My books' draws only podcasts and audiobooks (Books)" in r.text
    assert "takes music only" in r.text
    # a template that also draws music is fine: its podcasts and audiobooks are left out by the plan
    r = await _register(c, tmp_path / "rb2", ["mixed"], name="Mini 2")
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    r = await c.patch(
        f"/api/v1/devices/{rid}", json={"rule_set": {"template_ids": ["mixed", "books"]}}
    )
    assert r.status_code == 400 and "takes music only" in r.text


async def test_capability_overrides_for_podcasts_are_refused(env, tmp_path):
    app, c = env
    r = await _register(c, tmp_path / "rb", [])
    assert r.status_code == 201, r.text
    r = await c.patch(
        f"/api/v1/devices/{r.json()['id']}",
        json={"capability_overrides": {"supports_podcasts": True}},
    )
    assert r.status_code == 400 and "takes music only" in r.text


async def test_device_types_say_which_type_is_music_only(env):
    app, c = env
    types = {t["type"]: t for t in (await c.get("/api/v1/device-types")).json()}
    assert types["rockbox"]["music_only"] is True
    assert types["flashdrive"]["music_only"] is False and types["ipod"]["music_only"] is False


async def test_a_new_rockbox_links_rockbox_copy_unless_told_otherwise(env, tmp_path):
    """files go as they are by default; "Rockbox: MP3 320" stays for small players."""
    app, c = env
    tpls = {t["id"]: t for t in (await c.get("/api/v1/processor-templates")).json()}
    copy, mp3 = tpls["builtin-rockbox-copy"], tpls["builtin-rockbox"]
    assert copy["name"] == "Rockbox: copy" and copy["builtin"] and copy["device_type"] == "rockbox"
    # no transcoding: only the fallback Convert step for formats the player has no codec for
    assert copy["processors"][0] == {
        "type": "transcode",
        "enabled": True,
        "settings": {"max_sample_rate": None, "max_bit_depth": None},
    }
    assert mp3["name"] == "Rockbox: MP3 320" and mp3["processors"][0]["type"] == "transcode"
    types = {t["type"]: t for t in (await c.get("/api/v1/device-types")).json()}
    assert types["rockbox"]["default_processor_template_id"] == "builtin-rockbox-copy"
    assert types["flashdrive"]["default_processor_template_id"] is None
    r = await _register(c, tmp_path / "rb", [])
    assert r.status_code == 201, r.text
    cfg = r.json()
    assert cfg["processor_template_id"] == "builtin-rockbox-copy"
    assert [p["type"] for p in cfg["processors"]] == [p["type"] for p in copy["processors"]]
    r = await _register(
        c, tmp_path / "rb2", [], name="Mini 2", processor_template_id="builtin-rockbox"
    )
    assert r.json()["processor_template_id"] == "builtin-rockbox"
    r = await _register(c, tmp_path / "rb3", [], name="Mini 3", processors=[])
    assert r.json()["processor_template_id"] is None  # an explicit chain stays unlinked


async def test_convert_to_fit_on_a_rockbox_copy_device(env, tmp_path):
    """ "Rockbox: copy" plus the plan's Convert-to-fit offer (MP3 320 first) is how lossless
    files get converted when the library does not fit; accepting it leaves the built-in template."""
    app, c = env
    r = await _register(c, tmp_path / "rb", [])
    did = r.json()["id"]
    cfg = (await app.sync.fit_apply(did, "mp3", 320)).model_dump(mode="json")
    assert cfg["processors"][0]["type"] == "transcode"
    rule = cfg["processors"][0]["settings"]["rules"][0]
    assert (rule["to"], rule["quality"]["bitrate_kbps"], rule["above_target_only"]) == (
        "mp3",
        320,
        True,
    )
    assert (
        cfg["processor_template_id"] is None
    )  # the device's own chain now, the built-in stays as it is


async def test_rockbox_copy_converts_a_format_without_its_codec(env, music, tmp_path):
    """ "Rockbox: copy" on a player without the Opus codec converts the Opus file to a format
    it plays instead of leaving it off; the MP3s go as they are."""
    from tests.media import make_music
    from tests.unit.test_api_sync import ALL, _apply, _as_template

    app, c = env
    make_music(music / "Other" / "03 Opus.opus", title="Opus song", artist="Other")
    await c.post("/api/v1/libraries/music/scan")
    rb = _rockbox_tree(tmp_path / "rb")
    (rb / ".rockbox" / "codecs").mkdir()
    for name in ("mpa", "flac"):
        (rb / ".rockbox" / "codecs" / f"{name}.codec").write_bytes(b"")
    tpl = await _as_template(c, "Mini", ALL, ["music"])
    r = await c.post(
        "/api/v1/devices/register",
        json={"name": "Mini", "mount_path": str(rb), "type": "rockbox", "rule_set": tpl},
    )
    assert r.status_code == 201, r.text
    dev = r.json()
    assert dev["processor_template_id"] == "builtin-rockbox-copy"
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan")).json()
    assert not any("not playable" in w for w in plan["warnings"]), plan["warnings"]
    await _apply(app, c, plan["id"], first_sync="keep")
    files = sorted(p.suffix for p in rb.rglob("*") if p.is_file() and ".rockbox" not in p.parts)
    assert ".opus" not in files and files.count(".mp3") >= 3, files
