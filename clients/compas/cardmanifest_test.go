package main

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

// The server's manifest of the card lands in .syncmusic/manifest.jsonl with its backup copy, replacing the old
// one; a copy that arrives damaged leaves the card's manifest as it was.
func TestInstallCardManifest(t *testing.T) {
	body := []byte(`{"type":"header","manifest_version":2,"device_uid":"card-1"}` + "\n")
	h := sha256.Sum256(body)
	fid := hex.EncodeToString(h[:])
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write(body)
	}))
	defer srv.Close()
	card := t.TempDir()
	old := []byte(`{"type":"header","manifest_version":2,"device_uid":"card-1"}` + "\n" + "old\n")
	os.MkdirAll(filepath.Join(card, ".syncmusic"), 0o755)
	os.WriteFile(filepath.Join(card, ".syncmusic", "manifest.jsonl"), old, 0o644)
	c := &Client{root: card, data: filepath.Join(card, ".apps-data/syncmusic"), http: srv.Client(), st: State{Server: srv.URL}}

	bad := item{Path: ".syncmusic/manifest.jsonl", Fid: "00" + fid[2:], Size: int64(len(body))}
	if err := c.installCardManifest(bad); err == nil {
		t.Fatal("a damaged manifest was accepted")
	}
	if got, _ := os.ReadFile(filepath.Join(card, ".syncmusic", "manifest.jsonl")); string(got) != string(old) {
		t.Fatal("a damaged download replaced the card's manifest")
	}

	if err := c.installCardManifest(item{Path: ".syncmusic/manifest.jsonl", Fid: fid, Size: int64(len(body))}); err != nil {
		t.Fatal(err)
	}
	for _, n := range []string{"manifest.jsonl", "manifest.jsonl.bak"} {
		if got, _ := os.ReadFile(filepath.Join(card, ".syncmusic", n)); string(got) != string(body) {
			t.Fatalf("%s: %q", n, got)
		}
	}
	if id := readCardID(card); id != "card-1" {
		t.Fatalf("card id after the install: %q", id)
	}
}

// Files a USB sync wrote (listed in the card's manifest) are reported as held, so the first Wi-Fi sync after a
// USB sync sends only what is new; deleted rows, missing files and the user's own files are not reported.
func TestInventoryIncludesTheCardManifestsFiles(t *testing.T) {
	card := t.TempDir()
	write := func(rel, body string) {
		p := filepath.Join(card, filepath.FromSlash(rel))
		os.MkdirAll(filepath.Dir(p), 0o755)
		os.WriteFile(p, []byte(body), 0o644)
	}
	write(".syncmusic/manifest.jsonl", `{"type":"header","manifest_version":2,"device_uid":"card-1"}
{"type":"item","lib":"a","item_id":"1","path":"Music/A/one.flac","size":3}
{"type":"item","lib":"a","item_id":"2","path":"Music/A/two.flac","size":3}
{"type":"item","lib":"a","item_id":"3","path":"Music/A/gone.flac","size":3}
{"type":"delete","lib":"a","item_id":"2"}
{"type":"playlist","name":"Mix","path":"Playlists/Mix.m3u8"}
`)
	write("Music/A/one.flac", "one")
	write("Music/A/two.flac", "two")
	write("Music/Mine/own.mp3", "own")
	write("Playlists/Mix.m3u", "#EXTM3U\n")
	c := &Client{root: card, st: State{Files: map[string]int64{}}}
	got := map[string]int64{}
	for _, it := range c.inventory() {
		got[it["path"].(string)] = it["size"].(int64)
	}
	want := map[string]int64{"Music/A/one.flac": 3, "Playlists/Mix.m3u8": 8}
	if len(got) != len(want) {
		t.Fatalf("inventory %v, want %v", got, want)
	}
	for p, n := range want {
		if got[p] != n {
			t.Fatalf("inventory %v, want %v", got, want)
		}
	}
}
