package main

// Positions down (parity with the USB Compás device, compas_feedback.write_positions): the library's resume
// position of our audiobook files and podcast episodes goes into the plugins' state files
// (.plugins/.audiobooks_state_v3, Podcasts/.progress.tsv). The plugins read them only when they load and
// overwrite them on their next save, so this is written only right before the restart, like the hearts.

import (
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	audiobookState  = ".plugins/.audiobooks_state_v3"
	podcastProgress = "Podcasts/.progress.tsv"
	stateMaxBytes   = 2097152 - 8192 // the plugins' readers stop at these (Audiobooks.lua keeps the same margin)
	stateMaxLines   = 20000 - 10
)

// positionsDown: card path -> library position for our audiobook/podcast files whose library position changed
// since the last look (`seen`); a position the player just sent up (keys in skip) is left alone.
func (c *Client) positionsDown(want []Wanted, skip map[string]bool) map[string]Listen {
	byKey := map[string]Wanted{}
	for _, w := range want {
		byKey[w.Key] = w
	}
	out := map[string]Listen{}
	for card, key := range c.st.Keys {
		w, ok := byKey[key]
		if !ok || w.PositionS == nil || skip[key] {
			continue
		}
		if !strings.HasPrefix(card, "Audiobooks/") && !strings.HasPrefix(card, "Podcasts/") {
			continue
		}
		seen := c.st.Seen[card]
		if math.Abs(*w.PositionS-seen.Pos) >= 1 || w.Finished != seen.Finished {
			out[card] = Listen{Pos: *w.PositionS, Finished: w.Finished}
		}
	}
	return out
}

// writePositions puts the positions into the plugins' files and moves the baseline so they do not go back up.
// Returns the paths that did not fit the plugin's limits.
func (c *Client) writePositions(ups map[string]Listen, now int64) ([]string, error) {
	if len(ups) == 0 {
		return nil, nil
	}
	books, eps := map[string]Listen{}, map[string]Listen{}
	for p, v := range ups {
		if strings.HasPrefix(p, "Audiobooks/") {
			books[p] = v
		} else {
			eps[p] = v
		}
	}
	var dropped []string
	if len(books) > 0 {
		path := filepath.Join(c.root, audiobookState)
		old, _ := os.ReadFile(path)
		text, d := mergeAudiobookPositions(string(old), books, now)
		dropped = d
		if text != string(old) {
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				return nil, err
			}
			if err := replaceFile(path, text); err != nil {
				return nil, err
			}
		}
	}
	listed := map[string]bool{}
	if len(eps) > 0 {
		path := filepath.Join(c.root, podcastProgress)
		if old, err := os.ReadFile(path); err == nil { // never created: the plugin owns the episode list
			text, l := mergePodcastPositions(string(old), eps, now)
			listed = l
			if text != string(old) {
				if err := replaceFile(path, text); err != nil {
					return nil, err
				}
			}
		}
	}
	if c.st.Seen == nil {
		c.st.Seen = map[string]Listen{}
	}
	skip := map[string]bool{}
	for _, p := range dropped {
		skip[p] = true
	}
	for p, v := range ups {
		if skip[p] || (strings.HasPrefix(p, "Podcasts/") && !listed[p]) {
			continue // not on the player: tried again next time
		}
		s := c.st.Seen[p]
		s.Pos, s.Finished = v.Pos, v.Finished
		c.st.Seen[p] = s
	}
	return dropped, c.save()
}

// enc: percent-encoding as the plugins write it (everything but letters, digits, `._- ` and space).
func enc(s string) string {
	var b strings.Builder
	for _, ch := range []byte(s) {
		switch {
		case ch >= 'a' && ch <= 'z', ch >= 'A' && ch <= 'Z', ch >= '0' && ch <= '9',
			ch == '.', ch == '_', ch == '-', ch == ' ':
			b.WriteByte(ch)
		default:
			fmt.Fprintf(&b, "%%%02X", ch)
		}
	}
	return b.String()
}

