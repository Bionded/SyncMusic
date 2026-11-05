# SPDX-License-Identifier: GPL-3.0-or-later
"""`lyrics` processor: put the library's lyrics where the device reads them.

Input: `WorkItem.lyrics` (the prepare pipeline fetches `Library.get_lyrics` because this processor `needs`
"lyrics"). Per kind of lyrics (synced / plain) the settings choose:
- `lrc`   - a `.lrc` sidecar next to the track (`WorkItem.sidecars[".lrc"]`); synced lyrics keep their LRC text
            (incl. `[ar:]`/`[offset:]` tags) when the library supplied it, plain lyrics are written as is;
- `embed` - into the file: MP3 USLT (+ SYLT for synced), MP4 `©lyr` (plain lines), FLAC/Ogg `LYRICS`
            (LRC text for synced, as Navidrome/foobar read it);
- `both`  - the `.lrc` sidecar and embedded (players that read one or the other);
- `skip`  - nothing.
Synced lyrics are line-timed LRC `[mm:ss.xx]` (the widest support). Enhanced LRC word stamps (`<mm:ss.xx>`) are kept
in the `.lrc` only with `word_timing` (no supported device is known to show them); embedded lyrics never have them.
No lyrics → unchanged.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, ClassVar, Literal

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, SYLT, USLT
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from pydantic import BaseModel, ConfigDict

from syncmusic.base.capabilities import DeviceCapabilities
from syncmusic.base.processor import Processor
from syncmusic.core.models import Lyrics, PlanItem, ProcessorSlot, WorkItem
from syncmusic.processors.common import work_copy

Target = Literal["lrc", "embed", "both", "skip"]
FORMATS = frozenset({"mp3", "aac", "alac", "flac", "ogg", "opus"})
LYRICS_BYTES = 2048  # predicted size of one track's lyrics, for plan()


class LyricsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    synced: Target = "lrc"
    plain: Target = "lrc"
    word_timing: bool = False  # keep Enhanced LRC word stamps in the .lrc


_WORD_STAMP = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>\s?")


def line_timed(lyrics: Lyrics) -> Lyrics:
    """Enhanced LRC downgraded to line timing: word stamps dropped from the text and the lines."""
    if not lyrics.synced or not _WORD_STAMP.search(
        lyrics.text + "".join(x for _, x in lyrics.lines)
    ):
        return lyrics
    return lyrics.model_copy(
        update={
            "text": _WORD_STAMP.sub("", lyrics.text),
            "lines": [(t, _WORD_STAMP.sub("", x).strip()) for t, x in lyrics.lines],
        }
    )


def lrc_text(lyrics: Lyrics) -> str:
    """The .lrc file content: the library's own LRC text when it has one, else rendered lines/plain text."""
    if (
        lyrics.synced
        and lyrics.text.strip()
        and Lyrics.from_text(lyrics.text).lines == lyrics.lines
    ):
        text = lyrics.text.replace("\r\n", "\n")
        return text if text.endswith("\n") else text + "\n"
    return lyrics.to_lrc()


def plain_text(lyrics: Lyrics) -> str:
    if lyrics.synced:
        return "\n".join(line for _, line in lyrics.lines)
    return lyrics.text.strip()


def _lang(lyrics: Lyrics) -> str:
    lang = (lyrics.language or "").lower()
    return lang if len(lang) == 3 and lang.isalpha() else "und"


def embed_lyrics(path: Path, lyrics: Lyrics) -> bool:
    """Write lyrics into the file's tags. False when the format is not handled."""
    audio: Any = mutagen.File(path)
    if audio is None:
        return False
    if type(audio).__name__ == "MP3":
        try:
            id3 = ID3(path)
        except mutagen.id3.ID3NoHeaderError:
            id3 = ID3()
        version = 3 if id3.version[:2] == (2, 3) else 4
        lang = _lang(lyrics)
        id3.setall("USLT", [USLT(encoding=3, lang=lang, desc="", text=plain_text(lyrics))])
        if lyrics.synced:
            stamps = [(line, round(t * 1000)) for t, line in lyrics.lines]
            id3.setall("SYLT", [SYLT(encoding=3, lang=lang, format=2, type=1, text=stamps)])
        else:
            id3.delall("SYLT")
        id3.save(path, v2_version=version)
        return True
    if isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        audio.tags["©lyr"] = [plain_text(lyrics)]
        audio.save()
        return True
    if isinstance(audio, FLAC | OggVorbis | OggOpus):
        if audio.tags is None:
            audio.add_tags()
        tags: Any = audio.tags
        tags["LYRICS"] = [lrc_text(lyrics).rstrip("\n") if lyrics.synced else plain_text(lyrics)]
        audio.save()
        return True
    return False


