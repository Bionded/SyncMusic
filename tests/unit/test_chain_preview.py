"""Processor preview on one track: the picker offers one track per format, Run the chain gives a
before / after table, Download result serves the file, only the newest previews are kept."""

# ruff: noqa: F811 - fixtures imported from test_api_sync

from __future__ import annotations

from syncmusic.core import chain_preview
from tests.media import make_music
from tests.unit.test_api_sync import _register, env, music  # noqa: F401


async def _flac_library(app, c, music):
    make_music(
        music / "Воплі Відоплясова" / "Весна.flac",
        title="Весна",
        artist="Воплі Відоплясова",
        album="Трикутник",
    )
    r = await c.post("/api/v1/libraries/music/refresh")
    await app.jobs.wait(r.json()["id"])


async def test_preview_converts_one_track_and_serves_it(env, music, tmp_path):
    app, c = env
    await _flac_library(app, c, music)
    dev = await _register(c, tmp_path / "flash", name="Flash")
    await c.patch(f"/api/v1/devices/{dev['id']}", json={"quality_profile": "weak"})

    tracks = (await c.get(f"/api/v1/devices/{dev['id']}/chain-preview/tracks")).json()
    labels = [t["label"] for t in tracks]
    assert labels[:2] == ["Весна - Воплі Відоплясова (FLAC)", "Група крові - Скрябін (MP3)"], labels
    flac = tracks[0]
    # any item can be found, not only the samples
    found = (
        await c.get(f"/api/v1/devices/{dev['id']}/chain-preview/tracks", params={"q": "СКРЯБІН"})
    ).json()
    assert found and all("Скрябін" in t["label"] for t in found)
    assert (
        await c.get(f"/api/v1/devices/{dev['id']}/chain-preview/tracks", params={"q": "zzz"})
    ).json() == []

    r = await c.post(
        f"/api/v1/devices/{dev['id']}/chain-preview",
        json={"library_id": flac["library_id"], "item_id": flac["item_id"]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    rows = {x["key"]: x for x in out["rows"]}
    assert rows["Format"]["before"].startswith("FLAC") and rows["Format"]["after"].startswith("MP3")
    assert rows["Format"]["changed"] and rows["Bitrate"]["after"] == "192 kbit/s"
    assert rows["Title"] == {"key": "Title", "before": "Весна", "after": "Весна", "changed": False}
    assert "transcode" in out["steps"] and out["file"] == "Весна.mp3"
    dl = await c.get(out["download"])
    assert (
        dl.status_code == 200
        and dl.content[:3] in (b"ID3", b"\xff\xfb")
        and len(dl.content) == out["size"]
    )
    assert "attachment" in dl.headers["content-disposition"]

    # the chain on screen (unsaved) wins over the saved one: no steps = the file as it is
    r = await c.post(
        f"/api/v1/devices/{dev['id']}/chain-preview",
        json={**{k: flac[k] for k in ("library_id", "item_id")}, "processors": []},
    )
    rows = {x["key"]: x for x in r.json()["rows"]}
    assert rows["Format"]["after"].startswith("FLAC") and not rows["Format"]["changed"]

    for _ in range(chain_preview.KEEP + 1):
        await c.post(
            f"/api/v1/devices/{dev['id']}/chain-preview",
            json={k: flac[k] for k in ("library_id", "item_id")},
        )
    kept = list((app.config.cache_dir / chain_preview.FOLDER).iterdir())
    assert len(kept) == chain_preview.KEEP
    assert (await c.get(out["download"])).status_code == 404  # the first one is pruned


async def test_preview_refuses_unknown_items_and_paths(env, music, tmp_path):
    app, c = env
    dev = await _register(c, tmp_path / "flash", name="Flash")
    r = await c.post(
        f"/api/v1/devices/{dev['id']}/chain-preview",
        json={"library_id": "music", "item_id": "nope"},
    )
    assert r.status_code == 404
    assert chain_preview.result_file(app.config.cache_dir, "..", "etc") is None
    assert (await c.get("/api/v1/chain-preview/x/y.mp3")).status_code == 404


def test_rows_keep_lyrics_and_cover_sidecars_apart():
    """a Rockbox cover.jpg next to the file belongs to the Cover row, not to Lyrics."""
    from syncmusic.core.models import ItemRef, Track
    from syncmusic.infra.ffmpeg import ProbeResult

    track = Track(ref=ItemRef(library_id="l", item_id="1"), title="Ау", has_cover=True)
    after = ProbeResult(
        container="mp3",
        codec="mp3",
        duration_s=1,
        sample_rate=44100,
        channels=2,
        bit_depth=None,
        bitrate=192000,
        size_bytes=1,
        has_cover=True,
        tags={"title": "Ау"},
    )
    rows = {r["key"]: r for r in chain_preview.rows(track, after, ["cover.jpg"])}
    assert rows["Lyrics"]["after"] == "none" and not rows["Lyrics"]["changed"]
    assert rows["Cover"]["after"] == "in the file, cover.jpg file" and rows["Cover"]["changed"]
    rows = {r["key"]: r for r in chain_preview.rows(track, after, [".lrc", "cover.jpg"])}
    assert rows["Lyrics"]["after"] == ".lrc file"