func flag(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

func secs(v float64) string { return fmt.Sprint(int64(v)) }

func lines(text string) []string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	text = strings.TrimSuffix(text, "\n")
	if text == "" {
		return nil
	}
	return strings.Split(text, "\n")
}

func joinLines(ls []string) string {
	var b strings.Builder
	for _, l := range ls {
		b.WriteString(l)
		b.WriteByte('\n')
	}
	return b.String()
}

// bookKey: (book key, file) of a path under Audiobooks/; a file directly there is a single-file book.
func bookKey(rel string) (string, string, bool) {
	parts := strings.Split(rel, "/")
	if len(parts) < 2 || parts[0] != "Audiobooks" {
		return "", "", false
	}
	if len(parts) == 2 {
		return parts[1], parts[1], true
	}
	return parts[1], strings.Join(parts[2:], "/"), true
}

// mergeAudiobookPositions: a book's P line gets file, position, the finished flag and last played = now (duration 0
// when the file changed: its length is unknown here); a missing book gets a new line after the other P lines.
// Every other line stays byte for byte.
func mergeAudiobookPositions(text string, ups map[string]Listen, now int64) (string, []string) {
	ls := lines(text)
	index := map[string]int{}
	for i, l := range ls {
		if p := strings.Split(l, "\t"); p[0] == "P" && len(p) >= 7 {
			index[unq(p[1])] = i
		}
	}
	size := 0
	for _, l := range ls {
		size += len(l) + 1
	}
	var dropped []string
	paths := make([]string, 0, len(ups))
	for p := range ups {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	for _, rel := range paths {
		key, file, ok := bookKey(rel)
		if !ok {
			continue
		}
		pos := ups[rel]
		if i, ok := index[key]; ok {
			p := strings.Split(ls[i], "\t")
			if pos.Finished && unq(p[2]) > file {
				continue // a finished earlier chapter: the book already points at a later one
			}
			if unq(p[2]) != file {
				p[4] = "0"
			}
			p[2], p[3], p[5], p[6] = enc(file), secs(pos.Pos), flag(pos.Finished), fmt.Sprint(now)
			if len(p) >= 8 {
				p[7] = "0" // finished_manual
			}
			size += len(strings.Join(p, "\t")) - len(ls[i])
			ls[i] = strings.Join(p, "\t")
			continue
		}
		line := strings.Join([]string{"P", enc(key), enc(file), secs(pos.Pos), "0", flag(pos.Finished), fmt.Sprint(now), "0", "0"}, "\t")
		if len(ls)+1 > stateMaxLines || size+len(line)+1 > stateMaxBytes {
			dropped = append(dropped, rel)
			continue
		}
		at := 0 // P lines first: the plugin reads them before its bookmarks and history
		for _, v := range index {
			at = max(at, v+1)
		}
		ls = append(ls[:at], append([]string{line}, ls[at:]...)...)
		for k, v := range index {
			if v >= at {
				index[k] = v + 1
			}
		}
		index[key] = at
		size += len(line) + 1
	}
	return joinLines(ls), dropped
}

// mergePodcastPositions: the episodes the plugin lists (by their path on the player) get position, the played flag and
// last played = now; episodes it does not list are left out. Returns the text and the paths it listed.
func mergePodcastPositions(text string, ups map[string]Listen, now int64) (string, map[string]bool) {
	ls := lines(text)
	listed := map[string]bool{}
	for i, l := range ls {
		p := strings.Split(l, "\t")
		if p[0] != "P" || len(p) < 8 {
			continue
		}
		rel, ok := strings.CutPrefix(unq(p[6]), playerRoot)
		if v, want := ups[rel]; ok && want {
			p[2], p[4], p[7] = secs(v.Pos), flag(v.Finished), fmt.Sprint(now)
			ls[i] = strings.Join(p, "\t")
			listed[rel] = true
		}
	}
	return joinLines(ls), listed
}

// replaceFile writes next to the file and renames over it, as the plugins do.
func replaceFile(path, text string) error {
	tmp := path + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	if _, err := f.WriteString(text); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func nowUnix() int64 { return time.Now().Unix() }
