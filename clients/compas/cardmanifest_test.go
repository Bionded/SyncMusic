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
