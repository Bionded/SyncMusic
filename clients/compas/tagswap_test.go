package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// toG1 renames an unsuffixed test tagcache to the generation-1 names compas-sdtool writes on an empty card.
func toG1(t *testing.T, db string) {
	ents, _ := os.ReadDir(db)
	for _, e := range ents {
		if m := tcdName.FindStringSubmatch(e.Name()); m != nil && m[2] == "" {
			os.Rename(filepath.Join(db, e.Name()), filepath.Join(db, m[1]+".g1"+m[3]))
		}
	}
}

func TestInstallTagcacheReplacesTheWholeDatabase(t *testing.T) {
	built := t.TempDir()
	writeTC(t, built, []tcRowSpec{{"Music/A/01.flac", 4, true}, {"Music/A/02.flac", 0, false}})
	toG1(t, filepath.Join(built, ".compas"))
	card := t.TempDir()
	writeTC(t, card, []tcRowSpec{{"Music/Old/x.mp3", 1, false}})
	db := filepath.Join(card, ".compas")
	keep := []string{"device.json", "catalog.incarnation", "playlists.list", "queue.bin"}
	for _, n := range keep {
		os.WriteFile(filepath.Join(db, n), []byte("player"), 0o644)
	}
	stale := []string{"database_idx.tcd.g2", "database_4.tcd.g2", "database_idx.tcd.g2.imeta",
		"database_idx.tcd.g2.iquery.v7", "tagcache.refs.g2", "tagcache.refs.g1", "database_0.tcd.g1.tmp"}
	for _, n := range stale {
		os.WriteFile(filepath.Join(db, n), []byte("old"), 0o644)
	}
	os.WriteFile(filepath.Join(db, "tagcache.gen"), []byte("2"), 0o644)

	for run := 0; run < 2; run++ { // a second install of the same set gives the same result
		gen, err := installTagcache(card, filepath.Join(built, ".compas"))
		if err != nil || gen != 1 {
			t.Fatalf("gen %d err %v", gen, err)
		}
	}
	if b, _ := os.ReadFile(filepath.Join(db, "tagcache.gen")); string(b) != "1" {
		t.Fatalf("tagcache.gen = %q", b)
	}
	for _, n := range append(stale, "database_idx.tcd", "database_4.tcd") {
		if _, err := os.Stat(filepath.Join(db, n)); !os.IsNotExist(err) {
			t.Fatalf("old database file %s kept", n)
		}
	}
	for _, n := range keep {
		if b, _ := os.ReadFile(filepath.Join(db, n)); string(b) != "player" {
			t.Fatalf("player file %s touched", n)
		}
	}
	got, err := readTagcache(card)
	if err != nil || len(got) != 2 || got["Music/A/01.flac"] != (Listen{Plays: 4, Fav: true, Stars: 5}) {
		t.Fatalf("read back %v %v", got, err)
	}
	if m, _ := filepath.Glob(filepath.Join(db, "*.tmp")); len(m) != 0 {
		t.Fatalf("leftovers %v", m)
	}
}

func TestInstallTagcacheRefusesOtherGenerations(t *testing.T) {
	built := t.TempDir()
	writeTC(t, built, []tcRowSpec{{"Music/A/01.flac", 0, false}}) // unsuffixed = not what sdtool builds
	card := t.TempDir()
	writeTC(t, card, []tcRowSpec{{"Music/Old/x.mp3", 1, false}})
	if _, err := installTagcache(card, filepath.Join(built, ".compas")); err == nil {
		t.Fatal("non-g1 set accepted")
	}
	if _, err := os.Stat(filepath.Join(card, ".compas", "database_idx.tcd")); err != nil {
		t.Fatal("old database removed on a refused set")
	}
}

func TestInstallTagcacheRefusesBadDownloads(t *testing.T) {
	card := t.TempDir()
	src := t.TempDir()
	os.WriteFile(filepath.Join(src, "database_idx.tcd.g1"), make([]byte, 30), 0o644)
	os.WriteFile(filepath.Join(src, "database_4.tcd.g1"), make([]byte, 30), 0o644)
	if _, err := installTagcache(card, src); err == nil {
		t.Fatal("unknown magic accepted")
	}
	if _, err := os.Stat(filepath.Join(card, ".compas", "tagcache.gen")); !os.IsNotExist(err) {
		t.Fatal("generation switched on a refused download")
	}
}

func TestForeignAudioAndMtime(t *testing.T) {
	root := t.TempDir()
	for _, p := range []string{"Music/A/01.flac", "Music/Mine/own.MP3", ".plugins/x.mp3", "Music/A/cover.jpg"} {
		os.MkdirAll(filepath.Dir(filepath.Join(root, p)), 0o755)
		os.WriteFile(filepath.Join(root, p), []byte("x"), 0o644)
	}
	c := &Client{root: root, st: State{Files: map[string]int64{"Music/A/01.flac": 1}}}
	if n := c.foreignAudio(); n != 1 {
		t.Fatalf("foreign = %d", n)
	}
	if !strings.Contains(foreignNote(1), "(1 file)") || foreignNote(0) != "" {
		t.Fatal(foreignNote(1))
	}
	p := filepath.Join(root, "Music/A/01.flac")
	setMtime(p, 1700000000)
	if fi, _ := os.Stat(p); !fi.ModTime().Equal(time.Unix(1700000000, 0)) {
		t.Fatal(fi.ModTime())
	}
}

