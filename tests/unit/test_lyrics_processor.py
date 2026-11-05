"""lyrics processor: .lrc sidecars and embedded lyrics per format."""

from __future__ import annotations

import mutagen
import pytest
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.core.models import LibraryConfig, Lyrics
from syncmusic.libraries.folder import FolderLibrary
from syncmusic.processors.lyrics import LyricsProcessor, lrc_text
from tests.media import make_music, make_track, media_work_item

CAPS = DeviceCapabilities(formats={"mp3", "aac", "flac"})
SYNCED = Lyrics(synced=True, lines=[(0.5, "Перша"), (61.25, "Друга"), (59.999, "Майже")])
PLAIN = Lyrics(synced=False, text="Рядок один\nРядок два")


def item(tmp_path, name="a.mp3", lyrics=SYNCED):
    it = media_work_item(make_music(tmp_path / "src" / name, title="t"), tmp_path / "work")
    t = it.track.model_copy(update={"has_lyrics": True})
    return it.model_copy(update={"plan": it.plan.model_copy(update={"track": t}), "lyrics": lyrics})


async def test_synced_to_lrc_sidecar(tmp_path):
    it = item(tmp_path)
    p = LyricsProcessor({}, None)
    planned = p.plan(it.plan, CAPS)
    assert planned.sidecars == [".lrc"] and planned.sidecar_bytes > 0
    out = await p.run(it, CAPS)
    assert out.path == it.path  # the media file is untouched
    lrc = out.sidecars[".lrc"]
    assert lrc.parent == it.work_dir and lrc.name == "a.lrc"
    assert lrc.read_text(encoding="utf-8") == (
        "[00:00.50]Перша\n[01:01.25]Друга\n[01:00.00]Майже\n"
    )
    assert out.steps_done == ["lyrics"]


async def test_plain_to_unsynced_lrc(tmp_path):
    out = await LyricsProcessor({}, None).run(item(tmp_path, lyrics=PLAIN), CAPS)
    assert out.sidecars[".lrc"].read_text(encoding="utf-8") == "Рядок один\nРядок два\n"


async def test_plain_embedded_as_uslt(tmp_path):
    it = item(tmp_path, lyrics=PLAIN)
    out = await LyricsProcessor({"plain": "embed"}, None).run(it, CAPS)
    assert out.path != it.path and ".lrc" not in out.sidecars
    uslt = ID3(out.path).getall("USLT")
    assert [str(u) for u in uslt] == ["Рядок один\nРядок два"]
    assert not ID3(out.path).getall("SYLT")
    assert out.plan.file.size == out.path.stat().st_size


async def test_synced_embedded_mp3_sylt(tmp_path):
    out = await LyricsProcessor({"synced": "embed"}, None).run(item(tmp_path), CAPS)
    id3 = ID3(out.path)
    assert id3.getall("SYLT")[0].text == [("Перша", 500), ("Друга", 61250), ("Майже", 59999)]
    assert str(id3.getall("USLT")[0]) == "Перша\nДруга\nМайже"


@pytest.mark.parametrize("name", ["a.m4a", "a.flac", "a.opus"])
async def test_embedded_other_formats(tmp_path, name):
    out = await LyricsProcessor({"synced": "embed"}, None).run(item(tmp_path, name), CAPS)
    if name.endswith(".m4a"):
        assert MP4(out.path).tags["©lyr"] == ["Перша\nДруга\nМайже"]
    else:
        assert mutagen.File(out.path)["LYRICS"] == [SYNCED.to_lrc().rstrip("\n")]


async def test_no_lyrics_and_skip(tmp_path):
    it = item(tmp_path, lyrics=None)
    assert await LyricsProcessor({}, None).run(it, CAPS) == it
    it = item(tmp_path, lyrics=PLAIN)
    assert await LyricsProcessor({"plain": "skip"}, None).run(it, CAPS) == it
    no_flag = media_work_item(make_music(tmp_path / "b.mp3"), tmp_path / "work")
    assert LyricsProcessor({}, None).plan(no_flag.plan, CAPS) == no_flag.plan


def test_lrc_text_keeps_library_lrc_verbatim():
    raw = "[ar:Скрябін]\r\n[offset:+100]\r\n[00:01.00] Раз\r\n[00:02.00]Два"
    assert lrc_text(Lyrics.from_text(raw)) == raw.replace("\r\n", "\n") + "\n"
    # lines changed after parsing (e.g. by the library): rendered from lines
    edited = Lyrics.from_text(raw).model_copy(update={"lines": [(3.0, "Три")]})
    assert lrc_text(edited) == "[00:03.00]Три\n"


