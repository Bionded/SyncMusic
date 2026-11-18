package main

import (
	"encoding/json"
	"os"
	"testing"
)

// The golden file is compas_feedback.merge_podcast_episodes' output for the same input (USB device parity).
func TestMergePodcastEpisodesMatchesUSBDevice(t *testing.T) {
	b, err := os.ReadFile("testdata/episodes_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g [2]string
	if err := json.Unmarshal(b, &g); err != nil {
		t.Fatal(err)
	}
	got, dropped := mergePodcastEpisodes(g[0], map[string]Meta{
		"Podcasts/Шоу/ep 1.mp3": {Kind: "podcast", Series: "Шоу", Title: "Епізод: 1",
			Published: "2024-05-01T12:00:00+00:00", DurationS: fp(1800.5)},
		"Podcasts/Theirs/x.mp3": {Kind: "podcast", Album: "Theirs", Title: "x"},
		"Podcasts/S/e.mp3":      {Kind: "podcast"},
	})
	if dropped != 0 || got != g[1] {
		t.Fatalf("got\n%q\nwant\n%q", got, g[1])
	}
	if again, _ := mergePodcastEpisodes(got, map[string]Meta{"Podcasts/S/e.mp3": {}, "Podcasts/Theirs/x.mp3": {},
		"Podcasts/Шоу/ep 1.mp3": {}}); again != got {
		t.Fatal("not idempotent")
	}
}
