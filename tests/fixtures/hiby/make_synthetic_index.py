"""Write synthetic-index.json: a made-up SonixOS music index for tests/unit/test_hiby_index.py.

The names cover what a real player index holds (diacritics, Cyrillic, katakana, typographic apostrophes and
hyphens, leading digits and punctuation, "feat." credits, `;` genre lists); the expected columns come from the
formulas in syncmusic/devices/HibyR1/hiby_index.py, which were checked against an index a HiBy R1 wrote itself.
Regenerate only after a deliberate formula change: uv run python tests/fixtures/hiby/make_synthetic_index.py
"""

from __future__ import annotations

import json
from pathlib import Path

from syncmusic.devices.HibyR1 import hiby_index as h

RULES = "a0/g11/j1/x00000000"
ROOT = f"{h.VOLUME}/Music"
# album artist -> album folder -> (album title, genres, track titles)
LIBRARY = {
    "Anna Kowalczyk": {
        "2019 - Łódź Nights": (
            "Łódź Nights",
            "Electronic; Ambient; Synthwave",
            ["Prolog", "Światło", "V"],
        )
    },
    "Ольга Морозова": {
        "2021 - Зимові казки": ("Зимові казки", "Folk; Neofolk", ["Сутінки", "Міст"])
    },
    "マリコ": {"2018 - ナイトプール": ("ナイトプール", "Vaporwave", ["コーヒー", "プール"])},
    "The Paper Kites Club": {
        "2015 - Don’t Look Back": (
            "Don’t Look Back",
            "Indie Rock; Post-Punk",
            ["Why’s This Here?", "Café Société"],
        )
    },
    "Lee Smith‐Jones": {
        "2010 - 1999": ("1999", "Hip Hop; Rock", ["67 Mustang", "Track (feat. Ray & Bo)"])
    },
    "Various Artists": {
        "2022 - :Of Moons & Stars:": (":Of Moons & Stars:", "", ["Zed", "Quiet Hour", "Échos"])
    },
}


def build() -> dict:
    rules = h.Rules.parse(RULES)
    media, media_genre, genre_link, folders = [], [], [], []
    lookups: dict[str, set[str]] = {
        "ALBUM_TABLE": set(),
        "ARTIST_TABLE": set(),
        "GENRE_TABLE": set(),
        "ALBUM_ARTIST_TABLE": set(),
    }
    folders.append({"path": ROOT, "entries": [[a, True] for a in LIBRARY]})
    for artist, albums in LIBRARY.items():
        folders.append({"path": f"{ROOT}/{artist}", "entries": [[d, True] for d in albums]})
        lookups["ALBUM_ARTIST_TABLE"].add(artist)
        for folder, (album, genres, titles) in albums.items():
            base = f"{ROOT}/{artist}/{folder}"
            files = [f"{i:02d} - {t.replace('?', '_')}.flac" for i, t in enumerate(titles, 1)]
            folders.append(
                {"path": base, "entries": [[f, False] for f in files] + [["cover.jpg", False]]}
            )
            lookups["ALBUM_TABLE"].add(album)
            for title, file in zip(titles, files, strict=True):
                path = f"{base}/{file}"
                media.append({"path": path, "name": title, "album_artist": artist})
                media_genre.append([path, genres])
                genre_link += [[path, g] for g in h.split_names(genres, rules.genre)]
                lookups["GENRE_TABLE"].update(h.split_names(genres, rules.genre))
                lookups["ARTIST_TABLE"].update(h.split_names(title, rules.artist) or [artist])
    for r in media:
        r["character"] = h.index_char(r["name"])[0]
        r["sortkey"] = h.sort_key(r["name"])
        r["album_key"] = h.album_key(r["album_artist"], r["path"])
    for f in folders:
        f["files"], f["names"] = h.folder_signature([(n, d) for n, d in f["entries"]])
    return {
        "rules": RULES,
        "media": media,
        "folders": folders,
        "lookups": {
            t: [[v, *h.index_char(v), h.sort_key(v)] for v in sorted(vs)]
            for t, vs in lookups.items()
        },
        "genre_link": genre_link,
        "media_genre": media_genre,
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("synthetic-index.json")
    out.write_text(json.dumps(build(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}")
