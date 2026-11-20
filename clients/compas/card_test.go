package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCardIDReadsTheManifestHeader(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, ".syncmusic"), 0o755)
	os.WriteFile(filepath.Join(root, ".syncmusic", "manifest.jsonl"),
		[]byte(`{"type":"header","device_id":"player","device_uid":"uid-1"}`+"\n{\"type\":\"item\"}\n"), 0o644)
	c := &Client{root: root}
	if got := c.cardID(); got != "uid-1" {
		t.Fatalf("card id %q", got)
	}
}

func TestCardIDFallsBackToDeviceIDAndBackup(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, ".syncmusic"), 0o755)
	os.WriteFile(filepath.Join(root, ".syncmusic", "manifest.jsonl.bak"), []byte(`{"type":"header","device_id":"old"}`+"\n"), 0o644)
	if got := readCardID(root); got != "old" {
		t.Fatalf("card id %q", got)
	}
}

func TestCardIDCreatedOnceOnANewCard(t *testing.T) {
	root := t.TempDir()
	c := &Client{root: root}
	id := c.cardID()
	if len(id) != 36 {
		t.Fatalf("new id %q", id)
	}
	b, _ := os.ReadFile(filepath.Join(root, ".syncmusic", "manifest.jsonl"))
	if !strings.Contains(string(b), `"manifest_version":2`) || strings.Count(string(b), "\n") != 1 {
		t.Fatalf("manifest %s", b)
	}
	if again := c.cardID(); again != id {
		t.Fatalf("id changed: %q -> %q", id, again)
	}
}

func TestCardIDWithoutACardRootWritesNothing(t *testing.T) {
	cwd := t.TempDir()
	t.Chdir(cwd)
	c := &Client{}
	if got := c.cardID(); got != "" {
		t.Fatalf("card id %q", got)
	}
	if _, err := os.Stat(filepath.Join(cwd, ".syncmusic")); !os.IsNotExist(err) {
		t.Fatalf(".syncmusic created in the working directory: %v", err)
	}
}

func TestCardIDKeepsTheRowsOfADamagedManifest(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, manifestDir)
	os.MkdirAll(dir, 0o755)
	old := "{garbage\n" + `{"type":"item","lib":"m","item_id":"1","path":"Music/a.mp3"}` + "\n" +
		`{"type":"playlist","name":"P","path":"Playlists/P.m3u8"}` + "\n"
	os.WriteFile(filepath.Join(dir, "manifest.jsonl"), []byte(old), 0o644)
	c := &Client{root: root}
	id := c.cardID()
	if id == "" || readCardID(root) != id {
		t.Fatalf("id %q, header %q", id, readCardID(root))
	}
	files := readCardFiles(root)
	if len(files) != 2 {
		t.Fatalf("rows lost: %v", files)
	}
	aside, _ := filepath.Glob(filepath.Join(dir, "manifest.jsonl.broken-*"))
	if len(aside) != 1 {
		t.Fatalf("original not kept aside: %v", aside)
	}
	if b, _ := os.ReadFile(aside[0]); string(b) != old {
		t.Fatal("kept copy differs from the original")
	}
}
