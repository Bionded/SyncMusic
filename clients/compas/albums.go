// SPDX-License-Identifier: GPL-3.0-or-later

package main

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Album ratings: the player keeps them as `album://<album artist>\t<album>` rows of .compas/remote_state.tsv
// ("<rating code> <plays> <last played> <key>" per line, codes as in the tagcache). A library change goes down
// only while the player still shows what both agreed on last; a change on the player goes up as an
// `album_rating` event naming the server's album (`album_ref`) and is agreed on once the library has it.

const albumPrefix = "album://"

// AlbumBase is what player and library agreed on last for one album.
type AlbumBase struct {
	Ref   string `json:"ref"`
	Stars int    `json:"stars"`
	Sent  int    `json:"sent,omitempty"` // stars sent up and not yet in the library (-1 = none)
}

// albumKey: the player's key of an album (tabs and line breaks as spaces, trailing spaces of the album dropped).
func albumKey(artist, album string) string {
	if artist == "" || album == "" {
		return ""
	}
	clean := func(s string) string { return strings.NewReplacer("\t", " ", "\r", " ", "\n", " ").Replace(s) }
	return albumPrefix + clean(artist) + "\t" + strings.TrimRight(clean(album), " ")
}

type rsRow struct {
	code, plays, last int
	key               string
}

func remoteStatePath(root string) string { return filepath.Join(root, ".compas", "remote_state.tsv") }

func readRemoteState(root string) []rsRow {
	f, err := os.Open(remoteStatePath(root))
	if err != nil {
		return nil
	}
	defer f.Close()
	var rows []rsRow
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 4096), 1<<20)
	for sc.Scan() {
		parts := strings.SplitN(strings.TrimLeft(sc.Text(), " \t"), " ", 4)
		if len(parts) < 4 {
			continue
		}
		var n [3]int
		ok := true
		for i := range n {
			if n[i], err = strconv.Atoi(strings.TrimSpace(parts[i])); err != nil {
				ok = false
			}
		}
		if ok {
			rows = append(rows, rsRow{n[0], n[1], n[2], strings.TrimLeft(parts[3], " \t")})
		}
	}
	return rows
}

// albumStars: album key -> the stars the player shows.
func albumStars(root string) map[string]int {
	out := map[string]int{}
	for _, r := range readRemoteState(root) {
		if strings.HasPrefix(r.key, albumPrefix) {
			out[r.key] = starsOf(r.code)
		}
	}
	return out
}

// writeAlbumStars sets the albums' stars (0 clears), keeping every other row; written next to the file and renamed.
func writeAlbumStars(root string, stars map[string]int) error {
	left := map[string]int{}
	for k, v := range stars {
		left[k] = v
	}
	var b strings.Builder
	for _, r := range readRemoteState(root) {
		if n, ok := left[r.key]; ok {
			r.code = int(codeOf(n))
			delete(left, r.key)
		}
		fmt.Fprintf(&b, "%d %d %d %s\n", r.code, r.plays, r.last, r.key)
	}
	for k, n := range left {
		fmt.Fprintf(&b, "%d 0 0 %s\n", codeOf(n), k)
	}
	path := remoteStatePath(root)
	tmp := path + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	if _, err = f.WriteString(b.String()); err == nil {
		err = f.Sync()
	}
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, path)
}

// albumsDown: the library's album ratings to write onto the player; moves the agreed baselines.
func (c *Client) albumsDown(want []Wanted) map[string]int {
	if c.st.Albums == nil {
		c.st.Albums = map[string]AlbumBase{}
	}
	now := albumStars(c.root)
	writes := map[string]int{}
	for _, w := range want {
		key := albumKey(w.AlbumArtist, w.Album)
		if key == "" || w.AlbumRef == "" {
			continue
		}
		lib, player := w.AlbumRating, now[key]
		base, known := c.st.Albums[key]
		switch {
		case player == lib:
			c.st.Albums[key] = AlbumBase{Ref: w.AlbumRef, Stars: lib, Sent: -1}
		case !known && player == 0, known && player == base.Stars:
			writes[key] = lib
			c.st.Albums[key] = AlbumBase{Ref: w.AlbumRef, Stars: lib, Sent: -1}
		case !known: // rated on the player before the app knew the album: goes up with the next collect
			c.st.Albums[key] = AlbumBase{Ref: w.AlbumRef, Stars: lib, Sent: -1}
		}
	}
	return writes
}

// collectAlbums queues an `album_rating` event for each album whose stars the user changed on the player.
func (c *Client) collectAlbums() int {
	now := albumStars(c.root)
	n := 0
	for key, base := range c.st.Albums {
		stars := now[key]
		if stars == base.Stars || stars == base.Sent {
			continue
		}
		c.queue("album_rating", base.Ref, map[string]any{"value": stars})
		base.Sent = stars
		c.st.Albums[key] = base
		n++
	}
	return n
}