func TestInstallTagcacheFailureLeavesNoDatabase(t *testing.T) {
	built := t.TempDir()
	writeTC(t, built, []tcRowSpec{{"Music/A/01.flac", 0, false}})
	toG1(t, filepath.Join(built, ".compas"))
	card := t.TempDir()
	db := filepath.Join(card, ".compas")
	// a name that cannot be replaced (non-empty directory) makes the write fail half-way
	os.MkdirAll(filepath.Join(db, "database_4.tcd.g1", "x"), 0o755)
	if _, err := installTagcache(card, filepath.Join(built, ".compas")); err == nil {
		t.Fatal("failed install reported success")
	}
	if _, err := os.Stat(filepath.Join(db, "tagcache.gen")); !os.IsNotExist(err) {
		t.Fatal("pointer written after a failed install")
	}
	ents, _ := os.ReadDir(db)
	for _, e := range ents {
		if e.Type().IsRegular() && strings.HasPrefix(e.Name(), "database_") {
			t.Fatalf("half database left: %s", e.Name())
		}
	}
}

// A set built over the card's own database: generation G with its refs file and an older tag file it reuses.
func TestInstallTagcacheTakesALaterGeneration(t *testing.T) {
	built := t.TempDir()
	writeTC(t, built, []tcRowSpec{{"Music/A/01.flac", 2, false}, {"Music/A/02.flac", 0, false}})
	src := filepath.Join(built, ".compas")
	ents, _ := os.ReadDir(src)
	for _, e := range ents {
		if m := tcdName.FindStringSubmatch(e.Name()); m != nil && m[2] == "" {
			os.Rename(filepath.Join(src, e.Name()), filepath.Join(src, m[1]+".g3"+m[3]))
		}
	}
	os.WriteFile(filepath.Join(src, "database_0.tcd.g2"), []byte("reused"), 0o644)
	if _, err := installTagcache(t.TempDir(), src); err == nil {
		t.Fatal("generation 3 without its refs file accepted")
	}
	os.WriteFile(filepath.Join(src, "tagcache.refs.g3"), []byte("refs"), 0o644)
	card := t.TempDir()
	writeTC(t, card, []tcRowSpec{{"Music/Old/x.mp3", 1, false}})
	gen, err := installTagcache(card, src)
	if err != nil || gen != 3 {
		t.Fatalf("gen %d err %v", gen, err)
	}
	db := filepath.Join(card, ".compas")
	if b, _ := os.ReadFile(filepath.Join(db, "tagcache.gen")); string(b) != "3" {
		t.Fatalf("tagcache.gen = %q", b)
	}
	for _, n := range []string{"tagcache.refs.g3", "database_0.tcd.g2"} {
		if _, err := os.Stat(filepath.Join(db, n)); err != nil {
			t.Fatalf("%s not installed", n)
		}
	}
	if got, err := readTagcache(card); err != nil || len(got) != 2 {
		t.Fatalf("read back %v %v", got, err)
	}
	os.WriteFile(filepath.Join(src, "database_idx.tcd.g2"), make([]byte, 30), 0o644)
	if _, err := installTagcache(t.TempDir(), src); err == nil {
		t.Fatal("a set with two master indexes accepted")
	}
}

// The card's database goes up once per change, before the server plans.
func TestUploadDatabaseOnlyWhenItChanged(t *testing.T) {
	card := t.TempDir()
	writeTC(t, card, []tcRowSpec{{"Music/A/01.flac", 1, false}})
	db := filepath.Join(card, ".compas")
	os.WriteFile(filepath.Join(db, "tagcache.gen"), []byte("0"), 0o644)
	os.WriteFile(filepath.Join(db, "device.json"), []byte("{}"), 0o644) // the player's own: not sent
	var got []map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "PUT" || !strings.HasSuffix(r.URL.Path, "/player-database") {
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
		}
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		got = append(got, body)
		w.Write([]byte(`{"files": 1, "size": 1}`))
	}))
	defer srv.Close()
	c := &Client{root: card, data: filepath.Join(card, ".apps-data/syncmusic"), http: srv.Client(), st: State{Server: srv.URL}}
	c.uploadDatabase()
	c.uploadDatabase()
	if len(got) != 1 {
		t.Fatalf("%d uploads, want 1", len(got))
	}
	names := []string{}
	for _, f := range got[0]["files"].([]any) {
		names = append(names, f.(map[string]any)["name"].(string))
	}
	if !strings.Contains(strings.Join(names, " "), "database_idx.tcd") || strings.Contains(strings.Join(names, " "), "device.json") {
		t.Fatalf("sent %v", names)
	}
	os.WriteFile(filepath.Join(db, "tagcache.gen"), []byte("1"), 0o644)
	c.uploadDatabase()
	if len(got) != 2 {
		t.Fatal("a changed database was not sent again")
	}
}
