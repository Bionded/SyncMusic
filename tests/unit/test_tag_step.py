"""Every processor chain writes the library's tags into the file before it goes to
the device - built-in templates (iPod included), new and changed user templates, device chains, and the stored ones
from before (startup migration)."""

# ruff: noqa: F811 - pytest fixtures imported from test_api_sync
from __future__ import annotations

import mutagen

from syncmusic.core.models import DeviceConfig, ProcessorSlot, ProcessorTemplate
from syncmusic.core.proc_templates import builtin_processor_templates, with_tag_step
from tests.contracts.harness import ipod_tree
from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401


def _types(slots) -> list[str]:
    return [s.type if isinstance(s, ProcessorSlot) else s["type"] for s in slots]


def test_every_builtin_template_has_the_tag_step_after_the_audio_step():
    for tpl in builtin_processor_templates().values():
        types = _types(tpl.processors)
        assert "retag" in types, tpl.id
        audio = [i for i, x in enumerate(types) if x in ("transcode", "video")]  # audio stage
        if audio:
            assert types.index("retag") == max(audio) + 1, tpl.id


def test_with_tag_step_inserts_after_the_audio_step_only_when_missing():
    t = ProcessorSlot(type="transcode")
    c = ProcessorSlot(type="covers")
    assert _types(with_tag_step([])) == ["transcode", "retag"]  # + fallback Convert
    assert _types(with_tag_step([c])) == ["transcode", "retag", "covers"]
    assert _types(with_tag_step([t, c])) == ["transcode", "retag", "covers"]
    own = [
        t,
        ProcessorSlot(type="retag", enabled=False),
        c,
    ]  # present (also disabled): left as it is
    assert with_tag_step(own) is own


async def test_templates_and_devices_always_keep_the_tag_step(env, tmp_path):
    app, c = env
    r = await c.post(
        "/api/v1/processor-templates",
        json={"name": "Only covers", "processors": [{"type": "covers"}]},
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert _types(r.json()["processors"]) == ["transcode", "retag", "covers"]
    r = await c.patch(
        f"/api/v1/processor-templates/{tid}", json={"processors": [{"type": "lyrics"}]}
    )
    assert _types(r.json()["processors"]) == ["transcode", "retag", "lyrics"]
    dev = await _register(c, tmp_path / "stick", name="Stick", processors=[])
    assert _types(dev["processors"]) == ["transcode", "retag"]
    r = await c.patch(f"/api/v1/devices/{dev['id']}", json={"processors": [{"type": "lyrics"}]})
    assert _types(r.json()["processors"]) == ["transcode", "retag", "lyrics"]


async def test_startup_adds_the_tag_step_to_stored_chains(env, tmp_path):
    app, c = env
    await app.store.put_processor_template(
        ProcessorTemplate(
            id="old",
            name="Old",
            processors=[ProcessorSlot(type="transcode"), ProcessorSlot(type="covers")],
        )
    )
    dev = await _register(c, tmp_path / "stick", name="Stick")
    cfg = DeviceConfig.model_validate(
        (await c.get(f"/api/v1/devices/{dev['id']}")).json()["config"]
    )
    await app.store.put_device(
        cfg.model_copy(update={"processors": [ProcessorSlot(type="lyrics")]})
    )
    await app.sync.add_tag_steps()
    assert _types((await app.store.processor_template("old")).processors) == [
        "transcode",
        "retag",
        "covers",
    ]
    assert _types((await app.store.device(dev["id"])).processors) == [
        "transcode",
        "retag",
        "lyrics",
    ]


async def test_ipod_files_carry_the_library_tags(env, tmp_path):
    app, c = env
    mount = ipod_tree(tmp_path / "ipod")
    cfg = await _register(c, mount, name="Virtual iPod", type="ipod")
    assert "retag" in _types(cfg["processors"])
    plan = (await c.post(f"/api/v1/devices/{cfg['id']}/plan")).json()
    await _apply(app, c, plan["id"], confirm=True, first_sync="keep")
    files = [p for p in (mount / "iPod_Control" / "Music").rglob("*") if p.is_file()]
    assert len(files) == 3
    titles = sorted(str(mutagen.File(f, easy=True)["title"][0]) for f in files)
    assert titles == ["Група крові", "Зачини за мною двері", "Орбіт без цукру"]
