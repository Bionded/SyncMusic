"""'Sync everything' (built-in All) also puts every library playlist on the device - a folder library's
.m3u files arrive as .m3u8 playlists on a flash drive."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from tests.unit.test_api_sync import _apply, _register, env, music  # noqa: F401 - fixtures


async def test_all_template_syncs_every_folder_playlist(env, music, tmp_path):
    app, c = env
    (music / "Скрябін.m3u").write_text(
        "Скрябін/01 Група крові.mp3\nСкрябін/02 Зачини за мною двері.mp3\n"
    )
    (music / "Тартак" / "best.m3u8").write_text(
        "#PLAYLIST:Best\nОрбіт.mp3\n../Скрябін/01 Група крові.mp3\n"
    )
    await c.post("/api/v1/libraries/music/refresh")
    dev = await _register(
        c, tmp_path / "stick", name="Stick", rule_set={"template_ids": ["default.music.all"]}
    )
    plan = (await c.post(f"/api/v1/devices/{dev['id']}/plan", json={})).json()
    assert plan["counts"]["add"] == 3
    await _apply(app, c, plan["id"], first_sync="keep")
    lists = sorted(p.name for p in (tmp_path / "stick").rglob("*.m3u8"))
    assert lists == ["Best.m3u8", "Скрябін.m3u8"]
    best = next((tmp_path / "stick").rglob("Best.m3u8")).read_text(encoding="utf-8")
    assert "Орбіт" in best and "Група крові" in best