def test_suggest():
    assert LyricsProcessor.suggest(CAPS) is None
    lrc = CAPS.model_copy(update={"lyrics": "lrc_sidecar"})
    assert LyricsProcessor.suggest(lrc).settings == {"synced": "lrc", "plain": "lrc"}
    emb = CAPS.model_copy(update={"lyrics": "embedded"})
    assert LyricsProcessor.suggest(emb).settings == {"synced": "embed", "plain": "embed"}
    assert LyricsProcessor.needs == {"lyrics"}


# .lrc files as other programs write them: plain UTF-8, with a BOM, with CRLF line ends
LRC_FILES = {
    "Океан Ельзи/Обійми.lrc": (
        "[ar:Океан Ельзи]\n[ti:Обійми]\n[00:01.00]Обійми\n[00:02.50]мене\n",
        "utf-8",
    ),
    "Скрябін/Старі фотографії.lrc": ("[00:00.50]Старі\n[00:01.20]фотографії\n", "utf-8-sig"),
    "Бумбокс/Вахтерам.lrc": ("[00:03.00]Вахтерам\r\n[00:04.00]\r\n", "utf-8"),
}


async def test_lrc_round_trip(tmp_path):
    """A .lrc sidecar goes through Folder → processor unchanged (BOM and CRLF normalized)."""
    root = tmp_path / "lib"
    for rel, (text, encoding) in LRC_FILES.items():
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(text.encode(encoding))
        make_track(dst.with_suffix(".mp3"), title=dst.stem)
    lib = FolderLibrary(
        LibraryConfig(id="f", type="folder", name="F", settings={"root": str(root)}), None
    )
    snap = await lib.snapshot()
    for t in snap.tracks.values():
        assert t.has_lyrics
        lyrics = await lib.get_lyrics(t.ref.item_id)
        it = media_work_item(root / t.path, tmp_path / "work" / t.ref.item_id)
        out = await LyricsProcessor({}, None).run(it.model_copy(update={"lyrics": lyrics}), CAPS)
        original = (root / t.path).with_suffix(".lrc").read_text(encoding="utf-8-sig")
        written = out.sidecars[".lrc"].read_text(encoding="utf-8")
        assert written == original.replace("\r\n", "\n").rstrip("\n") + "\n"


ELRC = Lyrics.from_text(
    "[ar:Скрябін]\n[00:01.00]<00:01.00>Група <00:01.50>крові\n[00:04.20]<00:04.20>на <00:04.60>рукаві\n"
)


async def test_both_writes_lrc_and_embeds(tmp_path):
    """target 'both' = sidecar and USLT+SYLT; one step entry."""
    it = item(tmp_path)
    p = LyricsProcessor({"synced": "both"}, None)
    out = await p.run(it, CAPS)
    assert out.path != it.path and out.sidecars[".lrc"].read_text(encoding="utf-8").startswith(
        "[00:00.50]Перша"
    )
    id3 = ID3(out.path)
    assert [str(u) for u in id3.getall("USLT")] == ["Перша\nДруга\nМайже"]
    assert id3.getall("SYLT")
    assert out.steps_done == ["lyrics"]
    planned = p.plan(it.plan, CAPS)
    assert planned.sidecars == [".lrc"]


async def test_enhanced_lrc_is_line_timed_unless_word_timing(tmp_path):
    out = await LyricsProcessor({"synced": "both"}, None).run(item(tmp_path, lyrics=ELRC), CAPS)
    lrc = out.sidecars[".lrc"].read_text(encoding="utf-8")
    assert "<" not in lrc and "[ar:Скрябін]" in lrc and "[00:01.00]Група крові" in lrc
    assert [str(u) for u in ID3(out.path).getall("USLT")] == ["Група крові\nна рукаві"]
    out = await LyricsProcessor({"word_timing": True}, None).run(
        item(tmp_path / "w", lyrics=ELRC), CAPS
    )
    assert "<00:01.50>крові" in out.sidecars[".lrc"].read_text(encoding="utf-8")


async def test_flac_both_embeds_line_timed_lrc(tmp_path):
    it = item(tmp_path, "a.flac", lyrics=ELRC)
    out = await LyricsProcessor({"synced": "both"}, None).run(it, CAPS)
    tag = mutagen.File(out.path)["LYRICS"][0]
    assert "[00:01.00]Група крові" in tag and "<" not in tag
