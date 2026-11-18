package main

import (
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// Playlist edits made on the player go back to the server as playlist_edit events. The app keeps, per
// Playlists/*.m3u on the card, the item keys as it last wrote or reported them (State.Lists); a file whose keys
// differ is an edit, a file it never saw is a playlist made on the player. Whether the library takes the edit
// (never for smart playlists) is the server's decision.

var sdPrefix = regexp.MustCompile(`^/data/mnt/sd_?\d*/`)

// listKeys: the item keys of a card playlist (card-relative path), in order; entries that are not files
// this app put on the card are left out.
func (c *Client) listKeys(card string) ([]string, bool) {
	b, err := os.ReadFile(filepath.Join(c.root, filepath.FromSlash(card)))
	if err != nil {
		return nil, false
	}
	byCard := map[string]string{}
	for p, k := range c.st.Keys {
		byCard[strings.ToLower(cardPath(p))] = k
	}
	keys := []string{}
	for _, line := range strings.Split(strings.TrimPrefix(string(b), "\ufeff"), "\n") {
		line = strings.TrimSpace(strings.ReplaceAll(line, "\\", "/"))
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		var rel string
		if m := sdPrefix.FindString(line); m != "" {
			rel = line[len(m):]
		} else if strings.HasPrefix(line, "/") {
			rel = strings.TrimPrefix(line, "/")
		} else {
			rel = path.Join(path.Dir(card), line)
		}
		if k, ok := byCard[strings.ToLower(path.Clean(rel))]; ok {
			keys = append(keys, k)
		}
	}
	return keys, true
}

// editOps: the ops that turn `old` (as last synced) into `now`: remove what is gone, then move every key to
// its place (the server's move = remove if present + insert at index).
func editOps(old, now []string) []map[string]any {
	if strings.Join(old, "\x00") == strings.Join(now, "\x00") {
		return nil
	}
	keep := map[string]bool{}
	for _, k := range now {
		keep[k] = true
	}
	ops := []map[string]any{}
	for _, k := range old {
		if !keep[k] {
			ops = append(ops, map[string]any{"op": "remove", "key": k})
			keep[k] = true // a key listed twice is removed once
		}
	}
	for i, k := range now {
		ops = append(ops, map[string]any{"op": "move", "key": k, "index": i})
	}
	return ops
}

// cardLists: the Playlists/*.m3u files on the card (card-relative paths), sorted.
func (c *Client) cardLists() []string {
	es, _ := os.ReadDir(filepath.Join(c.root, "Playlists"))
	out := []string{}
	for _, e := range es {
		n := e.Name()
		if !e.IsDir() && !strings.HasPrefix(n, ".") && strings.HasSuffix(strings.ToLower(n), ".m3u") {
			out = append(out, "Playlists/"+n)
		}
	}
	sort.Strings(out)
	return out
}

// collectPlaylists queues one playlist_edit per card playlist that changed since the app last looked.
func (c *Client) collectPlaylists() int {
	if c.st.Lists == nil {
		c.st.Lists = map[string][]string{}
	}
	n := 0
	for _, card := range c.cardLists() {
		now, ok := c.listKeys(card)
		if !ok {
			continue
		}
		old, known := c.st.Lists[card]
		if !known && len(now) == 0 {
			continue // an empty playlist made on the player: nothing to tell yet
		}
		if ops := editOps(old, now); len(ops) > 0 {
			name := strings.TrimSuffix(path.Base(card), path.Ext(card))
			c.queue("playlist_edit", "", map[string]any{"name": name, "ops": ops})
			n++
		}
		c.st.Lists[card] = now
	}
	return n
}

// editedSince: the card playlist changed after the app last looked (an edit not sent yet).
func (c *Client) editedSince(card string) bool {
	old, known := c.st.Lists[card]
	if !known {
		return false
	}
	now, ok := c.listKeys(card)
	return ok && editOps(old, now) != nil
}

// forget drops a key the app removed from the card from the remembered playlists.
func (c *Client) forget(key string) {
	if key == "" {
		return
	}
	for card, keys := range c.st.Lists {
		kept := keys[:0]
		for _, k := range keys {
			if k != key {
				kept = append(kept, k)
			}
		}
		c.st.Lists[card] = kept
	}
}
