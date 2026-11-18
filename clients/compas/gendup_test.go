package main

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// A server-built set where several files have the same content (empty tables, mirrored indexes) must arrive
// complete: items are keyed by path, never by content.
func TestInstallGeneratedKeepsSameContentFiles(t *testing.T) {
	built := t.TempDir()
	writeTC(t, built, []tcRowSpec{{"Music/A/01.flac", 1, false}})
	src := filepath.Join(built, ".compas")
	toG1(t, src)
	dup := []byte("same content")
	for _, n := range []string{"database_0.tcd.g1", "database_12.tcd.g1", "database_idx.tcd.g1.irecency.v7",
		"database_idx.tcd.g1.irecent_inverse.v7"} {
		os.WriteFile(filepath.Join(src, n), dup, 0o644)
	}
	ents, _ := os.ReadDir(src)
	byFid := map[string]string{}
	var files []item
	for _, e := range ents {
		b, _ := os.ReadFile(filepath.Join(src, e.Name()))
		h := sha256.Sum256(b)
		fid := hex.EncodeToString(h[:])
		byFid[fid] = filepath.Join(src, e.Name())
		files = append(files, item{Path: ".apps-data/syncmusic/gen/" + e.Name(), Fid: fid, Size: int64(len(b))})
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		i := strings.LastIndex(r.URL.Path, "/")
		http.ServeFile(w, r, byFid[r.URL.Path[i+1:]])
	}))
	defer srv.Close()
	card := t.TempDir()
	c := &Client{root: card, data: filepath.Join(card, ".apps-data/syncmusic"), http: srv.Client(), st: State{Server: srv.URL}}
	if gen, err := c.installGenerated(genSet{SetID: "s1", Commit: ".apps-data/syncmusic/generated.set", Files: files}); err != nil || gen != 1 {
		t.Fatal(err)
	}
	for _, e := range ents {
		want, _ := os.ReadFile(filepath.Join(src, e.Name()))
		got, err := os.ReadFile(filepath.Join(card, ".compas", e.Name()))
		if err != nil || string(got) != string(want) {
			t.Fatalf("%s not installed intact: %v", e.Name(), err)
		}
	}
	set := genSet{SetID: "s1", Commit: ".apps-data/syncmusic/generated.set", Files: files}
	if gen, err := c.installGenerated(set); err != nil || gen != 0 {
		t.Fatalf("a complete set is installed again: %d %v", gen, err)
	}
	// a file of the set vanished from the card (seen on a real player): the next run installs the set again
	os.Remove(filepath.Join(card, ".compas", "database_12.tcd.g1"))
	if gen, err := c.installGenerated(set); err != nil || gen != 1 {
		t.Fatalf("incomplete set not reinstalled: %d %v", gen, err)
	}
	if _, err := os.Stat(filepath.Join(card, ".compas", "database_12.tcd.g1")); err != nil {
		t.Fatal(err)
	}
}
