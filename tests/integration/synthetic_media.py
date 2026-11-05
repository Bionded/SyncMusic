"""The test media: a generated set that every server of the compose stack, the integration and the hardware tests
scan (read-only). No real recordings: the audio is ffmpeg pink noise (tests/media.py); only the names are real -
Ukrainian artists, songs, books and a podcast.

Layout: Music (tags, covers, embedded and .lrc lyrics, Cyrillic and Latin names, a two-disc album, FLAC and MP3,
one album of long FLACs so a full music sync is over 48 MB), Audiobooks (a 3-part book whose epilogue is last only
by its track number, and a 27-chapter book whose file names sort differently from its track numbers) and Podcasts
(one show, 5 episodes). Written once into `root`; a marker file holds the version, and
a different version wipes `root` and writes the set again.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from tests.media import make_music

MARKER = ".syncmusic-synthetic"
VERSION = "9"

# the position tests pick book parts over 300 s and episodes over 120 s
BOOK_PART = 310.0
EPISODE = 130.0
CHAPTER = 20.0  # test_abs_feedback sets a position 12.5 s into a chapter
LONG_TRACK = 90.0  # the FLAC album: 5 x ~10 MB, so all music (~60 MB) is over the 48 MB store of test_emulated_flash

# (artist, album, year, genre, [(disc, track, title, ext)]); a track's lyrics are embedded when its number is 2
# or 3, and number 2 also has a .lrc sidecar (Navidrome and the folder library must see the same lyrics)
MUSIC = [
    (
        "Океан Ельзи",
        "Земля",
        2013,
        "Rock",
        [(1, 1, "Обійми", ".mp3"), (1, 2, "Мить", ".mp3"), (1, 3, "Земля", ".flac")],
    ),
    (
        "Kalush Orchestra",
        "Stefania",
        2022,
        "Hip-Hop",
        [(1, 1, "Stefania", ".mp3"), (1, 2, "Dodomu", ".mp3")],
    ),
    (
        "Бумбокс",
        "Family бізнес",
        2006,
        "Rock",
        [
            (1, 1, "Вахтерам", ".flac"),
            (1, 2, "Та4то", ".flac"),
            (2, 1, "Наодинці", ".flac"),
            (2, 2, "Квіти в волоссі", ".mp3"),
        ],
    ),
    (
        "Скрябін",
        "Танець пінгвіна",
        1997,
        "Pop",
        [
            (1, 1, "Танець пінгвіна", ".mp3"),
            (1, 2, "Старі фотографії", ".mp3"),
            (1, 3, "Спи собі сама", ".mp3"),
            (1, 4, "Мовчати", ".mp3"),
            (1, 5, "Люди як кораблі", ".mp3"),
        ],
    ),
    (
        "Тартак",
        "Демо-версія",
        2001,
        "Rock",
        [
            (1, 1, "Я не хочу", ".mp3"),
            (1, 2, "Наше літо", ".mp3"),
            (1, 3, "Понад хмарами", ".mp3"),
            (1, 4, "Демо-версія", ".mp3"),
            (1, 5, "Мало", ".mp3"),
        ],
    ),
    (
        "Антитіла",
        "Над полюсами",
        2016,
        "Pop",
        [
            (1, 1, "Над полюсами", ".mp3"),
            (1, 2, "TDME", ".mp3"),
            (1, 3, "Бери своє", ".mp3"),
            (1, 4, "Туди", ".mp3"),
            (1, 5, "Хочу з тобою", ".mp3"),
        ],
    ),
    (
        "Воплі Відоплясова",
        "Музіка",
        1997,
        "Rock",
        [
            (1, 1, "Весна", ".mp3"),
            (1, 2, "Танці", ".mp3"),
            (1, 3, "Були деньки", ".mp3"),
            (1, 4, "Галя", ".mp3"),
            (1, 5, "Любов", ".mp3"),
        ],
    ),
    (
        "Жадан і Собаки",
        "Пси",
        2016,
        "Rock",
        [
            (1, 1, "Вона", ".flac"),
            (1, 2, "Бетмен", ".flac"),
            (1, 3, "Мальчіки", ".flac"),
            (1, 4, "Ангели", ".flac"),
            (1, 5, "Пси", ".flac"),
        ],
    ),
]
LONG_ALBUMS = {"Пси"}

# (author, book, [(track number, file stem, seconds)])
BOOKS = [
    (
        "Стівен Кінг",
        "Воно",
        # by file name "Епілог" sorts first: only its track number puts it last
        [(1, "Розділ 1", BOOK_PART), (2, "Розділ 2", BOOK_PART), (3, "Епілог", BOOK_PART)],
    ),
    (
        "Террі Пратчетт, Ніл Гейман",
        "Добрі передвісники",
        # "10. ..." sorts before "2. ..." by name: the chapters must come in track-number order
        [(n, f"{n}. Розділ {n}", CHAPTER) for n in range(1, 28)],
    ),
]
PODCAST = "Історія без міфів"
EPISODES = ["Київська Русь", "Козацька доба", "Гетьманщина", "Українська революція", "Незалежність"]

MUSIC_TRACKS = sum(len(tracks) for *_, tracks in MUSIC)
MUSIC_LYRICS = sum(1 for *_, tracks in MUSIC for _, n, *_ in tracks if n in (2, 3))


def _mbid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"syncmusic-test:{name}"))


def generate(root: Path) -> Path:
    """Write the set into `root` unless it is already there in this version; returns `root`."""
    marker = root / MARKER
    if marker.is_file() and marker.read_text().strip() == VERSION:
        return root
    if root.exists():
        shutil.rmtree(root)
    for artist, album, year, genre, tracks in MUSIC:
        discs = len({d for d, *_ in tracks})
        seconds = LONG_TRACK if album in LONG_ALBUMS else 2.0
        for disc, n, title, ext in tracks:
            folder = (
                root / "Music" / artist / f"{year} - {album}" / (f"CD{disc}" if discs > 1 else "")
            )
            on_disc = sum(1 for d, *_ in tracks if d == disc)
            path = make_music(
                folder / f"{n:02} {title}{ext}",
                seconds=seconds,
                cover=True,
                lyrics=f"[00:00.50]{title}" if n in (2, 3) else None,
                title=title,
                artist=artist,
                albumartist=artist,
                album=album,
                date=str(year),
                tracknumber=f"{n}/{on_disc}",
                discnumber=f"{disc}/{discs}",
                genre=genre,
                # made-up ids (the tag-carry hardware test checks they reach the device)
                musicbrainz_artistid=_mbid(artist),
                musicbrainz_albumid=_mbid(f"{artist}/{album}"),
                musicbrainz_trackid=_mbid(f"{artist}/{album}/{disc}/{n}"),
                **({"label": "Test Records"} if ext == ".flac" else {}),
            )
            if n == 2:
                path.with_suffix(".lrc").write_text(f"[00:00.50]{title}\n", encoding="utf-8")
    for author, book, parts in BOOKS:
        for n, stem, seconds in parts:
            make_music(
                root / "Audiobooks" / author / book / f"{stem}.mp3",
                seconds=seconds,
                title=stem,
                artist=author,
                album=book,
                tracknumber=str(n),
                genre="Audiobook",
            )
    for n, title in enumerate(EPISODES, 1):
        make_music(
            root / "Podcasts" / PODCAST / f"{n:02} {title}.mp3",
            seconds=EPISODE,
            title=title,
            artist=PODCAST,
            album=PODCAST,
            date=f"2024-0{n}-01",
            genre="Podcast",
        )
    (root / MARKER).write_text(VERSION)
    return root
