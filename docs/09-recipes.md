# 9. Recipes

## Rotate a changing selection of music (e.g. 20 GB)

Your library is bigger than the player, and you want the player filled with music you have not heard lately, so that
what you listen to makes room for something else. The rules do this without a special feature: a played track drops
out of the selection and the next one moves in.

**Template** (*Templates → New template*, or the device's own rules), one **Include** rule:

- **Media:** music
- **Condition:** *Last played* - *not in the last* - `30` days (tracks never played count as "not in the last 30 days")
- Optional: *Rating* - *is not* - `1` star, or *Genre* - *is not in* - …, to keep music you dislike out
- **Order by:** *random*
- **At most:** `20GB`

The same rule in the **YAML** tab:

```yaml
rules:
  - id: rotation
    name: Rotation
    criteria:
      all:
        - notInTheLast: {lastplayed: 30}
    sort: random
    limit_size: 20GB
```

**How it moves:**

1. The first sync fills 20 GB with a random pick of everything not played in the last 30 days.
2. You listen. Reverse sync brings the play counts and *last played* dates back to the library (Navidrome:
   scrobbles) on the next connect.
3. The next sync (or *update*, with **Remove in automatic updates** on in the device settings) removes the tracks
   you played and fills their space with the next tracks of the random order.
4. After 30 days a played track may be picked again.

**What to know:**

- The random order is **stable**: it depends on the rule and the track, not on the day, so a sync without plays in
  between changes nothing (no copying the same 20 GB again on every connect). The rotation is driven by what you
  play, not by the calendar. Tracks you skip and never finish stay on the device.
- To rotate by the calendar as well, set **New random order** (next to *Order by: random*) to *Every week* or
  *Every month* (YAML: `random_period: week` / `month`). At the start of each period the order is shuffled anew, so
  the next sync swaps unplayed tracks too - up to the whole 20 GB is copied again once a week / month. Navidrome
  `.nsp` export drops this setting (with a warning).
- *Last played* comes from the library. It moves only when reverse sync is on for play counts (the device's
  *Reverse sync* settings) or when you play the music elsewhere through the same server.
- Use a size a little below the free space; the plan shows what fits. A shorter window (`7` days) rotates faster.
- To keep favourites on the device permanently, add a second **Include** rule *Starred* - *is* - *true*; both rules
  are combined, the 20 GB limit applies to the rotation rule only.

## Keep the newest unplayed episodes of every podcast

Preset **Newest unplayed episodes** (Audiobookshelf podcasts): each show keeps its newest *N* episodes (5 by
default) that you have not finished. When you finish one on the player, reverse sync tells the library, and the next
sync removes it and brings the next newest - the device never fills up with old episodes and never runs dry.

In the rule builder: *the latest* `5` *episodes of every podcast*, ticked **not played yet**. YAML:

```yaml
rules:
  - name: Newest unplayed
    kind: [podcast]
    criteria:
      latestEpisodes: {per_series: 5, unplayed: true}
```

Without **not played yet** the rule takes the newest 5 whether played or not, so a finished episode stays (or, with
"remove played", leaves a gap until a new one is published).

## A flash drive for a car stereo or an old player

Head units and cheap players often read only FAT32, show only plain ASCII names, stop at 255 files per folder,
choke on big embedded pictures and play only MP3. One flash drive device covers all of it:

1. **Format** the drive as *FAT32* (the default file system of *Format*).
2. *Device settings → Files on the device*: tick **Car stereo / old player mode**. Names become ASCII (Cyrillic and
   accents transliterated), paths stay within 128 characters and names within 64, a folder takes at most 255 files
   (the next go to *Album (2)*), and playlists are written as plain `.m3u` with Windows line ends.
3. *Playable formats*: `mp3` only - everything else is converted to MP3 by the device's processors on the way.
4. Artwork: in *Processors → Covers* choose **Image file in the album folder** (a `cover.jpg`, with *Remove embedded
   artwork*) or a small *Maximum size* such as 300 px if the unit shows embedded pictures; **Remove artwork** if it
   chokes on any.
5. For players that list everything in one long folder, a flat layout helps: *Path layout → Music*
   `{album_artist} - {album}/{track:02} {title}`.

Switching car mode on or off later renames the files already on the drive at the next sync.
