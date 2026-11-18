package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func fp(v float64) *float64 { return &v }

func TestMergeAudiobookPositions(t *testing.T) {
	old := "V\t3\nP\tBook%20A\t01.mp3\t10\t600\t1\t5\t1\t0\nB\tbookmark\n"
	text, dropped := mergeAudiobookPositions(old, map[string]Listen{
		"Audiobooks/Book A/02.mp3": {Pos: 125.7},
		"Audiobooks/Новая/01.mp3":  {Pos: 30},
		"Audiobooks/single.m4b":    {Pos: 5},
		"Music/not a book.mp3":     {Pos: 1},
	}, 1000)
	if len(dropped) != 0 {
		t.Fatal(dropped)
	}
	want := "V\t3\n" +
		"P\tBook%20A\t02.mp3\t125\t0\t0\t1000\t0\t0\n" +
		"P\tsingle.m4b\tsingle.m4b\t5\t0\t0\t1000\t0\t0\n" +
		"P\t%D0%9D%D0%BE%D0%B2%D0%B0%D1%8F\t01.mp3\t30\t0\t0\t1000\t0\t0\n" +
		"B\tbookmark\n"
	if text != want {
		t.Fatalf("got\n%q\nwant\n%q", text, want)
	}
}

func TestFinishedEarlierChapterKeepsTheBook(t *testing.T) {
	old := "P\tBook\t002%20-%20b.mp3\t30\t40\t0\t100\t0\t0\n"
	if text, _ := mergeAudiobookPositions(old, map[string]Listen{"Audiobooks/Book/001 - a.mp3": {Pos: 40, Finished: true}}, 500); text != old {
		t.Fatalf("%q", text)
	}
	if text, _ := mergeAudiobookPositions(old, map[string]Listen{"Audiobooks/Book/002 - b.mp3": {Pos: 40, Finished: true}}, 500); !strings.Contains(text, "\t40\t40\t1\t500") {
		t.Fatalf("%q", text)
	}
}

func TestMergePodcastPositionsOnlyListed(t *testing.T) {
	old := "P\tk%7Cg\t0\t900\t1\t1\t%2Fdata%2Fmnt%2Fsd_0%2FPodcasts%2FShow%2Fep.mp3\t0\tTitle\n" +
		"P\tother\t7\t100\t0\t0\t%2Fdata%2Fmnt%2Fsd_0%2FPodcasts%2FX%2Fy.mp3\t3\n"
	text, listed := mergePodcastPositions(old, map[string]Listen{
		"Podcasts/Show/ep.mp3": {Pos: 42}, "Podcasts/Show/missing.mp3": {Pos: 9},
	}, 77)
	if !listed["Podcasts/Show/ep.mp3"] || listed["Podcasts/Show/missing.mp3"] {
		t.Fatal(listed)
	}
	ls := strings.Split(text, "\n")
	if ls[0] != "P\tk%7Cg\t42\t900\t0\t1\t%2Fdata%2Fmnt%2Fsd_0%2FPodcasts%2FShow%2Fep.mp3\t77\tTitle" ||
		ls[1] != strings.Split(old, "\n")[1] {
		t.Fatalf("%q", text)
	}
}

func TestPositionsDownBaselineAndSkip(t *testing.T) {
	root := t.TempDir()
	c := &Client{root: root, data: t.TempDir(), st: State{
		Keys: map[string]string{"Audiobooks/B/01.mp3": "k1", "Audiobooks/C/01.mp3": "k2",
			"Audiobooks/D/01.mp3": "k3", "Music/a.flac": "k4"},
		Seen: map[string]Listen{"Audiobooks/C/01.mp3": {Pos: 50}},
	}}
	want := []Wanted{{Key: "k1", PositionS: fp(100)}, {Key: "k2", PositionS: fp(50.4)},
		{Key: "k3", PositionS: fp(9)}, {Key: "k4", PositionS: fp(3)}}
	ups := c.positionsDown(want, map[string]bool{"k3": true})
	if len(ups) != 1 || ups["Audiobooks/B/01.mp3"].Pos != 100 {
		t.Fatal(ups) // unchanged C, just-sent D and music are left alone
	}
	if _, err := c.writePositions(ups, 5); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(filepath.Join(root, audiobookState))
	if string(b) != "P\tB\t01.mp3\t100\t0\t0\t5\t0\t0\n" {
		t.Fatalf("%q", b)
	}
	if c.st.Seen["Audiobooks/B/01.mp3"].Pos != 100 || len(c.positionsDown(want, nil)) != 1 {
		t.Fatal("baseline not moved") // only D (never seen) is left
	}
	if p := positions(root)["Audiobooks/B/01.mp3"]; p.Pos != 100 {
		t.Fatal(p) // the up direction reads what we wrote: no echo
	}
}

func TestFinishedGoesDown(t *testing.T) {
	c := &Client{root: t.TempDir(), data: t.TempDir(), st: State{
		Keys: map[string]string{"Audiobooks/B/01.mp3": "k1"},
		Seen: map[string]Listen{"Audiobooks/B/01.mp3": {Pos: 100}},
	}}
	ups := c.positionsDown([]Wanted{{Key: "k1", PositionS: fp(100), Finished: true}}, nil)
	if !ups["Audiobooks/B/01.mp3"].Finished {
		t.Fatal(ups)
	}
	if _, err := c.writePositions(ups, 5); err != nil {
		t.Fatal(err)
	}
	if p := positions(c.root)["Audiobooks/B/01.mp3"]; !p.Finished {
		t.Fatal(p)
	}
}
