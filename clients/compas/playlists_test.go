package main

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// apply mirrors the server's read_playlists: remove / move (remove if present + insert at index) / add.
func apply(list []string, ops []map[string]any) []string {
	out := append([]string{}, list...)
	for _, op := range ops {
		k := op["key"].(string)
		if op["op"] == "remove" || op["op"] == "move" {
			for i, x := range out {
				if x == k {
					out = append(out[:i], out[i+1:]...)
					break
				}
			}
		}
		if op["op"] == "move" || op["op"] == "add" {
			i := op["index"].(int)
			out = append(out[:i], append([]string{k}, out[i:]...)...)
		}
	}
	return out
}

func TestEditOps(t *testing.T) {
	for _, c := range [][2][]string{
		{{"a", "b", "c"}, {"c", "a", "d"}},
		{{"a", "b"}, {}},
		{nil, {"x", "y"}},
		{{"a", "b"}, {"a", "b", "c"}},
	} {
		if got := apply(c[0], editOps(c[0], c[1])); !reflect.DeepEqual(got, append([]string{}, c[1]...)) {
			t.Errorf("%v -> %v: got %v", c[0], c[1], got)
		}
	}
	if editOps([]string{"a"}, []string{"a"}) != nil {
		t.Error("no change must give no ops")
	}
}

func TestCollectPlaylists(t *testing.T) {
	root := t.TempDir()
	c := &Client{root: root, data: t.TempDir()}
	c.st.Keys = map[string]string{"Music/A/01.flac": "k1", "Music/A/02.flac": "k2", "Music/B/Ä.mp3": "k3"}
	pl := filepath.Join(root, "Playlists")
	os.MkdirAll(pl, 0o755)
	write := func(name, body string) { os.WriteFile(filepath.Join(pl, name), []byte(body), 0o644) }
	write("Mix.m3u", "#EXTM3U\n../Music/A/01.flac\n../Music/A/02.flac\n")
	c.st.Lists = map[string][]string{"Playlists/Mix.m3u": {"k1", "k2"}}
	if n := c.collectPlaylists(); n != 0 || len(c.st.Events) != 0 {
		t.Fatalf("unchanged playlist queued %d", n)
	}
	// the player appends with its own absolute path form; a new playlist made on the player
	write("Mix.m3u", "../Music/A/01.flac\r\n../Music/A/02.flac\r\n/data/mnt/sd_0/Music/B/Ä.mp3\r\n/data/mnt/sd_0/Music/Other/x.mp3\n")
	write("New.m3u", "/Music/A/02.flac\n")
	write("Empty.m3u", "")
	if !c.editedSince("Playlists/Mix.m3u") {
		t.Fatal("edit not detected")
	}
	if n := c.collectPlaylists(); n != 2 {
		t.Fatalf("queued %d", n)
	}
	names := []string{}
	for _, e := range c.st.Events {
		if _, has := e["key"]; has || e["type"] != "playlist_edit" {
			t.Fatalf("bad event %v", e)
		}
		names = append(names, e["name"].(string))
	}
	if !reflect.DeepEqual(names, []string{"Mix", "New"}) {
		t.Fatal(names)
	}
	if got := apply([]string{"k1", "k2"}, c.st.Events[0]["ops"].([]map[string]any)); !reflect.DeepEqual(got, []string{"k1", "k2", "k3"}) {
		t.Fatal(got)
	}
	if c.editedSince("Playlists/Mix.m3u") || c.collectPlaylists() != 0 {
		t.Fatal("edit sent twice")
	}
}
