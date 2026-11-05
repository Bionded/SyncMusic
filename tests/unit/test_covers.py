"""covers processor: embed / sidecar / remove / device modes, read back with mutagen + Pillow."""

from __future__ import annotations

import io

import mutagen
import pytest
from PIL import Image

from syncmusic.base.capabilities import CoverSpec, DeviceCapabilities
from syncmusic.processors.covers import TAG_BYTES, Covers, embedded_picture, prepare_image
from tests.media import TINY_JPEG, big_cover, embed_cover, make_music, media_work_item

ROCKBOX = DeviceCapabilities(
    formats={"mp3", "aac", "flac"}, cover=CoverSpec(max_size=500, progressive=False)
)
IPOD = DeviceCapabilities(formats={"mp3", "aac", "alac"}, needs_device_db=True)


def item(tmp_path, name, cover: bytes | None = None):
    path = make_music(tmp_path / "src" / name, title="t")
    if cover is not None:
        embed_cover(path, cover)
    return media_work_item(path, tmp_path / "work", has_cover=cover is not None)


def img(data: bytes) -> Image.Image:
    i = Image.open(io.BytesIO(data))
    i.load()
    return i


@pytest.mark.parametrize("name", ["a.mp3", "a.m4a", "a.flac", "a.opus"])
async def test_embed_resizes_to_baseline_jpeg(tmp_path, name):
    it = item(tmp_path, name, big_cover(1200, 900, progressive=True))
    original = it.path.read_bytes()
    out = await Covers({"mode": "embed"}, None).run(it, ROCKBOX)
    assert it.path.read_bytes() == original
    assert out.path.parent == it.work_dir and out.steps_done == ["covers"]
    pic = img(embedded_picture(mutagen.File(out.path)))
    assert pic.format == "JPEG" and pic.size == (500, 375)
    assert not pic.info.get("progressive") and not pic.info.get("progression")
    assert out.cover is not None and img(out.cover.read_bytes()).size == (500, 375)
    assert out.plan.file.size == out.path.stat().st_size
    assert out.path.stat().st_size < it.path.stat().st_size  # 1200 px picture replaced


async def test_embed_from_library_cover_and_no_upscale(tmp_path):
    it = item(tmp_path, "a.flac")  # nothing embedded
    lib_cover = it.work_dir / "library-cover.jpg"
    lib_cover.write_bytes(big_cover(300, 300))
    it = it.model_copy(update={"cover": lib_cover})
    out = await Covers({}, None).run(it, ROCKBOX)
    assert img(embedded_picture(mutagen.File(out.path))).size == (300, 300)


async def test_png_and_progressive_settings(tmp_path):
    it = item(tmp_path, "a.mp3", big_cover(800, 800))
    out = await Covers({"format": "png", "max_size": 100}, None).run(it, ROCKBOX)
    pic = img(embedded_picture(mutagen.File(out.path)))
    assert (pic.format, pic.size) == ("PNG", (100, 100))
    out = await Covers({"progressive": True}, None).run(it, ROCKBOX)
    assert img(embedded_picture(mutagen.File(out.path))).info.get("progressive")


async def test_sidecar_mode(tmp_path):
    it = item(tmp_path, "a.mp3", big_cover())
    p = Covers({"mode": "sidecar"}, None)
    planned = p.plan(it.plan, ROCKBOX)
    assert planned.sidecars == ["cover.jpg"] and planned.sidecar_bytes > 0
    out = await p.run(it, ROCKBOX)
    assert out.path == it.path  # embedded picture kept, file untouched
    assert img(out.sidecars["cover.jpg"].read_bytes()).size == (500, 500)
    out = await Covers({"mode": "sidecar", "strip_embedded": True}, None).run(it, ROCKBOX)
    assert embedded_picture(mutagen.File(out.path)) is None
    assert "cover.jpg" in out.sidecars


@pytest.mark.parametrize("name", ["a.mp3", "a.m4a", "a.flac", "a.opus"])
async def test_remove_mode(tmp_path, name):
    it = item(tmp_path, name, big_cover())
    p = Covers({"mode": "remove"}, None)
    planned = p.plan(it.plan, ROCKBOX)
    out = await p.run(it, ROCKBOX)
    assert embedded_picture(mutagen.File(out.path)) is None
    if name.endswith((".mp3", ".opus")):  # lossy: size without the picture is estimated
        size = out.path.stat().st_size
        assert (
            abs(planned.file.size - size) <= 0.1 * size + TAG_BYTES
        )  # 3-s file: tag slack dominates
    assert mutagen.File(out.path, easy=True)["title"] == ["t"]  # other tags kept


async def test_device_mode_prepares_cover_only(tmp_path):
    it = item(tmp_path, "a.m4a", big_cover())
    out = await Covers({"mode": "device"}, None).run(it, IPOD)
    assert out.path == it.path
    assert img(out.cover.read_bytes()).size == (500, 500)


async def test_no_cover_passes_through(tmp_path):
    it = item(tmp_path, "a.mp3")
    p = Covers({}, None)
    assert p.plan(it.plan, ROCKBOX) == it.plan
    assert await p.run(it, ROCKBOX) == it


async def test_broken_cover_is_skipped(tmp_path):
    it = item(tmp_path, "a.mp3")
    bad = it.work_dir / "bad.jpg"
    bad.write_bytes(b"not an image")
    it = it.model_copy(update={"cover": bad})
    assert await Covers({}, None).run(it, ROCKBOX) == it


def test_prepare_image_modes():
    assert img(prepare_image(TINY_JPEG, CoverSpec())).size == (1, 1)
    rgba = io.BytesIO()
    Image.new("RGBA", (50, 50), (255, 0, 0, 128)).save(rgba, "PNG")
    assert img(prepare_image(rgba.getvalue(), CoverSpec())).mode == "RGB"
    with pytest.raises(ValueError):
        prepare_image(b"nope", CoverSpec())


def test_suggest():
    assert Covers.suggest(IPOD).settings == {"mode": "device"}
    assert Covers.suggest(ROCKBOX).settings == {"mode": "embed"}
    side = ROCKBOX.model_copy(update={"cover": CoverSpec(embed=False, sidecar_name="folder.jpg")})
    assert Covers.suggest(side).settings == {"mode": "sidecar", "sidecar_name": "folder.jpg"}
    assert Covers.suggest(DeviceCapabilities()) is None


async def test_remove_mode_clears_work_item_cover(tmp_path):
    """remove = no artwork at all: the library cover fetched by the pipeline must not reach the device."""
    it = item(tmp_path, "a.mp3", big_cover())
    lib_cover = it.work_dir / "library-cover.jpg"
    lib_cover.write_bytes(big_cover(300, 300))
    it = it.model_copy(update={"cover": lib_cover})
    out = await Covers({"mode": "remove"}, None).run(it, IPOD)
    assert embedded_picture(mutagen.File(out.path)) is None
    assert out.cover is None


def test_remove_mode_does_not_fetch_library_cover():
    from syncmusic.core.prepare import Chain

    assert Chain([Covers({"mode": "remove"}, None)]).needs == frozenset()
    assert Chain([Covers({"mode": "embed"}, None)]).needs == {"cover"}