class LyricsProcessor(Processor):
    type_name = "lyrics"
    display_name = "Lyrics"
    Settings: ClassVar[type[BaseModel]] = LyricsSettings
    stage: ClassVar[Literal["audio", "tags", "cover", "sidecar"]] = "sidecar"
    version: ClassVar[int] = 1
    needs: ClassVar[frozenset[Literal["cover", "lyrics"]]] = frozenset({"lyrics"})
    hash_omit_defaults: ClassVar[frozenset[str]] = frozenset({"word_timing"})

    @property
    def s(self) -> LyricsSettings:
        settings: LyricsSettings = self.settings  # type: ignore[assignment]
        return settings

    @classmethod
    def suggest(cls, caps: DeviceCapabilities) -> ProcessorSlot | None:
        if caps.lyrics == "lrc_sidecar":
            return ProcessorSlot(type=cls.type_name, settings={"synced": "lrc", "plain": "lrc"})
        if caps.lyrics == "embedded":
            return ProcessorSlot(type=cls.type_name, settings={"synced": "embed", "plain": "embed"})
        return None

    def plan(self, item: PlanItem, caps: DeviceCapabilities) -> PlanItem:
        # synced or plain is only known after fetching; plan for the more common target
        if not item.track.has_lyrics:
            return item
        targets = {self.s.synced, self.s.plain}
        if targets & {"lrc", "both"}:
            return item.model_copy(
                update={
                    "sidecars": sorted({*item.sidecars, ".lrc"}),
                    "sidecar_bytes": item.sidecar_bytes + LYRICS_BYTES,
                    "steps": [*item.steps, self.type_name],
                }
            )
        if targets & {"embed", "both"} and item.file.format in FORMATS:
            file = item.file.model_copy(update={"size": item.file.size + LYRICS_BYTES})
            return item.model_copy(update={"file": file, "steps": [*item.steps, self.type_name]})
        return item

    async def run(self, item: WorkItem, caps: DeviceCapabilities) -> WorkItem:
        lyrics = item.lyrics
        if lyrics is None or not (lyrics.lines or lyrics.text.strip()):
            return item
        target = self.s.synced if lyrics.synced else self.s.plain
        if target == "skip":
            return item
        return await asyncio.to_thread(self._apply, item, lyrics, target)

    def _apply(self, item: WorkItem, lyrics: Lyrics, target: Target) -> WorkItem:
        lines = line_timed(lyrics)
        if target in ("lrc", "both"):
            item.work_dir.mkdir(parents=True, exist_ok=True)
            lrc = item.work_dir / f"{item.path.stem}.lrc"
            lrc.write_text(lrc_text(lyrics if self.s.word_timing else lines), encoding="utf-8")
            item = item.model_copy(
                update={
                    "sidecars": {**item.sidecars, ".lrc": lrc},
                    "steps_done": [*item.steps_done, self.type_name],
                }
            )
            if target == "lrc":
                return item
        return self._embed(item, lines)

    def _embed(self, item: WorkItem, lyrics: Lyrics) -> WorkItem:
        try:
            handled = mutagen.File(item.path) is not None
        except mutagen.MutagenError:
            handled = False
        if not handled:
            return item
        path = work_copy(item, self.type_name)
        if not embed_lyrics(path, lyrics):
            if path != item.path:
                path.unlink()
            return item
        file = item.plan.file.model_copy(update={"size": path.stat().st_size})
        done = (
            item.steps_done
            if self.type_name in item.steps_done
            else [*item.steps_done, self.type_name]
        )
        return item.model_copy(
            update={
                "path": path,
                "plan": item.plan.model_copy(update={"file": file}),
                "steps_done": done,
            }
        )
