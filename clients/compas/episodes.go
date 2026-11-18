package main

// Our podcast episodes in the Podcasts plugin's list (parity with compas_feedback.write_podcast_episodes): the
// plugin plays only what its Podcasts/.progress.tsv lists. Our records carry the key `<show key>|syncmusic:<path>`;
// missing ones are added as new downloads, ours that left the card are removed, the plugin's own records and
// every other line stay byte for byte, as do the position and played flags of ours.

import (
	"crypto/md5"
	"encoding/hex"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	ourGUID            = "syncmusic:"
	maxPodcastRecords  = 5000
	maxPodcastBytes    = 2097152
	podcastPluginAsset = ".plugin-assets/Podcasts"
)

// Meta: the plan's `meta` of a track, the part the app keeps for podcast episodes.
type Meta struct {
	Kind      string   `json:"kind"`
	Title     string   `json:"title,omitempty"`
	Album     string   `json:"album,omitempty"`
	Series    string   `json:"series,omitempty"`
	Published string   `json:"published,omitempty"`
	DurationS *float64 `json:"duration_s,omitempty"`
}

func showKey(show string) string {
	h := md5.Sum([]byte(ourGUID + show)) // a key, not security; never meets a subscription's md5(feed URL)
	return hex.EncodeToString(h[:])[:16]
}

// mergePodcastEpisodes: the list with exactly `eps` (card path -> meta) among our records; returns the text and
// how many did not fit the plugin's limits.
func mergePodcastEpisodes(text string, eps map[string]Meta) (string, int) {
	listed := map[string]bool{}
	var kept []string
	for _, l := range lines(text) {
		p := strings.Split(l, "\t")
		var rel string
		var ok bool
		if p[0] == "P" && len(p) >= 7 {
			rel, ok = strings.CutPrefix(unq(p[6]), playerRoot)
		}
		if p[0] == "P" && len(p) >= 2 {
			_, guid, _ := strings.Cut(unq(p[1]), "|")
			if strings.HasPrefix(guid, ourGUID) {
				if _, want := eps[rel]; !ok || !want || listed[rel] {
					continue // ours, but no longer on the card (or a duplicate)
				}
			}
		}
		if ok {
			listed[rel] = true // ours already, or the plugin's own download
		}
		kept = append(kept, l)
	}
	records, size := 0, 0
	for _, l := range kept {
		if strings.HasPrefix(l, "P\t") {
			records++
		}
		size += len(l) + 1
	}
	paths := make([]string, 0, len(eps))
	for p := range eps {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	dropped := 0
	for _, rel := range paths {
		if listed[rel] {
			continue
		}
		m := eps[rel]
		show := m.Series
		if show == "" {
			show = m.Album
		}
		if parts := strings.Split(rel, "/"); show == "" && len(parts) > 1 {
			show = parts[1]
		}
		title := m.Title
		if title == "" {
			title = strings.TrimSuffix(path.Base(rel), path.Ext(rel))
		}
		day, epoch := "", int64(0)
		if t, err := time.Parse(time.RFC3339, m.Published); err == nil {
			day, epoch = t.Format("2006-01-02"), t.Unix()
		}
		dur := 0.0
		if m.DurationS != nil {
			dur = *m.DurationS
		}
		line := strings.Join([]string{"P", enc(showKey(show) + "|" + ourGUID + rel), "0", secs(dur), "0", "1",
			enc(playerRoot + rel), "0", enc(title), enc(day), fmt.Sprint(epoch)}, "\t")
		if records+1 > maxPodcastRecords || size+len(line)+1 > maxPodcastBytes {
			dropped++
			continue
		}
		kept = append(kept, line)
		records, size = records+1, size+len(line)+1
	}
	return joinLines(kept), dropped
}

// writeEpisodes refreshes our records in Podcasts/.progress.tsv when the Podcasts plugin is installed (the file is
// created only when there is an episode to list).
func (c *Client) writeEpisodes() (int, error) {
	if fi, err := os.Stat(filepath.Join(c.root, podcastPluginAsset)); err != nil || !fi.IsDir() {
		return 0, nil
	}
	eps := map[string]Meta{}
	for p, m := range c.st.Episodes {
		if strings.HasPrefix(p, "Podcasts/") && strings.Count(p, "/") >= 2 {
			if _, ours := c.st.Files[p]; !ours {
				continue
			}
			eps[p] = m
		}
	}
	file := filepath.Join(c.root, podcastProgress)
	old, _ := os.ReadFile(file)
	if len(old) == 0 && len(eps) == 0 {
		return 0, nil
	}
	text, dropped := mergePodcastEpisodes(string(old), eps)
	if text == string(old) {
		return dropped, nil
	}
	if err := os.MkdirAll(filepath.Dir(file), 0o755); err != nil {
		return 0, err
	}
	return dropped, replaceFile(file, text)
}
