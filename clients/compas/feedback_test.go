package main

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCardPath(t *testing.T) {
	for in, want := range map[string]string{
		"Playlists/Run.m3u8": "Playlists/Run.m3u", "Playlists/A.m3u": "Playlists/A.m3u",
		"Music/x.m3u8": "Music/x.m3u8", "Music/A/01.flac": "Music/A/01.flac",
	} {
		if got := cardPath(in); got != want {
			t.Errorf("cardPath(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestPositions(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, "Podcasts"), 0o755)
	os.MkdirAll(filepath.Join(root, ".plugins"), 0o755)
	os.WriteFile(filepath.Join(root, "Podcasts", ".progress.tsv"), []byte(
		"V\t1\nP\tk1\t125.5\t600\t0\t0\t/data/mnt/sd_0/Podcasts/Show/%D0%95%D0%BF.mp3\t1700000000\n"+
			"P\tk2\t0\t600\t0\t1\t/data/mnt/sd_0/Podcasts/Show/new.mp3\t0\n"), 0o644)
	os.WriteFile(filepath.Join(root, ".plugins", ".audiobooks_state_v3"), []byte(
		"P\tBook\t01.mp3\t30\t100\t1\t1700000000\n"+
			"P\tSingle.m4b\tSingle.m4b\t42\t100\t0\t0\n"), 0o644)
	got := positions(root)
	want := map[string]Listen{
		"Podcasts/Show/Еп.mp3":   {Pos: 125.5},
		"Audiobooks/Book/01.mp3": {Pos: 30, Finished: true},
		"Audiobooks/Single.m4b":  {Pos: 42},
	}
	if len(got) != len(want) {
		t.Fatalf("got %v", got)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s: got %v want %v", k, got[k], v)
		}
	}
}

func TestPlayTimesOrderedAtLastPlayed(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	got := playTimes(1_799_990_000, 1_799_999_000, 3, now)
	if len(got) != 3 || got[2].Unix() != 1_799_999_000 || !(got[0].Before(got[1]) && got[1].Before(got[2])) {
		t.Fatalf("plays: %v", got)
	}
	if got[0].Unix() <= 1_799_990_000 {
		t.Fatalf("before the previous look: %v", got[0])
	}
	first := playTimes(0, 1_799_999_000, 2, now) // no previous look: a minute apart
	if first[1].Unix()-first[0].Unix() != 60 {
		t.Fatalf("first look: %v", first)
	}
	if none := playTimes(0, 0, 2, now); none[1] != now || !none[0].Before(none[1]) {
		t.Fatalf("no last played: %v", none)
	}
}

func TestSummarySaysWhatWasSent(t *testing.T) {
	s := summary(Result{Fetched: 2, Sent: map[string]int{"play": 4, "favourite": 1}})
	if !strings.Contains(s, "Sent: 4 plays, 1 heart, 0 positions") || !strings.Contains(s, "2 new") {
		t.Fatal(s)
	}
}

// writeTC writes a minimal generation-0 tagcache: one row per path with (plays, fav).
func writeTC(t *testing.T, root string, rows []tcRowSpec) {
	t.Helper()
	db := filepath.Join(root, ".compas")
	os.MkdirAll(db, 0o755)
	le := binary.LittleEndian
	names := le.AppendUint32(make([]byte, 0), 0x54434810)
	names = append(names, make([]byte, 8)...)
	master := le.AppendUint32(nil, 0x54434810)
	master = append(master, make([]byte, 4)...)
	master = le.AppendUint32(master, uint32(len(rows)))
	master = append(master, make([]byte, 12)...)
	for i, r := range rows {
		e := make([]byte, entrySize)
		le.PutUint32(e[tagFile*4:], uint32(len(names)))
		le.PutUint32(e[tagPlays*4:], uint32(r.plays))
		if r.fav {
			le.PutUint32(e[tagRating*4:], 1)
		}
		master = append(master, e...)
		p := []byte(playerRoot + r.path + "\x00")
		names = le.AppendUint32(names, uint32(len(p)))
		names = le.AppendUint32(names, uint32(i))
		names = append(names, p...)
	}
	os.WriteFile(filepath.Join(db, "database_idx.tcd"), master, 0o644)
	os.WriteFile(filepath.Join(db, "database_4.tcd"), names, 0o644)
}

type tcRowSpec struct {
	path  string
	plays int
	fav   bool
}

func TestHeartsDownPatchesOnlyOurRows(t *testing.T) {
	root := t.TempDir()
	writeTC(t, root, []tcRowSpec{
		{"Music/A/01.flac", 1, false}, // library: heart + 5 plays -> both down
		{"Music/A/02.flac", 9, true},  // library: no heart, 3 plays -> heart off, plays stay 9
		{"Music/A/03.flac", 0, true},  // heart just sent up, library has none yet -> untouched
		{"Music/Mine/own.mp3", 0, false},
	})
	c := &Client{root: root, data: t.TempDir(), st: State{
		Keys: map[string]string{"Music/A/01.flac": "k1", "Music/A/02.flac": "k2", "Music/A/03.flac": "k3",
			"Music/A/new.flac": "k4"},
	}}
	c.collect() // the baseline, as Sync does before planning
	c.st.Events = nil
	want := []Wanted{{"k1", true, 5, 0, nil, false, AlbumWant{}}, {"k2", false, 3, 0, nil, false, AlbumWant{}}, {"k3", false, 0, 0, nil, false, AlbumWant{}}, {"k4", true, 1, 0, nil, false, AlbumWant{}}, {"own", true, 9, 0, nil, false, AlbumWant{}}}
	d, err := c.heartsDown(want, map[string]bool{"k3": true})
	if err != nil || len(d.patches) != 1 || d.patches[0].card != "Music/A/01.flac" {
		t.Fatalf("patches %v %v", d.patches, err) // only 01's plays: library hearts are not stars
	}
	if err := c.apply(d); err != nil {
		t.Fatal(err)
	}
	got, _ := readTagcache(root)
	if got["Music/A/01.flac"].Plays != 5 || got["Music/A/01.flac"].Stars != 0 ||
		got["Music/A/02.flac"].Plays != 9 || !got["Music/A/02.flac"].Fav ||
		!got["Music/A/03.flac"].Fav || got["Music/Mine/own.mp3"] != (Listen{}) {
		t.Fatalf("tagcache after: %v", got)
	}
	if n, _ := c.collect(); n != 0 {
		t.Fatalf("the patched values were sent back up: %d events", n)
	}
	// k3 is looked at now (first look without a library heart: kept); nothing else changed
	again, _ := c.heartsDown(want, nil)
	if len(again.patches) != 0 {
		t.Fatalf("second look: %v", again.patches)
	}
	c.apply(again)
	want[1].Favourite = true // a heart in the library never becomes stars; a rating does
	want[0].Rating = 2
	if d, _ := c.heartsDown(want, nil); len(d.patches) != 1 || d.patches[0].card != "Music/A/01.flac" || d.patches[0].to.Stars != 2 {
		t.Fatalf("rating down: %v", d.patches)
	}
}

func TestStarsAreRatingsNotHearts(t *testing.T) {
	root := t.TempDir()
	writeTC(t, root, []tcRowSpec{{"Music/A/01.flac", 1, true}, {"Music/A/02.flac", 1, false}})
	c := &Client{root: root, data: t.TempDir(), st: State{
		Keys: map[string]string{"Music/A/01.flac": "k1", "Music/A/02.flac": "k2"},
	}}
	c.collect()
	c.st.Events = nil
	// the user re-rates the heart to 4 stars and gives 02 two stars (Compás 1.6.0: stored 2 and 4)
	master := filepath.Join(root, ".compas", "database_idx.tcd")
	b, _ := os.ReadFile(master)
	binary.LittleEndian.PutUint32(b[24+tagRating*4:], 2)
	binary.LittleEndian.PutUint32(b[24+entrySize+tagRating*4:], 4)
	os.WriteFile(master, b, 0o644)
	if n, _ := c.collect(); n != 2 || c.st.Events[0]["type"] != "rating" || c.st.Events[1]["type"] != "rating" {
		t.Fatalf("stars not sent as ratings: %v", c.st.Events)
	}
	vals := map[any]any{c.st.Events[0]["key"]: c.st.Events[0]["value"], c.st.Events[1]["key"]: c.st.Events[1]["value"]}
	if vals["k1"] != 4 || vals["k2"] != 2 {
		t.Fatalf("ratings: %v", vals)
	}
	// the library (which has not taken the new stars yet) hearts 02 and adds plays: plays go down, the user's
	// stars just sent up stay
	sent := map[string]bool{"k1": true, "k2": true}
	d, err := c.heartsDown([]Wanted{{"k1", false, 3, 0, nil, false, AlbumWant{}}, {"k2", true, 3, 0, nil, false, AlbumWant{}}}, sent)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.apply(d); err != nil {
		t.Fatal(err)
	}
	b, _ = os.ReadFile(master)
	if r1, r2 := binary.LittleEndian.Uint32(b[24+tagRating*4:]), binary.LittleEndian.Uint32(b[24+entrySize+tagRating*4:]); r1 != 2 || r2 != 4 {
		t.Fatalf("ratings overwritten: %d %d", r1, r2)
	}
	if got, _ := readTagcache(root); got["Music/A/02.flac"].Plays != 1 {
		t.Fatalf("a skipped row was patched: %v", got)
	}
}

func TestLibraryRatingsGoDownButNeverOverNewStars(t *testing.T) {
	root := t.TempDir()
	writeTC(t, root, []tcRowSpec{{"Music/A/01.flac", 0, false}, {"Music/A/02.flac", 0, false}})
	c := &Client{root: root, data: t.TempDir(), st: State{
		Keys: map[string]string{"Music/A/01.flac": "k1", "Music/A/02.flac": "k2"},
	}}
	c.collect()
	want := []Wanted{{"k1", false, 0, 3, nil, false, AlbumWant{}}, {"k2", true, 0, 0, nil, false, AlbumWant{}}}
	d, _ := c.heartsDown(want, nil)
	if err := c.apply(d); err != nil {
		t.Fatal(err)
	}
	got, _ := readTagcache(root)
	if got["Music/A/01.flac"].Stars != 3 || got["Music/A/02.flac"].Stars != 0 {
		t.Fatalf("library ratings not shown (or a heart shown as stars): %v", got)
	}
	// the user gives 02 5 stars then clears them: ratings both times, never a heart
	master0 := filepath.Join(root, ".compas", "database_idx.tcd")
	b0, _ := os.ReadFile(master0)
	binary.LittleEndian.PutUint32(b0[24+entrySize+tagRating*4:], 1)
	os.WriteFile(master0, b0, 0o644)
	c.st.Events = nil
	if n, _ := c.collect(); n != 1 || c.st.Events[0]["type"] != "rating" || c.st.Events[0]["value"] != 5 {
		t.Fatalf("5 stars: %v", c.st.Events)
	}
	master := filepath.Join(root, ".compas", "database_idx.tcd")
	b, _ := os.ReadFile(master)
	binary.LittleEndian.PutUint32(b[24+entrySize+tagRating*4:], 0)
	os.WriteFile(master, b, 0o644)
	c.st.Events = nil
	if n, _ := c.collect(); n != 1 || c.st.Events[0]["type"] != "rating" || c.st.Events[0]["value"] != 0 {
		t.Fatalf("cleared stars: %v", c.st.Events)
	}
	// the user sets 01 to 1 star; the library changes 01 to 4 before it took that: the user's star stays
	b, _ = os.ReadFile(master)
	binary.LittleEndian.PutUint32(b[24+tagRating*4:], uint32(codeOf(1)))
	os.WriteFile(master, b, 0o644)
	want[0].Rating = 4
	d, _ = c.heartsDown(want, nil)
	for _, p := range d.patches {
		if p.card == "Music/A/01.flac" && p.to.Stars != 1 {
			t.Fatalf("the user's new star overwritten: %v", d.patches)
		}
	}
}
