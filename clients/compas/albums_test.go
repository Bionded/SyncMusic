// SPDX-License-Identifier: GPL-3.0-or-later

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAlbumKeyMatchesThePlayer(t *testing.T) {
	if k := albumKey("a", "b   "); k != "album://a\tb" {
		t.Fatal(k)
	}
	if k := albumKey("a\tb\n", "c\rd\te"); k != "album://a b \tc d e" {
		t.Fatal(k)
	}
	if albumKey("", "b") != "" {
		t.Fatal("no artist")
	}
}

func TestAlbumRatingsBothWays(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, ".compas"), 0o755)
	state := remoteStatePath(root)
	os.WriteFile(state, []byte("1 3 77 /data/remote/song.flac\n4 0 0 album://Гурт\tДругий\n"), 0o644)
	c := &Client{root: root, data: t.TempDir()}
	want := []Wanted{
		{Key: "k1", AlbumWant: AlbumWant{"lib/al1", "Гурт", "Перший", 4}},
		{Key: "k2", AlbumWant: AlbumWant{"lib/al2", "Гурт", "Другий", 0}},
	}
	d, _ := c.down(want, nil, nil)
	if err := c.apply(d); err != nil {
		t.Fatal(err)
	}
	got := albumStars(root)
	// the library's rating goes down; the one rated on the player first stays and goes up
	if got["album://Гурт\tПерший"] != 4 || got["album://Гурт\tДругий"] != 2 {
		t.Fatal(got)
	}
	raw, _ := os.ReadFile(state)
	if !strings.HasPrefix(string(raw), "1 3 77 /data/remote/song.flac\n") {
		t.Fatal(string(raw))
	}
	if n := c.collectAlbums(); n != 1 || c.st.Events[0]["type"] != "album_rating" || c.st.Events[0]["key"] != "lib/al2" {
		t.Fatal(n, c.st.Events)
	}
	if c.collectAlbums() != 0 { // sent once
		t.Fatal("sent again")
	}
	// the user re-rates the first album; a library change meanwhile does not overwrite it
	writeAlbumStars(root, map[string]int{"album://Гурт\tПерший": 1})
	want[0].AlbumRating = 5
	d, _ = c.down(want, nil, nil)
	c.apply(d)
	if albumStars(root)["album://Гурт\tПерший"] != 1 {
		t.Fatal("overwritten")
	}
	// the library takes the player's ratings: agreed, nothing more goes up
	want[0].AlbumRating, want[1].AlbumRating = 1, 2
	c.st.Events = nil
	d, _ = c.down(want, nil, nil)
	c.apply(d)
	if n := c.collectAlbums(); n != 0 {
		t.Fatal(n, c.st.Events)
	}
}
